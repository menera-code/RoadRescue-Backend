"""
RoadRescue API — FastAPI backend.

Responsibilities:
  - ML classification of incidents (Gemini + YOLO)
  - Analytics aggregations (cached, cheap on Firestore reads)
  - SMS notifications (MOCEAN)
  - Two-way SMS webhook (responder acknowledgment + QRT)

Run locally:
  uvicorn main:app --reload --port 8000
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Lifespan — starts / stops the Firestore worker
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan.

    On startup:
      1. Initialize Firebase Admin SDK
      2. Start the Firestore worker (classifies new incidents)

    On shutdown: unsubscribe the Firestore listener cleanly.
    """
    # ---------- Startup ----------
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

    # ---------- Shutdown ----------
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

# CORS — the Vue dev server runs on 5173.
# Add your production domain here later.
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
    image_urls: Optional[List[str]] = Field(
        default=None,
        description="Optional list of image URLs (Firebase Storage download URLs)",
    )
    reported_type: Optional[str] = Field(
        default=None,
        description="The type the citizen picked (used to compare against prediction)",
    )


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
    incident_id: str = Field(..., description="Incident document ID to dispatch")


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


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/")
def root() -> Dict[str, str]:
    return {
        "name": "RoadRescue API",
        "version": VERSION,
        "docs": "/docs",
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    """
    Quick status check.

    The `*_configured` flags tell you at a glance which integrations
    are wired up.
    """
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
    """
    Classify a road incident from text + optional images.

    Runs Gemini text classification + YOLOv8 image detection.
    Falls back gracefully if either fails.
    """
    from predictor import predict_incident

    result = predict_incident(
        text=req.text,
        image_urls=req.image_urls,
    )

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
    """
    Top-line counts: total, open, by-status, by-type, by-severity.
    Cached for 60 seconds.
    """
    from analytics_service import get_summary
    return get_summary(days=days)


@app.get("/analytics/timeline")
async def analytics_timeline(days: int = 30) -> Dict[str, Any]:
    """
    Daily incident counts for the last N days.
    """
    from analytics_service import get_timeline
    return get_timeline(days=days)


@app.get("/analytics/by-barangay")
async def analytics_by_barangay(days: int = 90) -> Dict[str, Any]:
    """
    Incident counts grouped by barangay.
    """
    from analytics_service import get_by_barangay
    return get_by_barangay(days=days)


@app.get("/analytics/qrt")
async def analytics_qrt(days: int = 90) -> Dict[str, Any]:
    """
    Quick Response Time stats: avg, median, p90, acknowledgment rate.
    """
    from analytics_service import get_qrt
    return get_qrt(days=days)


@app.get("/analytics/responders")
async def analytics_responders(days: int = 90) -> Dict[str, Any]:
    """
    Per-responder activity + QRT leaderboard.
    """
    from analytics_service import get_responders
    return get_responders(days=days)


@app.get("/analytics/hourly")
async def analytics_hourly(days: int = 30) -> Dict[str, Any]:
    """
    24-hour incident distribution (PH time).
    """
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
    """
    Filtered list of incidents for the admin History tab.

    Query params:
      from      - ISO date (YYYY-MM-DD), inclusive
      to        - ISO date (YYYY-MM-DD), inclusive
      status    - 'pending' | 'resolved' | ...
      type      - 'minor_collision' | 'road_hazard' | ...
      barangay  - substring match
      days      - pre-filter window (default 90)
      limit     - max incidents returned (default 500)
    """
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
    """Barangay × hour-of-day matrix for ML heatmap."""
    from analytics_service import get_barangay_temporal
    return get_barangay_temporal(days=days)


@app.get("/analytics/vehicles")
async def analytics_vehicles(days: int = 90) -> Dict[str, Any]:
    """Vehicle detection analytics (YOLO + text)."""
    from analytics_service import get_vehicle_analytics
    return get_vehicle_analytics(days=days)


@app.get("/analytics/ml-performance")
async def analytics_ml_performance(days: int = 90) -> Dict[str, Any]:
    """ML accuracy, confusion matrix, calibration."""
    from analytics_service import get_ml_performance
    return get_ml_performance(days=days)


@app.get("/analytics/patterns")
async def analytics_patterns(days: int = 180) -> Dict[str, Any]:
    """Statistical patterns: day-of-week, hour, monthly."""
    from analytics_service import get_patterns
    return get_patterns(days=days)


@app.get("/analytics/predictions")
async def analytics_predictions(days: int = 90) -> Dict[str, Any]:
    """Predictive forecasts: next-week, trends."""
    from analytics_service import get_predictions
    return get_predictions(days=days)
# ---------------------------------------------------------------------------
# Dispatch incident (admin -> responder + SMS)
# ---------------------------------------------------------------------------

@app.post("/dispatch-incident", response_model=DispatchResponse)
async def dispatch_incident(req: DispatchRequest) -> DispatchResponse:
    """
    Dispatch a verified incident to a responder.

    Called by the admin dashboard when they tap "Verify & Dispatch".

    Steps:
      1. Load the incident from Firestore
      2. Look up the assigned responder for its barangay
      3. Send an SMS alert via MOCEAN
      4. Update the incident -> status: 'pending' (visible to responders)
      5. Log the SMS result to notifications/
    """
    from firebase_admin import firestore
    from sms_service import build_incident_sms, send_incident_sms

    db = firestore.client()
    incident_ref = db.collection("incidents").document(req.incident_id)
    snap = incident_ref.get()

    if not snap.exists:
        return DispatchResponse(
            ok=False,
            incident_id=req.incident_id,
            status="not_found",
            message="Incident not found.",
        )

    incident = snap.to_dict() or {}
    barangay_name = incident.get("barangay")

    if not barangay_name:
        return DispatchResponse(
            ok=False,
            incident_id=req.incident_id,
            status="no_barangay",
            message="Incident has no barangay.",
        )

    # ------------------------------------------------------------------
    # Look up the assigned responder for this barangay
    # ------------------------------------------------------------------
    b_slug = slugify_py(barangay_name)
    b_snap = db.collection("barangays").document(b_slug).get()

    if not b_snap.exists:
        return DispatchResponse(
            ok=False,
            incident_id=req.incident_id,
            status="barangay_not_found",
            message=f"Barangay '{barangay_name}' not found in Firestore.",
        )

    b_data = b_snap.to_dict() or {}
    responder_uid = b_data.get("responderUid")
    responder_name = b_data.get("responderName")
    responder_phone = b_data.get("responderPhone")

    if not responder_uid or not responder_phone:
        return DispatchResponse(
            ok=False,
            incident_id=req.incident_id,
            status="no_responder",
            message=f"No responder assigned to '{barangay_name}'. Assign one first.",
        )

    # ------------------------------------------------------------------
    # Send SMS (stub or real depending on MOCEAN_API_TOKEN env)
    # ------------------------------------------------------------------
    sms_body = build_incident_sms(incident, barangay_name)
    sms_result = send_incident_sms(
        to_phone=responder_phone,
        message=sms_body,
        incident_id=req.incident_id,
    )

    # ------------------------------------------------------------------
    # Update the incident doc
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    incident_ref.update({
        "status": "pending",
        "assignedResponderUid": responder_uid,
        "assignedResponderName": responder_name,
        "assignedResponderPhone": responder_phone,
        "verifiedAt": now,
        "smsSent": sms_result["ok"],
        "smsSentAt": now if sms_result["ok"] else None,
        "smsProvider": sms_result["provider"],
        "smsMessageId": sms_result["messageId"],
        "smsError": sms_result.get("error"),
        "updatedAt": now,
    })

    # ------------------------------------------------------------------
    # Log to notifications/ for audit
    # ------------------------------------------------------------------
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

    # Invalidate analytics cache so the dashboard reflects the dispatch
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
# SMS webhook — receives inbound replies from MOCEAN
# ---------------------------------------------------------------------------

@app.post("/sms/webhook")
async def sms_webhook(request: Request) -> Dict[str, Any]:
    """
    MOCEAN posts inbound SMS replies here.

    Called when a responder replies "YES" to an incident SMS.
    Records acknowledgedAt + responseTimeSeconds on the matching incident.

    Configure in MOCEAN:
      API Account -> Global Settings -> MO URL
      = https://<your-backend>.onrender.com/sms/webhook
    """
    from sms_webhook import parse_incoming, match_and_acknowledge

    # ---------------------------------------------------------------------
    # Parse whatever MOCEAN sends
    # ---------------------------------------------------------------------
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
        # Fallback: try both
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

    # Log for debugging
    print(f"[webhook] inbound from={from_phone!r} text={text[:100]!r}")

    if not from_phone or not text:
        return {
            "ok": False,
            "error": "Missing sender or message body.",
        }

    # ---------------------------------------------------------------------
    # Match to incident + acknowledge
    # ---------------------------------------------------------------------
    try:
        result = match_and_acknowledge(from_phone, text)
    except Exception as e:
        print(f"[webhook] ❌ match_and_acknowledge crashed: {e}")
        return {
            "ok": False,
            "error": str(e)[:200],
        }

    # Invalidate analytics cache so QRT metrics refresh
    try:
        from analytics_service import clear_cache
        clear_cache()
    except Exception:
        pass

    return {"ok": True, **result}

# ===========================================================================
# ADMIN — User management
# ===========================================================================

import secrets
import string as _string


def _random_password(length: int = 10) -> str:
    """Readable 10-char password — no ambiguous chars."""
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
    """
    Admin-only. Creates a Firebase Auth user + Firestore profile for a
    new responder using a temporary email + password, sends an SMS with
    the credentials, and forces a credential reset on first login.
    """
    from firebase_admin import auth as fb_auth, firestore
    from sms_service import send_incident_sms

    db = firestore.client()

    # 1) Generate credentials
    temp_email = f"responder-{_random_email_slug()}@invite.roadrescue.app"
    temp_password = _random_password()

    # 2) Create Auth user
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

    # 3) Write Firestore doc
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

    # Mirror on barangay doc
    try:
        import re
        slug = re.sub(r"[()]", "", req.barangay.lower())
        slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
        db.collection("barangays").document(slug).update({
            "responderUid": user.uid,
            "responderName": req.fullName,
            "responderPhone": req.phone,
            "assignedAt": now,
            "updatedAt": now,
        })
    except Exception as e:
        print(f"[invite] barangay mirror failed: {e}")

    # 4) Send SMS with credentials
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
    """Enable or disable a user account."""
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify_py(name: str) -> str:
    """Match the frontend's slugify() exactly."""
    import re
    s = name.lower()
    s = re.sub(r"[()]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")