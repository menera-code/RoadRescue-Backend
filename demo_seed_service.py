"""
RoadRescue synthetic demo data seeder.

Creates:
- 45 synthetic incidents
- 36 resolved incidents
- 9 cancelled incidents
- Corresponding synthetic system_logs entries

Safety:
- Uses a dedicated deterministic demo batch ID.
- Does not send push notifications or SMS.
- Does not impersonate responders in activity logs.
- Deletes only documents belonging to this exact demo batch.
"""

from datetime import datetime, timedelta, timezone
from random import Random
from typing import Any, Dict, List
import re

from firebase_admin import firestore


# ============================================================
# CONFIGURATION
# ============================================================

BATCH_ID = "roadrescue_demo_2026_10_23_11_09"

START_DATE = datetime(2026, 10, 23)
END_DATE = datetime(2026, 11, 9)

TOTAL_INCIDENTS = 45
RESOLVED_COUNT = 36
CANCELLED_COUNT = 9

SEED_VERSION = "1.0"


# ============================================================
# HELPERS
# ============================================================

def _slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[()]", "", value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _normalize_name(value: str) -> str:
    value = re.sub(r"\(.*?\)", "", value.lower())
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _utc(value: datetime) -> datetime:
    """Convert a datetime to UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _incident_id(index: int) -> str:
    return f"demo_oct23_nov09_2026_{index:03d}"


def _log_id(index: int, action: str) -> str:
    return f"demo_oct23_nov09_2026_{index:03d}_{action}"


def _clear_analytics_cache() -> None:
    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception as exc:
        print(f"[demo-seed] Analytics cache clear skipped: {exc}")


# ============================================================
# BARANGAYS
# ============================================================

def _get_barangays() -> List[Dict[str, Any]]:
    from data.barangays import to_documents

    barangays = to_documents()

    if not barangays:
        raise RuntimeError("No barangays found in data/barangays.py")

    return barangays


# ============================================================
# REGISTERED RESPONDERS
# ============================================================

def _get_responders_by_barangay(
    db,
    barangays: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Only uses existing, registered, active responder accounts.

    Supports:
    - users.barangay
    - users.assignedBarangay
    - barangays.responders
    - barangays.responderUid (legacy schema)
    """

    users_by_uid: Dict[str, Dict[str, Any]] = {}

    for doc in db.collection("users").stream():
        user = doc.to_dict() or {}

        if user.get("role") != "responder":
            continue

        if user.get("disabled") is True:
            continue

        if user.get("status", "active") != "active":
            continue

        user["uid"] = doc.id
        users_by_uid[doc.id] = user

    result: Dict[str, List[Dict[str, Any]]] = {}

    for barangay in barangays:
        name = barangay["name"]
        slug = barangay["slug"]
        normalized = _normalize_name(name)

        matched: Dict[str, Dict[str, Any]] = {}

        # Match responders using their user profile.
        for uid, user in users_by_uid.items():
            assigned = (
                user.get("assignedBarangay")
                or user.get("barangay")
                or ""
            )

            if _normalize_name(str(assigned)) == normalized:
                matched[uid] = user

        # Also inspect the barangay document's responder assignments.
        try:
            b_snap = (
                db.collection("barangays")
                .document(slug)
                .get()
            )

            if b_snap.exists:
                b_data = b_snap.to_dict() or {}

                assigned_responders = b_data.get("responders", [])

                if isinstance(assigned_responders, list):
                    for responder in assigned_responders:
                        if not isinstance(responder, dict):
                            continue

                        uid = responder.get("uid")

                        if uid in users_by_uid:
                            matched[uid] = users_by_uid[uid]

                # Legacy single-responder schema.
                legacy_uid = b_data.get("responderUid")

                if legacy_uid in users_by_uid:
                    matched[legacy_uid] = users_by_uid[legacy_uid]

        except Exception as exc:
            print(
                f"[demo-seed] Could not read responder assignment "
                f"for {name}: {exc}"
            )

        responders = []

        for uid, user in matched.items():
            responders.append({
                "uid": uid,
                "name": (
                    user.get("fullName")
                    or user.get("displayName")
                    or "Registered Responder"
                ),
            })

        result[slug] = responders

    return result


# ============================================================
# SYNTHETIC DATA GENERATION
# ============================================================

def _build_dataset(db) -> List[Dict[str, Any]]:
    rng = Random(20261109)

    barangays = _get_barangays()
    responders_by_barangay = _get_responders_by_barangay(
        db,
        barangays,
    )

    incident_types = [
        "accident",
        "medical",
        "fire",
        "road_hazard",
        "emergency",
    ]

    descriptions = {
        "accident": [
            "SYNTHETIC DEMO: Simulated road accident requiring assistance.",
            "SYNTHETIC DEMO: Simulated vehicle collision report.",
            "SYNTHETIC DEMO: Simulated traffic accident for dashboard testing.",
        ],
        "medical": [
            "SYNTHETIC DEMO: Simulated medical emergency.",
            "SYNTHETIC DEMO: Simulated request for medical assistance.",
            "SYNTHETIC DEMO: Simulated patient assistance report.",
        ],
        "fire": [
            "SYNTHETIC DEMO: Simulated fire incident report.",
            "SYNTHETIC DEMO: Simulated smoke and fire assistance request.",
            "SYNTHETIC DEMO: Simulated fire response scenario.",
        ],
        "road_hazard": [
            "SYNTHETIC DEMO: Simulated road obstruction.",
            "SYNTHETIC DEMO: Simulated hazardous road condition.",
            "SYNTHETIC DEMO: Simulated traffic hazard report.",
        ],
        "emergency": [
            "SYNTHETIC DEMO: Simulated emergency assistance request.",
            "SYNTHETIC DEMO: Simulated urgent incident report.",
            "SYNTHETIC DEMO: Simulated emergency response scenario.",
        ],
    }

    total_days = (END_DATE - START_DATE).days + 1

    dataset = []

    for index in range(1, TOTAL_INCIDENTS + 1):
        barangay = barangays[(index - 1) % len(barangays)]

        incident_type = incident_types[
            (index - 1) % len(incident_types)
        ]

        is_resolved = index <= RESOLVED_COUNT

        status = "resolved" if is_resolved else "cancelled"

        # Spread records across October 23 through November 9.
        day_offset = (index * 7 + index // 3) % total_days

        local_created = (
            START_DATE
            + timedelta(days=day_offset)
        ).replace(
            hour=rng.randint(7, 20),
            minute=rng.randint(0, 59),
            second=0,
        )

        # The synthetic schedule is based on Philippine local time.
        local_created = local_created.replace(
            tzinfo=timezone(timedelta(hours=8))
        )

        created_at = local_created.astimezone(timezone.utc)

        verified_at = created_at + timedelta(
            minutes=rng.randint(2, 10)
        )

        dispatched_at = verified_at + timedelta(
            minutes=rng.randint(1, 5)
        )

        response_seconds = rng.randint(120, 900)

        accepted_at = dispatched_at + timedelta(
            seconds=response_seconds
        )

        responded_duration = rng.randint(600, 5400)

        resolved_at = accepted_at + timedelta(
            seconds=responded_duration
        )

        cancelled_at = dispatched_at + timedelta(
            minutes=rng.randint(5, 30)
        )

        lat = float(barangay["lat"]) + rng.uniform(
            -0.0015, 0.0015
        )

        lng = float(barangay["lng"]) + rng.uniform(
            -0.0015, 0.0015
        )

        assigned_responders = responders_by_barangay.get(
            barangay["slug"],
            [],
        )

        responder = None

        if assigned_responders:
            responder = assigned_responders[
                (index - 1) % len(assigned_responders)
            ]

        priority = (
            "critical"
            if incident_type in ("emergency", "fire")
            else "high"
            if incident_type in ("accident", "medical")
            else "medium"
        )

        incident_id = _incident_id(index)

        incident: Dict[str, Any] = {
            "shortId": f"DEMO{index:03d}",

            "type": incident_type,

            "description": rng.choice(
                descriptions[incident_type]
            ),

            "barangay": barangay["name"],
            "suggestedBarangay": barangay["name"],

            "location": firestore.GeoPoint(lat, lng),

            "citizenName": "Synthetic Demo Reporter",
            "citizenPhone": "",
            "citizenUid": None,

            "anonymous": True,
            "deviceId": f"demo-device-{index:03d}",

            "status": status,
            "priority": priority,

            "createdAt": created_at,
            "updatedAt": (
                resolved_at if is_resolved else cancelled_at
            ),

            "verifiedAt": verified_at,
            "dispatchedAt": dispatched_at,

            "photoUrls": [],
            "videoUrl": None,
            "audioUrl": None,
            "audioDurationSeconds": None,

            "ml": {
                "predictedType": incident_type,
                "predictedSeverity": priority,
                "confidence": round(
                    rng.uniform(0.85, 0.99), 2
                ),
                "keywords": ["synthetic", "demo"],
                "mentionedVehicles": [],
                "vehicles": {},
                "allVehicles": [],
                "sources": ["synthetic_demo_seed"],
                "reportedType": incident_type,
                "reportedTypeMatches": True,
                "processedAt": created_at,
            },

            # Synthetic-data identification.
            "isSynthetic": True,
            "demoBatchId": BATCH_ID,
            "demoSeedVersion": SEED_VERSION,
        }

        if responder:
            incident.update({
                "assignedResponderUid": responder["uid"],
                "assignedResponderName": responder["name"],
                "assignedResponderUids": [responder["uid"]],
                "assignedResponderNames": [responder["name"]],
            })

        if is_resolved:
            incident.update({
                "acknowledgedAt": accepted_at,
                "acknowledgedVia": "demo_simulation",
                "acceptedAt": accepted_at,
                "responseTimeSeconds": response_seconds,
                "respondedDurationSeconds": responded_duration,
                "resolvedAt": resolved_at,
            })

        else:
            incident.update({
                "cancelledAt": cancelled_at,
                "cancelReason": (
                    "Synthetic demo cancellation for testing."
                ),
            })

        dataset.append({
            "id": incident_id,
            "index": index,
            "data": incident,
        })

    return dataset


# ============================================================
# SYNTHETIC SYSTEM LOGS
# ============================================================

def _build_logs(
    incident_id: str,
    incident: Dict[str, Any],
    index: int,
) -> List[Dict[str, Any]]:

    batch_metadata = {
        "isSynthetic": True,
        "demoBatchId": BATCH_ID,
        "demoSeedVersion": SEED_VERSION,
    }

    actor = {
        "uid": "demo-seeder",
        "email": None,
        "role": "system",
    }

    target = {
        "type": "incident",
        "id": incident_id,
    }

    logs = []

    def add_log(
        action: str,
        message: str,
        timestamp: datetime,
        level: str = "info",
        extra_metadata: Dict[str, Any] = None,
    ):
        metadata = dict(batch_metadata)

        if extra_metadata:
            metadata.update(extra_metadata)

        logs.append({
            "id": _log_id(index, action.replace(".", "_")),
            "data": {
                "timestamp": timestamp,
                "level": level,
                "category": "incident",
                "action": action,
                "message": message[:500],
                "actor": actor,
                "target": target,
                "metadata": metadata,
                "userAgent": "RoadRescue Synthetic Demo Seeder",
                "demoBatchId": BATCH_ID,
            },
        })

    created_at = incident["createdAt"]
    dispatched_at = incident["dispatchedAt"]

    add_log(
        "incident.created",
        f"Synthetic demo incident {incident['shortId']} created.",
        created_at,
        extra_metadata={
            "barangay": incident["barangay"],
            "incidentType": incident["type"],
        },
    )

    add_log(
        "incident.dispatched",
        f"Synthetic demo incident {incident['shortId']} dispatched.",
        dispatched_at,
        extra_metadata={
            "simulated": True,
            "responderUid": incident.get(
                "assignedResponderUid"
            ),
        },
    )

    if incident["status"] == "resolved":
        add_log(
            "incident.acknowledged",
            f"Synthetic demo incident {incident['shortId']} acknowledged.",
            incident["acknowledgedAt"],
            extra_metadata={
                "simulated": True,
                "method": "demo_simulation",
                "responseTimeSeconds": incident[
                    "responseTimeSeconds"
                ],
            },
        )

        add_log(
            "incident.resolved",
            f"Synthetic demo incident {incident['shortId']} resolved.",
            incident["resolvedAt"],
            level="success",
            extra_metadata={
                "simulated": True,
                "respondedDurationSeconds": incident[
                    "respondedDurationSeconds"
                ],
            },
        )

    else:
        add_log(
            "incident.cancelled",
            f"Synthetic demo incident {incident['shortId']} cancelled.",
            incident["cancelledAt"],
            level="warn",
            extra_metadata={
                "simulated": True,
                "reason": incident["cancelReason"],
            },
        )

    return logs


# ============================================================
# PREVIEW
# ============================================================

def preview_demo_dataset(
    db,
    admin: Dict[str, Any],
) -> Dict[str, Any]:

    dataset = _build_dataset(db)

    existing_incidents = 0
    incident_collisions = 0
    existing_logs = 0
    log_collisions = 0

    for item in dataset:
        incident_ref = (
            db.collection("incidents")
            .document(item["id"])
        )

        snap = incident_ref.get()

        if snap.exists:
            data = snap.to_dict() or {}

            if data.get("demoBatchId") == BATCH_ID:
                existing_incidents += 1
            else:
                incident_collisions += 1

        logs = _build_logs(
            item["id"],
            item["data"],
            item["index"],
        )

        for log in logs:
            log_ref = (
                db.collection("system_logs")
                .document(log["id"])
            )

            log_snap = log_ref.get()

            if log_snap.exists:
                log_data = log_snap.to_dict() or {}

                if log_data.get("demoBatchId") == BATCH_ID:
                    existing_logs += 1
                else:
                    log_collisions += 1

    responders = _get_responders_by_barangay(
        db,
        _get_barangays(),
    )

    responder_count = len({
        r["uid"]
        for values in responders.values()
        for r in values
    })

    return {
        "ok": True,
        "batchId": BATCH_ID,
        "seedVersion": SEED_VERSION,
        "dateRange": {
            "from": START_DATE.date().isoformat(),
            "to": END_DATE.date().isoformat(),
        },
        "planned": {
            "incidents": TOTAL_INCIDENTS,
            "resolved": RESOLVED_COUNT,
            "cancelled": CANCELLED_COUNT,
        },
        "existing": {
            "incidents": existing_incidents,
            "logs": existing_logs,
        },
        "collisions": {
            "incidents": incident_collisions,
            "logs": log_collisions,
        },
        "registeredActiveRespondersFound": responder_count,
        "adminUid": admin.get("uid"),
        "safeToSeed": (
            incident_collisions == 0
            and log_collisions == 0
        ),
    }


# ============================================================
# SEED
# ============================================================

def seed_demo_dataset(
    db,
    admin: Dict[str, Any],
) -> Dict[str, Any]:

    dataset = _build_dataset(db)

    created_incidents = 0
    skipped_incidents = 0
    created_logs = 0
    skipped_logs = 0

    # Check for collisions before writing anything.
    for item in dataset:
        snap = (
            db.collection("incidents")
            .document(item["id"])
            .get()
        )

        if snap.exists:
            data = snap.to_dict() or {}

            if data.get("demoBatchId") != BATCH_ID:
                raise RuntimeError(
                    f"Document collision detected: {item['id']}. "
                    "No data was seeded."
                )

        for log in _build_logs(
            item["id"],
            item["data"],
            item["index"],
        ):
            log_snap = (
                db.collection("system_logs")
                .document(log["id"])
                .get()
            )

            if log_snap.exists:
                log_data = log_snap.to_dict() or {}

                if log_data.get("demoBatchId") != BATCH_ID:
                    raise RuntimeError(
                        f"Log collision detected: {log['id']}. "
                        "No data was seeded."
                    )

    # Create incidents and their corresponding logs.
    for item in dataset:
        incident_ref = (
            db.collection("incidents")
            .document(item["id"])
        )

        existing = incident_ref.get()

        if existing.exists:
            skipped_incidents += 1
        else:
            incident_ref.create(item["data"])
            created_incidents += 1

        logs = _build_logs(
            item["id"],
            item["data"],
            item["index"],
        )

        for log in logs:
            log_ref = (
                db.collection("system_logs")
                .document(log["id"])
            )

            if log_ref.get().exists:
                skipped_logs += 1
            else:
                log_ref.create(log["data"])
                created_logs += 1

    _clear_analytics_cache()

    return {
        "ok": True,
        "message": "Synthetic demo dataset processed.",
        "batchId": BATCH_ID,
        "incidentsCreated": created_incidents,
        "incidentsAlreadyExisting": skipped_incidents,
        "logsCreated": created_logs,
        "logsAlreadyExisting": skipped_logs,
        "plannedResolved": RESOLVED_COUNT,
        "plannedCancelled": CANCELLED_COUNT,
        "pushNotificationsSent": 0,
        "smsMessagesSent": 0,
        "adminUid": admin.get("uid"),
    }


# ============================================================
# CLEANUP
# ============================================================

def delete_demo_dataset(
    db,
    admin: Dict[str, Any],
) -> Dict[str, Any]:

    dataset = _build_dataset(db)

    deleted_incidents = 0
    deleted_logs = 0

    for item in dataset:
        incident_ref = (
            db.collection("incidents")
            .document(item["id"])
        )

        incident_snap = incident_ref.get()

        if incident_snap.exists:
            data = incident_snap.to_dict() or {}

            # Never delete a document not owned by this batch.
            if (
                data.get("isSynthetic") is True
                and data.get("demoBatchId") == BATCH_ID
            ):
                incident_ref.delete()
                deleted_incidents += 1

        logs = _build_logs(
            item["id"],
            item["data"],
            item["index"],
        )

        for log in logs:
            log_ref = (
                db.collection("system_logs")
                .document(log["id"])
            )

            log_snap = log_ref.get()

            if log_snap.exists:
                log_data = log_snap.to_dict() or {}

                if log_data.get("demoBatchId") == BATCH_ID:
                    log_ref.delete()
                    deleted_logs += 1

    _clear_analytics_cache()

    return {
        "ok": True,
        "message": "Synthetic demo dataset cleanup completed.",
        "batchId": BATCH_ID,
        "incidentsDeleted": deleted_incidents,
        "logsDeleted": deleted_logs,
        "adminUid": admin.get("uid"),
    }
