"""
Machine identity and registration for FieldRaven Desktop.
Each desktop machine registers itself in Firebase so the web app
can assign processing jobs to it.
"""
import socket
import platform
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from . import firebase_client
from google.cloud.firestore import SERVER_TIMESTAMP


# ── Machine identity ─────────────────────────────────────────
_MACHINE_ID: Optional[str] = None
_MACHINE_DISPLAY_NAME: Optional[str] = None
_HEARTBEAT_INTERVAL = 60  # seconds
_heartbeat_thread: Optional[threading.Thread] = None
_heartbeat_stop = threading.Event()


def get_machine_id() -> str:
    """Get the unique machine identifier. Uses hostname by default."""
    global _MACHINE_ID
    if _MACHINE_ID is None:
        _MACHINE_ID = socket.gethostname().lower().replace(' ', '-')
    return _MACHINE_ID


def set_machine_id(machine_id: str) -> None:
    """Override the machine ID (e.g. from settings)."""
    global _MACHINE_ID
    _MACHINE_ID = machine_id


def get_display_name() -> str:
    """Get the human-readable machine name."""
    global _MACHINE_DISPLAY_NAME
    if _MACHINE_DISPLAY_NAME is None:
        _MACHINE_DISPLAY_NAME = socket.gethostname()
    return _MACHINE_DISPLAY_NAME


def set_display_name(name: str) -> None:
    """Set a custom display name for this machine."""
    global _MACHINE_DISPLAY_NAME
    _MACHINE_DISPLAY_NAME = name


# ── Firestore registration ───────────────────────────────────

def get_machine_capabilities() -> dict:
    """Detect hardware capabilities for job assignment filtering."""
    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "platform_release": platform.release(),
        "processor": platform.processor(),
        "machine": platform.machine(),
    }


def register_machine(uid: str) -> str:
    """
    Register or update this machine in Firebase.
    Returns the machine ID.
    """
    machine_id = get_machine_id()
    display_name = get_display_name()
    caps = get_machine_capabilities()

    doc_ref = firebase_client.get_machines_collection().document(machine_id)
    doc_ref.set({
        "machineId": machine_id,
        "displayName": display_name,
        "status": "online",
        "lastSeen": SERVER_TIMESTAMP,
        "registeredBy": uid,
        "registeredAt": SERVER_TIMESTAMP,
        "capabilities": caps,
        "currentJob": None,
        "availableProfiles": ["splat3", "mcmc", "adc", "pointcloud"],
        "settings": {
            "autoImportCamera": True,
            "openViewerWhenDone": True,
        },
    }, merge=True)

    print(f"✅ Machine '{display_name}' ({machine_id}) registered in Firebase")
    return machine_id


def update_machine_heartbeat() -> None:
    """Update the lastSeen timestamp for this machine."""
    machine_id = get_machine_id()
    try:
        doc_ref = firebase_client.get_machines_collection().document(machine_id)
        doc_ref.update({
            "lastSeen": SERVER_TIMESTAMP,
            "status": "online",
        })
    except Exception as e:
        print(f"⚠️ Heartbeat update failed: {e}")


def set_machine_offline() -> None:
    """Mark machine as offline (called on shutdown)."""
    machine_id = get_machine_id()
    try:
        doc_ref = firebase_client.get_machines_collection().document(machine_id)
        doc_ref.update({
            "status": "offline",
            "lastSeen": SERVER_TIMESTAMP,
        })
    except Exception:
        pass


# ── Heartbeat loop ───────────────────────────────────────────

def _heartbeat_loop():
    """Background thread that sends heartbeat every N seconds."""
    while not _heartbeat_stop.is_set():
        update_machine_heartbeat()
        _heartbeat_stop.wait(_HEARTBEAT_INTERVAL)


def start_heartbeat(uid: str) -> None:
    """
    Start the heartbeat background thread.
    Also registers the machine on first call.
    """
    global _heartbeat_thread

    # Register machine first
    register_machine(uid)

    # Start heartbeat if not already running
    if _heartbeat_thread is None or not _heartbeat_thread.is_alive():
        _heartbeat_stop.clear()
        _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        _heartbeat_thread.start()
        print(f"💓 Heartbeat started (every {_HEARTBEAT_INTERVAL}s)")


def stop_heartbeat() -> None:
    """Stop the heartbeat and mark machine offline."""
    _heartbeat_stop.set()
    set_machine_offline()
    print("💔 Heartbeat stopped, machine marked offline")