import json
import os
import base64
from pywebpush import webpush, WebPushException
# NOTE: If Render crashes on the next line, change 'db' to whatever your firebase_init.py uses
from firebase_init import db 

# Load keys from Render environment variables
VAPID_PRIVATE_PEM = os.getenv("VAPID_PRIVATE_KEY_PEM", "").replace("\\n", "\n")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")

def save_subscription(responder_id: str, subscription: dict) -> None:
    db.collection("push_subscriptions").document(responder_id).set(
        {"subscription": subscription}, merge=True
    )

def delete_subscription(responder_id: str) -> None:
    db.collection("push_subscriptions").document(responder_id).delete()

def _get_subscription(responder_id: str):
    doc = db.collection("push_subscriptions").document(responder_id).get()
    if not doc.exists:
        return None
    return (doc.to_dict() or {}).get("subscription")

def send_push_to_responder(responder_id: str, title: str, body: str, data: dict = None) -> bool:
    sub = _get_subscription(responder_id)
    if not sub:
        print(f"[PUSH] No subscription found for {responder_id}")
        return False
        
    payload = json.dumps({"title": title, "body": body, "data": data or {}})
    try:
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_PEM,
            vapid_claims={"sub": VAPID_SUBJECT},
        )
        return True
    except WebPushException as e:
        # If the browser rejected it (user cleared data/uninstalled), delete it from DB
        if e.response is not None and e.response.status_code in (404, 410):
            delete_subscription(responder_id)
        print(f"[PUSH ERROR] {e}")
        return False

def get_public_key():
    """Helper for the frontend to get the exact key format it needs"""
    return VAPID_PUBLIC_KEY
