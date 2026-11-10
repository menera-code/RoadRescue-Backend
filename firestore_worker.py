"""
Firestore worker — processes new RoadRescue incidents with Gemini.

Runs as a background thread inside the FastAPI process.

Workflow:
1. Process existing unclassified incidents on startup.
2. Listen for new unverified/pending incidents.
3. Read report text, Firebase Storage photo URLs, and videoUrl.
4. Send available media to predictor.py.
5. Save prediction and analysis details in the incident's ml field.

The worker does not train models or run a local image-detection model.
"""

import threading
from typing import Any, Dict, List

from firebase_admin import firestore


_started = False
_watch = None

WATCHED_STATUSES = ["unverified", "pending"]


# ---------------------------------------------------------------------------
# INPUT NORMALIZATION
# ---------------------------------------------------------------------------

def _normalize_photo_urls(value: Any) -> List[str]:
    """Normalize photoUrls into a list of non-empty URL strings."""
    if isinstance(value, str):
        value = [value]

    if not isinstance(value, list):
        return []

    return [
        url.strip()
        for url in value
        if isinstance(url, str) and url.strip()
    ][:2]


def _normalize_video_url(value: Any) -> str:
    """Normalize videoUrl into one non-empty URL string."""
    if isinstance(value, str) and value.strip():
        return value.strip()

    return ""


# ---------------------------------------------------------------------------
# INCIDENT CLASSIFICATION
# ---------------------------------------------------------------------------

def _classify_and_update(db, doc_snapshot) -> None:
    """
    Classify one incident and save its Gemini analysis.

    Skips incidents that already have an ml result or have moved
    beyond the unverified/pending stage.
    """
    data = doc_snapshot.to_dict() or {}

    if data.get("ml"):
        return

    if data.get("status") not in WATCHED_STATUSES:
        return

    description = (
        data.get("description") or ""
    ).strip()

    reported_type = data.get("type")

    image_urls = _normalize_photo_urls(
        data.get("photoUrls")
    )

    video_url = _normalize_video_url(
        data.get("videoUrl")
    )

    # Do not skip video-only reports.
    if not description and not image_urls and not video_url:
        print(
            f"[worker] Skipping {doc_snapshot.id}: "
            "no description, photographs, or video"
        )
        return

    print(
        f"[worker] Analyzing incident {doc_snapshot.id}..."
    )

    print(
        f"[worker] Media available: "
        f"photos={len(image_urls)}, "
        f"video={'yes' if video_url else 'no'}"
    )

    try:
        from predictor import predict_incident

        result = predict_incident(
            text=description,
            image_urls=image_urls or None,
            video_url=video_url or None,
        )

    except Exception as exc:
        print(
            f"[worker] Prediction failed for {doc_snapshot.id}: "
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )

        # Preserve existing behavior: record the failure so the
        # listener does not repeatedly call Gemini for this record.
        doc_snapshot.reference.update({
            "ml": {
                "analysisStatus": "failed",
                "error": str(exc)[:200],
                "processedAt": firestore.SERVER_TIMESTAMP,
            }
        })

        return

    predicted_type = result.get(
        "predicted_type",
        "other",
    )

    predicted_severity = result.get(
        "predicted_severity",
        "medium",
    )

    reported_matches = (
        reported_type == predicted_type
        if reported_type
        else None
    )

    image_analysis = result.get(
        "image_analysis"
    ) or {}

    video_analysis = result.get(
        "video_analysis"
    ) or {
        "video_received": bool(video_url),
        "video_analyzed": False,
        "status": "not_provided" if not video_url else "unknown",
        "error": None,
    }

    images_analyzed = image_analysis.get(
        "images_analyzed",
        0,
    )

    video_analyzed = bool(
        video_analysis.get("video_analyzed")
    )

    # Determine the overall analysis status.
    if video_url and video_analyzed:
        if image_analysis.get("errors"):
            analysis_status = "completed_with_media_warnings"
        else:
            analysis_status = "completed_with_video"

    elif video_url and not video_analyzed:
        if result.get("sources") and "gemini" in result.get("sources", []):
            analysis_status = "completed_without_video"
        else:
            analysis_status = "completed_with_video_error"

    elif images_analyzed > 0:
        analysis_status = (
            "completed_with_image"
            if not image_analysis.get("errors")
            else "completed_with_image_warnings"
        )

    elif image_urls:
        analysis_status = "completed_without_image"

    elif result.get("sources") and "gemini" in result.get("sources", []):
        analysis_status = "completed_text_only"

    else:
        analysis_status = "completed_with_fallback"

    # Preserve existing fields for dashboard compatibility.
    payload: Dict[str, Any] = {
        "predictedType": predicted_type,
        "predictedSeverity": predicted_severity,
        "confidence": result.get(
            "confidence",
            0.4,
        ),
        "keywords": result.get(
            "keywords",
            [],
        ),
        "mentionedVehicles": result.get(
            "mentioned_vehicles",
            [],
        ),
        "vehicles": result.get(
            "vehicles",
            {},
        ),
        "allVehicles": result.get(
            "all_vehicles",
            [],
        ),
        "sources": result.get(
            "sources",
            [],
        ),

        # Image analysis.
        "imageVehicles": result.get(
            "image_vehicles",
            {},
        ),
        "detections": result.get(
            "detections",
            [],
        ),
        "imageAnalysis": image_analysis,

        # Video analysis.
        "videoAnalysis": video_analysis,

        # Text analysis.
        "textAnalysis": result.get(
            "text_analysis",
            {},
        ),

        # Review and severity information.
        "needsHumanReview": result.get(
            "needs_human_review",
            True,
        ),
        "severityBasis": result.get(
            "severity_basis",
            "insufficient_evidence",
        ),
        "analysisStatus": analysis_status,

        # Citizen-selected type comparison.
        "reportedType": reported_type,
        "reportedTypeMatches": reported_matches,

        "processedAt": firestore.SERVER_TIMESTAMP,
    }

    if reported_matches is False:
        payload["mismatch"] = True

    # Save all results inside the existing nested ml field.
    doc_snapshot.reference.update({
        "ml": payload,
    })

    # Invalidate analytics cache after the incident is analyzed.
    try:
        from analytics_service import clear_cache
        clear_cache()

    except Exception as exc:
        print(
            f"[worker] Analytics cache clear skipped: {exc}"
        )

    print(
        f"[worker] Completed {doc_snapshot.id}: "
        f"{predicted_type} / {predicted_severity}; "
        f"sources={result.get('sources', [])}; "
        f"analysis_status={analysis_status}; "
        f"video_analyzed={video_analyzed}; "
        f"human_review={result.get('needs_human_review', True)}"
    )


# ---------------------------------------------------------------------------
# BACKLOG PROCESSING
# ---------------------------------------------------------------------------

def _process_backlog(db) -> None:
    """
    Process existing unverified/pending incidents without ml results.
    """
    try:
        pending = (
            db.collection("incidents")
            .where("status", "in", WATCHED_STATUSES)
            .stream()
        )

        processed = 0

        for snapshot in pending:
            data = snapshot.to_dict() or {}

            if not data.get("ml"):
                _classify_and_update(db, snapshot)
                processed += 1

        if processed:
            print(
                f"[worker] Backlog scan completed: "
                f"{processed} incident(s) checked"
            )

    except Exception as exc:
        print(
            f"[worker] Backlog scan failed: {exc}"
        )


# ---------------------------------------------------------------------------
# FIRESTORE SNAPSHOT LISTENER
# ---------------------------------------------------------------------------

def _on_snapshot(col_snapshot, changes, read_time):
    """Process newly added incidents matching watched statuses."""
    db = firestore.client()

    for change in changes:
        if change.type.name != "ADDED":
            continue

        _classify_and_update(
            db,
            change.document,
        )


# ---------------------------------------------------------------------------
# WORKER STARTUP AND SHUTDOWN
# ---------------------------------------------------------------------------

def start_worker() -> None:
    """Start the Firestore listener once during FastAPI startup."""
    global _started, _watch

    if _started:
        return

    db = firestore.client()

    # Process existing unclassified incidents.
    threading.Thread(
        target=_process_backlog,
        args=(db,),
        daemon=True,
        name="roadrescue-ml-backlog",
    ).start()

    # Listen for newly added incidents.
    query = (
        db.collection("incidents")
        .where("status", "in", WATCHED_STATUSES)
    )

    _watch = query.on_snapshot(_on_snapshot)

    _started = True

    print(
        "[worker] Firestore Gemini text/image/video listener started"
    )


def stop_worker() -> None:
    """Stop the Firestore listener during application shutdown."""
    global _watch, _started

    if _watch:
        try:
            _watch.unsubscribe()

        except Exception as exc:
            print(
                f"[worker] Listener shutdown warning: {exc}"
            )

        _watch = None

    _started = False

    print(
        "[worker] Firestore listener stopped"
    )
