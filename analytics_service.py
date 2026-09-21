"""
Analytics service — computes aggregations over the incidents collection.
Cached for 60 seconds to bound Firestore reads.
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
    with _cache_lock:
        _cache.clear()


DEFAULT_WINDOW_DAYS = 90


def _fetch_incidents(days: int = DEFAULT_WINDOW_DAYS) -> List[Dict[str, Any]]:
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


def _dispatch_time(inc: Dict[str, Any]) -> Optional[datetime]:
    return (
        _to_dt(inc.get("dispatchedAt"))
        or _to_dt(inc.get("smsSentAt"))
        or _to_dt(inc.get("verifiedAt"))
    )


def _ack_time(inc: Dict[str, Any]) -> Optional[datetime]:
    return (
        _to_dt(inc.get("acknowledgedAt"))
        or _to_dt(inc.get("acceptedAt"))
    )


def _is_dispatched(inc: Dict[str, Any]) -> bool:
    return _dispatch_time(inc) is not None


def _is_acknowledged(inc: Dict[str, Any]) -> bool:
    return _ack_time(inc) is not None


def _is_resolved(inc: Dict[str, Any]) -> bool:
    return inc.get("status") == "resolved"


def _severity_of(incident: Dict[str, Any]) -> str:
    ml = incident.get("ml") or {}
    return (ml.get("predictedSeverity") or "medium").lower()


def _type_of(incident: Dict[str, Any]) -> str:
    ml = incident.get("ml") or {}
    return ml.get("predictedType") or incident.get("type") or "other"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def get_summary(days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    incidents = _fetch_incidents(days)
    by_status: Counter = Counter()
    by_type: Counter = Counter()
    by_severity: Counter = Counter()

    resolved_count = 0
    acknowledged_count = 0

    for inc in incidents:
        by_status[inc.get("status", "unknown")] += 1
        by_type[_type_of(inc)] += 1
        by_severity[_severity_of(inc)] += 1
        if _is_resolved(inc):
            resolved_count += 1
        if _is_acknowledged(inc):
            acknowledged_count += 1

    open_statuses = {
        "unverified",
        "pending",
        "emergency_pending",
        "accepted",
        "en_route",
        "on_scene",
    }
    open_count = sum(v for k, v in by_status.items() if k in open_statuses)

    total = len(incidents)

    return {
        "total": total,
        "open": open_count,
        "resolved": resolved_count,
        "acknowledged": acknowledged_count,
        "completion_rate": round(resolved_count / total, 3) if total else 0.0,
        "by_status": dict(by_status),
        "by_type": dict(by_type),
        "by_severity": dict(by_severity),
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Timeline
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
# By barangay
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
# QRT
# ---------------------------------------------------------------------------

def get_qrt(days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    incidents = _fetch_incidents(days)

    dispatched = [i for i in incidents if _is_dispatched(i)]
    acknowledged = [i for i in dispatched if _is_acknowledged(i)]
    resolved = [i for i in dispatched if _is_resolved(i)]

    # Ack QRT — dispatch → acknowledged/accepted
    ack_qrts: List[float] = []
    for inc in acknowledged:
        sent = _dispatch_time(inc)
        acked = _ack_time(inc)
        if sent and acked:
            delta = (acked - sent).total_seconds()
            if delta >= 0:
                ack_qrts.append(delta)

    # Response duration — accept → resolved
    response_durations: List[float] = []
    for inc in resolved:
        accepted = _to_dt(inc.get("acceptedAt"))
        resolved_at = _to_dt(inc.get("resolvedAt"))
        if accepted and resolved_at:
            delta = (resolved_at - accepted).total_seconds()
            if delta >= 0:
                response_durations.append(delta)
        else:
            # Fall back to precomputed field
            dur = inc.get("respondedDurationSeconds")
            if isinstance(dur, (int, float)) and dur >= 0:
                response_durations.append(float(dur))

    result: Dict[str, Any] = {
        "count": len(ack_qrts),
        "dispatched": len(dispatched),
        "acknowledged": len(acknowledged),
        "resolved": len(resolved),
        "acknowledgment_rate": (
            round(len(acknowledged) / len(dispatched), 3)
            if dispatched else 0.0
        ),
        "completion_rate": (
            round(len(resolved) / len(dispatched), 3)
            if dispatched else 0.0
        ),
    }

    # Ack QRT stats
    if not ack_qrts:
        result.update({
            "avg_seconds": None,
            "median_seconds": None,
            "p90_seconds": None,
            "min_seconds": None,
            "max_seconds": None,
        })
    else:
        ack_qrts.sort()
        n = len(ack_qrts)
        median = ack_qrts[n // 2] if n % 2 else (ack_qrts[n // 2 - 1] + ack_qrts[n // 2]) / 2
        p90_idx = min(int(n * 0.9), n - 1)
        result.update({
            "avg_seconds": round(sum(ack_qrts) / n, 1),
            "median_seconds": round(median, 1),
            "p90_seconds": round(ack_qrts[p90_idx], 1),
            "min_seconds": round(ack_qrts[0], 1),
            "max_seconds": round(ack_qrts[-1], 1),
        })

    # Response duration stats
    if not response_durations:
        result.update({
            "avg_response_duration_seconds": None,
            "median_response_duration_seconds": None,
        })
    else:
        response_durations.sort()
        m = len(response_durations)
        med = (
            response_durations[m // 2] if m % 2
            else (response_durations[m // 2 - 1] + response_durations[m // 2]) / 2
        )
        result.update({
            "avg_response_duration_seconds": round(sum(response_durations) / m, 1),
            "median_response_duration_seconds": round(med, 1),
        })

    return result


# ---------------------------------------------------------------------------
# Responders
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
                "response_durations": [],
            }

        entry = by_responder[uid]
        entry["assigned"] += 1

        if _is_resolved(inc):
            entry["resolved"] += 1
            duration = inc.get("respondedDurationSeconds")
            if not isinstance(duration, (int, float)):
                accepted = _to_dt(inc.get("acceptedAt"))
                resolved_at = _to_dt(inc.get("resolvedAt"))
                if accepted and resolved_at:
                    duration = (resolved_at - accepted).total_seconds()
            if isinstance(duration, (int, float)) and duration >= 0:
                entry["response_durations"].append(float(duration))

        sent = _dispatch_time(inc)
        acked = _ack_time(inc)
        if sent and acked:
            delta = (acked - sent).total_seconds()
            if delta >= 0:
                entry["acknowledged"] += 1
                entry["qrts"].append(delta)

    results = []
    for entry in by_responder.values():
        qrts = entry.pop("qrts")
        durations = entry.pop("response_durations")

        entry["avg_qrts"] = round(sum(qrts) / len(qrts), 1) if qrts else None
        entry["avg_responded_seconds"] = (
            round(sum(durations) / len(durations), 1) if durations else None
        )
        entry["completion_rate"] = (
            round(entry["resolved"] / entry["assigned"], 3)
            if entry["assigned"] else 0.0
        )
        results.append(entry)

    results.sort(key=lambda r: (-r["resolved"], -r["assigned"], r["avg_qrts"] or 999999))
    return {"responders": results[:20]}


# ---------------------------------------------------------------------------
# Hourly heatmap
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
# History
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
    incidents = _fetch_incidents(days)

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

        ack_dt = _ack_time(inc)
        resolved_dt = _to_dt(inc.get("resolvedAt"))

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
            "responderName": (
                inc.get("assignedResponderName")
                or inc.get("responderName")
                or ""
            ),
            "acknowledgedAt": ack_dt.isoformat() if ack_dt else None,
            "resolvedAt": resolved_dt.isoformat() if resolved_dt else None,
            "responseTimeSeconds": inc.get("responseTimeSeconds"),
            "respondedDurationSeconds": inc.get("respondedDurationSeconds"),
            "photoCount": len(inc.get("photoUrls") or []),
            "hasVideo": bool(inc.get("videoUrl")),
        })

    filtered.sort(key=lambda x: x["createdAt"], reverse=True)

    total = len(filtered)
    filtered = filtered[:limit]

    return {
        "incidents": filtered,
        "total": total,
        "returned": len(filtered),
        "limit": limit,
    }


# ===========================================================================
# ML CAPSTONE ANALYTICS
# ===========================================================================

from datetime import timedelta as _timedelta
_PH_TZ = timezone(_timedelta(hours=8))


def _ph_parts(ts: Any) -> Optional[Dict[str, int]]:
    dt = _to_dt(ts)
    if not dt:
        return None
    ph = dt.astimezone(_PH_TZ)
    return {
        "hour": ph.hour,
        "dow": ph.weekday(),
        "date": ph.strftime("%Y-%m-%d"),
        "month": ph.strftime("%Y-%m"),
        "week": ph.strftime("%Y-W%V"),
    }


def get_barangay_temporal(days: int = 90) -> Dict[str, Any]:
    from collections import Counter, defaultdict

    incidents = _fetch_incidents(days)

    barangay_hour = defaultdict(lambda: [0] * 24)
    barangay_dow = defaultdict(lambda: [0] * 7)
    barangay_totals = Counter()
    barangay_types = defaultdict(Counter)

    for inc in incidents:
        parts = _ph_parts(inc.get("createdAt"))
        if not parts:
            continue
        b = inc.get("barangay") or "Unspecified"
        barangay_hour[b][parts["hour"]] += 1
        barangay_dow[b][parts["dow"]] += 1
        barangay_totals[b] += 1
        barangay_types[b][_type_of(inc)] += 1

    result = []
    for barangay, total in barangay_totals.most_common():
        hours = barangay_hour[barangay]
        peak_hour = hours.index(max(hours)) if any(hours) else None
        dows = barangay_dow[barangay]
        peak_dow = dows.index(max(dows)) if any(dows) else None
        top_type, top_count = (
            barangay_types[barangay].most_common(1)[0]
            if barangay_types[barangay]
            else ("other", 0)
        )
        result.append({
            "barangay": barangay,
            "total": total,
            "hourly": hours,
            "daily": dows,
            "peak_hour": peak_hour,
            "peak_dow": peak_dow,
            "top_type": top_type,
            "top_type_count": top_count,
            "top_type_share": round(top_count / total, 3) if total else 0,
        })

    return {
        "barangays": result,
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_vehicle_analytics(days: int = 90) -> Dict[str, Any]:
    from collections import Counter, defaultdict

    incidents = _fetch_incidents(days)

    vehicle_mentions = Counter()
    vehicle_detections = Counter()
    vehicle_all = Counter()
    vehicle_by_type = defaultdict(Counter)
    vehicle_by_severity = defaultdict(Counter)

    detections_total = 0
    incidents_with_yolo = 0
    incidents_with_text_vehicles = 0
    detection_confidence_sum = 0.0
    detection_confidence_count = 0

    for inc in incidents:
        ml = inc.get("ml") or {}
        inc_type = _type_of(inc)
        severity = _severity_of(inc)

        text_vehicles = ml.get("mentionedVehicles") or []
        if text_vehicles:
            incidents_with_text_vehicles += 1
            for v in text_vehicles:
                v_lower = str(v).lower()
                vehicle_mentions[v_lower] += 1
                vehicle_all[v_lower] += 1
                vehicle_by_type[inc_type][v_lower] += 1
                vehicle_by_severity[severity][v_lower] += 1

        yolo_vehicles = ml.get("vehicles") or {}
        if yolo_vehicles:
            incidents_with_yolo += 1
            for v, count in yolo_vehicles.items():
                v_lower = str(v).lower()
                vehicle_detections[v_lower] += count
                vehicle_all[v_lower] += count
                vehicle_by_type[inc_type][v_lower] += count
                vehicle_by_severity[severity][v_lower] += count

        for d in (ml.get("detections") or []):
            conf = d.get("confidence")
            if isinstance(conf, (int, float)):
                detection_confidence_sum += conf
                detection_confidence_count += 1
            detections_total += 1

    total_incidents = len(incidents) or 1

    top_vehicles = [
        {
            "vehicle": v,
            "count": c,
            "from_text": vehicle_mentions.get(v, 0),
            "from_yolo": vehicle_detections.get(v, 0),
        }
        for v, c in vehicle_all.most_common(15)
    ]

    return {
        "total_incidents": len(incidents),
        "incidents_with_yolo": incidents_with_yolo,
        "incidents_with_text_vehicles": incidents_with_text_vehicles,
        "yolo_coverage_rate": round(incidents_with_yolo / total_incidents, 3),
        "total_detections": detections_total,
        "avg_yolo_confidence": round(
            detection_confidence_sum / detection_confidence_count, 3
        ) if detection_confidence_count else 0,
        "top_vehicles": top_vehicles,
        "by_incident_type": {t: dict(c) for t, c in vehicle_by_type.items()},
        "by_severity": {s: dict(c) for s, c in vehicle_by_severity.items()},
        "window_days": days,
    }


def get_ml_performance(days: int = 90) -> Dict[str, Any]:
    from collections import Counter, defaultdict

    incidents = _fetch_incidents(days)
    with_ml = [i for i in incidents if i.get("ml")]

    if not with_ml:
        return {
            "total_analyzed": 0,
            "evaluated": 0,
            "overall": {"match_rate": 0, "matches": 0, "mismatches": 0},
            "per_type": [],
            "confusion_matrix": [],
            "confidence_calibration": [],
            "language_performance": [],
            "sources_used": {},
            "timings": {"avg_total_ms": 0, "avg_gemini_ms": 0, "sample_count": 0},
            "window_days": days,
        }

    matches = 0
    mismatches = 0
    per_type = defaultdict(lambda: {"total": 0, "matched": 0})
    confusion = Counter()
    confidence_buckets = {
        "0.9-1.0": {"total": 0, "matched": 0},
        "0.75-0.9": {"total": 0, "matched": 0},
        "0.5-0.75": {"total": 0, "matched": 0},
        "<0.5": {"total": 0, "matched": 0},
    }
    lang_perf = defaultdict(lambda: {"total": 0, "matched": 0})
    sources = Counter()
    timings = []
    gemini_timings = []

    for inc in with_ml:
        ml = inc["ml"]
        reported = ml.get("reportedType") or inc.get("type") or "other"
        predicted = ml.get("predictedType") or "other"
        is_match = ml.get("reportedTypeMatches")

        per_type[reported]["total"] += 1
        if is_match:
            matches += 1
            per_type[reported]["matched"] += 1
        elif is_match is False:
            mismatches += 1
            confusion[f"{reported} → {predicted}"] += 1

        conf = ml.get("confidence") or 0
        if conf >= 0.9:
            bucket = "0.9-1.0"
        elif conf >= 0.75:
            bucket = "0.75-0.9"
        elif conf >= 0.5:
            bucket = "0.5-0.75"
        else:
            bucket = "<0.5"
        confidence_buckets[bucket]["total"] += 1
        if is_match:
            confidence_buckets[bucket]["matched"] += 1

        capstone = ml.get("capstone") or {}
        lang = capstone.get("language", "unknown")
        lang_perf[lang]["total"] += 1
        if is_match:
            lang_perf[lang]["matched"] += 1

        for s in (ml.get("sources") or []):
            sources[s] += 1

        t_total = capstone.get("total_processing_ms", 0)
        if t_total > 0:
            timings.append(t_total)
        t_gemini = (capstone.get("timings_ms") or {}).get("gemini_ms", 0)
        if t_gemini > 0:
            gemini_timings.append(t_gemini)

    evaluated = matches + mismatches

    return {
        "total_analyzed": len(with_ml),
        "evaluated": evaluated,
        "overall": {
            "match_rate": round(matches / evaluated, 3) if evaluated else 0,
            "matches": matches,
            "mismatches": mismatches,
        },
        "per_type": [
            {
                "type": t,
                "total": data["total"],
                "matched": data["matched"],
                "accuracy": round(data["matched"] / data["total"], 3)
                    if data["total"] else 0,
            }
            for t, data in sorted(per_type.items(), key=lambda x: -x[1]["total"])
        ],
        "confusion_matrix": [
            {"pattern": k, "count": v} for k, v in confusion.most_common(10)
        ],
        "confidence_calibration": [
            {
                "bucket": bucket,
                "total": data["total"],
                "matched": data["matched"],
                "accuracy": round(data["matched"] / data["total"], 3)
                    if data["total"] else 0,
            }
            for bucket, data in confidence_buckets.items()
        ],
        "language_performance": [
            {
                "language": lang,
                "total": data["total"],
                "matched": data["matched"],
                "accuracy": round(data["matched"] / data["total"], 3)
                    if data["total"] else 0,
            }
            for lang, data in lang_perf.items()
        ],
        "sources_used": dict(sources),
        "timings": {
            "avg_total_ms": round(sum(timings) / len(timings), 1) if timings else 0,
            "avg_gemini_ms": round(sum(gemini_timings) / len(gemini_timings), 1)
                if gemini_timings else 0,
            "sample_count": len(timings),
        },
        "window_days": days,
    }


def get_patterns(days: int = 180) -> Dict[str, Any]:
    from collections import Counter, defaultdict

    incidents = _fetch_incidents(days)

    dow_counts = [0] * 7
    hour_counts = [0] * 24
    monthly = Counter()
    weekly = Counter()
    dow_type = defaultdict(Counter)

    for inc in incidents:
        parts = _ph_parts(inc.get("createdAt"))
        if not parts:
            continue
        dow_counts[parts["dow"]] += 1
        hour_counts[parts["hour"]] += 1
        monthly[parts["month"]] += 1
        weekly[parts["week"]] += 1
        dow_type[parts["dow"]][_type_of(inc)] += 1

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    return {
        "day_of_week": [
            {"day": day_names[i], "count": dow_counts[i]} for i in range(7)
        ],
        "hour_of_day": [
            {"hour": h, "count": hour_counts[h]} for h in range(24)
        ],
        "monthly": [
            {"month": m, "count": c} for m, c in sorted(monthly.items())
        ],
        "weekly": [
            {"week": w, "count": c} for w, c in sorted(weekly.items())
        ],
        "top_types_by_day": [
            {
                "day": day_names[d],
                "types": [
                    {"type": t, "count": c}
                    for t, c in dow_type[d].most_common(3)
                ],
            }
            for d in range(7)
        ],
        "window_days": days,
    }


def get_predictions(days: int = 90) -> Dict[str, Any]:
    from collections import Counter, defaultdict

    incidents = _fetch_incidents(days)

    by_week = Counter()
    by_barangay_week = defaultdict(Counter)
    hourly = Counter()

    for inc in incidents:
        parts = _ph_parts(inc.get("createdAt"))
        if not parts:
            continue
        by_week[parts["week"]] += 1
        hourly[parts["hour"]] += 1
        b = inc.get("barangay") or "Unspecified"
        by_barangay_week[b][parts["week"]] += 1

    sorted_weeks = sorted(by_week.keys())
    weekly_counts = [by_week[w] for w in sorted_weeks]

    last_3 = weekly_counts[-3:] if len(weekly_counts) >= 3 else weekly_counts
    next_week_pred = round(sum(last_3) / len(last_3)) if last_3 else 0

    wow_growth = 0.0
    if len(weekly_counts) >= 2:
        last_week = weekly_counts[-1]
        prev_week = weekly_counts[-2]
        if prev_week:
            wow_growth = round((last_week - prev_week) / prev_week * 100, 1)

    barangay_forecasts = []
    for b, weeks in by_barangay_week.items():
        recent = [weeks.get(w, 0) for w in sorted_weeks[-4:]]
        avg = sum(recent) / len(recent) if recent else 0
        if len(recent) >= 2:
            if recent[-1] > recent[-2]:
                trend = "up"
            elif recent[-1] < recent[-2]:
                trend = "down"
            else:
                trend = "stable"
        else:
            trend = "stable"
        barangay_forecasts.append({
            "barangay": b,
            "predicted_incidents": round(avg, 1),
            "trend": trend,
        })

    barangay_forecasts.sort(key=lambda x: -x["predicted_incidents"])

    return {
        "predicted_next_week_total": next_week_pred,
        "wow_growth_percent": wow_growth,
        "predicted_peak_hours": [h for h, _ in hourly.most_common(3)],
        "barangay_forecasts": barangay_forecasts[:10],
        "data_points_used": len(weekly_counts),
        "window_days": days,
    }
