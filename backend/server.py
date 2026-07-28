"""
FastAPI application for FieldRaven Desktop.
Serves the local web dashboard and provides API endpoints
for auth, queue management, camera detection, and pipeline control.
"""
import asyncio
import logging
import os
import sys
import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

# ── File-based logging ────────────────────────────────────────
# Writes timestamped logs to C:/FieldRaven/logs/server_YYYY-MM-DD.log
# alongside all stdout output. Crashes produce full tracebacks in the log.
_LOG_DIR = Path("C:/FieldRaven/logs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / f"server_{date.today().isoformat()}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(str(_LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
logger = logging.getLogger("fieldraven")

# Suppress uvicorn access-log noise for high-frequency polling endpoints.
# These hit in-memory state and produce no useful diagnostic information
# during a pipeline run — only clutter the console.
class _SuppressPollingLogs(logging.Filter):
    _QUIET = frozenset([
        "/api/jobs/queue", "/api/camera/status",
        "/api/health", "/api/machine/status",
    ])
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "/api/jobs/" in msg and "/status" in msg:
            return False
        return not any(ep in msg for ep in self._QUIET)

logging.getLogger("uvicorn.access").addFilter(_SuppressPollingLogs())

# Tee print() to the log file too, so all existing print statements are captured
class _LogTee:
    """Writes to both the original stream and the log file."""
    def __init__(self, original, log_path: Path):
        self._orig = original
        self._log  = open(str(log_path), "a", encoding="utf-8", buffering=1)
    def write(self, msg):
        self._orig.write(msg)
        if msg.strip():
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._log.write(f"{ts} {msg}" if not msg.startswith("\n") else msg)
    def flush(self):
        self._orig.flush()
        self._log.flush()
    def __getattr__(self, name):
        return getattr(self._orig, name)

sys.stdout = _LogTee(sys.stdout, _LOG_FILE)
sys.stderr = _LogTee(sys.stderr, _LOG_FILE)

from fastapi import FastAPI, HTTPException, Depends, status, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import firebase_client
from .auth import CurrentUser, require_auth, get_current_user
from . import machine as machine_module
from . import queue_manager
from . import pipeline_runner
from . import splat_config

# ── Server state ─────────────────────────────────────────────
JOBS_DIR = Path("C:/FieldRaven/Jobs")
_is_initialized = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize Firebase on startup, cleanup on shutdown."""
    global _is_initialized
    print("\n" + "=" * 50)
    print("🦅 FieldRaven Desktop starting...")
    print("=" * 50)

    # Initialize Firebase
    try:
        firebase_client.initialize()
        _is_initialized = True
        print("✅ Firebase connected")
        # Queue is session-only — reset any local jobs left over from a previous session
        queue_manager.clear_local_jobs_on_startup()
    except Exception as e:
        print(f"⚠️ Firebase initialization failed: {e}")
        print("   The app will start but cloud features won't work until Firebase is configured.")
        print(f"   Service account expected at: {firebase_client.SERVICE_ACCOUNT_PATH}")

    # Ensure jobs directory exists
    JOBS_DIR.mkdir(parents=True, exist_ok=True)

    yield

    # Shutdown
    machine_module.stop_heartbeat()
    queue_manager.stop_polling()
    print("🦅 FieldRaven Desktop shut down")


app = FastAPI(
    title="FieldRaven Desktop",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.fieldraven.ca", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Routes ───────────────────────────────────────────────

# ── Firebase web config ──────────────────────────────────────

@app.get("/api/firebase-config")
async def get_firebase_config():
    """Serve Firebase web SDK config. No auth required — needed before login."""
    import json as _json
    config_path = Path(__file__).resolve().parent.parent / "config" / "firebase-web-config.json"
    if not config_path.exists():
        raise HTTPException(
            status_code=503,
            detail="config/firebase-web-config.json not found. Copy firebase-web-config.example.json and fill in your values."
        )
    return JSONResponse(_json.loads(config_path.read_text()))

# ── Health / Status ──────────────────────────────────────────

@app.get("/api/health")
async def health():
    """Basic health check."""
    return {
        "status": "running",
        "firebase": {"connected": _is_initialized, "project": "fieldraven-ffad8"},
        "machine_id": machine_module.get_machine_id(),
        "machine_name": machine_module.get_display_name(),
        "busy": queue_manager.is_busy(),
        "current_job": queue_manager.get_current_job(),
    }

# ── Auto-accept callback (used by polling loop for web-queued jobs) ──────────

def _auto_accept_job(job: dict) -> None:
    """
    Called by the queue polling loop when a new queued job is found.
    Accepts the job and starts the pipeline without any user interaction.
    Only runs for jobs that originated from the web app (have storageInputPath).
    Local desktop-queued jobs are handled by the frontend via /api/jobs/{id}/start.
    """
    job_id = job.get('docId')
    if not job_id:
        return
    if pipeline_runner.is_running(job_id):
        return
    # Only auto-start web-originated jobs (they have a storageInputPath set)
    if not job.get('storageInputPath'):
        return
    print(f"🌐 Web job detected — auto-accepting: {job_id}")
    if not queue_manager.accept_job(job_id):
        return
    # job dict already contains full Firestore data from poll_for_jobs
    pipeline_runner.start(job_id, job)


# ── Auth ─────────────────────────────────────────────────────

class AuthLoginRequest(BaseModel):
    idToken: str

@app.post("/api/auth/login")
async def auth_login(request: AuthLoginRequest):
    """Verify a Firebase ID token and start the machine heartbeat."""
    decoded = firebase_client.verify_id_token(request.idToken)
    if decoded is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    uid = decoded.get('uid', 'unknown')
    email = decoded.get('email', 'unknown')

    # Register machine and start heartbeat
    machine_module.start_heartbeat(uid)
    queue_manager.start_polling(on_new_job=_auto_accept_job)

    return {
        "uid": uid,
        "email": email,
        "machine_id": machine_module.get_machine_id(),
        "machine_name": machine_module.get_display_name(),
    }


@app.post("/api/auth/logout")
async def auth_logout(user: CurrentUser = Depends(require_auth)):
    """Log out: stop heartbeat and polling."""
    queue_manager.stop_polling()
    machine_module.stop_heartbeat()
    return {"status": "logged_out"}


@app.get("/api/auth/me")
async def auth_me(user: CurrentUser = Depends(require_auth)):
    """Get current authenticated user info."""
    display_name = firebase_client.get_user_display_name(user.uid)
    return {
        "uid": user.uid,
        "email": user.email,
        "name": display_name or user.name,
    }

# ── Machine ──────────────────────────────────────────────────

@app.get("/api/machine/status")
async def machine_status(user: CurrentUser = Depends(require_auth)):
    """Get this machine's status."""
    return {
        "machine_id": machine_module.get_machine_id(),
        "display_name": machine_module.get_display_name(),
        "capabilities": machine_module.get_machine_capabilities(),
        "busy": queue_manager.is_busy(),
        "current_job": queue_manager.get_current_job(),
    }


class MachineSettingsRequest(BaseModel):
    displayName: Optional[str] = None
    autoImportCamera: Optional[bool] = None
    openViewerWhenDone: Optional[bool] = None

@app.put("/api/machine/settings")
async def update_machine_settings(
    request: MachineSettingsRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Update machine settings."""
    if request.displayName:
        machine_module.set_display_name(request.displayName)
    return {"status": "updated"}

# ── Job Queue ────────────────────────────────────────────────

@app.get("/api/jobs/queue")
async def get_job_queue(user: CurrentUser = Depends(require_auth)):
    """Get all queued jobs assigned to this machine, plus all local-folder
    projects. Served from in-memory cache populated by the background poll
    thread — no synchronous Firestore call on the event loop."""
    cache = queue_manager.get_queue_cache()
    jobs             = list(cache.get("jobs", []))
    local_folder_jobs = cache.get("local_jobs", [])
    current = queue_manager.get_current_job()
    # Merge: queued jobs first, then any local-folder projects not already listed
    queued_ids = {j.get('docId') or j.get('id') for j in jobs}
    for lj in local_folder_jobs:
        if (lj.get('docId') or lj.get('id')) not in queued_ids:
            jobs.append(lj)
    return {
        "queued": jobs,
        "current": current,
        "busy": queue_manager.is_busy(),
    }


class AcceptJobRequest(BaseModel):
    jobId: str

@app.post("/api/jobs/accept")
async def accept_job(
    request: AcceptJobRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Accept a queued job for processing."""
    success = queue_manager.accept_job(request.jobId)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to accept job")
    return {"status": "accepted", "jobId": request.jobId}


class QueueForProcessingRequest(BaseModel):
    userJobId: str
    jobName: Optional[str] = None

@app.post("/api/jobs/queue-for-processing")
async def queue_for_processing(
    request: QueueForProcessingRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Queue a field job for SplatPipe processing on this machine."""
    import datetime

    job_data = firebase_client.get_user_job(user.uid, request.userJobId)
    client_name = ''
    job_date = None
    if job_data:
        client_name = job_data.get('clientName', '')
        job_date = job_data.get('startTime')

    if request.jobName:
        display_name = request.jobName
    else:
        try:
            if job_date is not None:
                if hasattr(job_date, 'strftime'):
                    date_str = job_date.strftime('%Y-%m-%d')
                else:
                    date_str = datetime.datetime.utcfromtimestamp(int(job_date) / 1000).strftime('%Y-%m-%d')
                display_name = f"{client_name} — {date_str}" if client_name else date_str
            else:
                display_name = client_name or 'Field Job'
        except Exception:
            display_name = client_name or 'Field Job'

    db = firebase_client.get_db()
    new_ref = db.collection('processing_queue').document()
    new_ref.set({
        'assignedMachine': machine_module.get_machine_id(),
        'status': 'queued',
        'jobType': 'fieldraven',
        'userJobId': request.userJobId,
        'userId': user.uid,
        'clientName': client_name,
        'jobDate': job_date,
        'name': display_name,
        'createdAt': datetime.datetime.utcnow(),
        'settings': {},
    })

    doc_id = new_ref.id
    job_dir = JOBS_DIR / doc_id / "import from camera"
    job_dir.mkdir(parents=True, exist_ok=True)

    return {"processingJobId": doc_id, "status": "queued", "name": display_name}


def _derive_session_times(dest_dir: Path, site_date: str = "", site_time: str = "") -> tuple:
    """Derive (startMs, endMs) UTC epoch millis for Garmin/Coros activity matching,
    for jobs with no mobile-app session record (From Camera, image-folder, and
    video local-import flows all call this).

    Prefers the actual EXIF DateTimeOriginal span across the imported image files
    (a real measured capture window) over the user-entered date/time (a guess).
    Insta360 .insp files carry a reliable DateTimeOriginal but never real GPS
    (confirmed empirically — GPS EXIF is always zeroed on this camera); plain
    photo formats from the image-folder import flow may carry both, but only
    DateTimeOriginal is used here for consistency. Video files have no per-frame
    EXIF to scan, so this glob simply finds nothing for that flow and falls
    through to the site_date/site_time fallback below.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    timestamps: list[_dt] = []
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        _exts = ("*.insp", "*.jpg", "*.jpeg", "*.tif", "*.tiff", "*.png")
        _files = sorted(f for ext in _exts for f in dest_dir.glob(ext))
        for f in _files:
            try:
                exif = Image.open(f)._getexif() or {}
                for tag_id, val in exif.items():
                    if TAGS.get(tag_id) == "DateTimeOriginal":
                        timestamps.append(_dt.strptime(val, "%Y:%m:%d %H:%M:%S"))
                        break
            except Exception:
                continue
    except Exception:
        pass

    local_tz = _dt.now().astimezone().tzinfo

    if timestamps:
        start = min(timestamps).replace(tzinfo=local_tz).astimezone(_tz.utc)
        end   = max(timestamps).replace(tzinfo=local_tz).astimezone(_tz.utc)
        # Photos only mark 360-camera stops during a hike, not the whole hike —
        # pad generously so the Garmin/Coros overlap match has a realistic window.
        pad = _td(hours=2)
        return int((start - pad).timestamp() * 1000), int((end + pad).timestamp() * 1000)

    # No EXIF timestamps at all (e.g. an all-.insv video job) — fall back to the
    # manually entered date/time as a single anchor with a wider window, since
    # it's an approximate guess rather than a measured span.
    if site_date:
        try:
            anchor_naive = _dt.strptime(f"{site_date} {site_time or '12:00'}", "%Y-%m-%d %H:%M")
            anchor = anchor_naive.replace(tzinfo=local_tz).astimezone(_tz.utc)
            pad = _td(hours=3)
            return int((anchor - pad).timestamp() * 1000), int((anchor + pad).timestamp() * 1000)
        except ValueError:
            pass

    return None, None


async def _write_session_meta(
    project_dir: Path, dest_dir: Path, doc_id: str,
    site_date: str = "", site_time: str = "",
    lat: Optional[float] = None, lon: Optional[float] = None,
) -> None:
    """Shared by create-from-files, import-folder, and create-video: derive
    session_times (for Garmin/Coros auto-fetch matching) and save a manually
    picked map location (fallback captureLocation when there's no real GPS),
    both written to fieldraven.json so _get_session_times()/_write_capture_location()
    in pipeline_runner.py pick them up automatically at publish time — no
    further wiring needed per call site."""
    import asyncio as _asyncio
    from . import pipeline_runner

    try:
        s_ms, e_ms = await _asyncio.get_event_loop().run_in_executor(
            None, _derive_session_times, dest_dir, site_date, site_time
        )
        if s_ms and e_ms:
            pipeline_runner._write_stage_progress(project_dir, "session_times", {
                "startMs": s_ms, "endMs": e_ms,
            })
            print(f"  [session] Derived session_times for {doc_id} ({project_dir.name})")
    except Exception as e:
        print(f"  [session] Could not derive session_times: {e}")

    if lat is not None and lon is not None:
        try:
            pipeline_runner._write_stage_progress(project_dir, "manual_location", {
                "lat": lat, "lon": lon,
            })
            print(f"  [location] Saved manually-picked location for {doc_id}: {lat:.5f}, {lon:.5f}")
        except Exception as e:
            print(f"  [location] Could not save manual location: {e}")


class CreateFromFilesRequest(BaseModel):
    filePaths: list[str]
    projectDir: str
    name: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    siteDate: Optional[str] = None
    siteTime: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

@app.post("/api/jobs/create-from-files")
async def create_from_files(
    request: CreateFromFilesRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Create a pipeline job from manually selected camera files.
    Creates the Firestore doc immediately (status='importing') so the UI can
    show progress, then copies files in a background thread before setting
    status='queued'."""
    import datetime, shutil as _shutil, asyncio as _asyncio

    display_name = request.name or Path(request.projectDir).name or 'Camera Import'
    project_dir  = Path(request.projectDir)
    dest_dir     = project_dir / "import from camera"

    db = firebase_client.get_db()
    new_ref = db.collection('processing_queue').document()
    new_ref.set({
        'assignedMachine': machine_module.get_machine_id(),
        'userId':          user.uid,
        'status':          'importing',
        'jobType':         'local_folder',
        'name':            display_name,
        'projectDir':      str(project_dir),
        'createdAt':       datetime.datetime.utcnow(),
        'settings':        {},
        'location':        request.location or '',
        'notes':           request.notes or '',
        'siteDate':        request.siteDate or '',
        'siteTime':        request.siteTime or '',
    })
    doc_id = new_ref.id

    file_paths  = list(request.filePaths)
    total_files = len(file_paths)

    def _do_copy():
        dest_dir.mkdir(parents=True, exist_ok=True)
        imported = skipped = errors = 0
        for i, path_str in enumerate(file_paths, 1):
            src = Path(path_str)
            dst = dest_dir / src.name
            if dst.exists():
                skipped += 1
            else:
                try:
                    _shutil.copy2(str(src), str(dst))
                    imported += 1
                except Exception as e:
                    errors += 1
                    print(f"  Copy failed {src.name}: {e}")
            queue_manager.update_job_progress(
                doc_id, 0,
                f"Copying {i}/{total_files}: {src.name}",
            )
        new_ref.update({'status': 'queued'})
        return imported, skipped, errors

    imported, skipped, errors = await _asyncio.get_event_loop().run_in_executor(None, _do_copy)

    await _write_session_meta(project_dir, dest_dir, doc_id,
                               request.siteDate or "", request.siteTime or "",
                               request.lat, request.lon)

    return {
        'processingJobId': doc_id,
        'name':            display_name,
        'imported':        imported,
        'skipped':         skipped,
        'errors':          errors,
    }


class CreateLocalJobRequest(BaseModel):
    name: Optional[str] = None
    projectDir: str
    location: Optional[str] = None
    notes: Optional[str] = None
    siteDate: Optional[str] = None
    siteTime: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

@app.post("/api/jobs/create-local")
async def create_local_job(
    request: CreateLocalJobRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Create a processing-queue job for a local folder of photos — no field job involved."""
    import datetime

    display_name = request.name or Path(request.projectDir).name or 'Imported Photos'

    db = firebase_client.get_db()
    new_ref = db.collection('processing_queue').document()
    new_ref.set({
        'assignedMachine': machine_module.get_machine_id(),
        'status': 'queued',
        'jobType': 'local_folder',  # distinguishes from real field jobs in the UI queue
        'name': display_name,
        'projectDir': request.projectDir,
        'createdAt': datetime.datetime.utcnow(),
        'settings': {},
        'location': request.location or '',
        'notes': request.notes or '',
        'siteDate': request.siteDate or '',
        'siteTime': request.siteTime or '',
    })

    return {"processingJobId": new_ref.id, "status": "queued", "name": display_name}


class CreateVideoJobRequest(BaseModel):
    projectDir: str
    videoPath: str
    name: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    siteDate: Optional[str] = None
    siteTime: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

@app.post("/api/jobs/create-video")
async def create_video_job(
    request: CreateVideoJobRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Create a processing-queue job for a local video file.
    The video is copied into <projectDir>/import from camera/ so the pipeline
    can find it via _find_primary_input and trigger frame extraction as stage 1."""
    import datetime, shutil as _shutil

    video_path = Path(request.videoPath)
    project_dir = Path(request.projectDir)

    input_dir = project_dir / "import from camera"
    input_dir.mkdir(parents=True, exist_ok=True)

    dest = input_dir / video_path.name
    if not dest.exists():
        import asyncio as _asyncio
        await _asyncio.get_event_loop().run_in_executor(
            None, lambda: _shutil.copy2(str(video_path), str(dest))
        )

    display_name = request.name or video_path.stem

    db = firebase_client.get_db()
    new_ref = db.collection('processing_queue').document()
    new_ref.set({
        'assignedMachine': machine_module.get_machine_id(),
        'status': 'queued',
        'jobType': 'local_video',
        'name': display_name,
        'projectDir': str(project_dir),
        'videoFile': video_path.name,
        'createdAt': datetime.datetime.utcnow(),
        'settings': {},
        'location': request.location or '',
        'notes': request.notes or '',
        'siteDate': request.siteDate or '',
        'siteTime': request.siteTime or '',
    })

    # No per-frame EXIF to scan for a single video file — _derive_session_times
    # finds nothing under input_dir and falls straight through to the
    # site_date/site_time fallback, same as any other flow with no EXIF hits.
    await _write_session_meta(project_dir, input_dir, new_ref.id,
                               request.siteDate or "", request.siteTime or "",
                               request.lat, request.lon)

    return {"processingJobId": new_ref.id, "status": "queued", "name": display_name}


@app.delete("/api/jobs/{job_id}")
async def delete_job(
    job_id: str,
    user: CurrentUser = Depends(require_auth),
):
    """Permanently delete a local processing-queue job (local_folder or local_video).
    Cancels any running pipeline thread first, then removes the Firestore doc."""
    pipeline_runner.cancel(job_id)
    firebase_client.get_processing_queue().document(job_id).delete()
    return {"status": "deleted"}


@app.get("/api/jobs/{job_id}/status")
async def get_job_status(
    job_id: str,
    user: CurrentUser = Depends(require_auth),
):
    """Get current status of a specific job."""
    status_data = queue_manager.get_job_status(job_id)
    if status_data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return status_data


@app.get("/api/jobs/history")
async def get_job_history(user: CurrentUser = Depends(require_auth)):
    """Get all processing jobs for this machine."""
    jobs = queue_manager.get_all_jobs()
    return {"jobs": jobs}


@app.post("/api/jobs/{job_id}/start")
async def start_job(
    job_id: str,
    raw_request: Request,
    user: CurrentUser = Depends(require_auth),
):
    """Start the SplatPipe pipeline for an accepted job."""
    if pipeline_runner.is_running(job_id):
        raise HTTPException(status_code=409, detail="Pipeline already running for this job")
    job_data = queue_manager.get_job_status(job_id)
    if not job_data:
        raise HTTPException(status_code=404, detail="Job not found")
    # Read UI settings from request body (snake_case keys, string values)
    try:
        ui_settings = await raw_request.json()
    except Exception:
        ui_settings = {}
    if ui_settings:
        job_data = {**job_data, "_ui_settings": ui_settings}
        print(f"📋 UI settings: run_vggt={ui_settings.get('run_vggt')} run_brush={ui_settings.get('run_brush')} yaw_steps={ui_settings.get('yaw_steps')}")
    started = pipeline_runner.start(job_id, job_data)
    if not started:
        raise HTTPException(status_code=409, detail="Could not start pipeline")
    return {"status": "started", "jobId": job_id}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user: CurrentUser = Depends(require_auth),
):
    """Cancel a running or queued job."""
    pipeline_runner.cancel(job_id)
    queue_manager.fail_job(job_id, "Cancelled by user")
    return {"status": "cancelled"}


@app.get("/api/config")
async def get_config(user: CurrentUser = Depends(require_auth)):
    """Get all SplatPipe pipeline settings from the shared INI file."""
    return splat_config.get_all()


@app.put("/api/config")
async def update_config(
    request: Dict[str, Any] = Body(...),
    user: CurrentUser = Depends(require_auth),
):
    """Save pipeline settings back to the shared SplatPipe INI file."""
    splat_config.save(request)
    return {"status": "saved", "config": splat_config.get_all()}


@app.get("/api/jobs/{job_id}/output/glb")
async def get_job_glb(
    job_id: str,
    user: CurrentUser = Depends(require_auth),
):
    """Serve the VGGT scene GLB output file."""
    glb = pipeline_runner.find_output_glb(job_id)
    if not glb:
        raise HTTPException(status_code=404, detail="GLB output not found for this job")
    return FileResponse(str(glb), media_type="model/gltf-binary", filename="vggt_scene.glb")

# ── User field jobs ──────────────────────────────────────────

@app.get("/api/user-jobs")
async def get_user_jobs(
    limit: int = 100,
    user: CurrentUser = Depends(require_auth),
):
    """List field jobs from users/{uid}/jobs, most recent first."""
    try:
        from google.cloud.firestore import Query
        docs = (
            firebase_client.get_user_collection(user.uid, 'jobs')
            .order_by('startTime', direction=Query.DESCENDING)
            .limit(limit)
            .get()
        )
        jobs = []
        for doc in docs:
            data = doc.to_dict() or {}
            data['id'] = doc.id
            photos = data.pop('photos', None) or []
            data['photoCount'] = len(photos)
            for k, v in list(data.items()):
                if hasattr(v, 'isoformat'):
                    data[k] = v.isoformat()
                elif hasattr(v, '_seconds'):
                    data[k] = v._seconds * 1000
            jobs.append(data)
        return {"jobs": jobs}
    except Exception as e:
        print(f"⚠️ get_user_jobs failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ── Camera ───────────────────────────────────────────────────

# File types to import from a confirmed Insta360 camera drive
_CAMERA_EXTS = {'.insp', '.insv', '.jpg', '.jpeg', '.png', '.dng'}
# Insta360-specific extensions — presence of either confirms this is an Insta360 drive
_INSTA_EXTS  = {'.insp', '.insv'}
# Low-res proxy files — skip, not needed for processing
_SKIP_EXTS   = {'.lrv', '.thm'}


def _scan_dcim(drive_root: str) -> list[dict]:
    """Scan only the DCIM folder of an Insta360 camera drive.
    Returns empty list if no DCIM folder exists or no Insta360 files (.insp/.insv) are found
    — this prevents misidentifying any USB drive with photos as a camera."""
    dcim = os.path.join(drive_root, "DCIM")
    if not os.path.isdir(dcim):
        return []
    files = []
    for root, _dirs, names in os.walk(dcim):
        for name in sorted(names):
            ext = os.path.splitext(name)[1].lower()
            if ext in _SKIP_EXTS:
                continue
            if ext in _CAMERA_EXTS:
                full = os.path.join(root, name)
                try:
                    size = os.path.getsize(full)
                    files.append({"name": name, "path": full, "size": size, "ext": ext})
                except OSError:
                    pass
    # Only treat this as a camera drive if it contains Insta360-specific files
    if not any(f["ext"] in _INSTA_EXTS for f in files):
        return []
    return files


@app.get("/api/camera/status")
async def camera_status(user: CurrentUser = Depends(require_auth)):
    """Check if camera/media is connected and list importable files."""
    import ctypes
    import string

    camera_drive = None
    camera_files: list[dict] = []
    all_drives: list[str] = []

    # Use Windows GetLogicalDrives bitmask to enumerate all present drives
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase[3:]:  # skip A B C
            if bitmask & (1 << (ord(letter) - ord('A'))):
                all_drives.append(f"{letter}:\\")
    except Exception:
        # Fallback: scan common letters
        all_drives = [f"{l}:\\" for l in 'DEFGHIJKLMNOPQRSTUVWXYZ']

    for drive in all_drives:
        if not os.path.exists(drive):
            continue
        files = _scan_dcim(drive)
        if files:
            camera_drive = drive
            camera_files = files
            break  # use first drive that has camera files

    total_bytes = sum(f["size"] for f in camera_files)
    video_count = sum(1 for f in camera_files if f["ext"] == ".insv")
    photo_count = sum(1 for f in camera_files if f["ext"] in (".insp", ".jpg", ".jpeg", ".png", ".dng"))

    return {
        "camera_connected": camera_drive is not None,
        "camera_drive":     camera_drive,
        "camera_type":      "Insta360" if camera_drive else None,
        "file_count":       photo_count,   # photos only — what the user cares about
        "video_count":      video_count,
        "photo_count":      photo_count,
        "total_bytes":      total_bytes,
        "files":            camera_files[:100],  # cap for JSON size
    }


def _tk_browse_folder(initial: str, title: str = "Select or create a project folder (click Make New Folder to create one)") -> Optional[str]:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost', True)
    path = filedialog.askdirectory(
        initialdir=initial,
        title=title,
    )
    root.destroy()
    return path.replace('/', '\\') if path else None


def _tk_browse_files(initial: str) -> list[str]:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost', True)
    paths = filedialog.askopenfilenames(
        initialdir=initial,
        title="Select Insta360 camera files to import",
        filetypes=[
            ("Camera files", "*.insp *.insv *.jpg *.jpeg *.png *.dng"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return [p.replace('/', '\\') for p in paths]


def _tk_browse_file(initial: str, filetypes: list) -> Optional[str]:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost', True)
    path = filedialog.askopenfilename(
        initialdir=initial,
        title="Open project file",
        filetypes=filetypes,
    )
    root.destroy()
    return path.replace('/', '\\') if path else None


def _tk_browse_video(initial: str) -> Optional[str]:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost', True)
    path = filedialog.askopenfilename(
        initialdir=initial,
        title="Select video file",
        filetypes=[
            ("Video files", "*.mp4 *.mov *.avi *.mkv *.insv"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return path.replace('/', '\\') if path else None


@app.get("/api/browse/file")
async def browse_file(initial: str = "C:\\Users", type: str = "json"):
    """Open a single-file picker dialog; returns selected path or null."""
    if type == "json":
        ft = [("FieldRaven project", "fieldraven.json"), ("JSON files", "*.json"), ("All files", "*.*")]
    else:
        ft = [("All files", "*.*")]
    path = await asyncio.get_event_loop().run_in_executor(None, _tk_browse_file, initial, ft)
    return {"path": path}


@app.get("/api/browse/folder")
async def browse_folder(initial: str = "C:\\Users", title: Optional[str] = None):
    """Open a folder-picker dialog; returns selected path or null."""
    import functools
    fn = functools.partial(_tk_browse_folder, initial, title) if title else functools.partial(_tk_browse_folder, initial)
    path = await asyncio.get_event_loop().run_in_executor(None, fn)
    return {"path": path}


@app.get("/api/browse/video")
async def browse_video(initial: str = "C:\\Users"):
    """Open a video file-picker dialog; returns selected path or null."""
    # Auto-navigate into DCIM subfolder if it exists (e.g. when initial is a
    # camera's mounted drive root) -- same convenience as /api/browse/files.
    dcim = os.path.join(initial.rstrip("\\"), "DCIM")
    start_dir = dcim if os.path.isdir(dcim) else initial
    path = await asyncio.get_event_loop().run_in_executor(None, _tk_browse_video, start_dir)
    return {"path": path}


@app.get("/api/browse/files")
async def browse_files(initial: str = "D:\\"):
    """Open a multi-file picker starting in DCIM; returns selected paths."""
    # Auto-navigate into DCIM subfolder if it exists
    dcim = os.path.join(initial.rstrip("\\"), "DCIM")
    start_dir = dcim if os.path.isdir(dcim) else initial
    paths = await asyncio.get_event_loop().run_in_executor(None, _tk_browse_files, start_dir)
    return {"paths": paths}


@app.post("/api/browse/open-folder")
async def open_folder_in_explorer(
    request: Request,
    user: CurrentUser = Depends(require_auth),
):
    """Open a directory in Windows Explorer (fire-and-forget)."""
    body = await request.json()
    path = body.get("path", "")
    if path and os.path.isdir(path):
        import subprocess as _sp
        _sp.Popen(["explorer", os.path.normpath(path)])
    return {"ok": True}


class CameraImportRequest(BaseModel):
    jobId: str
    sourceDrive: str  # e.g. "J:\\"
    projectDir: Optional[str] = None  # user-selected project root

@app.post("/api/camera/import")
async def import_from_camera(
    request: CameraImportRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Import only the camera files associated with this job from the drive."""
    import shutil as _shutil
    source     = request.sourceDrive
    job_id     = request.jobId
    project_dir = Path(request.projectDir) if request.projectDir else None

    if not project_dir:
        raise HTTPException(status_code=400, detail="projectDir is required")

    dest_dir = project_dir / "import from camera"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Persist projectDir to the processing queue doc
    try:
        firebase_client.get_processing_queue().document(job_id).update(
            {"projectDir": str(project_dir)}
        )
    except Exception as e:
        print(f"⚠️ Could not save projectDir to Firestore: {e}")

    # Resolve which filenames belong to this job via the field job in Firestore.
    # Also build a GPS map: {filename_stem: {lat, lon}} for files that have GPS.
    target_filenames: set[str] | None = None
    gps_by_stem: dict[str, dict] = {}
    try:
        pq_doc = firebase_client.get_processing_queue().document(job_id).get()
        if pq_doc.exists:
            pq_data = pq_doc.to_dict()
            user_job_id = pq_data.get('userJobId')
            user_id     = pq_data.get('userId')
            if user_job_id and user_id:
                job_data = firebase_client.get_user_job(user_id, user_job_id)
                if job_data:
                    photos = job_data.get('photos', [])
                    names = [p.get('cameraFileName') for p in photos if p.get('cameraFileName')]
                    if names:
                        target_filenames = set(names)
                        print(f"📷 Job has {len(target_filenames)} recorded filenames")
                    for p in photos:
                        fn = p.get('cameraFileName')
                        gps = p.get('gps')
                        if fn and gps and gps.get('lat') is not None and gps.get('lon') is not None:
                            stem = Path(fn).stem
                            gps_by_stem[stem] = {'lat': float(gps['lat']), 'lon': float(gps['lon']),
                                                 'alt': float(gps.get('alt', 0))}
                    if gps_by_stem:
                        print(f"📍 GPS available for {len(gps_by_stem)}/{len(photos)} photos")
    except Exception as e:
        print(f"⚠️ Could not fetch job filenames from Firestore: {e}")

    if not target_filenames:
        # No cameraFileName on photos — ask the user to pick files manually
        print("⚠️ No filename list found — requesting manual file selection")
        return {
            "imported": 0, "skipped": 0, "errors": 0,
            "needsManualSelect": True,
            "cameraDrive": source,
            "jobId": job_id,
        }

    # Scan the camera drive and match
    all_files = _scan_dcim(source)
    files_to_copy = [f for f in all_files if f['name'] in target_filenames]
    print(f"📷 Matched {len(files_to_copy)}/{len(all_files)} files on camera drive")

    if not files_to_copy:
        return {"imported": 0, "errors": 0, "skipped": 0,
                "destination": str(dest_dir), "jobId": job_id}

    imported = 0
    skipped  = 0
    errors   = 0
    total    = len(files_to_copy)

    queue_manager.update_job_progress(job_id, 1, f"Starting import of {total} files…")

    attempted: list[dict] = []   # files we actually tried to copy this run
    for i, f in enumerate(files_to_copy):
        dst = dest_dir / f["name"]
        if dst.exists():
            skipped += 1
            continue
        attempted.append(f)
        try:
            _shutil.copy2(f["path"], str(dst))
            imported += 1
            pct = int((i + 1) / total * 30)  # 0-30% progress
            queue_manager.update_job_progress(
                job_id, pct,
                f"Copied {imported}/{total}: {f['name']}"
            )
        except Exception as exc:
            errors += 1
            print(f"⚠️ Import error {f['name']}: {exc}")

    # ── Validate transfer: retry any file that is missing or has the wrong size ──
    import time as _time
    retry_transfer = [
        f for f in attempted
        if not (dest_dir / f["name"]).exists()
        or (dest_dir / f["name"]).stat().st_size != f["size"]
    ]
    if retry_transfer:
        queue_manager.update_job_progress(
            job_id, 30, f"Validating — {len(retry_transfer)} file(s) incomplete, retrying…"
        )
        print(f"🔄 Retrying {len(retry_transfer)} incomplete transfers")
        for attempt in range(2):
            still_bad: list[dict] = []
            for f in retry_transfer:
                dst = dest_dir / f["name"]
                if dst.exists():
                    dst.unlink()
                try:
                    _shutil.copy2(f["path"], str(dst))
                    if dst.stat().st_size == f["size"]:
                        imported += 1
                        errors   -= 1
                        print(f"  ✅ Retry succeeded: {f['name']}")
                    else:
                        dst.unlink()
                        still_bad.append(f)
                except Exception as exc2:
                    print(f"  ⚠️ Retry {attempt+1} failed for {f['name']}: {exc2}")
                    still_bad.append(f)
            retry_transfer = still_bad
            if not retry_transfer:
                break
            if attempt < 1:
                _time.sleep(1)
        errors = len(retry_transfer)
        if retry_transfer:
            print(f"❌ {len(retry_transfer)} file(s) could not be transferred after retries")

    queue_manager.update_job_progress(
        job_id, 30,
        f"Import complete — {imported} copied, {skipped} skipped"
        + (f", {errors} failed" if errors else "")
    )

    # Write GPS sidecars alongside each imported file so the pipeline can use
    # position priors without a second Firestore lookup.
    # File: <dest_dir>/<stem>.gps.json  →  {"lat": ..., "lon": ..., "alt": ...}
    gps_written = 0
    if gps_by_stem:
        for f in dest_dir.iterdir():
            if f.is_file() and f.stem in gps_by_stem:
                sidecar = dest_dir / f"{f.stem}.gps.json"
                if not sidecar.exists():
                    sidecar.write_text(json.dumps(gps_by_stem[f.stem]), encoding="utf-8")
                    gps_written += 1
        if gps_written:
            print(f"📍 Wrote {gps_written} GPS sidecar files")

    return {
        "imported":    imported,
        "skipped":     skipped,
        "errors":      errors,
        "destination": str(dest_dir),
        "jobId":       job_id,
        "gpsWritten":  gps_written,
    }


class ManualImportRequest(BaseModel):
    jobId: str
    filePaths: list[str]
    projectDir: str

@app.post("/api/camera/import-manual")
async def import_manual(
    request: ManualImportRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Copy a user-selected list of files into the project's input directory."""
    import shutil as _shutil
    job_id      = request.jobId
    project_dir = Path(request.projectDir)
    dest_dir    = project_dir / "import from camera"
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        firebase_client.get_processing_queue().document(job_id).update(
            {"projectDir": str(project_dir)}
        )
    except Exception as e:
        print(f"⚠️ Could not save projectDir: {e}")

    imported = skipped = errors = 0
    error_details: list[str] = []
    total = len(request.filePaths)
    queue_manager.update_job_progress(job_id, 1, f"Starting manual import of {total} files…")

    for i, src_path in enumerate(request.filePaths):
        dst = dest_dir / Path(src_path).name
        if dst.exists():
            skipped += 1
            continue
        try:
            _shutil.copy2(src_path, str(dst))
            imported += 1
            pct = int((i + 1) / total * 30)
            queue_manager.update_job_progress(
                job_id, pct, f"Copied {imported}/{total}: {Path(src_path).name}"
            )
        except Exception as exc:
            errors += 1
            msg = f"{Path(src_path).name}: {exc}"
            error_details.append(msg)
            print(f"⚠️ Manual import error {msg}")

    if imported == 0 and errors > 0:
        detail = f"All {errors} file copies failed. First error: {error_details[0]}"
        raise HTTPException(status_code=500, detail=detail)

    queue_manager.update_job_progress(
        job_id, 30, f"Import complete — {imported} copied, {skipped} skipped"
    )
    return {
        "imported": imported, "skipped": skipped, "errors": errors,
        "errorDetails": error_details[:5],
        "destination": str(dest_dir), "jobId": job_id,
    }


class ImportFolderRequest(BaseModel):
    jobId: Optional[str] = None
    projectDir: str
    sourceFolder: str
    siteDate: Optional[str] = None
    siteTime: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

_IMPORT_FOLDER_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'}

@app.post("/api/project/import-folder")
async def import_folder(
    request: ImportFolderRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Copy photos from an arbitrary local folder into <projectDir>/imported photos/.
    Entry point for projects that already have images on disk (not a field job import,
    not a camera import) -- e.g. picture files already sitting on the user's hard drive.
    Only plain picture formats are copied (no .insp/.insv/.dng -- those are camera-raw
    formats handled by the camera-import flow, not this one). Only the top level of the
    source folder is scanned (no subfolders), so camera-generated thumbnail/cache
    subfolders never get swept in alongside the real photos.
    pipeline_runner._input_dir() resolves either "import from camera" or
    "imported photos" so every other endpoint (gallery, thumbnails, stitching) finds
    these files regardless of which import path was used."""
    import shutil as _shutil
    source_dir  = Path(request.sourceFolder)
    project_dir = Path(request.projectDir)
    if not source_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Source folder not found: {source_dir}")

    dest_dir = project_dir / "imported photos"
    dest_dir.mkdir(parents=True, exist_ok=True)

    if request.jobId:
        try:
            firebase_client.get_processing_queue().document(request.jobId).update(
                {"projectDir": str(project_dir)}
            )
        except Exception as e:
            print(f"⚠️ Could not save projectDir: {e}")

    files = sorted(
        f for f in source_dir.glob("*")
        if f.is_file() and f.suffix.lower() in _IMPORT_FOLDER_EXTS
    )
    total = len(files)
    if total == 0:
        return {"imported": 0, "skipped": 0, "errors": 0, "destination": str(dest_dir)}

    imported = skipped = errors = 0
    if request.jobId:
        queue_manager.update_job_progress(request.jobId, 1, f"Starting import of {total} files…")

    for i, f in enumerate(files):
        dst = dest_dir / f.name
        if dst.exists():
            skipped += 1
            continue
        try:
            _shutil.copy2(str(f), str(dst))
            imported += 1
            if request.jobId:
                pct = int((i + 1) / total * 30)
                queue_manager.update_job_progress(request.jobId, pct, f"Copied {imported}/{total}: {f.name}")
        except Exception as exc:
            errors += 1
            print(f"⚠️ Import error {f.name}: {exc}")

    if request.jobId:
        queue_manager.update_job_progress(request.jobId, 30, f"Import complete — {imported} copied, {skipped} skipped")
        await _write_session_meta(project_dir, dest_dir, request.jobId,
                                   request.siteDate or "", request.siteTime or "",
                                   request.lat, request.lon)

    return {
        "imported": imported, "skipped": skipped, "errors": errors,
        "destination": str(dest_dir),
    }


# ── Project config ───────────────────────────────────────────

class ProjectConfigWriteRequest(BaseModel):
    dir: str
    jobId: str
    settings: Optional[Dict[str, Any]] = None

@app.get("/api/project/config")
async def read_project_config(
    dir: str,
    user: CurrentUser = Depends(require_auth),
):
    """Return the fieldraven.json config + current file list for a project directory."""
    project_dir = Path(dir)
    config_path = project_dir / "fieldraven.json"

    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    input_dir = pipeline_runner._input_dir(project_dir)
    files: list[dict] = []
    if input_dir.exists():
        for f in sorted(input_dir.iterdir()):
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() not in {".lrv", ".thm"}:
                files.append({"name": f.name, "size": f.stat().st_size, "ext": f.suffix.lower()})

    def _count_files(d: Path, exts: set) -> int:
        if not d.exists(): return 0
        return sum(1 for f in d.rglob("*") if f.is_file() and f.suffix.lower() in exts)

    img_exts = {".jpg", ".jpeg", ".png"}
    stages = {
        "frames":   {"count": _count_files(project_dir / "01_frames",   img_exts)},
        "views":    {"count": _count_files(project_dir / "02_views",    img_exts)},
        "training": {
            "postshot": (project_dir / "04_training" / "postshot_input").exists(),
            "brush":    (project_dir / "04_training" / "brush_input").exists(),
            "vggt":     (project_dir / "04_training" / "vggt_output").exists(),
        },
    }

    return {"config": config, "files": files, "total": len(files),
            "path": str(input_dir), "stages": stages}


@app.post("/api/project/config")
async def write_project_config(
    request: ProjectConfigWriteRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Write or update fieldraven.json in the project directory."""
    project_dir = Path(request.dir)
    config_path = project_dir / "fieldraven.json"

    # Preserve any existing fields (e.g. createdAt) and update
    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    config = {
        **existing,
        "version": 1,
        "jobId": request.jobId,
        "savedAt": datetime.now().isoformat(timespec="seconds"),
    }
    if "createdAt" not in config:
        config["createdAt"] = config["savedAt"]
    if request.settings is not None:
        config["settings"] = {**existing.get("settings", {}), **request.settings}

    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Persist projectDir to the processing_queue Firestore doc so the
    # pipeline worker can find the files when it runs.
    # Use set(merge=True) so the doc is recreated if it was deleted.
    if request.jobId:
        try:
            doc_ref = firebase_client.get_processing_queue().document(request.jobId)
            # This endpoint is called generically for every job type (a debounced
            # autosave effect in App.jsx fires it for whatever job is currently
            # selected, video or folder). It used to hardcode jobType="local_folder"
            # unconditionally, which silently reclassified freshly-created video
            # jobs as folder jobs within moments of selecting them. Preserve
            # whatever jobType is already set; only default it for a genuinely
            # new doc that has none yet.
            existing_doc = doc_ref.get()
            existing_data = existing_doc.to_dict() if existing_doc.exists else {}
            update_data = {
                "projectDir":      str(project_dir),
                "assignedMachine": machine_module.get_machine_id(),
                "name":            request.dir.split("\\")[-1].split("/")[-1] or "Project",
            }
            if not existing_data.get("jobType"):
                update_data["jobType"] = "local_folder"
            doc_ref.set(update_data, merge=True)
        except Exception as e:
            print(f"⚠️ Could not save projectDir to Firestore: {e}")

    return {"ok": True}


# ── Project state / prepare / resume ──────────────────────────

@app.get("/api/project/state")
async def get_project_state(
    dir: str,
    user: CurrentUser = Depends(require_auth),
):
    """Scan a project directory and return actual pipeline stage completion status."""
    project_dir = Path(dir)
    if not project_dir.exists():
        return {"found": False}

    json_path = project_dir / "fieldraven.json"
    saved: dict = {}
    if json_path.exists():
        try:
            saved = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    _img_exts = {".jpg", ".jpeg", ".png"}

    def _has_images(d: Path) -> bool:
        return d.exists() and any(f.is_file() and f.suffix.lower() in _img_exts for f in d.rglob("*"))

    def _count_images(d: Path) -> int:
        if not d.exists():
            return 0
        return sum(1 for f in d.rglob("*") if f.is_file() and f.suffix.lower() in _img_exts)

    # Import stage: stitched/imported photos in "import from camera" or "imported photos"
    import_dir    = pipeline_runner._input_dir(project_dir)
    stitched_count = sum(
        1 for f in import_dir.iterdir()
        if f.is_file() and f.suffix.lower() in _img_exts
    ) if import_dir.exists() else 0

    # View extraction: 02_views
    views_dir   = project_dir / "02_views"
    views_done  = _has_images(views_dir)
    views_count = _count_images(views_dir)

    # RealityScan: 03_alignment/COLMAP_for_Brush must have COLMAP text files
    colmap_dir   = project_dir / "03_alignment" / "COLMAP_for_Brush"
    colmap_items = list(colmap_dir.iterdir()) if colmap_dir.exists() else []
    rs_done      = any(f.name.lower() in ("cameras.txt", "images.txt") for f in colmap_items)
    rs_images    = sum(1 for f in colmap_items if f.suffix.lower() == ".png")

    # Brush training: 04_training/*.ply
    training_dir = project_dir / "04_training"
    ply_files    = list(training_dir.glob("*.ply")) if training_dir.exists() else []
    brush_done   = len(ply_files) > 0

    saved_stages = saved.get("stages", {})
    stages = {
        "import": {
            "done":        stitched_count > 0,
            "stitched":    stitched_count,
            "completedAt": saved_stages.get("import", {}).get("completedAt"),
        },
        "view_extraction": {
            "done":        views_done,
            "views":       views_count,
            "completedAt": saved_stages.get("view_extraction", {}).get("completedAt"),
        },
        "realityscan": {
            "done":        rs_done,
            "images":      rs_images,
            "completedAt": saved_stages.get("realityscan", {}).get("completedAt"),
        },
        "brush_training": {
            "done":        brush_done,
            "plyFiles":    len(ply_files),
            "completedAt": saved_stages.get("brush_training", {}).get("completedAt"),
        },
    }

    def _b(v, default=False):
        if isinstance(v, bool): return v
        return str(v).lower() in ("true", "1", "yes") if v is not None else default

    saved_settings = saved.get("settings", {})
    run_colmap  = _b(saved_settings.get("run_colmap"),  False)
    run_vggt    = _b(saved_settings.get("run_vggt"),    False)
    run_gluemap = _b(saved_settings.get("run_gluemap"), False)
    run_rigsfm  = _b(saved_settings.get("run_rigsfm"),  False)
    run_equisfm = _b(saved_settings.get("run_equisfm"), False)

    if run_colmap:
        pipeline_mode = "colmap"
    elif run_vggt:
        pipeline_mode = "vggt"
    elif run_gluemap:
        pipeline_mode = "gluemap"
    elif run_rigsfm:
        pipeline_mode = "rigsfm"
    elif run_equisfm:
        pipeline_mode = "equisfm"
    else:
        pipeline_mode = "rs_brush"

    # COLMAP alignment: sparse reconstruction in 03_alignment/colmap/sparse_txt
    colmap_sparse = project_dir / "03_alignment" / "colmap" / "sparse_txt"
    colmap_done   = colmap_sparse.exists() and any(
        (colmap_sparse / f).exists() for f in ("cameras.txt", "images.txt")
    )
    colmap_cameras = sum(1 for _ in colmap_sparse.glob("*.txt")) if colmap_sparse.exists() else 0

    # VGGT alignment: output in 04_training/vggt_output
    vggt_dir  = project_dir / "04_training" / "vggt_output"
    vggt_done = vggt_dir.exists() and _has_images(vggt_dir)

    # GlueMap alignment: GluMap writes .bin files into output/gluemap_aba/ or output/coarse/
    # and the pipeline copies them to 04_training/brush_input/
    gluemap_dir     = project_dir / "03_alignment" / "gluemap"
    gluemap_out     = gluemap_dir / "output"
    brush_input_dir = project_dir / "04_training" / "brush_input"
    _gluemap_bin    = ("cameras.bin", "images.bin")
    gluemap_done = gluemap_out.exists() and (
        any((gluemap_out / sub / f).exists()
            for sub in ("gluemap_aba", "coarse") for f in _gluemap_bin)
        or any((brush_input_dir / f).exists() for f in _gluemap_bin)
    )

    # RigGluemap alignment: worker writes sparse_txt to 03_alignment/rigsfm/sparse_txt
    rigsfm_sparse = project_dir / "03_alignment" / "rigsfm" / "sparse_txt"
    rigsfm_done   = rigsfm_sparse.exists() and any(
        (rigsfm_sparse / f).exists() for f in ("cameras.txt", "images.txt", "cameras.bin", "images.bin")
    )

    # EquiSfM alignment: runner writes sensor_sparse_txt to 03_alignment/equisfm/sensor_sparse_txt
    equisfm_sparse = project_dir / "03_alignment" / "equisfm" / "sensor_sparse_txt"
    equisfm_done   = equisfm_sparse.exists() and any(
        (equisfm_sparse / f).exists() for f in ("cameras.txt", "images.txt")
    )

    stages["colmap_alignment"] = {
        "done":        colmap_done,
        "cameras":     colmap_cameras,
        "completedAt": saved_stages.get("colmap_alignment", {}).get("completedAt"),
    }
    stages["vggt_alignment"] = {
        "done":        vggt_done,
        "completedAt": saved_stages.get("vggt_alignment", {}).get("completedAt"),
    }
    stages["gluemap_alignment"] = {
        "done":        gluemap_done,
        "completedAt": saved_stages.get("gluemap_alignment", {}).get("completedAt"),
    }
    stages["rigsfm_alignment"] = {
        "done":        rigsfm_done,
        "completedAt": saved_stages.get("rigsfm_alignment", {}).get("completedAt"),
    }
    stages["equisfm_alignment"] = {
        "done":        equisfm_done,
        "completedAt": saved_stages.get("equisfm_alignment", {}).get("completedAt"),
    }

    if pipeline_mode == "colmap":
        stage_order = ["import", "view_extraction", "colmap_alignment", "brush_training"]
    elif pipeline_mode == "vggt":
        stage_order = ["import", "view_extraction", "vggt_alignment", "brush_training"]
    elif pipeline_mode == "gluemap":
        stage_order = ["import", "view_extraction", "gluemap_alignment", "brush_training"]
    elif pipeline_mode == "rigsfm":
        stage_order = ["import", "view_extraction", "rigsfm_alignment", "brush_training"]
    elif pipeline_mode == "equisfm":
        stage_order = ["import", "view_extraction", "equisfm_alignment", "brush_training"]
    else:
        stage_order = ["import", "view_extraction", "realityscan", "brush_training"]

    completed_stages: list[str] = []
    next_stage: Optional[str]   = None
    for s in stage_order:
        if stages[s]["done"]:
            completed_stages.append(s)
        elif next_stage is None:
            next_stage = s

    return {
        "found":           True,
        "projectDir":      str(project_dir),
        "jobId":           saved.get("jobId"),
        "settings":        saved_settings,
        "stages":          stages,
        "nextStage":       next_stage,
        "completedStages": completed_stages,
        "pipelineMode":    pipeline_mode,
        "hasHistory":      len(completed_stages) > 0,
    }


class ProjectPrepareRequest(BaseModel):
    dir: str
    startFrom: str  # "view_extraction" | "realityscan" | "brush_training"


_STAGE_DIRS_TO_DELETE: dict[str, list[str]] = {
    "view_extraction":   ["02_views", "03_alignment", "04_training"],
    "realityscan":       ["03_alignment", "04_training"],
    "colmap_alignment":  ["03_alignment", "04_training"],
    "vggt_alignment":    ["03_alignment", "04_training"],
    "gluemap_alignment": ["03_alignment", "04_training"],
    "rigsfm_alignment":  ["03_alignment", "04_training"],
    "equisfm_alignment": ["03_alignment", "04_training"],
    "brush_training":    ["04_training"],
}


@app.post("/api/project/prepare")
async def prepare_project_rerun(
    request: ProjectPrepareRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Delete stage output directories from startFrom onwards (cascade)."""
    import shutil as _shutil
    project_dir  = Path(request.dir)
    dirs_to_wipe = _STAGE_DIRS_TO_DELETE.get(request.startFrom, [])
    deleted = []
    errors  = []
    for name in dirs_to_wipe:
        p = project_dir / name
        if p.exists():
            try:
                _shutil.rmtree(str(p))
                deleted.append(name)
                print(f"[prepare] Deleted {p}")
            except PermissionError as exc:
                # Windows: files locked by a running process (COLMAP db, .ply, etc.)
                msg = f"Could not delete {name}: {exc} — close any running processes and retry"
                errors.append(msg)
                print(f"⚠️ [prepare] {msg}")
            except Exception as exc:
                msg = f"Could not delete {name}: {exc}"
                errors.append(msg)
                print(f"⚠️ [prepare] {msg}")
    if errors and not deleted:
        raise HTTPException(status_code=500, detail="; ".join(errors))
    return {"deleted": deleted, "startFrom": request.startFrom, "errors": errors}


class ProjectResumeRequest(BaseModel):
    dir:       str
    startFrom: str
    jobId:     Optional[str] = None
    settings:  Optional[Dict[str, Any]] = None


@app.post("/api/project/resume")
async def resume_project(
    request: ProjectResumeRequest,
    user:    CurrentUser = Depends(require_auth),
):
    """Resume an existing project from a specific stage without re-queuing."""
    project_dir = Path(request.dir)
    if not project_dir.exists():
        raise HTTPException(status_code=400, detail="Project directory not found")

    # Read fieldraven.json for jobId and settings
    saved: dict = {}
    json_path = project_dir / "fieldraven.json"
    if json_path.exists():
        try:
            saved = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    job_id = request.jobId or saved.get("jobId")
    if not job_id:
        raise HTTPException(status_code=400, detail="No jobId — project was never queued")

    # Prefer settings sent by the UI (always current) over saved settings (may be stale)
    saved_settings = request.settings if request.settings is not None else saved.get("settings", {})

    # Reset the Firestore doc to processing state so the frontend can track it
    # accept_job also sets queue_manager._current_job_id
    queue_manager.accept_job(job_id)

    # Read userId / userJobId — try Firestore queue doc first, then fieldraven.json fallback.
    # Mobile-app-created queue docs may not have these fields; fieldraven.json always will
    # (written at job start by _worker) for any subsequent resume.
    _user_id     = None
    _user_job_id = None
    try:
        q_snap = firebase_client.get_processing_queue().document(job_id).get()
        if q_snap.exists:
            q_data = q_snap.to_dict() or {}
            _user_id     = q_data.get("userId") or q_data.get("user_id")
            _user_job_id = q_data.get("userJobId") or q_data.get("user_job_id")
    except Exception:
        pass
    if not _user_id or not _user_job_id:
        try:
            import json as _json
            _fj_path = project_dir / "fieldraven.json"
            if _fj_path.exists():
                _fj = _json.loads(_fj_path.read_text(encoding="utf-8"))
                _ids = _fj.get("mobile_ids", {})
                _user_id     = _user_id     or _ids.get("userId")
                _user_job_id = _user_job_id or _ids.get("userJobId")
        except Exception:
            pass

    # Also persist projectDir on the doc so pipeline_runner can find files
    try:
        firebase_client.get_processing_queue().document(job_id).update(
            {"projectDir": str(project_dir)}
        )
    except Exception as e:
        print(f"⚠️  Could not set projectDir on Firestore doc: {e}")

    # Build job_data — _ui_settings carries highest-priority overrides including start_from
    job_data = {
        "projectDir": str(project_dir),
        "settings":   {},
        "userId":     _user_id,
        "userJobId":  _user_job_id,
        "_ui_settings": {
            "run_vggt":          saved_settings.get("run_vggt", False),
            "run_brush":         saved_settings.get("run_brush", True),
            "run_postshot":      saved_settings.get("run_postshot", False),
            "skip_realityscan":  saved_settings.get("skip_realityscan", True),
            "run_colmap":        saved_settings.get("run_colmap", False),
            "colmap_mode":       saved_settings.get("colmap_mode", "rig"),
            "colmap_matcher":    saved_settings.get("colmap_matcher", "sequential"),
            "colmap_visualize":  saved_settings.get("colmap_visualize", False),
            "run_gluemap":                saved_settings.get("run_gluemap", False),
            "gluemap_backbone":           saved_settings.get("gluemap_backbone", "pi3"),
            "gluemap_skip_doppelgangers": saved_settings.get("gluemap_skip_doppelgangers", True),
            "gluemap_coarse_only":        saved_settings.get("gluemap_coarse_only", False),
            "gluemap_is_sequential":      saved_settings.get("gluemap_is_sequential", True),
            "gluemap_num_neighbors":      saved_settings.get("gluemap_num_neighbors", 100),
            "gluemap_batch_size":         saved_settings.get("gluemap_batch_size", 60),
            "gluemap_num_track_per_img":  saved_settings.get("gluemap_num_track_per_img", 512),
            "gluemap_wsl_home":           saved_settings.get("gluemap_wsl_home", ""),
            "gluemap_wsl_distro":         saved_settings.get("gluemap_wsl_distro", ""),
            "run_rigsfm":                 saved_settings.get("run_rigsfm", False),
            "rigsfm_anchor_sensor":       saved_settings.get("rigsfm_anchor_sensor", 0),
            "rigsfm_matcher":             saved_settings.get("rigsfm_matcher", "sequential"),
            "run_equisfm":                saved_settings.get("run_equisfm", False),
            "equisfm_matcher":            saved_settings.get("equisfm_matcher", "sequential"),
            "equisfm_triangulate":        saved_settings.get("equisfm_triangulate", False),
            "export_xmp":          saved_settings.get("export_xmp", False),
            "brush_rerun_logging": saved_settings.get("brush_rerun_logging", False),
            "gps_priors_rs":       saved_settings.get("gps_priors_rs", False),
            "gps_priors_colmap": saved_settings.get("gps_priors_colmap", False),
            "yaw_steps":         saved_settings.get("yaw_steps", 6),
            "pitch_angles_str":  saved_settings.get("pitch_angles_str", "-7"),
            "fov":               saved_settings.get("fov", 94.6),
            "horizon_ref":       saved_settings.get("horizon_ref", True),
            "extraction_method": saved_settings.get("extraction_method", "interval"),
            "interval_value":    saved_settings.get("interval_value", 1.0),
            "interval_unit":     saved_settings.get("interval_unit", "seconds"),
            "start_from":        request.startFrom,
        },
    }

    started = pipeline_runner.start(job_id, job_data)
    if not started:
        raise HTTPException(status_code=409, detail="Pipeline already running for this job")

    return {"ok": True, "jobId": job_id, "startFrom": request.startFrom}


@app.get("/api/jobs/{job_id}/files")
async def list_job_files(
    job_id: str,
    projectDir: Optional[str] = None,
    user: CurrentUser = Depends(require_auth),
):
    """List files in a job's input directory."""
    if projectDir:
        input_dir = pipeline_runner._input_dir(Path(projectDir))
    else:
        # Fall back to Firestore lookup then hardcoded path
        input_dir = None
        try:
            doc = firebase_client.get_processing_queue().document(job_id).get()
            if doc.exists:
                pd = doc.to_dict().get('projectDir')
                if pd:
                    input_dir = pipeline_runner._input_dir(Path(pd))
        except Exception:
            pass
        if input_dir is None:
            input_dir = pipeline_runner._input_dir(JOBS_DIR / job_id)
    if not input_dir.exists():
        return {"files": [], "total": 0}

    files = []
    for f in sorted(input_dir.iterdir()):
        if f.is_file():
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "ext": f.suffix.lower(),
            })

    return {
        "files": files,
        "total": len(files),
        "path": str(input_dir),
    }

@app.post("/api/jobs/{job_id}/stitch")
async def stitch_job(
    job_id: str,
    user: CurrentUser = Depends(require_auth),
):
    """Convert any .insp photos or .insv videos in the job's input dir to
    equirectangular output (background thread). Photos take priority if a
    job somehow has both; the pipeline's own synchronous pre-check
    (backend/pipeline_runner.py's _worker) is the real correctness guarantee
    regardless of whether this endpoint ever ran -- this is just the
    immediate-after-import head start, same as the existing .insp flow."""
    import threading
    from . import pipeline_runner

    try:
        doc = firebase_client.get_processing_queue().document(job_id).get()
        job_data = doc.to_dict() if doc.exists else {}
    except Exception:
        job_data = {}

    input_dir = pipeline_runner._input_dir(pipeline_runner._job_root(job_id, job_data))
    insp_files = list(input_dir.glob("*.insp")) if input_dir.exists() else []
    insv_files = list(input_dir.glob("*.insv")) if input_dir.exists() else []
    if not insp_files and not insv_files:
        return {"total": 0, "message": "No .insp/.insv files to convert"}

    is_video = not insp_files and bool(insv_files)
    total = len(insv_files) if is_video else len(insp_files)
    cancel = threading.Event()

    def _run():
        try:
            if is_video:
                count = pipeline_runner._stitch_insv_files(job_id, cancel, job_data)
                queue_manager.update_job_progress(
                    job_id, 50, f"Converted {count}/{total} .insv video(s) to equirectangular"
                )
            else:
                count = pipeline_runner._stitch_insp_files(job_id, cancel, job_data)
                queue_manager.update_job_progress(
                    job_id, 50, f"Converted {count}/{total} .insp files to equirectangular"
                )
        except Exception as e:
            queue_manager.update_job_progress(job_id, 0, f"Conversion failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    kind = ".insv video(s)" if is_video else ".insp files"
    queue_manager.update_job_progress(job_id, 31, f"Converting {total} {kind}…")
    return {"total": total, "message": f"Converting {total} {kind} in background"}


@app.get("/api/jobs/{job_id}/input/{filename}")
async def serve_input_file(
    job_id: str,
    filename: str,
    projectDir: Optional[str] = None,
    thumb: bool = False,
):
    """Serve a file from the job input directory for in-app preview. No auth — localhost only."""
    if projectDir:
        # projectDir in the URL is authoritative — skip the Firestore read entirely
        # (saves a round-trip per image, critical when 25+ thumbnails load in parallel)
        job_data: dict = {"projectDir": projectDir}
    else:
        try:
            doc = firebase_client.get_processing_queue().document(job_id).get()
            job_data = doc.to_dict() if doc.exists else {}
        except Exception:
            job_data = {}

    input_dir = pipeline_runner._input_dir(pipeline_runner._job_root(job_id, job_data))
    file_path = (input_dir / filename).resolve()

    if not str(file_path).startswith(str(input_dir.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # The Insta360 stitcher writes equirectangular JPEGs in a background thread
    # while the gallery may already be polling/loading them. Reading mid-write
    # doesn't raise -- PIL and browser JPEG decoders both tolerantly render
    # whatever complete scanlines exist so far (valid top rows, black below)
    # rather than erroring, so this previously slipped through as a normal
    # 200 response and got permanently cached (server .thumbs/ cache AND the
    # browser's own image cache) as a corrupt-but-valid-looking thumbnail/
    # preview. A real JPEG always ends with the EOI marker (0xFFD9); checking
    # for it lets us reject a mid-write read with an error instead, which
    # triggers the frontend's existing onerror retry-with-cache-bust logic
    # (already implemented for both the canvas preview and the thumbnail
    # row) rather than silently serving/caching bad data.
    def _is_complete_jpeg(p: Path) -> bool:
        try:
            with open(p, "rb") as fh:
                fh.seek(-2, 2)
                return fh.read(2) == b"\xff\xd9"
        except OSError:
            return False

    _ext = file_path.suffix.lower()
    if _ext in ('.jpg', '.jpeg') and not _is_complete_jpeg(file_path):
        raise HTTPException(status_code=503, detail="File still being written — retry shortly")

    # Serve a small disk-cached thumbnail so the gallery doesn't load full 6-12K images
    if thumb and file_path.suffix.lower() in ('.jpg', '.jpeg', '.png'):
        thumb_dir = input_dir / ".thumbs"
        thumb_path = (thumb_dir / filename).resolve()
        if thumb_path.exists() and thumb_path.stat().st_size < 500:
            # Cached thumbnail is suspiciously small (corrupt/black) — regenerate it
            thumb_path.unlink(missing_ok=True)
        if not thumb_path.exists():
            try:
                from PIL import Image as _PILImage
                thumb_dir.mkdir(exist_ok=True)
                with _PILImage.open(str(file_path)) as img:
                    if img.mode not in ('RGB', 'L'):
                        img = img.convert('RGB')
                    img.thumbnail((600, 600), _PILImage.LANCZOS)
                    img.save(str(thumb_path), "JPEG", quality=80, optimize=True)
            except Exception as e:
                print(f"⚠️ Thumb failed for {filename}: {e}")
        if thumb_path.exists():
            return FileResponse(str(thumb_path), media_type="image/jpeg")

    # Read into memory before sending — avoids Content-Length mismatch when the
    # Insta360 stitcher is still writing the JPEG concurrently with this request.
    ext = file_path.suffix.lower()
    if ext in ('.jpg', '.jpeg', '.png', '.webp'):
        try:
            file_bytes = file_path.read_bytes()
        except OSError:
            raise HTTPException(status_code=404, detail="File not found")
        media_type = 'image/jpeg' if ext in ('.jpg', '.jpeg') else f'image/{ext[1:]}'
        return Response(content=file_bytes, media_type=media_type)

    return FileResponse(str(file_path))


# ── Video preview endpoints ───────────────────────────────────
# Uses OpenCV VideoCapture (same approach as GPU_video_extraction.py in V13):
#   - video-info: instant cap props, no subprocess
#   - preview-frame: seek + single decode + in-memory imencode, no temp file,
#     optional CUDA decode, orders of magnitude faster than spawning ffmpeg per frame

def _resolve_video_path(job_id: str) -> dict:
    """Shared helper: look up job data from Firestore for video preview endpoints."""
    try:
        doc = firebase_client.get_processing_queue().document(job_id).get()
        return doc.to_dict() if doc.exists else {}
    except Exception:
        return {}


def _get_cv2_backend():
    """Return (cv2, cuda_available) — lazy import so server still starts if cv2 is missing."""
    try:
        import cv2 as _cv2
        cuda_ok = (
            hasattr(_cv2, 'cuda') and
            _cv2.cuda.getCudaEnabledDeviceCount() > 0
        )
        return _cv2, cuda_ok
    except ImportError:
        return None, False


@app.get("/api/jobs/{job_id}/video-info")
async def get_video_info_for_job(job_id: str):
    """Return duration/fps/dimensions for a local_video job (no auth — localhost only).
    Uses OpenCV VideoCapture to read container headers instantly — no subprocess."""
    job_data = _resolve_video_path(job_id)

    video_file = job_data.get('videoFile')
    if not video_file:
        raise HTTPException(status_code=404, detail="No video file for this job")

    input_dir = pipeline_runner._input_dir(pipeline_runner._job_root(job_id, job_data))
    video_path = (input_dir / video_file).resolve()

    if not str(video_path).startswith(str(input_dir.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    cv2, _cuda = _get_cv2_backend()
    if cv2 is None:
        raise HTTPException(status_code=500, detail="cv2 not available")

    def _read_info() -> dict:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return {}
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            duration = n_frames / fps if fps > 0 else 0.0
        finally:
            cap.release()

        # Some containers (e.g. .insv) report 0 frames via CAP_PROP_FRAME_COUNT —
        # fall back to ffprobe for duration only in that case
        if duration <= 0:
            import subprocess as _sp, json as _json, shutil as _shutil
            ffprobe = _shutil.which('ffprobe') or 'ffprobe'
            _NO_WIN = {'creationflags': 0x08000000} if os.name == 'nt' else {}
            try:
                r = _sp.run(
                    [ffprobe, '-v', 'quiet', '-print_format', 'json',
                     '-show_format', str(video_path)],
                    capture_output=True, text=True, timeout=10, **_NO_WIN,
                )
                fmt = _json.loads(r.stdout).get('format', {})
                duration = float(fmt.get('duration', 0))
                if fps > 0 and duration > 0:
                    n_frames = int(duration * fps)
            except Exception:
                pass

        if duration <= 0:
            return {}
        return {'duration': round(duration, 3), 'fps': round(fps, 3),
                'width': w, 'height': h, 'frame_count': n_frames}

    info = await asyncio.get_event_loop().run_in_executor(None, _read_info)
    if not info:
        raise HTTPException(status_code=500, detail="Cannot open video")
    return info


@app.get("/api/jobs/{job_id}/preview-frame")
async def preview_frame(job_id: str, timestamp: float = 0.0):
    """Extract a single JPEG from the job video at the given timestamp (no auth — localhost only).
    Uses OpenCV seek + decode + in-memory imencode — no subprocess spawn, no temp file.
    Falls back to ffmpeg subprocess if OpenCV cannot open the file (e.g. unsupported codec)."""
    job_data = _resolve_video_path(job_id)

    video_file = job_data.get('videoFile')
    if not video_file:
        raise HTTPException(status_code=404, detail="No video file for this job")

    input_dir = pipeline_runner._input_dir(pipeline_runner._job_root(job_id, job_data))
    video_path = (input_dir / video_file).resolve()

    if not str(video_path).startswith(str(input_dir.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    timestamp = max(0.0, timestamp)
    cv2, cuda_ok = _get_cv2_backend()

    def _extract_opencv() -> bytes | None:
        if cv2 is None:
            return None
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return None
        try:
            # Seek to timestamp via milliseconds (nearest keyframe for compressed video)
            cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
            ret, frame = cap.read()
            if not ret or frame is None:
                return None
            # Scale down to max 1920px wide while keeping aspect ratio
            h, w = frame.shape[:2]
            if w > 1920:
                scale = 1920.0 / w
                new_w, new_h = int(w * scale), int(h * scale)
                interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
                if cuda_ok:
                    try:
                        gpu = cv2.cuda_GpuMat()
                        gpu.upload(frame)
                        gpu = cv2.cuda.resize(gpu, (new_w, new_h), interpolation=interp)
                        frame = gpu.download()
                    except Exception:
                        frame = cv2.resize(frame, (new_w, new_h), interpolation=interp)
                else:
                    frame = cv2.resize(frame, (new_w, new_h), interpolation=interp)
            ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return buf.tobytes() if ok else None
        finally:
            cap.release()

    def _extract_ffmpeg_fallback() -> bytes | None:
        import subprocess as _sp, tempfile as _tmp, shutil as _shutil
        ffmpeg = _shutil.which('ffmpeg') or 'ffmpeg'
        _NO_WIN = {'creationflags': 0x08000000} if os.name == 'nt' else {}
        tmp = _tmp.NamedTemporaryFile(suffix='.jpg', delete=False)
        tmp.close()
        try:
            _sp.run(
                [ffmpeg, '-y', '-ss', f'{timestamp:.3f}', '-i', str(video_path),
                 '-frames:v', '1', '-vf', 'scale=1920:-2', '-q:v', '3', tmp.name],
                capture_output=True, timeout=20, **_NO_WIN,
            )
            with open(tmp.name, 'rb') as f:
                data = f.read()
            return data if data else None
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    def _extract():
        result = _extract_opencv()
        if result:
            return result
        # OpenCV couldn't open or decode — fall back to ffmpeg subprocess
        return _extract_ffmpeg_fallback()

    frame_bytes = await asyncio.get_event_loop().run_in_executor(None, _extract)
    if not frame_bytes:
        raise HTTPException(status_code=500, detail="Frame extraction failed")
    return Response(content=frame_bytes, media_type='image/jpeg')


# ── Static Maps proxy ────────────────────────────────────────

import urllib.request as _urllib_req

_STATIC_MAPS_KEY = "AIzaSyB9tm8xhFdzLDzCj88-MqQB826h0B-uQfs"

@app.get("/api/static-map")
async def static_map_proxy(lat: float, lon: float, zoom: int = 12):
    """Proxy Google Static Maps so the browser key referrer check is bypassed."""
    from fastapi.concurrency import run_in_threadpool
    url = (
        f"https://maps.googleapis.com/maps/api/staticmap"
        f"?center={lat},{lon}&zoom={zoom}&size=600x160"
        f"&maptype=terrain&markers=color:green%7C{lat},{lon}"
        f"&key={_STATIC_MAPS_KEY}"
    )
    def _fetch():
        with _urllib_req.urlopen(url, timeout=10) as r:
            return r.read(), r.headers.get("Content-Type", "image/png")
    try:
        data, ct = await run_in_threadpool(_fetch)
        return Response(content=data, media_type=ct)
    except Exception as e:
        logger.warning(f"Static map fetch failed ({lat},{lon}): {e}")
        # Return a simple SVG placeholder so the UI doesn't show a broken image
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="160">'
            f'<rect width="600" height="160" fill="#1a2235"/>'
            f'<text x="300" y="72" font-family="sans-serif" font-size="13" fill="#556" text-anchor="middle">Map unavailable</text>'
            f'<text x="300" y="92" font-family="sans-serif" font-size="11" fill="#445" text-anchor="middle">{lat:.4f}, {lon:.4f}</text>'
            f'<text x="300" y="112" font-family="sans-serif" font-size="10" fill="#334" text-anchor="middle">Enable Maps Static API in Google Cloud Console</text>'
            f'</svg>'
        )
        return Response(content=svg.encode(), media_type="image/svg+xml")


# ── Serve React frontend ─────────────────────────────────────

REACT_DIST = Path(__file__).resolve().parent.parent / "frontend-react" / "dist"


@app.get("/")
async def serve_index():
    """Serve the React app entry point — never cache so bundle updates load immediately."""
    index_path = REACT_DIST / "index.html"
    if index_path.exists():
        return FileResponse(
            str(index_path),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )
    return JSONResponse(
        status_code=503,
        content={"status": "frontend_not_built",
                 "message": "Run: cd frontend-react && npm run build"}
    )


# Vite build assets (hashed JS/CSS bundles)
if (REACT_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(REACT_DIST / "assets")), name="react_assets")

print(f"📁 Frontend served from: {REACT_DIST}")
print(f"📁 Jobs directory: {JOBS_DIR}")