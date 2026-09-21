"""
RoadRescue API — FastAPI backend.

Responsibilities:
  - ML classification of incidents (Gemini + YOLO)
  - Analytics aggregations (cached, cheap on Firestore reads)
  - SMS notifications (MOCEAN)
  - Acknowledgement tracking (SMS link + responder app)

Run locally:
  uvicorn main:app --reload --port 8000
"""

import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

load_dotenv()

VERSION = "0.2.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify_py(name: str) -> str:
    """Match the frontend's slugify() exactly."""
    s = name.lower()
    s = re.sub(r"[()]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _to_dt(ts: Any) -> Optional[datetime]:
    """Coerce a Firestore Timestamp / datetime / None into an aware datetime."""
    if not ts:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    return None


# ---------------------------------------------------------------------------
# Lifespan — starts / stops the Firestore worker
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


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ClassifyRequest(BaseModel):
    text: str = Field(..., description="Free-text description of the incident")
    image_urls: Optional[List[str]] = Field(default=None)
    reported_type: Optional[str] = Field(default=None)


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
# Classify (ML)
# ---------------------------------------------------------------------------

@app.post("/classify", response_model=ClassifyResponse)
async def classify(req: ClassifyRequest) -> ClassifyResponse:
    from predictor import predict_incident

    result = predict_incident(text=req.text, image_urls=req.image_urls)

    reported_matches = None
    if req.reported_type:
        reported_matches = req.reported_type == result["predicted_type"]

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
# Analytics — cached aggregations
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
# Dispatch incident (admin -> responder + SMS)
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
            message=f"Barangay '{barangay_name}' not found in Firestore.",
        )

    b_data = b_snap.to_dict() or {}
    responder_uid = b_data.get("responderUid")
    responder_name = b_data.get("responderName")
    responder_phone = b_data.get("responderPhone")

    if not responder_uid or not responder_phone:
        return DispatchResponse(
            ok=False, incident_id=req.incident_id, status="no_responder",
            message=f"No responder assigned to '{barangay_name}'. Assign one first.",
        )

    sms_body = build_incident_sms(incident, barangay_name, req.incident_id)
    sms_result = send_incident_sms(
        to_phone=responder_phone,
        message=sms_body,
        incident_id=req.incident_id,
    )

    now = datetime.now(timezone.utc)
    short_id = req.incident_id[:6].upper()

    incident_ref.update({
        "status": "pending",
        "shortId": short_id,
        "assignedResponderUid": responder_uid,
        "assignedResponderName": responder_name,
        "assignedResponderPhone": responder_phone,
        "verifiedAt": now,
        "dispatchedAt": now,
        "smsSent": sms_result["ok"],
        "smsSentAt": now if sms_result["ok"] else None,
        "smsProvider": sms_result["provider"],
        "smsMessageId": sms_result["messageId"],
        "smsError": sms_result.get("error"),
        "updatedAt": now,
    })

    try:
        db.collection("notifications").add({
            "incidentId": req.incident_id,
            "responderUid": responder_uid,
            "responderPhone": responder_phone,
            "channel": "sms",
            "provider": sms_result["provider"],
            "message": sms_body,
            "ok": sms_result["ok"],
            "error": sms_result.get("error"),
            "createdAt": now,
        })
    except Exception as e:
        print(f"[dispatch] notification log failed: {e}")

    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception:
        pass

    return DispatchResponse(
        ok=True,
        incident_id=req.incident_id,
        status="dispatched",
        assigned_responder_uid=responder_uid,
        assigned_responder_name=responder_name,
        assigned_responder_phone=responder_phone,
        sms_ok=sms_result["ok"],
        sms_provider=sms_result["provider"],
        sms_message_id=sms_result["messageId"],
        sms_error=sms_result.get("error"),
        message=(
            "Dispatched and SMS sent."
            if sms_result["ok"]
            else "Dispatched, but SMS failed."
        ),
    )


# ---------------------------------------------------------------------------
# Acknowledgement — shared core used by both ack paths
# ---------------------------------------------------------------------------

def _acknowledge_incident(
    incident_id: str,
    method: str,
    responder_uid: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Idempotently mark an incident acknowledged.

    Writes:
      - acknowledgedAt        (server timestamp)
      - acknowledgedVia       ('sms_link' | 'app')
      - acknowledgedBy        (responder uid, if supplied)
      - responseTimeSeconds   (QRT — dispatchedAt → now)
      - updatedAt

    Also invalidates the analytics cache so the admin dashboard updates
    immediately.
    """
    from firebase_admin import firestore

    db = firestore.client()
    ref = db.collection("incidents").document(incident_id)
    snap = ref.get()

    if not snap.exists:
        return {"ok": False, "error": "Incident not found."}

    data = snap.to_dict() or {}

    if data.get("acknowledgedAt"):
        return {
            "ok": True,
            "already": True,
            "incident_id": incident_id,
            "acknowledged_at": (
                _to_dt(data["acknowledgedAt"]).isoformat()
                if _to_dt(data["acknowledgedAt"]) else None
            ),
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


# ---------------------------------------------------------------------------
# SMS link — GET /ack/{short_id}
# ---------------------------------------------------------------------------

@app.get("/ack/{short_id}", response_class=HTMLResponse)
async def acknowledge_via_link(short_id: str) -> HTMLResponse:
    """
    Endpoint hit when a responder taps the acknowledgement link in the
    dispatch SMS. No login required — the short ID is the capability.
    """
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
                f"No incident matches reference <strong>{short}</strong>. "
                "It may have been removed.",
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
            f"Incident <strong>{short}</strong> was already acknowledged. "
            "No action needed.",
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
    """Minimal, mobile-first confirmation page for the SMS ack link."""
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
      margin: 0;
      min-height: 100dvh;
      display: grid;
      place-items: center;
      background: #0b1220;
      color: #eaf0fa;
      font: 16px/1.5 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
      padding: 24px;
    }}
    .card {{
      max-width: 420px;
      width: 100%;
      background: #121c2e;
      border: 1px solid #22304a;
      border-radius: 18px;
      padding: 32px 24px;
      text-align: center;
      box-shadow: 0 20px 60px rgba(0,0,0,.5);
    }}
    .badge {{
      width: 64px; height: 64px;
      border-radius: 50%;
      display: grid; place-items: center;
      margin: 0 auto 20px;
      font-size: 32px;
      font-weight: 800;
      color: #fff;
      background: {accent};
      box-shadow: 0 8px 24px {accent}55;
    }}
    h1 {{ font-size: 1.375rem; margin: 0 0 10px; letter-spacing: -0.01em; }}
    p  {{ margin: 0; color: #93a3bd; line-height: 1.55; }}
    strong {{ color: #eaf0fa; }}
    .brand {{
      margin-top: 24px;
      font-size: 0.75rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
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


# ---------------------------------------------------------------------------
# Acknowledgement — POST /acknowledge (used by responder app)
# ---------------------------------------------------------------------------

@app.post("/acknowledge")
async def acknowledge(req: AcknowledgeRequest) -> Dict[str, Any]:
    """
    Called by the responder PWA when a responder taps "Accept".

    Idempotent. Writes acknowledgedAt, responseTimeSeconds, and flips
    status to 'accepted' when method == 'app'.
    """
    return _acknowledge_incident(
        incident_id=req.incident_id,
        method=req.method or "app",
        responder_uid=req.responder_uid,
    )


# ---------------------------------------------------------------------------
# SMS webhook — legacy "YES" reply path (still supported)
# ---------------------------------------------------------------------------

@app.post("/sms/webhook")
async def sms_webhook(request: Request) -> Dict[str, Any]:
    from sms_webhook import parse_incoming

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

    from sms_webhook import match_and_acknowledge
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

    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# ADMIN — User management
# ---------------------------------------------------------------------------

import secrets
import string as _string


def _random_password(length: int = 10) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _random_email_slug(length: int = 8) -> str:
    alphabet = _string.ascii_lowercase + _string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class InviteResponderRequest(BaseModel):
    fullName: str = Field(..., min_length=2)
    phone: str = Field(..., min_length=10)
    barangay: str = Field(..., min_length=2)
    agency: Optional[str] = None


class InviteResponderResponse(BaseModel):
    ok: bool
    uid: Optional[str] = None
    tempEmail: Optional[str] = None
    tempPassword: Optional[str] = None
    sms_ok: bool = False
    sms_error: Optional[str] = None
    message: str = ""


@app.post("/admin/invite-responder", response_model=InviteResponderResponse)
async def admin_invite_responder(req: InviteResponderRequest) -> InviteResponderResponse:
    from firebase_admin import auth as fb_auth, firestore
    from sms_service import send_incident_sms

    db = firestore.client()

    temp_email = f"responder-{_random_email_slug()}@invite.roadrescue.app"
    temp_password = _random_password()

    try:
        user = fb_auth.create_user(
            email=temp_email,
            password=temp_password,
            display_name=req.fullName,
        )
    except Exception as e:
        return InviteResponderResponse(
            ok=False,
            message=f"Failed to create user: {str(e)[:120]}",
        )

    now = datetime.now(timezone.utc)
    db.collection("users").document(user.uid).set({
        "uid": user.uid,
        "fullName": req.fullName,
        "email": temp_email,
        "phone": req.phone,
        "role": "responder",
        "status": "active",
        "agency": req.agency or "",
        "badgeId": "",
        "barangay": req.barangay,
        "assignedBarangay": req.barangay,
        "disabled": False,
        "lastSeen": None,
        "mustChangeCredentials": True,
        "invitedBy": "admin",
        "invitedAt": now,
        "termsAccepted": True,
        "termsVersion": "1.0.0",
        "termsAcceptedAt": now,
        "createdAt": now,
        "updatedAt": now,
    })

    try:
        slug = slugify_py(req.barangay)
        db.collection("barangays").document(slug).update({
            "responderUid": user.uid,
            "responderName": req.fullName,
            "responderPhone": req.phone,
            "assignedAt": now,
            "updatedAt": now,
        })
    except Exception as e:
        print(f"[invite] barangay mirror failed: {e}")

    sms_body = (
        "ROADRESCUE RESPONDER INVITATION\n"
        "\n"
        "You have been invited as a responder.\n"
        "\n"
        "Login credentials:\n"
        f"Email:    {temp_email}\n"
        f"Password: {temp_password}\n"
        "\n"
        "Open the app and sign in. You will be\n"
        "asked to set your real email and password\n"
        "on first login.\n"
        "\n"
        "--\n"
        "RoadRescue Response System"
    )
    sms_result = send_incident_sms(
        to_phone=req.phone,
        message=sms_body,
        incident_id=f"invite-{user.uid[:6]}",
    )

    return InviteResponderResponse(
        ok=True,
        uid=user.uid,
        tempEmail=temp_email,
        tempPassword=temp_password,
        sms_ok=sms_result["ok"],
        sms_error=sms_result.get("error"),
        message=(
            "Responder invited and SMS sent."
            if sms_result["ok"]
            else "Responder created, but SMS failed."
        ),
    )


class ToggleUserStatusRequest(BaseModel):
    uid: str
    disabled: bool


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
            "updatedAt": datetime.now(timezone.utc),
        })
    except Exception as e:
        return {"ok": False, "error": f"Firestore update failed: {str(e)[:120]}"}

    return {"ok": True, "uid": req.uid, "disabled": req.disabled}
