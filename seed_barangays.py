"""
One-time seed script — populates the `barangays` collection in Firestore.

Idempotent:
  - Safe to run multiple times.
  - Existing docs are updated (name/center refreshed) but `responderUid`
    and other admin-assigned fields are PRESERVED.
  - This means re-running the script will never wipe admin assignments.

Run:
  python seed_barangays.py
"""

import os
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

from data.barangays import to_documents

load_dotenv()


def init_firebase():
    """Initialize firebase-admin using the service account JSON in .env."""
    if firebase_admin._apps:
        return  # already initialized

    service_account_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()

    if not service_account_path:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT_JSON is not set in .env. "
            "Set it to the filename of your service account JSON."
        )

    # Resolve relative to the backend folder
    path = Path(service_account_path)
    if not path.is_absolute():
        path = Path(__file__).parent / path

    if not path.exists():
        raise FileNotFoundError(
            f"Service account JSON not found at: {path}\n"
            f"Update FIREBASE_SERVICE_ACCOUNT_JSON in .env to the correct path."
        )

    cred = credentials.Certificate(str(path))
    firebase_admin.initialize_app(cred)


def seed():
    init_firebase()
    db = firestore.client()

    docs = to_documents()
    collection = db.collection("barangays")

    created = 0
    updated = 0
    preserved = 0

    for d in docs:
        slug = d["slug"]
        ref = collection.document(slug)
        snapshot = ref.get()

        base = {
            "name": d["name"],
            "slug": slug,
            "center": firestore.GeoPoint(d["lat"], d["lng"]),
        }

        if snapshot.exists:
            existing = snapshot.to_dict() or {}

            # Preserve admin-assigned fields
            for key in ("responderUid", "responderName", "responderPhone", "assignedAt"):
                if key in existing:
                    base[key] = existing[key]
                    preserved += 1

            ref.update(base)
            updated += 1
            print(f"  ↻ updated   {slug}")
        else:
            # New doc — initialize responder fields to null
            base.update({
                "responderUid": None,
                "responderName": None,
                "responderPhone": None,
                "assignedAt": None,
            })
            ref.set(base)
            created += 1
            print(f"  + created   {slug}")

    print()
    print(f"✅ Done.")
    print(f"   Created: {created}")
    print(f"   Updated: {updated}")
    print(f"   Preserved admin fields: {preserved}")


if __name__ == "__main__":
    seed()