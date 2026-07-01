"""
Job queue management for FieldRaven Desktop.
Polls Firebase for jobs assigned to this machine, reports status.
"""
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional, Callable
from google.cloud.firestore import SERVER_TIMESTAMP, DocumentSnapshot

from . import firebase_client
from .machine import get_machine_id

# Single-worker pool so progress writes are fire-and-forget and never block
# the pipeline thread. One worker is enough — writes are sequential anyway.
_write_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fstore-write")


# ── Status constants (match web app expectations) ────────────
STATUS_QUEUED = 'queued'
STATUS_PROCESSING = 'processing'
STATUS_COMPLETE = 'complete'
STATUS_ERROR = 'error'
STATUS_CANCELLED = 'cancelled'
STATUS_WAITING_CAMERA = 'waiting_for_camera'

# ── Polling state ────────────────────────────────────────────
_poll_interval = 15  # seconds
_poll_thread: Optional[threading.Thread] = None
_poll_stop = threading.Event()
_on_new_job: Optional[Callable] = None  # callback when a new job is picked up

# Current running job
_current_job: Optional[dict] = None
_current_job_id: Optional[str] = None


# ── Job operations ───────────────────────────────────────────

def poll_for_jobs() -> list[dict]:
    """
    Check Firebase for jobs assigned to this machine with status 'queued'.
    Returns a list of job dicts (max 5).
    """
    machine_id = get_machine_id()
    queue_ref = firebase_client.get_processing_queue()

    try:
        docs = queue_ref \
            .where('assignedMachine', '==', machine_id) \
            .where('status', '==', STATUS_QUEUED) \
            .limit(5) \
            .get()

        jobs = []
        for doc in docs:
            data = doc.to_dict()
            data['docId'] = doc.id
            jobs.append(data)
        return jobs
    except Exception as e:
        print(f"⚠️ Queue poll failed: {e}")
        return []


def get_local_folder_jobs() -> list[dict]:
    """Return active (queued or processing) local-folder and local-video jobs for
    this machine. Only active jobs are returned so that completed/errored jobs
    do not persist across server restarts."""
    machine_id = get_machine_id()
    try:
        docs = firebase_client.get_processing_queue() \
            .where('assignedMachine', '==', machine_id) \
            .limit(50) \
            .get()
        jobs = []
        for doc in docs:
            data = doc.to_dict()
            jtype = data.get('jobType', '')
            status = data.get('status', '')
            if jtype in ('local_folder', 'local_video') and status in ('queued', 'processing'):
                data['docId'] = doc.id
                jobs.append(data)
        return jobs
    except Exception as e:
        print(f"⚠️ Local jobs query failed: {e}")
        return []


def get_job_status(job_id: str) -> Optional[dict]:
    """Get current status of a specific job."""
    try:
        doc = firebase_client.get_processing_queue().document(job_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        data['docId'] = doc.id
        return data
    except Exception as e:
        print(f"⚠️ Get job status failed: {e}")
        return None


def accept_job(job_id: str) -> bool:
    """Accept a job: set status to 'processing', record start time."""
    global _current_job, _current_job_id
    try:
        doc_ref = firebase_client.get_processing_queue().document(job_id)
        now = datetime.now(timezone.utc).isoformat()
        doc_ref.update({
            'status': STATUS_PROCESSING,
            'startedAt': SERVER_TIMESTAMP,
            'progress': 0,
            'currentStep': 'Starting...',
            'machineId': get_machine_id(),
        })
        _current_job_id = job_id
        _current_job = get_job_status(job_id)
        print(f"📋 Accepted job: {job_id}")
        return True
    except Exception as e:
        print(f"⚠️ Accept job failed: {e}")
        return False


def update_job_progress(job_id: str, progress: int, current_step: str,
                        extra: Optional[dict] = None) -> bool:
    """Update job progress (0-100) and current step description.
    The Firestore write is submitted to a background thread so it never blocks
    the pipeline (each synchronous write could otherwise pause the pipeline
    thread for several hundred ms while Firestore ACKs)."""
    update = {
        'progress': min(100, max(0, progress)),
        'currentStep': current_step,
        'updatedAt': SERVER_TIMESTAMP,
    }
    if extra:
        update.update(extra)

    def _write():
        try:
            firebase_client.get_processing_queue().document(job_id).update(update)
        except Exception as e:
            print(f"⚠️ Update progress failed: {e}")

    _write_pool.submit(_write)
    return True


def complete_job(job_id: str, output_path: str, output_format: str,
                 preview_url: Optional[str] = None) -> bool:
    """Mark a job as complete with output details."""
    try:
        update = {
            'status': STATUS_COMPLETE,
            'progress': 100,
            'currentStep': 'Complete',
            'outputPath': output_path,
            'outputFormat': output_format,
            'completedAt': SERVER_TIMESTAMP,
            'updatedAt': SERVER_TIMESTAMP,
        }
        if preview_url:
            update['previewUrl'] = preview_url
        firebase_client.get_processing_queue().document(job_id).update(update)
        global _current_job, _current_job_id
        _current_job = None
        _current_job_id = None
        print(f"✅ Job complete: {job_id}")
        return True
    except Exception as e:
        print(f"⚠️ Complete job failed: {e}")
        return False


def fail_job(job_id: str, error_message: str) -> bool:
    """Mark a job as failed with error message."""
    try:
        firebase_client.get_processing_queue().document(job_id).update({
            'status': STATUS_ERROR,
            'errorMessage': error_message,
            'currentStep': f'Error: {error_message}',
            'completedAt': SERVER_TIMESTAMP,
            'updatedAt': SERVER_TIMESTAMP,
        })
        global _current_job, _current_job_id
        _current_job = None
        _current_job_id = None
        print(f"❌ Job failed: {job_id}: {error_message}")
        return True
    except Exception as e:
        print(f"⚠️ Fail job failed: {e}")
        return False


def get_current_job() -> Optional[dict]:
    """Get the currently active job (if any)."""
    if _current_job_id:
        return get_job_status(_current_job_id)
    return None


def is_busy() -> bool:
    """Check if the machine is currently processing a job."""
    return _current_job_id is not None


def get_all_jobs(limit: int = 50) -> list[dict]:
    """Get all processing jobs for this machine, most recent first."""
    machine_id = get_machine_id()
    queue_ref = firebase_client.get_processing_queue()
    try:
        docs = queue_ref \
            .where('assignedMachine', '==', machine_id) \
            .limit(limit) \
            .get()
        jobs = []
        for doc in docs:
            data = doc.to_dict()
            data['docId'] = doc.id
            for k, v in list(data.items()):
                if hasattr(v, 'isoformat'):
                    data[k] = v.isoformat()
            jobs.append(data)
        jobs.sort(key=lambda j: j.get('createdAt') or '', reverse=True)
        return jobs
    except Exception as e:
        print(f"⚠️ Get all jobs failed: {e}")
        return []


def get_completed_jobs(limit: int = 20) -> list[dict]:
    """Get only completed jobs (kept for compatibility)."""
    return [j for j in get_all_jobs(limit) if j.get('status') == STATUS_COMPLETE]


# ── Polling loop ─────────────────────────────────────────────

def _poll_loop():
    """Background polling loop."""
    while not _poll_stop.is_set():
        if not is_busy():
            jobs = poll_for_jobs()
            if jobs and _on_new_job:
                for job in jobs:
                    _on_new_job(job)
                    break  # Only process one at a time
        _poll_stop.wait(_poll_interval)


def start_polling(on_new_job: Optional[Callable] = None) -> None:
    """Start the background polling thread."""
    global _poll_thread, _on_new_job
    _on_new_job = on_new_job

    if _poll_thread is None or not _poll_thread.is_alive():
        _poll_stop.clear()
        _poll_thread = threading.Thread(target=_poll_loop, daemon=True)
        _poll_thread.start()
        print(f"🔄 Queue polling started (every {_poll_interval}s)")


def stop_polling() -> None:
    """Stop the polling thread."""
    _poll_stop.set()
    print("🛑 Queue polling stopped")


def set_poll_interval(seconds: int) -> None:
    """Change the polling interval."""
    global _poll_interval
    _poll_interval = max(5, seconds)