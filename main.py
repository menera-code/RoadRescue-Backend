"""
RoadRescue API — FastAPI backend.

Responsibilities:
  - ML classification of incidents (Gemini + YOLO)
  - Analytics aggregations (cached, cheap on Firestore reads)
  - SMS notifications (MOCEAN)

Run locally:
  uvicorn main:app --reload --port 8000
"""

import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from datetime import datetime, timezone

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
# Analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/summary")
async def analytics_summary() -> Dict[str, Any]:
    """
    Top-line analytics for the admin dashboard.

    Phase 3 (now)  → stub response.
    Phase 5        → reads from Firestore with in-memory caching.
    Phase 10       → daily rollups + ML insights.
    """
    return {
        "total_incidents": 0,
        "by_status": {},
        "by_type": {},
        "by_barangay": {},
        "note": "Stub — will be wired to Firestore in Phase 5",
    }

# ---------------------------------------------------------------------------
# Dispatch incident (admin → responder + SMS)
# ---------------------------------------------------------------------------

@app.post("/dispatch-incident", response_model=DispatchResponse)
async def dispatch_incident(req: DispatchRequest) -> DispatchResponse:
    """
    Dispatch a verified incident to a responder.

    Called by the admin dashboard when they tap "Verify & Dispatch".

    Steps:
      1. Load the incident from Firestore
      2. Look up the assigned responder for its barangay
      3. Send an SMS alert (stub in A6, real in A7)
      4. Update the incident → status: 'pending' (visible to responders)
      5. Log the SMS result to notifications/

    Auth note: for now this endpoint is unauthenticated on the backend.
    The frontend only calls it after Firebase Auth login, and Firestore
    rules still protect the writes. In production, add Firebase ID token
    verification (30 lines) — deferred to a later phase.
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
    from_data = slugify_py(barangay_name)
    b_snap = db.collection("barangays").document(from_data).get()

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
    # Send SMS (stub or real depending on SMS_PROVIDER env)
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
# Helpers
# ---------------------------------------------------------------------------

def slugify_py(name: str) -> str:
    """Match the frontend's slugify() exactly."""
    import re
    s = name.lower()
    s = re.sub(r"[()]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")