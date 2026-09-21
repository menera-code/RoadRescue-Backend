"""
SMS service — sends incident alerts to responders via MOCEAN.

MOCEAN auth: Bearer token in the Authorization header.
Endpoint:    https://rest.moceanapi.com/rest/2/sms
Body format: application/x-www-form-urlencoded

Two modes:
  - Stub mode  → MOCEAN_API_TOKEN not set, logs the SMS locally
  - Live mode  → MOCEAN_API_TOKEN is set, sends real SMS

Encoding notes:
  PH carriers often mangle non-ASCII characters (emojis, accented letters)
  into "?". The message body must be PLAIN ASCII to survive GSM-7 encoding.
  This module guarantees that all output is 7-bit safe.
"""

import os
from typing import Any, Dict


# Human-readable labels for incident types
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


def _ascii_safe(text: str) -> str:
    """
    Force a string into GSM-7 safe ASCII.

    Replaces common unicode punctuation with ASCII equivalents
    (em dash → hyphen, smart quotes → straight quotes, ellipsis → dots)
    and drops anything else that can't be encoded.
    """
    if not text:
        return ""
    replacements = {
        "\u2018": "'", "\u2019": "'",   # smart single quotes
        "\u201c": '"', "\u201d": '"',   # smart double quotes
        "\u2013": "-", "\u2014": "-",   # en/em dash
        "\u2026": "...",                # ellipsis
        "\u00a0": " ",                  # non-breaking space
        "\u2022": "*",                  # bullet
    }
    for k, v in replacements.items():
        text = text.replace(k, v)

    # Drop everything outside printable ASCII
    return "".join(c if 32 <= ord(c) < 127 else "" for c in text)


def build_incident_sms(
    incident: dict,
    barangay: str,
    incident_id: str,
) -> str:
    """
    Format a formal dispatch SMS with a deep link for acknowledgment.

    The ack URL hits the BACKEND directly — no login required. When the
    responder taps it, the backend marks the incident acknowledged and
    returns a small confirmation page. This is what feeds the analytics
    acknowledgement rate.

    Args:
        incident:    The incident document data (from Firestore).
        barangay:    Barangay name for display.
        incident_id: The Firestore document ID. MUST be passed in —
                     snap.to_dict() does not include the ID.
    """
    short_id = (incident_id or "")[:6].upper()
    incident_type = incident.get("type", "other")
    label = TYPE_LABELS.get(incident_type, "Incident")

    ml = incident.get("ml") or {}
    severity = (ml.get("predictedSeverity") or "medium").upper()

    desc = (incident.get("description") or "").strip()
    if len(desc) > 60:
        desc = desc[:57] + "..."

    vehicles_list = []
    if ml.get("mentionedVehicles"):
        vehicles_list = ml["mentionedVehicles"]
    elif ml.get("vehicles"):
        vehicles_list = list(ml["vehicles"].keys())

    citizen_name = (incident.get("citizenName") or "").strip()
    citizen_phone = (incident.get("citizenPhone") or "").strip()

    # Backend URL — the SMS link hits the backend directly, so no login
    # is required to acknowledge. Fall back to FRONTEND_URL if not set.
    backend_url = (
        os.getenv("BACKEND_URL")
        or os.getenv("FRONTEND_URL")
        or "http://127.0.0.1:8000"
    ).rstrip("/")
    ack_url = f"{backend_url}/ack/{short_id}"

    # ---- Compose the SMS ----
    lines = [
        f"ROADRESCUE DISPATCH  Ref {short_id}",
        "",
        f"Type      {label}",
        f"Severity  {severity}",
        f"Location  {barangay}",
    ]

    if vehicles_list:
        lines.append(f"Vehicles  {', '.join(vehicles_list[:3])}")

    if desc:
        lines.append(f'Report    "{desc}"')

    if citizen_name or citizen_phone:
        contact = citizen_name or "Citizen"
        if citizen_phone:
            contact = f"{contact}, {citizen_phone}"
        lines.append(f"Contact   {contact}")

    lines.append("")
    lines.append(f"Ack: {ack_url}")

    return _ascii_safe("\n".join(lines))


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
        print(f"[sms][stub]")
        for line in message.split("\n"):
            print(f"[sms][stub]    {line}")
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

        # Final encoding safety check
        safe_message = _ascii_safe(message)

        resp = requests.post(
            "https://rest.moceanapi.com/rest/2/sms",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "mocean-from": sender,
                "mocean-to": phone,
                "mocean-text": safe_message,
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
