"""
Central logging service.

Writes structured entries to the Firestore `system_logs` collection.
Never raises — logging failures must never break a real request.

Usage:
    from log_service import log_event, log_from_request

    log_event(
        level="success",
        category="dispatch",
        action="dispatch.sent",
        message="Incident ABC123 dispatched to responder Juan",
        actor={"uid": "...", "email": "...", "role": "admin"},
        target={"type": "incident", "id": "abc123"},
        metadata={"responder_phone": "+63...", "sms_ok": True},
    )
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from firebase_admin import firestore


def _now():
    return datetime.now(timezone.utc)


def log_event(
    *,
    level: str = "info",
    category: str = "system",
    action: str = "event",
    message: str = "",
    actor: Optional[Dict[str, Any]] = None,
    target: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    user_agent: Optional[str] = None,
) -> None:
    """
    Fire-and-forget write to `system_logs`. Never raises.
    """
    try:
        db = firestore.client()
        db.collection("system_logs").add({
            "timestamp": _now(),
            "level": level,
            "category": category,
            "action": action,
            "message": message[:500] if message else "",
            "actor": actor or None,
            "target": target or None,
            "metadata": metadata or {},
            "userAgent": (user_agent or "")[:250] or None,
        })
    except Exception as e:
        print(f"[log_service] ❌ failed to write log: {e}")


def log_from_request(
    request,
    *,
    level: str = "info",
    category: str = "system",
    action: str = "event",
    message: str = "",
    actor: Optional[Dict[str, Any]] = None,
    target: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Same as log_event but auto-captures the User-Agent header."""
    ua = None
    try:
        ua = request.headers.get("user-agent")
    except Exception:
        pass

    log_event(
        level=level,
        category=category,
        action=action,
        message=message,
        actor=actor,
        target=target,
        metadata=metadata,
        user_agent=ua,
    )