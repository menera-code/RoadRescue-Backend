"""
Two-way SMS webhook — handles inbound replies from responders.

When a responder replies "YES" (or "YES <id>") to an incident SMS,
MOCEAN POSTs the message to /sms/webhook. This module:

  1. Parses the incoming message
  2. Matches it to the most recent dispatched incident for that phone
     (any responder in the barangay's list can reply)
  3. Records acknowledgedAt + responseTimeSeconds on the incident
  4. Returns a result dict for logging

Called by main.py via @app.post("/sms/webhook").
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from firebase_admin import firestore


def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = re.sub(r"[^\d]", "", phone)
    if digits.startswith("63"):
        return digits
    if digits.startswith("0"):
        return "63" + digits[1:]
    if digits.startswith("9") and len(digits) == 10:
        return "63" + digits
    return digits


def parse_incoming(form_data: dict, json_data: Optional[dict]) -> Dict[str, str]:
    from_ = ""
    text = ""

    if form_data:
        from_ = (
            form_data.get("mocean-from")
            or form_data.get("from")
            or form_data.get("sender")
            or ""
        )
        text = (
            form_data.get("mocean-text")
            or form_data.get("text")
            or form_data.get("message")
            or ""
        )

    if not from_ and json_data:
        from_ = (
            json_data.get("mocean-from")
            or json_data.get("from")
            or json_data.get("sender")
            or ""
        )
    if not text and json_data:
        text = (
            json_data.get("mocean-text")
            or json_data.get("text")
            or json_data.get("message")
            or ""
        )

    return {"from": str(from_).strip(), "text": str(text).strip()}


def match_and_acknowledge(
    from_phone: str,
    text: str,
) -> Dict[str, Any]:
    """
    Match an inbound SMS to a recent dispatched incident and record
    the acknowledgment + quick response time (QRT).

    Matches against BOTH the array field (`assignedResponderPhones`) and
    the legacy single field (`assignedResponderPhone`) so old incidents
    still work.
    """
    db = firestore.client()
    normalized = normalize_phone(from_phone)

    if not normalized:
        return {
            "matched": False,
            "incident_id": None,
            "response_time_seconds": None,
            "message": "Missing sender phone number.",
        }

    reply = text.strip().upper()
    short_id = None
    parts = reply.split()
    if parts and parts[0] == "YES":
        if len(parts) >= 2:
            short_id = parts[1][:6]
    else:
        return {
            "matched": False,
            "incident_id": None,
            "response_time_seconds": None,
            "message": f"Unrecognized reply: {reply[:50]}",
        }

    active_statuses = ["pending", "accepted", "en_route", "on_scene"]
    candidates: list = []
    seen_ids: set = set()

    # Try both the E.164-normalized form and the raw form.
    phone_forms = {normalized, from_phone}

    for phone_form in phone_forms:
        # --- New multi-responder model: array-contains ---
        try:
            q = (
                db.collection("incidents")
                .where("assignedResponderPhones", "array-contains", phone_form)
                .where("status", "in", active_statuses)
                .stream()
            )
            for snap in q:
                data = snap.to_dict() or {}
                if data.get("acknowledgedAt"):
                    continue
                if snap.id in seen_ids:
                    continue
                seen_ids.add(snap.id)
                candidates.append({"id": snap.id, **data})
        except Exception as e:
            print(f"[webhook] array-contains query failed for {phone_form}: {e}")

        # --- Legacy single-responder model: == ---
        try:
            q = (
                db.collection("incidents")
                .where("assignedResponderPhone", "==", phone_form)
                .where("status", "in", active_statuses)
                .stream()
            )
            for snap in q:
                data = snap.to_dict() or {}
                if data.get("acknowledgedAt"):
                    continue
                if snap.id in seen_ids:
                    continue
                seen_ids.add(snap.id)
                candidates.append({"id": snap.id, **data})
        except Exception as e:
            print(f"[webhook] legacy query failed for {phone_form}: {e}")

    if not candidates:
        return {
            "matched": False,
            "incident_id": None,
            "response_time_seconds": None,
            "message": "No open incident found for this phone.",
        }

    if short_id:
        candidates = [
            c for c in candidates
            if c["id"][:6].upper() == short_id
        ]
        if not candidates:
            return {
                "matched": False,
                "incident_id": None,
                "response_time_seconds": None,
                "message": f"No incident matching ID {short_id}.",
            }

    def sort_key(c):
        for f in ("smsSentAt", "verifiedAt", "updatedAt", "createdAt"):
            v = c.get(f)
            if v:
                return v
        return datetime.min.replace(tzinfo=timezone.utc)

    candidates.sort(key=sort_key, reverse=True)
    incident = candidates[0]

    dispatched_at = (
        incident.get("smsSentAt")
        or incident.get("verifiedAt")
        or incident.get("updatedAt")
    )
    now = datetime.now(timezone.utc)

    response_seconds = None
    if dispatched_at:
        try:
            delta = now - dispatched_at
            response_seconds = max(0, int(delta.total_seconds()))
        except Exception as e:
            print(f"[webhook] could not compute QRT: {e}")

    ref = db.collection("incidents").document(incident["id"])
    ref.update({
        "acknowledgedAt": now,
        "acknowledgedVia": "sms",
        "acknowledgedReply": text[:200],
        "acknowledgedByPhone": normalized,
        "responseTimeSeconds": response_seconds,
        "updatedAt": now,
    })

    print(
        f"[webhook] OK {incident['id'][:6].upper()} acknowledged "
        f"by {normalized} in {response_seconds}s"
    )

    return {
        "matched": True,
        "incident_id": incident["id"],
        "response_time_seconds": response_seconds,
        "message": f"Acknowledged in {response_seconds}s.",
    }