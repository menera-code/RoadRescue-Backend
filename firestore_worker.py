"""
Firestore worker — listens for new incidents and classifies them.

Runs as a background thread inside the FastAPI process (started at
app startup in main.py). Idempotent — safe to restart the server any
number of times.

How it works:
  1. On startup, processes any unprocessed incidents from the backlog
  2. Subscribes to incidents where status is 'unverified' or 'pending'
     (i.e. incidents that haven't been dispatched to a responder yet)
  3. For each incident without an `ml` field, runs predict_incident()
  4. Writes `ml` + `mlProcessedAt` back to the incident document

The listener only writes to Firestore; it never touches Firebase Auth
or Storage.
"""

import threading
from typing import Any, Dict

from firebase_admin import firestore

# Guard so we don't start the listener twice on --reload
_started = False
_watch = None

# Statuses the worker cares about — anything not yet dispatched
WATCHED_STATUSES = ["unverified", "pending"]


def _classify_and_update(db, doc_snapshot) -> None:
    """
    Classify a single incident and write the ML result back.

    Silently skips incidents that:
      - Already have an `ml` field (already classified)
      - Are already accepted / in progress / resolved (too late)
      - Have no description or images to work with
    """
    from predictor import predict_incident  # local import — keeps startup fast

    data = doc_snapshot.to_dict() or {}

    # Skip if already processed
    if data.get("ml"):
        return

    # Skip if the incident has already moved past the unverified/pending stage
    if data.get("status") not in WATCHED_STATUSES:
        return

    description = (data.get("description") or "").strip()
    reported_type = data.get("type")
    image_urls = data.get("photoUrls") or []

    # We can still classify on images alone, but require at least one signal
    if not description and not image_urls:
        print(f"[worker] skip {doc_snapshot.id}: no description and no images")
        return

    print(f"[worker] classifying incident {doc_snapshot.id}…")

    try:
        result = predict_incident(
            text=description,
            image_urls=image_urls or None,
        )
    except Exception as e:
        print(f"[worker] ❌ predict_incident failed for {doc_snapshot.id}: {e}")
        # Write an error marker so we don't retry forever
        doc_snapshot.reference.update({
            "ml": {
                "error": str(e)[:200],
                "processedAt": firestore.SERVER_TIMESTAMP,
            },
        })
        return

    # Compare with what the citizen picked
    reported_matches = (
        reported_type == result["predicted_type"]
        if reported_type
        else None
    )

    payload: Dict[str, Any] = {
        "predictedType": result["predicted_type"],
        "predictedSeverity": result["predicted_severity"],
        "confidence": result["confidence"],
        "keywords": result["keywords"],
        "mentionedVehicles": result["mentioned_vehicles"],
        "vehicles": result["vehicles"],
        "allVehicles": result["all_vehicles"],
        "sources": result["sources"],
        "reportedType": reported_type,
        "reportedTypeMatches": reported_matches,
        "processedAt": firestore.SERVER_TIMESTAMP,
    }

    # Flag disagreements for admin review
    if reported_matches is False:
        payload["mismatch"] = True

    doc_snapshot.reference.update({"ml": payload})

    print(
        f"[worker] ✅ {doc_snapshot.id} → {result['predicted_type']} "
        f"({result['predicted_severity']}, "
        f"src={result['sources']}, "
        f"match={reported_matches})"
    )


def _process_backlog(db) -> None:
    """Process any unverified/pending incidents that don't have an `ml` field yet."""
    try:
        # Firestore 'in' queries support up to 30 values — 2 is fine
        pending = (
            db.collection("incidents")
            .where("status", "in", WATCHED_STATUSES)
            .stream()
        )
        count = 0
        for snap in pending:
            data = snap.to_dict() or {}
            if not data.get("ml"):
                _classify_and_update(db, snap)
                count += 1
        if count:
            print(f"[worker] backlog processed: {count} incidents")
    except Exception as e:
        print(f"[worker] backlog scan failed: {e}")


def _on_snapshot(col_snapshot, changes, read_time):
    """Firestore listener callback — runs on every change to the query."""
    db = firestore.client()
    for change in changes:
        # We only care about newly added documents
        if change.type.name != "ADDED":
            continue
        _classify_and_update(db, change.document)


def start_worker() -> None:
    """
    Start the Firestore listener as a background thread.
    Called once at FastAPI startup. Safe to call multiple times.
    """
    global _started, _watch

    if _started:
        return
    _started = True

    db = firestore.client()

    # 1. Process any existing unprocessed incidents first
    threading.Thread(
        target=_process_backlog,
        args=(db,),
        daemon=True,
    ).start()

    # 2. Subscribe to new incidents
    query = (
        db.collection("incidents")
        .where("status", "in", WATCHED_STATUSES)
    )
    _watch = query.on_snapshot(_on_snapshot)
    print("[worker] ✅ Firestore listener started")


def stop_worker() -> None:
    """Stop the listener (called at shutdown)."""
    global _watch, _started
    if _watch:
        try:
            _watch.unsubscribe()
        except Exception:
            pass
        _watch = None
    _started = False