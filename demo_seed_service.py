"""
RoadRescue synthetic capstone demo seeder.
Creates 45 simulated incidents and corresponding system logs.
Does not send push notifications.
"""

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from google.cloud.firestore_v1 import GeoPoint

BATCH_ID = "roadrescue-demo-2026-10-23-to-2026-11-09"

START_DATE = datetime(2026, 10, 23).date()
END_DATE = datetime(2026, 11, 9).date()

TOTAL_INCIDENTS = 45
RESOLVED_COUNT = 36
CANCELLED_COUNT = 9

PH_TZ = timezone(timedelta(hours=8))
USER_AGENT = "RoadRescue Demo Seeder"


def get_responders(db):
    responders = []

    for snap in db.collection("users").where(
        "role", "==", "responder"
    ).stream():

        data = snap.to_dict() or {}

        status = str(data.get("status") or "active").lower()

        if data.get("disabled") is True or status == "suspended":
            continue

        responders.append({
            "uid": snap.id,
            "name": (
                data.get("fullName")
                or data.get("displayName")
                or "Registered Responder"
            ),
            "email": data.get("email") or "",
            "barangay": (
                data.get("assignedBarangay")
                or data.get("barangay")
                or ""
            ),
            "status": status
        })

    responders.sort(key=lambda x: x["name"].lower())

    return responders


def get_barangays(db):
    barangays = []

    for snap in db.collection("barangays").stream():
        data = snap.to_dict() or {}

        name = data.get("name") or data.get("barangay")
        lat = data.get("lat")
        lng = data.get("lng")

        center = data.get("center")

        if isinstance(center, dict):
            lat = lat if lat is not None else center.get(
                "lat", center.get("latitude")
            )
            lng = lng if lng is not None else center.get(
                "lng", center.get("longitude")
            )

        elif center is not None:
            lat = lat if lat is not None else getattr(
                center, "latitude", None
            )
            lng = lng if lng is not None else getattr(
                center, "longitude", None
            )

        if name and lat is not None and lng is not None:
            barangays.append({
                "name": name,
                "lat": float(lat),
                "lng": float(lng)
            })

    if not barangays:
        from data.barangays import to_documents

        for item in to_documents():
            barangays.append({
                "name": item["name"],
                "lat": float(item["lat"]),
                "lng": float(item["lng"])
            })

    return sorted(barangays, key=lambda x: x["name"])


def generate_schedule():
    rng = random.Random(20261023)

    days = []
    current = START_DATE

    while current <= END_DATE:
        days.append(current)
        current += timedelta(days=1)

    # Nine days receive three incidents.
    # Nine days receive two incidents.
    counts = [2] * len(days)

    for index in rng.sample(range(len(days)), 9):
        counts[index] = 3

    incidents = []

    for day, count in zip(days, counts):
        minutes = sorted(
            rng.sample(range(8 * 60, 21 * 60), count)
        )

        for minute in minutes:
            created = datetime(
                day.year,
                day.month,
                day.day,
                minute // 60,
                minute % 60,
                tzinfo=PH_TZ
            ).astimezone(timezone.utc)

            incidents.append(created)

    outcomes = (
        [True] * RESOLVED_COUNT
        + [False] * CANCELLED_COUNT
    )

    rng.shuffle(outcomes)

    return list(zip(sorted(incidents), outcomes))


def build_dataset(db, admin):
    responders = get_responders(db)
    barangays = get_barangays(db)

    if not responders:
        raise ValueError(
            "No eligible responder accounts were found."
        )

    if not barangays:
        raise ValueError(
            "No barangay coordinates were found."
        )

    rng = random.Random(20261023)

    incident_docs = []
    log_docs = []

    barangay_map = {
        b["name"].lower(): b for b in barangays
    }

    incident_types = [
        "flat_tire",
        "battery",
        "fuel",
        "stalled_vehicle",
        "minor_collision",
        "major_collision",
        "vehicle_fire",
        "road_hazard"
    ]

    descriptions = {
        "flat_tire": "A vehicle has a flat tire and needs assistance.",
        "battery": "A vehicle battery has failed.",
        "fuel": "A vehicle has run out of fuel.",
        "stalled_vehicle": "A vehicle has stalled on the road.",
        "minor_collision": "A simulated minor road collision.",
        "major_collision": "A simulated major road collision.",
        "vehicle_fire": "Smoke was reported from a vehicle.",
        "road_hazard": "A road hazard is affecting passing vehicles."
    }

    for index, (created_at, resolved) in enumerate(
        generate_schedule(), start=1
    ):

        incident_id = f"demo-rr-20261023-{index:03d}"

        responder = responders[
            (index - 1) % len(responders)
        ]

        place = barangay_map.get(
            responder["barangay"].lower()
        ) if responder["barangay"] else None

        if place is None:
            place = barangays[
                (index - 1) % len(barangays)
            ]

        incident_type = incident_types[
            (index * 7 + 2) % len(incident_types)
        ]

        dispatch_at = created_at + timedelta(
            minutes=rng.randint(2, 8)
        )

        citizen_name = f"Demo Citizen {index:02d}"

        responder_actor = {
            "uid": responder["uid"],
            "email": responder["email"],
            "name": responder["name"],
            "role": "responder"
        }

        admin_actor = {
            "uid": admin.get("uid", "admin"),
            "email": admin.get("email"),
            "name": "RoadRescue Admin",
            "role": "admin"
        }

        citizen_actor = {
            "uid": f"demo-citizen-{index:03d}",
            "name": citizen_name,
            "role": "citizen"
        }

        doc = {
            "citizenUid": f"demo-citizen-{index:03d}",
            "citizenName": citizen_name,
            "citizenPhone": "",
            "type": incident_type,
            "description": (
                "SIMULATED CAPSTONE DEMO: "
                + descriptions[incident_type]
            ),
            "barangay": place["name"],
            "location": GeoPoint(
                place["lat"], place["lng"]
            ),
            "accuracy": 15,
            "status": "resolved" if resolved else "cancelled",

            "responderUid": responder["uid"],
            "responderName": responder["name"],

            "assignedResponderUid": responder["uid"],
            "assignedResponderName": responder["name"],
            "assignedResponderUids": [responder["uid"]],
            "assignedResponderNames": [responder["name"]],

            "photoUrls": [],
            "videoUrl": None,

            "createdAt": created_at,
            "verifiedAt": dispatch_at,
            "dispatchedAt": dispatch_at,
            "updatedAt": dispatch_at,

            "isSynthetic": True,
            "demoBatchId": BATCH_ID,
            "demoLabel": "CAPSTONE DEMO — SIMULATED INCIDENT",

            "demoOutcome": (
                "resolved"
                if resolved
                else "cancelled_without_acknowledgment"
            )
        }

        def add_log(
            suffix,
            timestamp,
            level,
            action,
            message,
            actor,
            category="incident",
            metadata=None
        ):

            log_id = f"{incident_id}-{suffix}"

            log_docs.append({
                "id": log_id,
                "timestamp": timestamp,
                "level": level,
                "category": category,
                "action": action,
                "message": "DEMO SIMULATION — " + message,
                "actor": actor,
                "target": {
                    "type": "incident",
                    "id": incident_id
                },
                "metadata": {
                    "isSynthetic": True,
                    "demoBatchId": BATCH_ID,
                    "simulatedAction": True,
                    **(metadata or {})
                },
                "userAgent": USER_AGENT,
                "isSynthetic": True,
                "demoBatchId": BATCH_ID
            })

        # INCIDENT REPORTED
        add_log(
            "reported",
            created_at,
            "info",
            "incident.reported",
            f"Demo citizen reported {incident_type} in {place['name']}",
            citizen_actor,
            metadata={
                "type": incident_type,
                "barangay": place["name"]
            }
        )

        # INCIDENT DISPATCHED
        add_log(
            "dispatched",
            dispatch_at,
            "success",
            "incident.dispatched",
            f"Admin dispatched incident {incident_id} to {responder['name']}",
            admin_actor,
            category="dispatch",
            metadata={
                "responderUid": responder["uid"],
                "responderName": responder["name"]
            }
        )

        if resolved:

            accepted_at = dispatch_at + timedelta(
                minutes=rng.randint(1, 4)
            )

            en_route_at = accepted_at + timedelta(
                minutes=rng.randint(1, 4)
            )

            on_scene_at = en_route_at + timedelta(
                minutes=rng.randint(5, 12)
            )

            resolved_at = on_scene_at + timedelta(
                minutes=rng.randint(8, 24)
            )

            response_seconds = int(
                (accepted_at - dispatch_at).total_seconds()
            )

            duration_seconds = int(
                (resolved_at - accepted_at).total_seconds()
            )

            doc.update({
                "status": "resolved",

                "acceptedAt": accepted_at,
                "acknowledgedAt": accepted_at,
                "acknowledgedVia": "demo",
                "acknowledgedBy": responder["uid"],

                "responseTimeSeconds": response_seconds,

                "resolvedAt": resolved_at,
                "resolvedBy": responder["uid"],

                "respondedDurationSeconds": duration_seconds,
                "updatedAt": resolved_at,

                "demoResponseStages": {
                    "acceptedAt": accepted_at,
                    "enRouteAt": en_route_at,
                    "onSceneAt": on_scene_at,
                    "resolvedAt": resolved_at
                }
            })

            # RESPONDER ACKNOWLEDGED
            add_log(
                "accepted",
                accepted_at,
                "success",
                "incident.accepted",
                f"Responder {responder['name']} acknowledged incident {incident_id}",
                responder_actor,
                metadata={
                    "response_seconds": response_seconds,
                    "method": "demo"
                }
            )

            # RESPONDER EN ROUTE
            add_log(
                "en-route",
                en_route_at,
                "info",
                "incident.status_changed",
                f"Responder {responder['name']} changed incident {incident_id} to en_route",
                responder_actor,
                metadata={
                    "new_status": "en_route"
                }
            )

            # RESPONDER ARRIVED
            add_log(
                "on-scene",
                on_scene_at,
                "info",
                "incident.status_changed",
                f"Responder {responder['name']} arrived at the scene for incident {incident_id}",
                responder_actor,
                metadata={
                    "new_status": "on_scene"
                }
            )

            # INCIDENT RESOLVED
            add_log(
                "resolved",
                resolved_at,
                "success",
                "incident.resolved",
                f"Responder {responder['name']} resolved incident {incident_id}",
                responder_actor,
                metadata={
                    "duration_seconds": duration_seconds
                }
            )

        else:

            cancelled_at = dispatch_at + timedelta(
                minutes=rng.randint(10, 35)
            )

            doc.update({
                "status": "cancelled",
                "cancelledAt": cancelled_at,
                "cancelledBy": "demo-system",
                "updatedAt": cancelled_at
            })

            add_log(
                "cancelled",
                cancelled_at,
                "warn",
                "incident.status_changed",
                f"Demo incident {incident_id} was cancelled before responder acknowledgment",
                {
                    "uid": "demo-system",
                    "name": "RoadRescue Demo Seeder",
                    "role": "system"
                },
                metadata={
                    "new_status": "cancelled",
                    "acknowledged": False
                }
            )

        incident_docs.append({
            "id": incident_id,
            "data": doc
        })

    return incident_docs, log_docs


def preview_demo_dataset(db, admin):
    incidents, logs = build_dataset(db, admin)
    responders = get_responders(db)

    return {
        "ok": True,
        "action": "preview",
        "demoBatchId": BATCH_ID,

        "dateRange": {
            "start": START_DATE.isoformat(),
            "end": END_DATE.isoformat(),
            "timezone": "Asia/Manila (UTC+8)",
            "inclusiveDays": 18
        },

        "incidents": {
            "total": len(incidents),
            "resolved": sum(
                1 for x in incidents
                if x["data"]["status"] == "resolved"
            ),
            "cancelled": sum(
                1 for x in incidents
                if x["data"]["status"] == "cancelled"
            )
        },

        "systemLogs": len(logs),

        "responders": responders,

        "notificationsSent": 0,
        "writesPerformed": 0
    }


def seed_demo_dataset(db, admin):
    incidents, logs = build_dataset(db, admin)

    existing_incidents = {
        snap.id for snap in
        db.collection("incidents")
        .where("demoBatchId", "==", BATCH_ID)
        .stream()
    }

    existing_logs = {
        snap.id for snap in
        db.collection("system_logs")
        .where("demoBatchId", "==", BATCH_ID)
        .stream()
    }

    pending = []

    for item in incidents:
        if item["id"] not in existing_incidents:
            pending.append((
                "incidents",
                item["id"],
                item["data"]
            ))

    for item in logs:
        if item["id"] not in existing_logs:
            pending.append((
                "system_logs",
                item["id"],
                {k: v for k, v in item.items() if k != "id"}
            ))

    if not pending:
        return {
            "ok": True,
            "alreadySeeded": True,
            "demoBatchId": BATCH_ID,
            "message": "Demo dataset already exists."
        }

    written = 0

    for offset in range(0, len(pending), 400):

        batch = db.batch()
        chunk = pending[offset:offset + 400]

        for collection, document_id, data in chunk:
            batch.set(
                db.collection(collection).document(document_id),
                data
            )

        batch.commit()
        written += len(chunk)

    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception:
        pass

    return {
        "ok": True,
        "alreadySeeded": False,
        "demoBatchId": BATCH_ID,
        "incidentsCreated": sum(
            1 for c, _, _ in pending if c == "incidents"
        ),
        "logsCreated": sum(
            1 for c, _, _ in pending if c == "system_logs"
        ),
        "documentsWritten": written,
        "notificationsSent": 0
    }


def cleanup_demo_dataset(db):
    deleted = {
        "incidents": 0,
        "system_logs": 0
    }

    for collection in ("incidents", "system_logs"):

        docs = list(
            db.collection(collection)
            .where("demoBatchId", "==", BATCH_ID)
            .stream()
        )

        for offset in range(0, len(docs), 400):

            batch = db.batch()
            chunk = docs[offset:offset + 400]

            for snap in chunk:
                batch.delete(snap.reference)

            if chunk:
                batch.commit()
                deleted[collection] += len(chunk)

    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception:
        pass

    return {
        "ok": True,
        "demoBatchId": BATCH_ID,
        "deleted": deleted
    }
