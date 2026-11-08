import json
import os
import tempfile
from pywebpush import webpush, WebPushException
from firebase_init import db

# We use temp files because pywebpush can be extremely picky 
# about how PEM strings are formatted on different servers.
def _get_key_file():
    pem_content = os.getenv("VAPID_PRIVATE_KEY_PEM", "")
    if not pem_content:
        return None
    # Render sometimes squishes the key into one line with literal \n. 
    # We fix that and ensure it has real line breaks.
    pem_content = pem_content.replace("\\n", "\n")
    if not pem_content.endswith("\n"):
        pem_content += "\n"
        
    # Create a temporary file to hold the key safely
    temp = tempfile.NamedTemporaryFile(delete=False, mode="w", suffix=".pem")
    temp.write(pem_content)
    temp.close()
    return temp.name

VAPID_PRIVATE_KEY_PATH = _get_key_file()
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
        
    if not VAPID_PRIVATE_KEY_PATH:
        print("[PUSH ERROR] VAPID_PRIVATE_KEY_PEM is missing!")
        return False
        
    payload = json.dumps({"title": title, "body": body, "data": data or {}})
    try:
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY_PATH, # Use the safe file path
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
    # Strip all whitespace and newlines from the public key
    key = os.getenv("VAPID_PUBLIC_KEY", "")
    return key.replace("\n", "").replace(" ", "").strip()
