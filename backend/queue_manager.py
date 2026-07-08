"""
Job queue management for FieldRaven Desktop.

Firebase role: job coordination only (queue/accept/complete/fail + stage milestones).
In-memory cache: source of truth for progress during a run. The local FastAPI
/api/jobs/{id}/status endpoint serves from memory — no Firestore read on every poll.

Firebase write budget per pipeline run:
  1  accept_job()             → status = processing
  ~5 update_job_progress(..., milestone=True)  → stage transitions only
  1  complete_job() / fail_job()
  ──────────────────────────────
  < 10 writes total (vs hundreds previously)
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional, Callable
from google.cloud.firestore import SERVER_TIMESTAMP

from . import firebase_client
from .machine import get_machine_id

# Single-worker pool for fire-and-forget Firestore milestone writes.
_write_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fstore-write")

# ── Status constants ──────────────────────────────────────────
STATUS_QUEUED         = 'queued'
STATUS_PROCESSING     = 'processing'
STATUS_COMPLETE       = 'complete'
STATUS_ERROR          = 'error'
STATUS_CANCELLED      = 'cancelled'
STATUS_WAITING_CAMERA = 'waiting_for_camera'

# ── In-memory status cache ────────────────────────────────────
# Populated on accept_job(); updated by update_job_progress() and milestone
# writes. get_job_status() serves from here during a run — zero Firestore reads.
_status_cache: dict[str, dict] = {}

# ── Polling state ─────────────────────────────────────────────
_poll_interval = 15
_poll_thread: Optional[threading.Thread] = None
_poll_stop    = threading.Event()
_on_new_job:  Optional[Callable] = None

# Currently running job
_current_job_id: Optional[str] = None


# ── Internal helpers ──────────────────────────────────────────

def _fb_write_async(job_id: str, update: dict) -> None:
    """Submit a Firestore write to the background pool (fire-and-forget)."""
    def _write():
        try:
            firebase_client.get_processing_queue().document(job_id).update(update)
        except Exception as e:
            print(f"⚠️ Firestore write failed ({job_id}): {e}")
    _write_pool.submit(_write)


def _sanitise(d: dict) -> dict:
    """Convert Firestore Timestamps to ISO strings for JSON serialisation."""
    out = {}
    for k, v in d.items():
        out[k] = v.isoformat() if hasattr(v, 'isoformat') else v
    return out


# ── Queue polling ─────────────────────────────────────────────

def poll_for_jobs() -> list[dict]:
    """Check Firebase for jobs assigned to this machine with status 'queued'."""
    machine_id = get_machine_id()
    try:
        docs = (
            firebase_client.get_processing_queue()
            .where('assignedMachine', '==', machine_id)
            .where('status', '==', STATUS_QUEUED)
            .limit(5)
            .get()
        )
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
    """Return active local-folder and local-video jobs for this machine."""
    machine_id = get_machine_id()
    try:
        docs = (
            firebase_client.get_processing_queue()
            .where('assignedMachine', '==', machine_id)
            .limit(50)
            .get()
        )
        jobs = []
        for doc in docs:
            data = doc.to_dict()
            jtype  = data.get('jobType', '')
            status = data.get('status', '')
            if jtype in ('local_folder', 'local_video') and status in ('queued', 'processing'):
                data['docId'] = doc.id
                jobs.append(data)
        return jobs
    except Exception as e:
        print(f"⚠️ Local jobs query failed: {e}")
        return []


# ── Job lifecycle ─────────────────────────────────────────────

def accept_job(job_id: str) -> bool:
    """Accept a queued job: write processing status to Firestore, seed local cache."""
    global _current_job_id
    try:
        now = datetime.now(timezone.utc).isoformat()
        update = {
            'status':      STATUS_PROCESSING,
            'startedAt':   SERVER_TIMESTAMP,
            'progress':    0,
            'currentStep': 'Starting…',
            'machineId':   get_machine_id(),
        }
        firebase_client.get_processing_queue().document(job_id).update(update)

        # Seed the in-memory cache from Firestore so we have the full doc
        snap = firebase_client.get_processing_queue().document(job_id).get()
        cached = snap.to_dict() if snap.exists else {}
        cached['docId'] = job_id
        cached.update({'status': STATUS_PROCESSING, 'progress': 0,
                       'currentStep': 'Starting…', 'startedAt': now})
        _status_cache[job_id] = _sanitise(cached)

        _current_job_id = job_id
        print(f"📋 Accepted job: {job_id}")
        return True
    except Exception as e:
        print(f"⚠️ Accept job failed: {e}")
        return False


def update_job_progress(
    job_id: str,
    progress: int,
    current_step: str,
    extra: Optional[dict] = None,
    milestone: bool = False,
) -> bool:
    """Update job progress in memory. Set milestone=True only at stage transitions
    to also write to Firestore (keeps Firebase writes to ~5 per pipeline run)."""
    update = {
        'progress':    min(100, max(0, progress)),
        'currentStep': current_step,
    }
    if extra:
        update.update(extra)

    # Always update the in-memory cache (serves the local status endpoint).
    # If the job isn't cached yet (manually-started jobs bypass accept_job),
    # seed from Firestore first so we preserve userId/userJobId/etc.
    if job_id not in _status_cache:
        try:
            snap = firebase_client.get_processing_queue().document(job_id).get()
            if snap.exists:
                _status_cache[job_id] = _sanitise(snap.to_dict())
                _status_cache[job_id]['docId'] = job_id
        except Exception:
            _status_cache[job_id] = {}
    _status_cache[job_id].update(update)

    # Only touch Firestore at stage milestones — not on every progress tick
    if milestone:
        fb_update = dict(update)
        fb_update['updatedAt'] = SERVER_TIMESTAMP
        _fb_write_async(job_id, fb_update)

    return True


def complete_job(
    job_id: str,
    output_path: str,
    output_format: str,
    preview_url: Optional[str] = None,
) -> bool:
    """Mark a job complete. Writes synchronously so the gallery doc lands after."""
    global _current_job_id
    try:
        update = {
            'status':      STATUS_COMPLETE,
            'progress':    100,
            'currentStep': 'Complete',
            'outputPath':  output_path,
            'outputFormat': output_format,
            'completedAt': SERVER_TIMESTAMP,
            'updatedAt':   SERVER_TIMESTAMP,
        }
        if preview_url:
            update['previewUrl'] = preview_url

        firebase_client.get_processing_queue().document(job_id).update(update)

        if job_id in _status_cache:
            _status_cache[job_id].update({
                'status': STATUS_COMPLETE, 'progress': 100, 'currentStep': 'Complete',
                'outputPath': output_path, 'outputFormat': output_format,
            })

        _current_job_id = None
        print(f"✅ Job complete: {job_id}")
        return True
    except Exception as e:
        print(f"⚠️ Complete job failed: {e}")
        return False


def fail_job(job_id: str, error_message: str) -> bool:
    """Mark a job failed. Writes synchronously so the error is always recorded."""
    global _current_job_id
    try:
        firebase_client.get_processing_queue().document(job_id).update({
            'status':       STATUS_ERROR,
            'errorMessage': error_message,
            'currentStep':  f'Error: {error_message}',
            'completedAt':  SERVER_TIMESTAMP,
            'updatedAt':    SERVER_TIMESTAMP,
        })
        if job_id in _status_cache:
            _status_cache[job_id].update({
                'status': STATUS_ERROR,
                'errorMessage': error_message,
                'currentStep': f'Error: {error_message}',
            })
        _current_job_id = None
        print(f"❌ Job failed: {job_id}: {error_message}")
        return True
    except Exception as e:
        print(f"⚠️ Fail job failed: {e}")
        return False


def cancel_job(job_id: str) -> bool:
    """Cancel a job."""
    global _current_job_id
    try:
        firebase_client.get_processing_queue().document(job_id).update({
            'status':    STATUS_CANCELLED,
            'updatedAt': SERVER_TIMESTAMP,
        })
        if job_id in _status_cache:
            _status_cache[job_id]['status'] = STATUS_CANCELLED
        if _current_job_id == job_id:
            _current_job_id = None
        return True
    except Exception as e:
        print(f"⚠️ Cancel job failed: {e}")
        return False


# ── Status reads ──────────────────────────────────────────────

def get_job_status(job_id: str) -> Optional[dict]:
    """Return job status. Serves from in-memory cache during a run (no Firestore read).
    Falls back to Firestore for jobs not in cache (e.g. historical lookups)."""
    if job_id in _status_cache:
        return dict(_status_cache[job_id])

    # Not in cache — read from Firestore once and cache the result
    try:
        doc = firebase_client.get_processing_queue().document(job_id).get()
        if not doc.exists:
            return None
        data = _sanitise(doc.to_dict())
        data['docId'] = doc.id
        # Only cache active jobs to avoid stale data for historical queries
        if data.get('status') in (STATUS_QUEUED, STATUS_PROCESSING):
            _status_cache[job_id] = data
        return dict(data)
    except Exception as e:
        print(f"⚠️ Get job status failed: {e}")
        return None


def get_current_job() -> Optional[dict]:
    """Return the in-memory state of the currently running job (no Firestore read)."""
    if _current_job_id:
        return dict(_status_cache.get(_current_job_id, {})) or None
    return None


def is_busy() -> bool:
    return _current_job_id is not None


def get_all_jobs(limit: int = 50) -> list[dict]:
    """Get all processing jobs for this machine (history view — reads Firestore)."""
    machine_id = get_machine_id()
    try:
        docs = (
            firebase_client.get_processing_queue()
            .where('assignedMachine', '==', machine_id)
            .limit(limit)
            .get()
        )
        jobs = []
        for doc in docs:
            # Merge with in-memory cache so in-progress job shows live state
            data = _sanitise(doc.to_dict())
            data['docId'] = doc.id
            if doc.id in _status_cache:
                data.update(_status_cache[doc.id])
            jobs.append(data)
        jobs.sort(key=lambda j: j.get('createdAt') or '', reverse=True)
        return jobs
    except Exception as e:
        print(f"⚠️ Get all jobs failed: {e}")
        return []


def get_completed_jobs(limit: int = 20) -> list[dict]:
    return [j for j in get_all_jobs(limit) if j.get('status') == STATUS_COMPLETE]


# ── Polling loop ──────────────────────────────────────────────

def _poll_loop():
    while not _poll_stop.is_set():
        if not is_busy():
            jobs = poll_for_jobs()
            if jobs and _on_new_job:
                _on_new_job(jobs[0])
        _poll_stop.wait(_poll_interval)


def start_polling(on_new_job: Optional[Callable] = None) -> None:
    global _poll_thread, _on_new_job
    _on_new_job = on_new_job
    if _poll_thread is None or not _poll_thread.is_alive():
        _poll_stop.clear()
        _poll_thread = threading.Thread(target=_poll_loop, daemon=True)
        _poll_thread.start()
        print(f"🔄 Queue polling started (every {_poll_interval}s)")


def stop_polling() -> None:
    _poll_stop.set()
    print("🛑 Queue polling stopped")


def set_poll_interval(seconds: int) -> None:
    global _poll_interval
    _poll_interval = max(5, seconds)
