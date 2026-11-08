import json
import os
import base64
import tempfile
from pywebpush import webpush, WebPushException
from firebase_admin import firestore

def _get_key_file():
    # Read the Base64 encoded key
    b64_key = os.getenv("VAPID_PRIVATE_KEY_BASE64", "")
    if not b64_key:
        print("[PUSH ERROR] VAPID_PRIVATE_KEY_BASE64 is missing!")
        return None
        
    try:
        # Decode it back to the normal PEM format
        pem_content = base64.b64decode(b64_key).decode('utf-8')
        
        # Write to a temporary file so pywebpush can read it perfectly
        temp = tempfile.NamedTemporaryFile(delete=False, mode="w", suffix=".pem")
        temp.write(pem_content)
        temp.close()
        return temp.name
    except Exception as e:
        print(f"[PUSH ERROR] Failed to decode key: {e}")
        return None

VAPID_PRIVATE_KEY_PATH = _get_key_file()
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")

def save_subscription(responder_id: str, subscription: dict) -> None:
    db = firestore.client()  # 👈 FIXED: Get client here
    db.collection("push_subscriptions").document(responder_id).set(
        {"subscription": subscription}, merge=True
    )

def delete_subscription(responder_id: str) -> None:
    db = firestore.client()  # 👈 FIXED: Get client here
    db.collection("push_subscriptions").document(responder_id).delete()

def _get_subscription(responder_id: str):
    db = firestore.client()  # 👈 FIXED: Get client here
    doc = db.collection("push_subscriptions").document(responder_id).get()
    if not doc.exists:
        return None
    return (doc.to_dict() or {}).get("subscription")

def send_push_to_responder(responder_id: str, title: str, body: str, data: dict = None) -> bool:
    sub = _get_subscription(responder_id)
    if not sub:
        print(f"[PUSH] No subscription found for {responder_id}")
        return False
        
    if not VAPID_PRIVATE_KEY_PATH:
        print("[PUSH ERROR] VAPID_PRIVATE_KEY_PATH is not set (File creation failed).")
        return False
        
    payload = json.dumps({"title": title, "body": body, "data": data or {}})
    try:
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY_PATH, 
            vapid_claims={"sub": VAPID_SUBJECT},
        )
        return True
    except WebPushException as e:
        if e.response is not None and e.response.status_code in (404, 410):
            delete_subscription(responder_id)
        print(f"[PUSH ERROR] {e}")
        return False
    except Exception as e:
        print(f"[PUSH FATAL] {e}")
        return False

def get_public_key():
    # Ensure the public key is clean
    key = os.getenv("VAPID_PUBLIC_KEY", "")
    return key.replace("\n", "").replace(" ", "").strip()
