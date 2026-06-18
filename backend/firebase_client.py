"""
Firebase Admin SDK client for FieldRaven Desktop.
Handles service account initialization, token verification,
and Firestore/Storage operations on behalf of authenticated users.
"""
import os
import json
from pathlib import Path
from typing import Optional
import firebase_admin
from firebase_admin import credentials, firestore, storage, auth as firebase_auth
from google.cloud.firestore import Client as FirestoreClient
from google.cloud.storage import Client as StorageClient

# ── Paths ────────────────────────────────────────────────────
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SERVICE_ACCOUNT_PATH = CONFIG_DIR / "fieldraven-service-account.json"

# ── Singleton state ──────────────────────────────────────────
_app: Optional[firebase_admin.App] = None
_db: Optional[FirestoreClient] = None
_bucket = None
_initialized = False

# Cache for verified tokens (uid -> token string) to avoid re-verifying on every request
_token_cache: dict[str, str] = {}


def get_credentials() -> credentials.Certificate:
    """Load service account credentials from config file."""
    if not SERVICE_ACCOUNT_PATH.exists():
        raise FileNotFoundError(
            f"Firebase service account not found at {SERVICE_ACCOUNT_PATH}. "
            "Download it from Firebase Console → Project Settings → Service Accounts."
        )
    return credentials.Certificate(str(SERVICE_ACCOUNT_PATH))


def initialize() -> None:
    """Initialize the Firebase Admin SDK (idempotent)."""
    global _app, _db, _bucket, _initialized
    if _initialized:
        return

    cred = get_credentials()
    _app = firebase_admin.initialize_app(cred, {
        'storageBucket': 'fieldraven-ffad8.firebasestorage.app',
        'projectId': 'fieldraven-ffad8',
    })
    _db = firestore.client()
    _bucket = storage.bucket()
    _initialized = True

    print("✅ Firebase Admin SDK initialized for project fieldraven-ffad8")


def verify_id_token(id_token: str) -> Optional[dict]:
    """
    Verify a Firebase ID token from the client-side web SDK.
    Returns decoded token dict with 'uid', 'email', etc., or None if invalid.
    """
    if not _initialized:
        initialize()
    try:
        decoded = firebase_auth.verify_id_token(id_token, clock_skew_seconds=10)
        return decoded
    except Exception as e:
        print(f"⚠️ Token verification failed: {e}")
        return None


def get_user_display_name(uid: str) -> Optional[str]:
    """Get a user's display name from Firebase Auth."""
    if not _initialized:
        initialize()
    try:
        user = firebase_auth.get_user(uid)
        return user.display_name or user.email or uid
    except Exception:
        return None


# ── Firestore helpers ────────────────────────────────────────

def get_db() -> FirestoreClient:
    """Get the Firestore client instance."""
    if not _initialized:
        initialize()
    return _db


def get_user_collection(uid: str, collection_name: str):
    """
    Get a reference to a user subcollection.
    e.g. get_user_collection(uid, 'jobs') -> users/{uid}/jobs
    """
    db = get_db()
    return db.collection('users').document(uid).collection(collection_name)


def get_user_job(uid: str, job_id: str) -> Optional[dict]:
    """Read a single field job document from users/{uid}/jobs/{job_id}."""
    try:
        doc = get_user_collection(uid, 'jobs').document(job_id).get()
        if doc.exists:
            return {'id': doc.id, **doc.to_dict()}
        return None
    except Exception as e:
        print(f"⚠️ get_user_job failed: {e}")
        return None


def get_processing_queue():
    """Get the global processing_queue collection reference."""
    db = get_db()
    return db.collection('processing_queue')


def get_machines_collection():
    """Get the global machines collection reference."""
    db = get_db()
    return db.collection('machines')


# ── Storage helpers ──────────────────────────────────────────

def get_bucket():
    """Get the Firebase Storage bucket."""
    if not _initialized:
        initialize()
    return _bucket


def upload_preview(job_id: str, local_path: str, uid: str) -> Optional[str]:
    """
    Upload a preview/thumbnail to Firebase Storage.
    Returns the download URL or None on failure.
    """
    try:
        bucket = get_bucket()
        blob = bucket.blob(f'users/{uid}/previews/{job_id}.jpg')
        blob.upload_from_filename(local_path)
        blob.make_public()
        return blob.public_url
    except Exception as e:
        print(f"⚠️ Preview upload failed: {e}")
        return None


# ── Health check ─────────────────────────────────────────────

def check_connection() -> dict:
    """Quick connectivity check. Returns status dict."""
    try:
        if not _initialized:
            initialize()
        # Try a simple read to verify connectivity
        db = get_db()
        list(db.collection('machines').limit(1).get())
        return {"connected": True, "project": "fieldraven-ffad8"}
    except Exception as e:
        return {"connected": False, "error": str(e)}