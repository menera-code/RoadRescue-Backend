"""
Admin user management router.

Handles:
  - List all users (with pending update-request flags)
  - Create user with role-dependent fields (citizen / responder / admin)
  - Edit user (name, phone, role, status, responder fields, permissions)
  - Delete user (Auth + Firestore)
  - Approve / reject user-initiated update requests

Auth: Firebase ID token in Authorization: Bearer <token>.
Only users with role == 'admin' may access.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field
from firebase_admin import auth as fb_auth, firestore


router = APIRouter(prefix="/admin/users", tags=["admin-users"])


# =========================================================================
# AUTH
# =========================================================================

async def require_admin(
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()

    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    uid = decoded["uid"]
    role = decoded.get("role")

    if not role:
        try:
            snap = firestore.client().collection("users").document(uid).get()
            if snap.exists:
                role = (snap.to_dict() or {}).get("role")
        except Exception:
            role = None

    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    return {"uid": uid, "email": decoded.get("email"), "role": role}


# =========================================================================
# SCHEMAS
# =========================================================================

VALID_ROLES = {"citizen", "responder", "admin"}
VALID_STATUSES = {"active", "suspended", "pending"}


class CreateUserRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(..., min_length=2, max_length=80)
    phone: Optional[str] = None
    role: str = "citizen"

    responder_type: Optional[str] = None
    license_number: Optional[str] = None
    service_area: Optional[str] = None
    barangay: Optional[str] = None
    agency: Optional[str] = None

    permissions: Optional[List[str]] = None


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    status: Optional[str] = None

    responder_type: Optional[str] = None
    license_number: Optional[str] = None
    service_area: Optional[str] = None
    barangay: Optional[str] = None
    agency: Optional[str] = None

    permissions: Optional[List[str]] = None


class UserUpdateRequestBody(BaseModel):
    requested_changes: Dict[str, Any]
    reason: Optional[str] = None


# =========================================================================
# HELPERS
# =========================================================================

def _db():
    return firestore.client()


def _normalize_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    digits = re.sub(r"[^\d]", "", phone)
    if not digits:
        return None
    if digits.startswith("63"):
        return f"+{digits}"
    if digits.startswith("0"):
        return f"+63{digits[1:]}"
    if digits.startswith("9") and len(digits) == 10:
        return f"+63{digits}"
    if phone.startswith("+"):
        return phone
    return f"+{digits}"


def _slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[()]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _iso_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# =========================================================================
# LIST
# =========================================================================

@router.get("")
async def list_users(admin: dict = Depends(require_admin)) -> Dict[str, Any]:
    db = _db()

    pending_uids: set = set()
    try:
        reqs = (
            db.collection("user_update_requests")
            .where("status", "==", "pending")
            .stream()
        )
        for r in reqs:
            data = r.to_dict() or {}
            if data.get("uid"):
                pending_uids.add(data["uid"])
    except Exception as e:
        print(f"[admin_users] pending-request scan skipped: {e}")

    users: List[Dict[str, Any]] = []
    for doc in db.collection("users").stream():
        data = doc.to_dict() or {}
        data["uid"] = doc.id
        data.setdefault("status", "active")
        data["hasPendingUpdateRequest"] = doc.id in pending_uids

        for k in ("createdAt", "updatedAt", "lastSeen", "termsAcceptedAt", "invitedAt"):
            data[k] = _iso_or_none(data.get(k))

        users.append(data)

    users.sort(key=lambda u: (u.get("fullName") or u.get("displayName") or "").lower())

    return {"users": users, "total": len(users)}


# =========================================================================
# CREATE
# =========================================================================

@router.post("")
async def create_user(
    payload: CreateUserRequest,
    admin: dict = Depends(require_admin),
) -> Dict[str, Any]:
    db = _db()

    if payload.role not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role: {payload.role}")

    if payload.role == "responder" and not payload.responder_type:
        raise HTTPException(400, "responder_type is required for responder role")

    email = payload.email.lower().strip()

    existing = list(
        db.collection("users").where("email", "==", email).limit(1).stream()
    )
    if existing:
        raise HTTPException(400, "A user with this email already exists")

    auth_kwargs: Dict[str, Any] = {
        "email": email,
        "display_name": payload.display_name,
        "email_verified": False,
    }
    normalized_phone = _normalize_phone(payload.phone)
    if normalized_phone:
        auth_kwargs["phone_number"] = normalized_phone

    try:
        fb_user = fb_auth.create_user(**auth_kwargs)
    except Exception as e:
        raise HTTPException(400, f"Failed to create Firebase user: {e}")

    try:
        fb_auth.set_custom_user_claims(fb_user.uid, {"role": payload.role})
    except Exception as e:
        print(f"[admin_users] custom claims failed: {e}")

    now = datetime.now(timezone.utc)

    user_doc: Dict[str, Any] = {
        "uid": fb_user.uid,
        "email": email,
        "fullName": payload.display_name,
        "displayName": payload.display_name,
        "phone": normalized_phone or payload.phone,
        "role": payload.role,
        "status": "active",
        "disabled": False,
        "mustChangeCredentials": True,
        "createdAt": now,
        "updatedAt": now,
        "createdBy": admin["uid"],
        "lastSeen": None,
        "termsAccepted": True,
        "termsVersion": "1.0.0",
        "termsAcceptedAt": now,
    }

    if payload.role == "responder":
        user_doc.update({
            "responderType": payload.responder_type,
            "licenseNumber": payload.license_number or "",
            "serviceArea": payload.service_area or "",
            "barangay": payload.barangay or "",
            "assignedBarangay": payload.barangay or "",
            "agency": payload.agency or "",
            "badgeId": "",
        })

    if payload.role == "admin":
        user_doc["permissions"] = payload.permissions or []

    db.collection("users").document(fb_user.uid).set(user_doc)

    if payload.role == "responder" and payload.barangay:
        try:
            slug = _slugify(payload.barangay)
            b_ref = db.collection("barangays").document(slug)
            if b_ref.get().exists:
                b_ref.update({
                    "responderUid": fb_user.uid,
                    "responderName": payload.display_name,
                    "responderPhone": normalized_phone or payload.phone,
                    "assignedAt": now,
                    "updatedAt": now,
                })
        except Exception as e:
            print(f"[admin_users] barangay mirror failed: {e}")

    reset_link = None
    try:
        reset_link = fb_auth.generate_password_reset_link(email)
    except Exception as e:
        print(f"[admin_users] reset link failed: {e}")

    sms_ok = False
    sms_error = None
    if normalized_phone and reset_link:
        try:
            from sms_service import send_incident_sms
            sms_body = (
                "ROADRESCUE ACCOUNT CREATED\n"
                f"\nHi {payload.display_name},"
                f"\n\nYour {payload.role} account is ready."
                f"\n\nSet your password here:\n{reset_link}"
                f"\n\n--\nRoadRescue"
            )
            result = send_incident_sms(
                to_phone=normalized_phone,
                message=sms_body,
                incident_id=f"account-{fb_user.uid[:6]}",
            )
            sms_ok = result.get("ok", False)
            sms_error = result.get("error")
        except Exception as e:
            sms_error = str(e)[:200]

    return {
        "ok": True,
        "uid": fb_user.uid,
        "email": email,
        "role": payload.role,
        "resetLink": reset_link,
        "sms_ok": sms_ok,
        "sms_error": sms_error,
        "message": "User created successfully.",
    }


# =========================================================================
# UPDATE
# =========================================================================

@router.patch("/{uid}")
async def update_user(
    uid: str,
    payload: UpdateUserRequest,
    admin: dict = Depends(require_admin),
) -> Dict[str, Any]:
    db = _db()
    doc_ref = db.collection("users").document(uid)
    snap = doc_ref.get()

    if not snap.exists:
        raise HTTPException(404, "User not found")

    existing = snap.to_dict() or {}
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)

    if not updates:
        return {"ok": True, "uid": uid, "noChanges": True}

    if "role" in updates and updates["role"] not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role: {updates['role']}")
    if "status" in updates and updates["status"] not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status: {updates['status']}")

    auth_updates: Dict[str, Any] = {}
    if "display_name" in updates:
        auth_updates["display_name"] = updates["display_name"]
    if "phone" in updates:
        norm = _normalize_phone(updates["phone"])
        if norm:
            auth_updates["phone_number"] = norm

    if auth_updates:
        try:
            fb_auth.update_user(uid, **auth_updates)
        except Exception as e:
            raise HTTPException(400, f"Failed to update auth user: {e}")

    if "role" in updates:
        try:
            fb_auth.set_custom_user_claims(uid, {"role": updates["role"]})
            fb_auth.revoke_refresh_tokens(uid)
        except Exception as e:
            print(f"[admin_users] claim update failed: {e}")

    if "status" in updates:
        disabled = updates["status"] == "suspended"
        try:
            fb_auth.update_user(uid, disabled=disabled)
            updates["disabled"] = disabled
        except Exception as e:
            print(f"[admin_users] disable toggle failed: {e}")

    if "display_name" in updates:
        updates["fullName"] = updates.pop("display_name")

    if "phone" in updates:
        norm = _normalize_phone(updates["phone"])
        if norm:
            updates["phone"] = norm

    if "responder_type" in updates:
        updates["responderType"] = updates.pop("responder_type")
    if "license_number" in updates:
        updates["licenseNumber"] = updates.pop("license_number")
    if "service_area" in updates:
        updates["serviceArea"] = updates.pop("service_area")

    updates["updatedAt"] = datetime.now(timezone.utc)
    updates["updatedBy"] = admin["uid"]

    doc_ref.update(updates)

    if updates.get("role") == "responder" or existing.get("role") == "responder":
        new_barangay = updates.get("barangay") or existing.get("barangay")
        if new_barangay:
            try:
                slug = _slugify(new_barangay)
                b_ref = db.collection("barangays").document(slug)
                if b_ref.get().exists:
                    b_ref.update({
                        "responderUid": uid,
                        "responderName": updates.get("fullName") or existing.get("fullName", ""),
                        "responderPhone": updates.get("phone") or existing.get("phone", ""),
                        "updatedAt": datetime.now(timezone.utc),
                    })
            except Exception as e:
                print(f"[admin_users] barangay mirror update failed: {e}")

    try:
        reqs = (
            db.collection("user_update_requests")
            .where("uid", "==", uid)
            .where("status", "==", "pending")
            .stream()
        )
        for r in reqs:
            r.reference.update({
                "status": "approved",
                "reviewedBy": admin["uid"],
                "reviewedAt": datetime.now(timezone.utc),
            })
    except Exception:
        pass

    return {"ok": True, "uid": uid, "updated": list(updates.keys())}


# =========================================================================
# DELETE
# =========================================================================

@router.delete("/{uid}")
async def delete_user(
    uid: str,
    admin: dict = Depends(require_admin),
) -> Dict[str, Any]:
    if uid == admin["uid"]:
        raise HTTPException(400, "Cannot delete your own account")

    db = _db()

    try:
        snap = db.collection("users").document(uid).get()
        if snap.exists:
            data = snap.to_dict() or {}
            barangay = data.get("barangay") or data.get("assignedBarangay")
            if barangay:
                slug = _slugify(barangay)
                b_ref = db.collection("barangays").document(slug)
                if b_ref.get().exists:
                    b_ref.update({
                        "responderUid": None,
                        "responderName": None,
                        "responderPhone": None,
                        "assignedAt": None,
                    })
    except Exception as e:
        print(f"[admin_users] barangay unlink failed: {e}")

    try:
        fb_auth.delete_user(uid)
    except Exception as e:
        print(f"[admin_users] auth delete failed: {e}")

    db.collection("users").document(uid).delete()

    return {"ok": True, "uid": uid}


# =========================================================================
# UPDATE REQUESTS
# =========================================================================

@router.post("/request-update")
async def request_my_update(
    payload: UserUpdateRequestBody,
    authorization: Optional[str] = Header(None),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception as e:
        raise HTTPException(401, f"Invalid token: {e}")

    uid = decoded["uid"]

    ALLOWED = {"display_name", "phone", "barangay", "agency"}
    requested = {
        k: v for k, v in (payload.requested_changes or {}).items()
        if k in ALLOWED
    }
    if not requested:
        raise HTTPException(400, "No valid fields to update")

    db = _db()
    ref = db.collection("user_update_requests").document()
    ref.set({
        "uid": uid,
        "requestedChanges": requested,
        "reason": payload.reason or "",
        "status": "pending",
        "createdAt": datetime.now(timezone.utc),
    })

    return {"ok": True, "id": ref.id}


@router.get("/update-requests/all")
async def list_update_requests(
    admin: dict = Depends(require_admin),
) -> Dict[str, Any]:
    db = _db()
    reqs: List[Dict[str, Any]] = []

    try:
        q = (
            db.collection("user_update_requests")
            .where("status", "==", "pending")
            .stream()
        )
        for r in q:
            data = r.to_dict() or {}
            data["id"] = r.id
            data["createdAt"] = _iso_or_none(data.get("createdAt"))
            reqs.append(data)
    except Exception as e:
        raise HTTPException(500, str(e))

    return {"requests": reqs, "total": len(reqs)}


@router.post("/update-requests/{request_id}/approve")
async def approve_update_request(
    request_id: str,
    admin: dict = Depends(require_admin),
) -> Dict[str, Any]:
    db = _db()
    ref = db.collection("user_update_requests").document(request_id)
    snap = ref.get()

    if not snap.exists:
        raise HTTPException(404, "Request not found")

    data = snap.to_dict() or {}
    if data.get("status") != "pending":
        raise HTTPException(400, "Request is not pending")

    changes = data.get("requestedChanges") or {}
    if not changes:
        raise HTTPException(400, "Request has no changes")

    payload = UpdateUserRequest(**changes)
    await update_user(data["uid"], payload, admin)

    ref.update({
        "status": "approved",
        "reviewedBy": admin["uid"],
        "reviewedAt": datetime.now(timezone.utc),
    })

    return {"ok": True, "id": request_id}


@router.post("/update-requests/{request_id}/reject")
async def reject_update_request(
    request_id: str,
    admin: dict = Depends(require_admin),
) -> Dict[str, Any]:
    db = _db()
    ref = db.collection("user_update_requests").document(request_id)
    snap = ref.get()

    if not snap.exists:
        raise HTTPException(404, "Request not found")

    ref.update({
        "status": "rejected",
        "reviewedBy": admin["uid"],
        "reviewedAt": datetime.now(timezone.utc),
    })

    return {"ok": True, "id": request_id}