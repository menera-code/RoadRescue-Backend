"""
Firebase Admin SDK initialization.

Importing this module initializes the Firebase Admin SDK using the
service account JSON configured in .env. Safe to import multiple times —
guarded so it only initializes once.

Called by:
  - main.py (before starting the Firestore worker)
  - seed_barangays.py
  - Any other script that needs Firestore / Auth / Storage admin access
"""

import os
from pathlib import Path

import firebase_admin
from firebase_admin import credentials
from dotenv import load_dotenv

load_dotenv()

_initialized = False


def init_firebase_admin() -> None:
    """
    Initialize the Firebase Admin SDK.

    Reads FIREBASE_SERVICE_ACCOUNT_JSON from .env — either a relative
    path to a service account JSON file, or the JSON content itself.
    """
    global _initialized
    if _initialized or firebase_admin._apps:
        _initialized = True
        return

    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()

    if not raw:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT_JSON is not set in .env. "
            "Set it to the filename of your service account JSON."
        )

    # Case 1: the value is a path to a file
    if raw.endswith(".json") and "{" not in raw:
        path = Path(raw)
        if not path.is_absolute():
            # Resolve relative to the backend folder
            path = Path(__file__).parent / path

        if not path.exists():
            raise FileNotFoundError(
                f"Service account JSON not found at: {path}\n"
                f"Check FIREBASE_SERVICE_ACCOUNT_JSON in .env."
            )

        cred = credentials.Certificate(str(path))
    else:
        # Case 2: the value is the JSON content itself
        import json
        cred = credentials.Certificate(json.loads(raw))

    firebase_admin.initialize_app(cred)
    _initialized = True
    print("[firebase] ✅ Admin SDK initialized")