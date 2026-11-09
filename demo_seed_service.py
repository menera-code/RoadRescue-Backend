
"""
RoadRescue Realistic Incident Demonstration Seeder
Version: 2.1

Creates:
- 45 fictional demonstration incidents
- 36 resolved incidents
- 9 cancelled incidents
- 8 actual RoadRescue emergency categories
- Realistic roadside assistance descriptions
- Corresponding system_logs entries

Safety:
- Dedicated deterministic demo batch ID.
- No SMS or push notifications.
- No real responder impersonation in activity logs.
- Uses existing active responders for assignment fields.
- Preserves internal demo identification fields.
- Checks document collisions before writing.
- Uses atomic Firestore batch writes.
- Deletes only records belonging to this demo batch.
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

SEED_VERSION = "2.1"

PH_TIMEZONE = timezone(timedelta(hours=8))

RANDOM_SEED = 20261109

# Firestore batch maximum is 500 operations.
FIRESTORE_BATCH_LIMIT = 450


# ============================================================
# EMERGENCY CATEGORIES
# ============================================================

INCIDENT_DISTRIBUTION = {
    "Flat Tire": 8,
    "Dead Battery": 7,
    "Out of Fuel": 6,
    "Stalled": 6,
    "Minor Crash": 7,
    "Major Crash": 3,
    "Vehicle Fire": 2,
    "Road Hazard": 6,
}


# ============================================================
# INCIDENT DESCRIPTIONS
# ============================================================

DESCRIPTIONS = {
    "Flat Tire": [
        "The vehicle sustained a flat front tire while traveling along the road. The driver is requesting roadside assistance.",
        "A motorist reported a punctured rear tire and is unable to continue the journey safely.",
        "The driver noticed a sudden loss of tire pressure and pulled over near the roadside.",
        "A vehicle is stranded after experiencing a flat tire. Assistance is requested for tire replacement.",
    ],

    "Dead Battery": [
        "The vehicle will not start due to a suspected drained battery. The driver is requesting jump-start assistance.",
        "A motorist reported that the engine would not start after the vehicle was parked for an extended period.",
        "The vehicle's battery appears to have lost power, leaving the driver stranded.",
        "The driver requested assistance after experiencing repeated starting problems.",
    ],

    "Out of Fuel": [
        "The vehicle ran out of fuel while traveling and is currently stopped along the roadside.",
        "A motorist reported being stranded after the fuel supply was depleted.",
        "The driver is requesting fuel assistance after the vehicle stopped due to an empty fuel tank.",
        "A vehicle is parked along the roadside after running out of gasoline.",
    ],

    "Stalled": [
        "The vehicle's engine suddenly stopped while traveling. The driver was able to move to the roadside.",
        "A motorist reported engine trouble that caused the vehicle to stall unexpectedly.",
        "The vehicle is unable to restart after stalling along the roadway.",
        "The driver requested assistance after experiencing mechanical problems during travel.",
    ],

    "Minor Crash": [
        "A minor collision involving two vehicles was reported. The drivers are requesting assistance and incident assessment.",
        "A vehicle sustained minor damage following a low-speed collision near an intersection.",
        "A motorist reported a minor road accident involving another vehicle. Traffic is partially affected.",
        "A minor vehicle collision was reported. Initial reports indicate no obvious serious injuries.",
    ],

    "Major Crash": [
        "A serious collision involving multiple vehicles was reported. Immediate assessment and emergency assistance are requested.",
        "A major road accident was reported along the main roadway. Possible injuries have been reported.",
        "A vehicle sustained significant damage following a collision. Medical and emergency response assistance is requested.",
        "A serious traffic accident was reported, causing partial obstruction of the roadway.",
    ],

    "Vehicle Fire": [
        "Smoke was observed coming from the vehicle's engine compartment. The driver immediately stopped and requested assistance.",
        "A motorist reported flames coming from a vehicle parked along the roadside.",
        "A vehicle fire was reported after the driver noticed smoke and unusual heat coming from the engine.",
        "A burning smell followed by visible smoke was reported from a vehicle. Immediate assistance is requested.",
    ],

    "Road Hazard": [
        "A fallen tree branch is obstructing part of the roadway and creating a hazard for passing vehicles.",
        "A large object was reported blocking one lane of the road.",
        "A damaged portion of the roadway was reported, creating difficulty for passing motorists.",
        "Debris scattered along the road is posing a potential danger to passing vehicles.",
    ],
}


# ============================================================
# HELPERS
# ============================================================

def _normalize_name(value: str) -> str:
    value = re.sub(r"\(.*?\)", "", value.lower())
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _incident_id(index: int) -> str:
    return f"demo_oct23_nov09_2026_{index:03d}"


def _log_id(index: int, action: str) -> str:
    return (
        f"demo_oct23_nov09_2026_"
        f"{index:03d}_{action.replace('.', '_')}"
    )


def _clear_analytics_cache() -> None:
    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception as exc:
        print(
            f"[demo-seed] Analytics cache clear skipped: {exc}"
        )


def _get_barangays() -> List[Dict[str, Any]]:
    from data.barangays import to_documents

    barangays = to_documents()

    if not barangays:
        raise RuntimeError(
            "No barangays found in data/barangays.py"
        )

    for barangay in barangays:
        required = ["name", "slug", "lat", "lng"]

        for field in required:
            if field not in barangay:
                raise RuntimeError(
                    f"Barangay missing required field: {field}"
                )

    return barangays


def _get_priority(incident_type: str) -> str:
    priorities = {
        "Flat Tire": "low",
        "Dead Battery": "low",
        "Out of Fuel": "low",
        "Stalled": "medium",
        "Minor Crash": "medium",
        "Major Crash": "critical",
        "Vehicle Fire": "critical",
        "Road Hazard": "medium",
    }

    return priorities.get(incident_type, "medium")


def _validate_configuration() -> None:

    if sum(INCIDENT_DISTRIBUTION.values()) != TOTAL_INCIDENTS:
        raise RuntimeError(
            "Emergency category distribution must total 45."
        )

    if RESOLVED_COUNT + CANCELLED_COUNT != TOTAL_INCIDENTS:
        raise RuntimeError(
            "Resolved and cancelled counts must total 45."
        )

    if START_DATE > END_DATE:
        raise RuntimeError(
            "START_DATE cannot be after END_DATE."
        )

    for category in INCIDENT_DISTRIBUTION:
        if category not in DESCRIPTIONS:
            raise RuntimeError(
                f"Missing descriptions for {category}."
            )


# ============================================================
# REGISTERED RESPONDERS
# ============================================================

def _get_responders_by_barangay(
    db,
    barangays: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:

    users_by_uid = {}

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

    result = {}

    for barangay in barangays:

        name = barangay["name"]
        slug = barangay["slug"]

        normalized = _normalize_name(name)

        matched = {}

        # Match responders through their profiles.
        for uid, user in users_by_uid.items():

            assigned = (
                user.get("assignedBarangay")
                or user.get("barangay")
                or ""
            )

            if _normalize_name(str(assigned)) == normalized:
                matched[uid] = user

        # Match responders through barangay documents.
        try:

            snapshot = (
                db.collection("barangays")
                .document(slug)
                .get()
            )

            if snapshot.exists:

                data = snapshot.to_dict() or {}

                assigned = data.get("responders", [])

                if isinstance(assigned, list):

                    for responder in assigned:

                        if not isinstance(responder, dict):
                            continue

                        uid = responder.get("uid")

                        if uid in users_by_uid:
                            matched[uid] = users_by_uid[uid]

                # Legacy schema.
                legacy_uid = data.get("responderUid")

                if legacy_uid in users_by_uid:
                    matched[legacy_uid] = users_by_uid[legacy_uid]

        except Exception as exc:

            print(
                f"[demo-seed] Responder lookup failed "
                f"for {name}: {exc}"
            )

        result[slug] = [
            {
                "uid": uid,
                "name": (
                    user.get("fullName")
                    or user.get("displayName")
                    or "Registered Responder"
                ),
            }
            for uid, user in matched.items()
        ]

    return result


# ============================================================
# BUILD INCIDENT DATASET
# ============================================================

def _build_dataset(db) -> List[Dict[str, Any]]:

    _validate_configuration()

    rng = Random(RANDOM_SEED)

    barangays = _get_barangays()

    responders_by_barangay = (
        _get_responders_by_barangay(db, barangays)
    )

    # Generate exact emergency category distribution.
    incident_types = []

    for category, count in INCIDENT_DISTRIBUTION.items():
        incident_types.extend([category] * count)

    rng.shuffle(incident_types)

    # Generate 36 resolved and 9 cancelled records.
    statuses = (
        ["resolved"] * RESOLVED_COUNT
        + ["cancelled"] * CANCELLED_COUNT
    )

    rng.shuffle(statuses)

    total_days = (END_DATE - START_DATE).days + 1

    dataset = []

    for index in range(1, TOTAL_INCIDENTS + 1):

        barangay = barangays[
            (index - 1) % len(barangays)
        ]

        incident_type = incident_types[index - 1]
        status = statuses[index - 1]

        # Generate reporting times in Philippine time.
        day_offset = rng.randint(0, total_days - 1)

        local_created = (
            START_DATE + timedelta(days=day_offset)
        ).replace(
            hour=rng.randint(6, 22),
            minute=rng.randint(0, 59),
            second=rng.randint(0, 59),
        )

        created_at = local_created.replace(
            tzinfo=PH_TIMEZONE
        ).astimezone(timezone.utc)

        verified_at = created_at + timedelta(
            minutes=rng.randint(2, 12)
        )

        dispatched_at = verified_at + timedelta(
            minutes=rng.randint(1, 8)
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

        # Barangay coordinates with small location variation.
        lat = float(barangay["lat"]) + rng.uniform(
            -0.0015, 0.0015
        )

        lng = float(barangay["lng"]) + rng.uniform(
            -0.0015, 0.0015
        )

        # Find existing responder assigned to barangay.
        assigned_responders = responders_by_barangay.get(
            barangay["slug"],
            [],
        )

        responder = None

        if assigned_responders:
            responder = assigned_responders[
                (index - 1) % len(assigned_responders)
            ]

        priority = _get_priority(incident_type)

        incident_id = _incident_id(index)

        incident = {
            "shortId": f"DEMO{index:03d}",

            "type": incident_type,

            "description": rng.choice(
                DESCRIPTIONS[incident_type]
            ),

            "barangay": barangay["name"],

            "suggestedBarangay": barangay["name"],

            "location": firestore.GeoPoint(lat, lng),

            "citizenName": f"Demo Citizen {index:03d}",

            "citizenPhone": "",

            "citizenUid": None,

            "anonymous": False,

            "deviceId": (
                f"roadrescue-demo-device-{index:03d}"
            ),

            "status": status,

            "priority": priority,

            "createdAt": created_at,

            "updatedAt": (
                resolved_at
                if status == "resolved"
                else cancelled_at
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
                "keywords": [
                    incident_type.lower(),
                    "roadside assistance",
                ],
                "mentionedVehicles": [],
                "vehicles": {},
                "allVehicles": [],
                "sources": ["demo_incident"],
                "reportedType": incident_type,
                "reportedTypeMatches": True,
                "processedAt": created_at,
            },

            # Internal identification fields.
            "isSynthetic": True,
            "demoBatchId": BATCH_ID,
            "demoSeedVersion": SEED_VERSION,
        }

        if responder:

            incident.update({
                "assignedResponderUid": responder["uid"],
                "assignedResponderName": responder["name"],
                "assignedResponderUids": [
                    responder["uid"]
                ],
                "assignedResponderNames": [
                    responder["name"]
                ],
            })

        if status == "resolved":

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
                    "Report cancelled in the demonstration workflow."
                ),
            })

        dataset.append({
            "id": incident_id,
            "index": index,
            "data": incident,
        })

    return dataset


# ============================================================
# BUILD INCIDENT ACTIVITY LOGS
# ============================================================

def _build_logs(
    incident_id: str,
    incident: Dict[str, Any],
    index: int,
) -> List[Dict[str, Any]]:

    logs = []

    # All events are attributed to the system.
    # No real responder is impersonated.
    actor = {
        "uid": "demo-seeder",
        "email": None,
        "role": "system",
    }

    target = {
        "type": "incident",
        "id": incident_id,
    }

    batch_metadata = {
        "isSynthetic": True,
        "demoBatchId": BATCH_ID,
        "demoSeedVersion": SEED_VERSION,
        "demonstration": True,
    }

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
            "id": _log_id(index, action),

            "data": {
                "timestamp": timestamp,
                "level": level,
                "category": "incident",
                "action": action,
                "message": message[:500],
                "actor": actor,
                "target": target,
                "metadata": metadata,
                "userAgent": "RoadRescue Demonstration",

                # Top-level ownership fields for safe cleanup.
                "isSynthetic": True,
                "demoBatchId": BATCH_ID,
                "demoSeedVersion": SEED_VERSION,
            },
        })

    short_id = incident["shortId"]

    created_at = incident["createdAt"]
    verified_at = incident["verifiedAt"]
    dispatched_at = incident["dispatchedAt"]

    # Incident received.
    add_log(
        "incident.created",
        f"Incident report {short_id} received.",
        created_at,
        extra_metadata={
            "barangay": incident["barangay"],
            "incidentType": incident["type"],
        },
    )

    # Report verification.
    add_log(
        "incident.verified",
        f"Incident report {short_id} verified in the demonstration workflow.",
        verified_at,
    )

    # Dispatch event.
    add_log(
        "incident.dispatched",
        f"Incident {short_id} routed to the designated response area.",
        dispatched_at,
        extra_metadata={
            "responderUid": incident.get(
                "assignedResponderUid"
            ),
        },
    )

    if incident["status"] == "resolved":

        add_log(
            "incident.acknowledged",
            f"Incident {short_id} acknowledged in the demonstration workflow.",
            incident["acknowledgedAt"],
            extra_metadata={
                "method": "demo_simulation",
                "responseTimeSeconds": incident[
                    "responseTimeSeconds"
                ],
            },
        )

        add_log(
            "incident.resolved",
            f"Incident {short_id} marked as resolved in the demonstration workflow.",
            incident["resolvedAt"],
            level="success",
            extra_metadata={
                "respondedDurationSeconds": incident[
                    "respondedDurationSeconds"
                ],
            },
        )

    else:

        add_log(
            "incident.cancelled",
            f"Incident report {short_id} marked as cancelled in the demonstration workflow.",
            incident["cancelledAt"],
            level="warn",
            extra_metadata={
                "reason": incident["cancelReason"],
            },
        )

    return logs


# ============================================================
# PREVIEW DATASET
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

            if (
                data.get("isSynthetic") is True
                and data.get("demoBatchId") == BATCH_ID
            ):
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
        responder["uid"]
        for values in responders.values()
        for responder in values
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
            "distribution": INCIDENT_DISTRIBUTION,
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
# SEED DATASET
# ============================================================

def seed_demo_dataset(
    db,
    admin: Dict[str, Any],
) -> Dict[str, Any]:

    dataset = _build_dataset(db)

    pending_writes = []

    skipped_incidents = 0
    skipped_logs = 0

    created_incidents = 0
    created_logs = 0

    # --------------------------------------------------------
    # FIRST PASS: CHECK ALL DOCUMENT COLLISIONS.
    # NO WRITES ARE PERFORMED DURING THIS PASS.
    # --------------------------------------------------------

    for item in dataset:

        incident_ref = (
            db.collection("incidents")
            .document(item["id"])
        )

        snap = incident_ref.get()

        if snap.exists:

            data = snap.to_dict() or {}

            if (
                data.get("isSynthetic") is not True
                or data.get("demoBatchId") != BATCH_ID
            ):
                raise RuntimeError(
                    f"Incident document collision: {item['id']}. "
                    "No data was seeded."
                )

            skipped_incidents += 1

        else:

            pending_writes.append(
                (incident_ref, item["data"], "incident")
            )

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

                if log_data.get("demoBatchId") != BATCH_ID:
                    raise RuntimeError(
                        f"System log document collision: "
                        f"{log['id']}. No data was seeded."
                    )

                skipped_logs += 1

            else:

                pending_writes.append(
                    (log_ref, log["data"], "log")
                )

    # --------------------------------------------------------
    # SECOND PASS: ATOMIC FIRESTORE BATCH WRITES.
    # --------------------------------------------------------

    for start in range(
        0,
        len(pending_writes),
        FIRESTORE_BATCH_LIMIT,
    ):

        chunk = pending_writes[
            start:start + FIRESTORE_BATCH_LIMIT
        ]

        batch = db.batch()

        for ref, data, record_type in chunk:

            # create() prevents overwriting existing documents.
            batch.create(ref, data)

            if record_type == "incident":
                created_incidents += 1
            else:
                created_logs += 1

        # Firestore commits each chunk atomically.
        batch.commit()

    _clear_analytics_cache()

    return {
        "ok": True,

        "message": "Demonstration incident dataset processed.",

        "batchId": BATCH_ID,

        "seedVersion": SEED_VERSION,

        "incidentsCreated": created_incidents,

        "incidentsAlreadyExisting": skipped_incidents,

        "logsCreated": created_logs,

        "logsAlreadyExisting": skipped_logs,

        "plannedIncidents": TOTAL_INCIDENTS,

        "plannedResolved": RESOLVED_COUNT,

        "plannedCancelled": CANCELLED_COUNT,

        "emergencyDistribution": INCIDENT_DISTRIBUTION,

        "pushNotificationsSent": 0,

        "smsMessagesSent": 0,

        "adminUid": admin.get("uid"),
    }


# ============================================================
# DELETE DEMONSTRATION DATASET
# ============================================================

def delete_demo_dataset(
    db,
    admin: Dict[str, Any],
) -> Dict[str, Any]:

    deleted_incidents = 0
    deleted_logs = 0

    skipped_incidents = 0
    skipped_logs = 0

    # --------------------------------------------------------
    # FIND INCIDENTS USING THE EXACT BATCH ID.
    # --------------------------------------------------------

    incident_docs = (
        db.collection("incidents")
        .where("demoBatchId", "==", BATCH_ID)
        .stream()
    )

    pending_deletes = []

    for doc in incident_docs:

        data = doc.to_dict() or {}

        # Require both ownership markers.
        if data.get("isSynthetic") is True:

            pending_deletes.append(
                (doc.reference, "incident")
            )

        else:
            skipped_incidents += 1

    # --------------------------------------------------------
    # FIND SYSTEM LOGS USING THE EXACT BATCH ID.
    # --------------------------------------------------------

    log_docs = (
        db.collection("system_logs")
        .where("demoBatchId", "==", BATCH_ID)
        .stream()
    )

    for doc in log_docs:

        data = doc.to_dict() or {}

        metadata = data.get("metadata") or {}

        # Support both current and legacy log structures.
        is_synthetic = (
            data.get("isSynthetic") is True
            or metadata.get("isSynthetic") is True
        )

        if is_synthetic:

            pending_deletes.append(
                (doc.reference, "log")
            )

        else:
            skipped_logs += 1

    # --------------------------------------------------------
    # DELETE IN FIRESTORE BATCHES.
    # --------------------------------------------------------

    for start in range(
        0,
        len(pending_deletes),
        FIRESTORE_BATCH_LIMIT,
    ):

        chunk = pending_deletes[
            start:start + FIRESTORE_BATCH_LIMIT
        ]

        batch = db.batch()

        for ref, record_type in chunk:

            batch.delete(ref)

            if record_type == "incident":
                deleted_incidents += 1
            else:
                deleted_logs += 1

        batch.commit()

    _clear_analytics_cache()

    return {
        "ok": True,

        "message": "Demonstration incident cleanup completed.",

        "batchId": BATCH_ID,

        "incidentsDeleted": deleted_incidents,

        "logsDeleted": deleted_logs,

        "incidentsSkipped": skipped_incidents,

        "logsSkipped": skipped_logs,

        "adminUid": admin.get("uid"),
    }


# ============================================================
# DATASET VERIFICATION
# ============================================================

def verify_demo_dataset(
    db,
    admin: Dict[str, Any],
) -> Dict[str, Any]:

    incidents = (
        db.collection("incidents")
        .where("demoBatchId", "==", BATCH_ID)
        .stream()
    )

    total = 0
    resolved = 0
    cancelled = 0
    invalid = 0

    distribution = {}

    for doc in incidents:

        data = doc.to_dict() or {}

        total += 1

        if data.get("isSynthetic") is not True:
            invalid += 1

        status = data.get("status")

        if status == "resolved":
            resolved += 1

        elif status == "cancelled":
            cancelled += 1

        category = data.get("type", "Unknown")

        distribution[category] = (
            distribution.get(category, 0) + 1
        )

    logs = (
        db.collection("system_logs")
        .where("demoBatchId", "==", BATCH_ID)
        .stream()
    )

    log_count = sum(1 for _ in logs)

    return {
        "ok": (
            total == TOTAL_INCIDENTS
            and resolved == RESOLVED_COUNT
            and cancelled == CANCELLED_COUNT
            and invalid == 0
        ),

        "batchId": BATCH_ID,

        "seedVersion": SEED_VERSION,

        "actual": {
            "incidents": total,
            "resolved": resolved,
            "cancelled": cancelled,
            "logs": log_count,
            "invalidSyntheticMarkers": invalid,
            "distribution": distribution,
        },

        "expected": {
            "incidents": TOTAL_INCIDENTS,
            "resolved": RESOLVED_COUNT,
            "cancelled": CANCELLED_COUNT,
            "distribution": INCIDENT_DISTRIBUTION,
        },

        "adminUid": admin.get("uid"),
    }
