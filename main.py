"""
RoadRescue API — FastAPI backend.
"""

import math
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

load_dotenv()

VERSION = "0.3.2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify_py(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[()]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _to_dt(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    return None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from firebase_init import init_firebase_admin
        init_firebase_admin()
    except Exception as e:
        print(f"[startup] ⚠️ Firebase Admin init failed: {e}")

    try:
        from firestore_worker import start_worker
        start_worker()
        print("[startup] ✅ Firestore worker requested")
    except Exception as e:
        print(f"[startup] ⚠️ Firestore worker failed to start: {e}")

    yield

    try:
        from firestore_worker import stop_worker
        stop_worker()
        print("[shutdown] Firestore worker stopped")
    except Exception:
        pass


app = FastAPI(
    title="RoadRescue API",
    description="ML classification + analytics backend for RoadRescue",
    version=VERSION,
    lifespan=lifespan,
)

_allowed = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
)
ALLOWED_ORIGINS = [
    origin.strip() for origin in _allowed.split(",") if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register admin user management router
from admin_users import router as admin_users_router, me_router
app.include_router(admin_users_router)
app.include_router(me_router)

# Logging service
from log_service import log_event, log_from_request


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ClassifyRequest(BaseModel):
    text: str
    image_urls: Optional[List[str]] = None
    reported_type: Optional[str] = None


class ClassifyResponse(BaseModel):
    predicted_type: str
    predicted_severity: str
    confidence: float
    keywords: List[str] = []
    mentioned_vehicles: List[str] = []
    detections: List[Dict[str, Any]] = []
    vehicles: Dict[str, int] = {}
    all_vehicles: List[str] = []
    sources: List[str] = []
    reported_type: Optional[str] = None
    reported_type_matches: Optional[bool] = None


class DispatchRequest(BaseModel):
    incident_id: str


class DispatchResponse(BaseModel):
    ok: bool
    incident_id: str
    status: str
    assigned_responder_uid: Optional[str] = None
    assigned_responder_name: Optional[str] = None
    assigned_responder_phone: Optional[str] = None
    sms_ok: bool = False
    sms_provider: Optional[str] = None
    sms_message_id: Optional[str] = None
    sms_error: Optional[str] = None
    message: str = ""


class AcknowledgeRequest(BaseModel):
    incident_id: str
    method: str = "app"
    responder_uid: Optional[str] = None


class ResolveRequest(BaseModel):
    incident_id: str
    responder_uid: Optional[str] = None


class EmergencyRequest(BaseModel):
    lat: float
    lng: float
    note: Optional[str] = None
    device_id: Optional[str] = None
    audio_url: Optional[str] = None
    audio_duration_seconds: Optional[int] = None


class EmergencyResponse(BaseModel):
    ok: bool
    incident_id: Optional[str] = None
    short_id: Optional[str] = None
    barangay: Optional[str] = None
    responder_assigned: bool = False
    sms_sent: bool = False
    has_audio: bool = False
    message: str = ""


class ToggleUserStatusRequest(BaseModel):
    uid: str
    disabled: bool


class ClientLogRequest(BaseModel):
    level: str = "info"
    category: str = "ui"
    action: str
    message: str = ""
    target: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/")
def root() -> Dict[str, str]:
    return {"name": "RoadRescue API", "version": VERSION, "docs": "/docs"}


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": VERSION,
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY")),
        "firebase_configured": bool(os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")),
        "mocean_configured": bool(os.getenv("MOCEAN_API_TOKEN")),
    }


# ---------------------------------------------------------------------------
# Client log ingestion (for frontend-originated events)
# ---------------------------------------------------------------------------

@app.post("/log")
async def ingest_client_log(req: ClientLogRequest, request: Request) -> Dict[str, Any]:
    """Endpoint the frontend can call to write a log entry with UA captured."""
    log_from_request(
        request,
        level=req.level,
        category=req.category,
        action=req.action,
        message=req.message,
        target=req.target,
        metadata=req.metadata,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Classify
# ---------------------------------------------------------------------------

@app.post("/classify", response_model=ClassifyResponse)
async def classify(req: ClassifyRequest) -> ClassifyResponse:
    from predictor import predict_incident

    result = predict_incident(text=req.text, image_urls=req.image_urls)

    reported_matches = None
    if req.reported_type:
        reported_matches = req.reported_type == result["predicted_type"]

    log_event(
        level="info",
        category="ai",
        action="classify.run",
        message=f"ML classified as {result['predicted_type']} ({result['predicted_severity']})",
        actor={"uid": "system", "role": "system"},
        metadata={
            "predicted_type": result["predicted_type"],
            "confidence": result["confidence"],
            "sources": result["sources"],
        },
    )

    return ClassifyResponse(
        predicted_type=result["predicted_type"],
        predicted_severity=result["predicted_severity"],
        confidence=result["confidence"],
        keywords=result["keywords"],
        mentioned_vehicles=result["mentioned_vehicles"],
        detections=result["detections"],
        vehicles=result["vehicles"],
        all_vehicles=result["all_vehicles"],
        sources=result["sources"],
        reported_type=req.reported_type,
        reported_type_matches=reported_matches,
    )


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/summary")
async def analytics_summary(days: int = 90) -> Dict[str, Any]:
    from analytics_service import get_summary
    return get_summary(days=days)


@app.get("/analytics/timeline")
async def analytics_timeline(days: int = 30) -> Dict[str, Any]:
    from analytics_service import get_timeline
    return get_timeline(days=days)


@app.get("/analytics/by-barangay")
async def analytics_by_barangay(days: int = 90) -> Dict[str, Any]:
    from analytics_service import get_by_barangay
    return get_by_barangay(days=days)


@app.get("/analytics/qrt")
async def analytics_qrt(days: int = 90) -> Dict[str, Any]:
    from analytics_service import get_qrt
    return get_qrt(days=days)


@app.get("/analytics/responders")
async def analytics_responders(days: int = 90) -> Dict[str, Any]:
    from analytics_service import get_responders
    return get_responders(days=days)


@app.get("/analytics/hourly")
async def analytics_hourly(days: int = 30) -> Dict[str, Any]:
    from analytics_service import get_hourly_heatmap
    return get_hourly_heatmap(days=days)


@app.get("/analytics/history")
async def analytics_history(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    status: Optional[str] = None,
    type: Optional[str] = None,
    barangay: Optional[str] = None,
    days: int = 90,
    limit: int = 500,
) -> Dict[str, Any]:
    from analytics_service import get_history
    return get_history(
        days=days,
        from_date=from_date,
        to_date=to_date,
        status=status,
        incident_type=type,
        barangay=barangay,
        limit=limit,
    )


@app.get("/analytics/barangay-temporal")
async def analytics_barangay_temporal(days: int = 90) -> Dict[str, Any]:
    from analytics_service import get_barangay_temporal
    return get_barangay_temporal(days=days)


@app.get("/analytics/vehicles")
async def analytics_vehicles(days: int = 90) -> Dict[str, Any]:
    from analytics_service import get_vehicle_analytics
    return get_vehicle_analytics(days=days)


@app.get("/analytics/ml-performance")
async def analytics_ml_performance(days: int = 90) -> Dict[str, Any]:
    from analytics_service import get_ml_performance
    return get_ml_performance(days=days)


@app.get("/analytics/patterns")
async def analytics_patterns(days: int = 180) -> Dict[str, Any]:
    from analytics_service import get_patterns
    return get_patterns(days=days)


@app.get("/analytics/predictions")
async def analytics_predictions(days: int = 90) -> Dict[str, Any]:
    from analytics_service import get_predictions
    return get_predictions(days=days)


# ---------------------------------------------------------------------------
# Dispatch incident
# ---------------------------------------------------------------------------

@app.post("/dispatch-incident", response_model=DispatchResponse)
async def dispatch_incident(req: DispatchRequest) -> DispatchResponse:
    from firebase_admin import firestore
    from sms_service import build_incident_sms, send_incident_sms

    db = firestore.client()
    incident_ref = db.collection("incidents").document(req.incident_id)
    snap = incident_ref.get()

    if not snap.exists:
        return DispatchResponse(
            ok=False, incident_id=req.incident_id, status="not_found",
            message="Incident not found.",
        )

    incident = snap.to_dict() or {}
    barangay_name = incident.get("barangay")

    if not barangay_name:
        return DispatchResponse(
            ok=False, incident_id=req.incident_id, status="no_barangay",
            message="Incident has no barangay.",
        )

    b_slug = slugify_py(barangay_name)
    b_snap = db.collection("barangays").document(b_slug).get()

    if not b_snap.exists:
        return DispatchResponse(
            ok=False, incident_id=req.incident_id, status="barangay_not_found",
            message=f"Barangay '{barangay_name}' not found.",
        )

    b_data = b_snap.to_dict() or {}

    # ---- Collect responders (multi-responder model + legacy fallback) ----
    responders = []
    if isinstance(b_data.get("responders"), list):
        responders = [
            r for r in b_data["responders"]
            if isinstance(r, dict) and r.get("uid") and r.get("phone")
        ]
    # Legacy fallback: single responder assigned on the old schema
    if not responders and b_data.get("responderUid") and b_data.get("responderPhone"):
        responders = [{
            "uid": b_data["responderUid"],
            "name": b_data.get("responderName") or "",
            "phone": b_data["responderPhone"],
        }]

    if not responders:
        return DispatchResponse(
            ok=False, incident_id=req.incident_id, status="no_responder",
            message=f"No responders assigned to '{barangay_name}'.",
        )

    # ---- Build SMS body once; send to every responder ----
    sms_body = build_incident_sms(incident, barangay_name, req.incident_id)

    sms_results = []
    for r in responders:
        try:
            result = send_incident_sms(
                to_phone=r["phone"],
                message=sms_body,
                incident_id=req.incident_id,
            )
        except Exception as e:
            result = {
                "ok": False,
                "provider": "error",
                "messageId": None,
                "error": str(e)[:200],
            }
        sms_results.append({"responder": r, "result": result})

    any_ok = any(sr["result"].get("ok") for sr in sms_results)
    ok_count = sum(1 for sr in sms_results if sr["result"].get("ok"))

    now = datetime.now(timezone.utc)
    short_id = req.incident_id[:6].upper()

    # ---- Persist: store arrays + legacy first-responder mirror ----
    first = responders[0]
    incident_ref.update({
        "status": "pending",
        "shortId": short_id,

        # Multi-responder fields (new)
        "assignedResponderUids":  [r["uid"] for r in responders],
        "assignedResponderNames": [r.get("name") or "" for r in responders],
        "assignedResponderPhones": [r["phone"] for r in responders],

        # Legacy single-responder mirror (first in list)
        "assignedResponderUid":   first["uid"],
        "assignedResponderName":  first.get("name") or "",
        "assignedResponderPhone": first["phone"],

        "verifiedAt": now,
        "dispatchedAt": now,
        "smsSent": any_ok,
        "smsSentAt": now if any_ok else None,
        "smsProvider": sms_results[0]["result"].get("provider"),
        "smsMessageId": sms_results[0]["result"].get("messageId"),
        "smsError": None if any_ok else sms_results[0]["result"].get("error"),
        "smsRecipientCount": len(responders),
        "smsSuccessCount": ok_count,
        "updatedAt": now,
    })

    # ---- Per-responder notification log ----
    try:
        for sr in sms_results:
            db.collection("notifications").add({
                "incidentId": req.incident_id,
                "responderUid": sr["responder"]["uid"],
                "responderPhone": sr["responder"]["phone"],
                "responderName": sr["responder"].get("name") or "",
                "channel": "sms",
                "provider": sr["result"].get("provider"),
                "message": sms_body,
                "ok": sr["result"].get("ok", False),
                "error": sr["result"].get("error"),
                "createdAt": now,
            })
    except Exception as e:
        print(f"[dispatch] notification log failed: {e}")

    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception:
        pass

    log_event(
        level="success" if any_ok else "warn",
        category="dispatch",
        action="incident.dispatched",
        message=(
            f"Incident {short_id} dispatched to {len(responders)} responder(s) "
            f"in {barangay_name} ({ok_count} SMS sent)"
        ),
        actor={"uid": "system", "role": "system"},
        target={"type": "incident", "id": req.incident_id},
        metadata={
            "barangay": barangay_name,
            "responder_count": len(responders),
            "responder_uids": [r["uid"] for r in responders],
            "sms_ok_count": ok_count,
            "sms_any_ok": any_ok,
        },
    )

    return DispatchResponse(
        ok=True,
        incident_id=req.incident_id,
        status="dispatched",
        assigned_responder_uid=first["uid"],
        assigned_responder_name=first.get("name") or "",
        assigned_responder_phone=first["phone"],
        sms_ok=any_ok,
        sms_provider=sms_results[0]["result"].get("provider"),
        sms_message_id=sms_results[0]["result"].get("messageId"),
        sms_error=None if any_ok else sms_results[0]["result"].get("error"),
        message=(
            f"Dispatched. SMS sent to {ok_count}/{len(responders)} responders."
            if any_ok
            else "Dispatched, but SMS failed for all responders."
        ),
    )

# ---------------------------------------------------------------------------
# Resolve incident
# ---------------------------------------------------------------------------

@app.post("/resolve-incident")
async def resolve_incident(req: ResolveRequest) -> Dict[str, Any]:
    from firebase_admin import firestore

    db = firestore.client()
    ref = db.collection("incidents").document(req.incident_id)
    snap = ref.get()

    if not snap.exists:
        return {"ok": False, "error": "Incident not found."}

    data = snap.to_dict() or {}

    if data.get("status") == "resolved":
        return {
            "ok": True,
            "already": True,
            "incident_id": req.incident_id,
            "responded_duration_seconds": data.get("respondedDurationSeconds"),
        }

    now = datetime.now(timezone.utc)

    accepted_at = _to_dt(data.get("acceptedAt"))
    responded_duration = (
        max(0, int((now - accepted_at).total_seconds()))
        if accepted_at else None
    )

    payload: Dict[str, Any] = {
        "status": "resolved",
        "resolvedAt": now,
        "respondedDurationSeconds": responded_duration,
        "updatedAt": now,
    }
    if req.responder_uid:
        payload["resolvedBy"] = req.responder_uid

    ref.update(payload)

    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception:
        pass

    log_event(
        level="success",
        category="incident",
        action="incident.resolved",
        message=f"Incident {req.incident_id[:6].upper()} resolved in {responded_duration}s",
        actor={"uid": req.responder_uid or "system", "role": "responder"},
        target={"type": "incident", "id": req.incident_id},
        metadata={"duration_seconds": responded_duration},
    )

    print(
        f"[resolve] {req.incident_id[:6].upper()} resolved "
        f"in {responded_duration}s"
    )

    return {
        "ok": True,
        "already": False,
        "incident_id": req.incident_id,
        "responded_duration_seconds": responded_duration,
    }


# ---------------------------------------------------------------------------
# Anonymous emergency
# ---------------------------------------------------------------------------

def _nearest_barangay(lat: float, lng: float) -> Optional[Dict[str, Any]]:
    try:
        from data.barangays import to_documents
    except Exception as e:
        print(f"[emergency] could not import barangay list: {e}")
        return None

    docs = to_documents()
    if not docs:
        return None

    def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        R = 6371000.0
        to_rad = math.radians
        d_lat = to_rad(lat2 - lat1)
        d_lng = to_rad(lng2 - lng1)
        a = (
            math.sin(d_lat / 2) ** 2
            + math.cos(to_rad(lat1))
            * math.cos(to_rad(lat2))
            * math.sin(d_lng / 2) ** 2
        )
        return 2 * R * math.asin(math.sqrt(a))

    best = None
    best_dist = float("inf")
    for d in docs:
        try:
            dist = _haversine_m(lat, lng, float(d["lat"]), float(d["lng"]))
        except (KeyError, TypeError, ValueError):
            continue
        if dist < best_dist:
            best_dist = dist
            best = d
    return best


@app.post("/emergency", response_model=EmergencyResponse)
async def emergency(req: EmergencyRequest) -> EmergencyResponse:
    from firebase_admin import firestore

    db = firestore.client()
    now = datetime.now(timezone.utc)

    if req.device_id:
        five_min_ago = now - timedelta(minutes=5)
        try:
            recent = (
                db.collection("incidents")
                .where("deviceId", "==", req.device_id)
                .where("anonymous", "==", True)
                .order_by("createdAt", "desc")
                .limit(1)
                .stream()
            )
            for snap in recent:
                data = snap.to_dict() or {}
                created = _to_dt(data.get("createdAt"))
                if created and created > five_min_ago:
                    return EmergencyResponse(
                        ok=False,
                        incident_id=snap.id,
                        short_id=(data.get("shortId") or "")[:6],
                        barangay=data.get("barangay"),
                        message="You just sent an emergency. Please wait before sending another.",
                    )
        except Exception as e:
            print(f"[emergency] rate-limit check skipped: {e}")

    b = _nearest_barangay(req.lat, req.lng)
    if not b:
        return EmergencyResponse(
            ok=False,
            message="Could not determine your barangay. Call local emergency services.",
        )

    barangay_name = b.get("name") or "Unknown"

    incident_ref = db.collection("incidents").document()
    incident_id = incident_ref.id
    short_id = incident_id[:6].upper()

    description = (
        (req.note or "").strip()
        or "Anonymous emergency — immediate assistance needed"
    )

    payload: Dict[str, Any] = {
        "shortId": short_id,
        "type": "emergency",
        "description": description,
        "barangay": barangay_name,
        "suggestedBarangay": barangay_name,
        "location": firestore.GeoPoint(req.lat, req.lng),
        "citizenName": "Anonymous",
        "citizenPhone": "",
        "citizenUid": None,
        "anonymous": True,
        "deviceId": req.device_id,

        "status": "unverified",
        "priority": "critical",

        "createdAt": now,
        "updatedAt": now,
        "photoUrls": [],
        "videoUrl": None,
        "audioUrl": req.audio_url,
        "audioDurationSeconds": req.audio_duration_seconds,
        "ml": {
            "predictedType": "emergency",
            "predictedSeverity": "critical",
            "confidence": 1.0,
            "keywords": [],
            "mentionedVehicles": [],
            "vehicles": {},
            "allVehicles": [],
            "sources": ["anonymous_quick_report"],
            "reportedType": "emergency",
            "reportedTypeMatches": True,
            "processedAt": now,
        },
    }

    incident_ref.set(payload)

    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception:
        pass

    log_event(
        level="error",
        category="emergency",
        action="emergency.received",
        message=f"Anonymous SOS received at {barangay_name}",
        actor={"uid": "anonymous", "role": "citizen"},
        target={"type": "incident", "id": incident_id},
        metadata={
            "barangay": barangay_name,
            "has_audio": bool(req.audio_url),
            "device_id": req.device_id,
        },
    )

    print(
        f"[emergency] {short_id} received at {barangay_name} "
        f"(audio={'yes' if req.audio_url else 'no'}) — awaiting admin dispatch"
    )

    return EmergencyResponse(
        ok=True,
        incident_id=incident_id,
        short_id=short_id,
        barangay=barangay_name,
        responder_assigned=False,
        sms_sent=False,
        has_audio=bool(req.audio_url),
        message="Emergency received. Admin is reviewing and will dispatch shortly.",
    )


# ---------------------------------------------------------------------------
# Acknowledgement
# ---------------------------------------------------------------------------

def _acknowledge_incident(
    incident_id: str,
    method: str,
    responder_uid: Optional[str] = None,
) -> Dict[str, Any]:
    from firebase_admin import firestore

    db = firestore.client()
    ref = db.collection("incidents").document(incident_id)
    snap = ref.get()

    if not snap.exists:
        return {"ok": False, "error": "Incident not found."}

    data = snap.to_dict() or {}

    if data.get("acknowledgedAt"):
        ack_dt = _to_dt(data["acknowledgedAt"])
        return {
            "ok": True,
            "already": True,
            "incident_id": incident_id,
            "acknowledged_at": ack_dt.isoformat() if ack_dt else None,
        }

    now = datetime.now(timezone.utc)

    dispatched_at = (
        _to_dt(data.get("dispatchedAt"))
        or _to_dt(data.get("smsSentAt"))
        or _to_dt(data.get("verifiedAt"))
    )
    response_seconds = None
    if dispatched_at:
        response_seconds = max(0, int((now - dispatched_at).total_seconds()))

    payload: Dict[str, Any] = {
        "acknowledgedAt": now,
        "acknowledgedVia": method,
        "responseTimeSeconds": response_seconds,
        "updatedAt": now,
    }
    if responder_uid:
        payload["acknowledgedBy"] = responder_uid

    if method == "app":
        payload["status"] = "accepted"
        payload["acceptedAt"] = now

    ref.update(payload)

    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception:
        pass

    log_event(
        level="success",
        category="incident",
        action="incident.acknowledged",
        message=f"Incident {incident_id[:6].upper()} acknowledged via {method}",
        actor={"uid": responder_uid or "system", "role": "responder"},
        target={"type": "incident", "id": incident_id},
        metadata={"method": method, "response_seconds": response_seconds},
    )

    print(
        f"[ack] {incident_id[:6].upper()} acknowledged via {method} "
        f"in {response_seconds}s"
    )

    return {
        "ok": True,
        "already": False,
        "incident_id": incident_id,
        "response_time_seconds": response_seconds,
        "method": method,
    }


@app.get("/ack/{short_id}", response_class=HTMLResponse)
async def acknowledge_via_link(short_id: str) -> HTMLResponse:
    from firebase_admin import firestore

    short = (short_id or "").strip().upper()

    if not short:
        return HTMLResponse(
            _ack_page("Invalid link", "This acknowledgement link is malformed.", ok=False),
            status_code=400,
        )

    db = firestore.client()
    matches = list(
        db.collection("incidents")
        .where("shortId", "==", short)
        .limit(1)
        .stream()
    )

    if not matches:
        return HTMLResponse(
            _ack_page(
                "Incident not found",
                f"No incident matches reference <strong>{short}</strong>.",
                ok=False,
            ),
            status_code=404,
        )

    incident_id = matches[0].id
    result = _acknowledge_incident(incident_id, method="sms_link")

    if not result.get("ok"):
        return HTMLResponse(
            _ack_page("Error", result.get("error", "Unknown error"), ok=False),
            status_code=500,
        )

    if result.get("already"):
        return HTMLResponse(_ack_page(
            "Already acknowledged",
            f"Incident <strong>{short}</strong> was already acknowledged.",
            ok=True,
        ))

    secs = result.get("response_time_seconds")
    detail = (
        f"Acknowledged in {secs}s. The dispatcher has been notified."
        if secs is not None
        else "Acknowledged. The dispatcher has been notified."
    )
    return HTMLResponse(_ack_page("Acknowledged", detail, ok=True))


def _ack_page(title: str, body: str, ok: bool) -> str:
    accent = "#2f9e73" if ok else "#e63946"
    icon = "✓" if ok else "!"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{title} — RoadRescue</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100dvh;
      display: grid; place-items: center;
      background: #0b1220; color: #eaf0fa;
      font: 16px/1.5 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
      padding: 24px;
    }}
    .card {{
      max-width: 420px; width: 100%;
      background: #121c2e; border: 1px solid #22304a;
      border-radius: 18px; padding: 32px 24px;
      text-align: center;
      box-shadow: 0 20px 60px rgba(0,0,0,.5);
    }}
    .badge {{
      width: 64px; height: 64px;
      border-radius: 50%;
      display: grid; place-items: center;
      margin: 0 auto 20px;
      font-size: 32px; font-weight: 800;
      color: #fff; background: {accent};
      box-shadow: 0 8px 24px {accent}55;
    }}
    h1 {{ font-size: 1.375rem; margin: 0 0 10px; letter-spacing: -0.01em; }}
    p  {{ margin: 0; color: #93a3bd; line-height: 1.55; }}
    strong {{ color: #eaf0fa; }}
    .brand {{
      margin-top: 24px; font-size: 0.75rem;
      letter-spacing: 0.08em; text-transform: uppercase;
      color: #64748b;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge" aria-hidden="true">{icon}</div>
    <h1>{title}</h1>
    <p>{body}</p>
    <p class="brand">RoadRescue</p>
  </div>
</body>
</html>"""


@app.post("/acknowledge")
async def acknowledge(req: AcknowledgeRequest) -> Dict[str, Any]:
    return _acknowledge_incident(
        incident_id=req.incident_id,
        method=req.method or "app",
        responder_uid=req.responder_uid,
    )


# ---------------------------------------------------------------------------
# SMS webhook
# ---------------------------------------------------------------------------

@app.post("/sms/webhook")
async def sms_webhook(request: Request) -> Dict[str, Any]:
    from sms_webhook import parse_incoming, match_and_acknowledge

    content_type = (request.headers.get("content-type") or "").lower()
    json_data = None
    form_data = {}

    if "application/json" in content_type:
        try:
            json_data = await request.json()
        except Exception:
            json_data = None
    elif "form" in content_type:
        try:
            form = await request.form()
            form_data = dict(form)
        except Exception:
            form_data = {}
    else:
        try:
            form = await request.form()
            form_data = dict(form)
        except Exception:
            pass
        try:
            json_data = await request.json()
        except Exception:
            pass

    parsed = parse_incoming(form_data, json_data)
    from_phone = parsed["from"]
    text = parsed["text"]

    print(f"[webhook] inbound from={from_phone!r} text={text[:100]!r}")

    if not from_phone or not text:
        return {"ok": False, "error": "Missing sender or message body."}

    try:
        result = match_and_acknowledge(from_phone, text)
    except Exception as e:
        print(f"[webhook] ❌ match_and_acknowledge crashed: {e}")
        return {"ok": False, "error": str(e)[:200]}

    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception:
        pass

    log_event(
        level="success" if result.get("matched") else "warn",
        category="sms",
        action="sms.webhook_received",
        message=result.get("message") or f"Inbound SMS from {from_phone}",
        actor={"uid": "sms", "role": "provider"},
        target=(
            {"type": "incident", "id": result.get("incident_id")}
            if result.get("incident_id")
            else None
        ),
        metadata={"from": from_phone, "text": text[:200]},
    )

    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# ADMIN — Toggle user status
# ---------------------------------------------------------------------------

@app.post("/admin/toggle-user-status")
async def admin_toggle_user_status(req: ToggleUserStatusRequest) -> Dict[str, Any]:
    from firebase_admin import auth as fb_auth, firestore

    db = firestore.client()

    try:
        fb_auth.update_user(req.uid, disabled=req.disabled)
    except Exception as e:
        return {"ok": False, "error": f"Auth update failed: {str(e)[:120]}"}

    try:
        db.collection("users").document(req.uid).update({
            "disabled": req.disabled,
            "status": "suspended" if req.disabled else "active",
            "updatedAt": datetime.now(timezone.utc),
        })
    except Exception as e:
        return {"ok": False, "error": f"Firestore update failed: {str(e)[:120]}"}

    log_event(
        level="warn" if req.disabled else "info",
        category="admin",
        action="user.toggled",
        message=f"User {req.uid} {'disabled' if req.disabled else 'enabled'}",
        actor={"uid": "admin", "role": "admin"},
        target={"type": "user", "id": req.uid},
        metadata={"disabled": req.disabled},
    )

    return {"ok": True, "uid": req.uid, "disabled": req.disabled}