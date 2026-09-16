"""
Analytics service — computes aggregations over the incidents collection.

Design principles:
  - Query Firestore ONCE per cache window (60 seconds)
  - Aggregate in Python (cheap, deterministic)
  - Serve from in-memory cache between refreshes
  - Pull a rolling 90-day window by default to bound query cost

Firestore reads: ~1 per cache window, vs ~80 queries per dashboard load
if done client-side. This is the entire reason analytics live on the server.
"""

import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from firebase_admin import firestore


CACHE_TTL_SECONDS = 60
_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def _get_cached(key: str) -> Optional[Any]:
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        if datetime.now(timezone.utc) >= entry["expiresAt"]:
            return None
        return entry["value"]


def _set_cached(key: str, value: Any) -> None:
    with _cache_lock:
        _cache[key] = {
            "value": value,
            "expiresAt": datetime.now(timezone.utc)
            + timedelta(seconds=CACHE_TTL_SECONDS),
        }


def clear_cache() -> None:
    """Called by the Firestore worker when a new incident lands."""
    with _cache_lock:
        _cache.clear()


DEFAULT_WINDOW_DAYS = 90


def _fetch_incidents(days: int = DEFAULT_WINDOW_DAYS) -> List[Dict[str, Any]]:
    """Fetch incidents from the last `days` days. Cached for 60s."""
    cache_key = f"incidents:{days}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    db = firestore.client()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    query = db.collection("incidents").stream()
    incidents: List[Dict[str, Any]] = []

    for snap in query:
        data = snap.to_dict() or {}
        created = data.get("createdAt")
        if created:
            if hasattr(created, "tzinfo") and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < since:
                continue
        incidents.append({"id": snap.id, **data})

    _set_cached(cache_key, incidents)
    return incidents


def _to_dt(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    return None


def _severity_of(incident: Dict[str, Any]) -> str:
    ml = incident.get("ml") or {}
    return (ml.get("predictedSeverity") or "medium").lower()


def _type_of(incident: Dict[str, Any]) -> str:
    ml = incident.get("ml") or {}
    return ml.get("predictedType") or incident.get("type") or "other"


# ---------------------------------------------------------------------------
# Endpoint 1 — Summary
# ---------------------------------------------------------------------------

def get_summary(days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    incidents = _fetch_incidents(days)
    by_status: Counter = Counter()
    by_type: Counter = Counter()
    by_severity: Counter = Counter()

    for inc in incidents:
        by_status[inc.get("status", "unknown")] += 1
        by_type[_type_of(inc)] += 1
        by_severity[_severity_of(inc)] += 1

    open_statuses = {"unverified", "pending", "accepted", "en_route", "on_scene"}
    open_count = sum(v for k, v in by_status.items() if k in open_statuses)

    return {
        "total": len(incidents),
        "open": open_count,
        "by_status": dict(by_status),
        "by_type": dict(by_type),
        "by_severity": dict(by_severity),
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Endpoint 2 — Timeline
# ---------------------------------------------------------------------------

def get_timeline(days: int = 30) -> Dict[str, Any]:
    incidents = _fetch_incidents(days)
    now = datetime.now(timezone.utc)
    buckets: Dict[str, Dict[str, int]] = {}

    for i in range(days):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        buckets[day] = {"total": 0, "resolved": 0, "open": 0}

    for inc in incidents:
        created = _to_dt(inc.get("createdAt"))
        if not created:
            continue
        day = created.strftime("%Y-%m-%d")
        if day not in buckets:
            continue
        buckets[day]["total"] += 1
        if inc.get("status") == "resolved":
            buckets[day]["resolved"] += 1
        else:
            buckets[day]["open"] += 1

    ordered = [{"date": d, **c} for d, c in sorted(buckets.items())]
    return {"days": ordered, "window_days": days}


# ---------------------------------------------------------------------------
# Endpoint 3 — By barangay
# ---------------------------------------------------------------------------

def get_by_barangay(days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    incidents = _fetch_incidents(days)
    counts: Counter = Counter()

    for inc in incidents:
        b = inc.get("barangay") or "Unspecified"
        counts[b] += 1

    ranked = [{"barangay": n, "count": c} for n, c in counts.most_common()]
    return {"barangays": ranked, "total": len(incidents)}


# ---------------------------------------------------------------------------
# Endpoint 4 — Quick Response Time (QRT)
# ---------------------------------------------------------------------------

def get_qrt(days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    incidents = _fetch_incidents(days)
    dispatched = [i for i in incidents if i.get("smsSentAt")]
    acknowledged = [i for i in dispatched if i.get("acknowledgedAt")]

    qrts: List[float] = []
    for inc in acknowledged:
        sent = _to_dt(inc.get("smsSentAt"))
        acked = _to_dt(inc.get("acknowledgedAt"))
        if sent and acked:
            qrts.append((acked - sent).total_seconds())

    if not qrts:
        return {
            "count": 0,
            "dispatched": len(dispatched),
            "acknowledged": 0,
            "acknowledgment_rate": 0.0,
            "avg_seconds": None,
            "median_seconds": None,
            "p90_seconds": None,
            "min_seconds": None,
            "max_seconds": None,
        }

    qrts.sort()
    n = len(qrts)
    median = qrts[n // 2] if n % 2 else (qrts[n // 2 - 1] + qrts[n // 2]) / 2
    p90_idx = min(int(n * 0.9), n - 1)

    return {
        "count": n,
        "dispatched": len(dispatched),
        "acknowledged": len(acknowledged),
        "acknowledgment_rate": round(len(acknowledged) / len(dispatched), 3) if dispatched else 0.0,
        "avg_seconds": round(sum(qrts) / n, 1),
        "median_seconds": round(median, 1),
        "p90_seconds": round(qrts[p90_idx], 1),
        "min_seconds": round(qrts[0], 1),
        "max_seconds": round(qrts[-1], 1),
    }


# ---------------------------------------------------------------------------
# Endpoint 5 — Responder leaderboard
# ---------------------------------------------------------------------------

def get_responders(days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    incidents = _fetch_incidents(days)
    by_responder: Dict[str, Dict[str, Any]] = {}

    for inc in incidents:
        uid = inc.get("assignedResponderUid") or inc.get("responderUid")
        if not uid:
            continue

        if uid not in by_responder:
            by_responder[uid] = {
                "uid": uid,
                "name": inc.get("assignedResponderName") or inc.get("responderName") or "Unknown",
                "assigned": 0,
                "resolved": 0,
                "acknowledged": 0,
                "qrts": [],
            }

        entry = by_responder[uid]
        entry["assigned"] += 1
        if inc.get("status") == "resolved":
            entry["resolved"] += 1

        sent = _to_dt(inc.get("smsSentAt"))
        acked = _to_dt(inc.get("acknowledgedAt"))
        if sent and acked:
            entry["acknowledged"] += 1
            entry["qrts"].append((acked - sent).total_seconds())

    results = []
    for entry in by_responder.values():
        qrts = entry.pop("qrts")
        entry["avg_qrts"] = round(sum(qrts) / len(qrts), 1) if qrts else None
        results.append(entry)

    results.sort(key=lambda r: (-r["assigned"], r["avg_qrts"] or 999999))
    return {"responders": results[:20]}


# ---------------------------------------------------------------------------
# Endpoint 6 — Hourly heatmap
# ---------------------------------------------------------------------------

def get_hourly_heatmap(days: int = 30) -> Dict[str, Any]:
    incidents = _fetch_incidents(days)
    hour_counts = [0] * 24

    for inc in incidents:
        created = _to_dt(inc.get("createdAt"))
        if not created:
            continue
        ph_hour = (created.hour + 8) % 24
        hour_counts[ph_hour] += 1

    return {
        "hours": [{"hour": h, "count": hour_counts[h]} for h in range(24)],
        "window_days": days,
    }

# ---------------------------------------------------------------------------
# Endpoint 7 — History (raw incident list with filters)
# ---------------------------------------------------------------------------

def get_history(
    days: int = 90,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    status: Optional[str] = None,
    incident_type: Optional[str] = None,
    barangay: Optional[str] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    """
    Return a filtered, sorted list of incidents for the admin history view.

    Filters:
      - from_date / to_date: ISO date strings (YYYY-MM-DD), inclusive
      - status: exact match (e.g. 'pending', 'resolved')
      - incident_type: exact match (e.g. 'minor_collision')
      - barangay: substring match (case-insensitive)

    Returns the incidents sorted newest-first, capped at `limit`.
    """
    incidents = _fetch_incidents(days)

    # ---- Date range filter ----
    from_dt = None
    to_dt = None
    if from_date:
        try:
            from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    if to_date:
        try:
            # Inclusive end-of-day
            to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        except ValueError:
            pass

    filtered = []
    for inc in incidents:
        created = _to_dt(inc.get("createdAt"))
        if not created:
            continue

        if from_dt and created < from_dt:
            continue
        if to_dt and created > to_dt:
            continue

        if status and inc.get("status") != status:
            continue

        inc_type = _type_of(inc)
        if incident_type and inc_type != incident_type:
            continue

        if barangay:
            b = (inc.get("barangay") or "").lower()
            if barangay.lower() not in b:
                continue

        filtered.append({
            "id": inc.get("id"),
            "createdAt": created.isoformat(),
            "type": inc_type,
            "reportedType": inc.get("type"),
            "severity": _severity_of(inc),
            "status": inc.get("status", "unknown"),
            "barangay": inc.get("barangay") or "",
            "citizenName": inc.get("citizenName") or "",
            "citizenPhone": inc.get("citizenPhone") or "",
            "description": inc.get("description") or "",
            "responderName": inc.get("assignedResponderName") or inc.get("responderName") or "",
            "acknowledgedAt": (
                _to_dt(inc.get("acknowledgedAt")).isoformat()
                if _to_dt(inc.get("acknowledgedAt"))
                else None
            ),
            "responseTimeSeconds": inc.get("responseTimeSeconds"),
            "photoCount": len(inc.get("photoUrls") or []),
            "hasVideo": bool(inc.get("videoUrl")),
        })

    # Sort newest first
    filtered.sort(key=lambda x: x["createdAt"], reverse=True)

    total = len(filtered)
    filtered = filtered[:limit]

    return {
        "incidents": filtered,
        "total": total,
        "returned": len(filtered),
        "limit": limit,
    }