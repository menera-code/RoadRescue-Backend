"""
SMS service — sends incident alerts to responders via MOCEAN.

MOCEAN auth: Bearer token in the Authorization header.
Endpoint:    https://rest.moceanapi.com/rest/2/sms
Body format: application/x-www-form-urlencoded

Phase A6 (stub mode)  → MOCEAN_API_TOKEN not set, logs the SMS locally
Phase A7 (real mode)  → MOCEAN_API_TOKEN is set, sends real SMS
"""

import os
from typing import Any, Dict


TYPE_LABELS = {
    "flat_tire":       "Flat Tire",
    "battery":         "Dead Battery",
    "fuel":            "Out of Fuel",
    "stalled_vehicle": "Stalled Vehicle",
    "minor_collision": "Minor Crash",
    "major_collision": "Major Crash",
    "vehicle_fire":    "Vehicle Fire",
    "road_hazard":     "Road Hazard",
    "other":           "Incident",
}


def build_incident_sms(incident: dict, barangay: str) -> str:
    """
    Format a structured SMS alert for the responder.

    Layout uses emoji prefixes for fast visual scanning:
      🚨 header
      🛞 / 💥 / ⚠️  incident type + severity
      📍 location
      🚗 detected vehicles
      📝 citizen description (truncated)
      ID short ID for acknowledgment matching
      Reply hint

    Target length: under 300 chars to stay in 2 SMS segments.
    """
    TYPE_ICONS = {
        "flat_tire":       "🛞",
        "battery":         "🔋",
        "fuel":            "⛽",
        "stalled_vehicle": "🛑",
        "minor_collision": "🚗",
        "major_collision": "💥",
        "vehicle_fire":    "🔥",
        "road_hazard":     "⚠️",
        "other":           "❓",
    }

    SEVERITY_ICONS = {
        "low":      "·",
        "medium":   "••",
        "high":     "•••",
        "critical": "••••",
    }

    incident_type = incident.get("type", "other")
    label = TYPE_LABELS.get(incident_type, "Incident")
    icon = TYPE_ICONS.get(incident_type, "❓")

    # Severity from ML if available, else from citizen's type
    ml = incident.get("ml") or {}
    severity = ml.get("predictedSeverity") or "medium"
    sev_marker = SEVERITY_ICONS.get(severity, "••")

    # Short ID — first 6 chars of incident ID for SMS reply matching
    short_id = (incident.get("id") or "")[:6].upper()

    # Vehicles detected (from text or image)
    vehicles_list = []
    if ml.get("mentionedVehicles"):
        vehicles_list = ml["mentionedVehicles"]
    elif ml.get("vehicles"):
        vehicles_list = list(ml["vehicles"].keys())

    # Truncate description to keep SMS compact
    desc = (incident.get("description") or "").strip()
    if len(desc) > 60:
        desc = desc[:57] + "…"

    lines = [
        "🚨 ROADRESCUE ALERT",
        "",
        f"{icon} {label}  {sev_marker}",
        f"📍 {barangay}",
    ]

    if vehicles_list:
        # Cap at 3 vehicle types
        v = ", ".join(vehicles_list[:3])
        lines.append(f"🚗 {v}")

    if desc:
        lines.append(f"📝 \"{desc}\"")

    lines.append("")
    lines.append(f"ID: {short_id}")
    lines.append("")
    lines.append("Reply YES to acknowledge.")

    return "\n".join(lines)

def send_incident_sms(
    to_phone: str,
    message: str,
    incident_id: str,
) -> Dict[str, Any]:
    """
    Send an SMS alert about an incident.

    Uses Bearer token auth (MOCEAN's current API).

    Returns:
        {
            "ok": bool,
            "provider": "stub" | "mocean",
            "messageId": str | None,
            "to": str,
            "error": str | None,
        }
    """
    token = os.getenv("MOCEAN_API_TOKEN", "").strip()

    # ---- Stub mode when no token ----
    if not token:
        print(f"[sms][stub] -> {to_phone}")
        print(f"[sms][stub]    {message}")
        return {
            "ok": True,
            "provider": "stub",
            "messageId": f"stub-{incident_id[:8]}",
            "to": to_phone,
            "error": None,
        }

    # ---- Real MOCEAN mode ----
    try:
        import requests

        sender = os.getenv("MOCEAN_SENDER", "MOCEAN")

        # Normalize phone: digits only, no +, no spaces, no dashes
        phone = to_phone.replace("+", "").replace(" ", "").replace("-", "")
        # If it starts with 0 (local format), prepend 63
        if phone.startswith("0"):
            phone = "63" + phone[1:]

        resp = requests.post(
            "https://rest.moceanapi.com/rest/2/sms",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "mocean-from": sender,
                "mocean-to": phone,
                "mocean-text": message,
                "mocean-resp-format": "json",
            },
            timeout=15,
        )

        print(f"[sms] MOCEAN status {resp.status_code}")
        print(f"[sms] MOCEAN body: {resp.text[:300]}")

        resp.raise_for_status()
        data = resp.json()

        # MOCEAN response shapes vary — handle several
        messages = data.get("messages") or []
        first = messages[0] if messages else {}

        # status 0 = queued, 1 = sent, others = error
        status_code = first.get("status", 0)
        message_id = first.get("msgid") or first.get("id")

        ok = status_code in (0, 1)

        return {
            "ok": ok,
            "provider": "mocean",
            "messageId": message_id,
            "to": to_phone,
            "error": None if ok else f"MOCEAN status={status_code}: {first.get('err_msg', '')}",
        }
    except Exception as e:
        print(f"[sms] MOCEAN send failed: {e}")
        return {
            "ok": False,
            "provider": "mocean",
            "messageId": None,
            "to": to_phone,
            "error": str(e)[:200],
        }