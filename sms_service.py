"""
SMS service — sends incident alerts to responders via MOCEAN.

MOCEAN auth: Bearer token in the Authorization header.
Endpoint:    https://rest.moceanapi.com/rest/2/sms
Body format: application/x-www-form-urlencoded

Two modes:
  - Stub mode  → MOCEAN_API_TOKEN not set, logs the SMS locally
  - Live mode  → MOCEAN_API_TOKEN is set, sends real SMS
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
    "emergency":       "EMERGENCY",
    "other":           "Incident",
}


def _ascii_safe(text: str) -> str:
    if not text:
        return ""
    replacements = {
        "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-",
        "\u2026": "...",
        "\u00a0": " ",
        "\u2022": "*",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return "".join(c if 32 <= ord(c) < 127 else "" for c in text)


def build_incident_sms(
    incident: dict,
    barangay: str,
    incident_id: str,
) -> str:
    """
    Format a formal dispatch SMS with a deep link for acknowledgment.
    Emergency incidents get a distinct, high-visibility format.
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

    backend_url = (
        os.getenv("BACKEND_URL")
        or os.getenv("FRONTEND_URL")
        or "http://127.0.0.1:8000"
    ).rstrip("/")
    ack_url = f"{backend_url}/ack/{short_id}"

    # ---- Emergency format ----
    if incident_type == "emergency":
        lines = [
            "!! ROADRESCUE EMERGENCY !!",
            f"Ref {short_id}",
            "",
            "ANONYMOUS SOS - RESPOND IMMEDIATELY",
            f"Location  {barangay}",
            "Severity  CRITICAL",
        ]
        if incident.get("audioUrl"):
            lines.append("Voice     Message attached (see admin)")
        if desc:
            lines.append(f'Note      "{desc}"')
        lines.append("")
        lines.append(f"Ack: {ack_url}")
        return _ascii_safe("\n".join(lines))

    # ---- Normal dispatch format ----
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
    Send an SMS alert about an incident via MOCEAN (or stub).
    """
    token = os.getenv("MOCEAN_API_TOKEN", "").strip()

    # ---- Stub mode ----
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

    # ---- Real MOCEAN ----
    try:
        import requests

        sender = os.getenv("MOCEAN_SENDER", "MOCEAN")
        phone = to_phone.replace("+", "").replace(" ", "").replace("-", "")
        if phone.startswith("0"):
            phone = "63" + phone[1:]

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

        messages = data.get("messages") or []
        first = messages[0] if messages else {}
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
