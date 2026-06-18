"""
FastAPI application for FieldRaven Desktop.
Serves the local web dashboard and provides API endpoints
for auth, queue management, camera detection, and pipeline control.
"""
import os
import json
from pathlib import Path
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, status, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
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
    fb_status = firebase_client.check_connection()
    return {
        "status": "running",
        "firebase": fb_status,
        "machine_id": machine_module.get_machine_id(),
        "machine_name": machine_module.get_display_name(),
        "busy": queue_manager.is_busy(),
        "current_job": queue_manager.get_current_job(),
    }

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
    queue_manager.start_polling(on_new_job=None)  # Auto-accept handled by frontend

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
    """Get all queued jobs assigned to this machine."""
    jobs = queue_manager.poll_for_jobs()
    current = queue_manager.get_current_job()
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
        'userJobId': request.userJobId,
        'userId': user.uid,
        'clientName': client_name,
        'jobDate': job_date,
        'name': display_name,
        'createdAt': datetime.datetime.utcnow(),
        'settings': {},
    })

    doc_id = new_ref.id
    job_dir = JOBS_DIR / doc_id / "input"
    job_dir.mkdir(parents=True, exist_ok=True)

    return {"processingJobId": doc_id, "status": "queued", "name": display_name}


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
    user: CurrentUser = Depends(require_auth),
):
    """Start the SplatPipe pipeline for an accepted job."""
    if pipeline_runner.is_running(job_id):
        raise HTTPException(status_code=409, detail="Pipeline already running for this job")
    job_data = queue_manager.get_job_status(job_id)
    if not job_data:
        raise HTTPException(status_code=404, detail="Job not found")
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

# ── Camera ───────────────────────────────────────────────────

# File types to detect and import from camera
_CAMERA_EXTS = {'.insp', '.insv', '.jpg', '.jpeg', '.png', '.dng'}
# Low-res proxy files — skip, not needed for processing
_SKIP_EXTS   = {'.lrv', '.thm'}


def _scan_dcim(drive_root: str) -> list[dict]:
    """Walk camera files on a drive. Checks DCIM first, falls back to drive root."""
    dcim = os.path.join(drive_root, "DCIM")
    search_root = dcim if os.path.isdir(dcim) else drive_root
    files = []
    for root, _dirs, names in os.walk(search_root):
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
        "file_count":       len(camera_files),
        "video_count":      video_count,
        "photo_count":      photo_count,
        "total_bytes":      total_bytes,
        "files":            camera_files[:100],  # cap for JSON size
    }


class CameraImportRequest(BaseModel):
    jobId: str
    sourceDrive: str  # e.g. "J:\\"

@app.post("/api/camera/import")
async def import_from_camera(
    request: CameraImportRequest,
    user: CurrentUser = Depends(require_auth),
):
    """Import only the camera files associated with this job from the drive."""
    import shutil as _shutil
    source   = request.sourceDrive
    job_id   = request.jobId
    dest_dir = JOBS_DIR / job_id / "input"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Resolve which filenames belong to this job via the field job in Firestore
    target_filenames: set[str] | None = None
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
    except Exception as e:
        print(f"⚠️ Could not fetch job filenames from Firestore: {e}")

    # Scan the camera drive
    all_files = _scan_dcim(source)

    if target_filenames:
        files_to_copy = [f for f in all_files if f['name'] in target_filenames]
        print(f"📷 Matched {len(files_to_copy)}/{len(all_files)} files on camera drive")
    else:
        # No filename list available — fall back to full import with a warning
        print(f"⚠️ No filename list found — importing all {len(all_files)} camera files")
        files_to_copy = all_files

    if not files_to_copy:
        return {"imported": 0, "errors": 0, "skipped": 0,
                "destination": str(dest_dir), "jobId": job_id}

    imported = 0
    skipped  = 0
    errors   = 0
    total    = len(files_to_copy)

    queue_manager.update_job_progress(job_id, 1, f"Starting import of {total} files…")

    for i, f in enumerate(files_to_copy):
        dst = dest_dir / f["name"]
        if dst.exists():
            skipped += 1
            continue
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

    queue_manager.update_job_progress(
        job_id, 30,
        f"Import complete — {imported} files copied, {skipped} skipped"
    )

    return {
        "imported":    imported,
        "skipped":     skipped,
        "errors":      errors,
        "destination": str(dest_dir),
        "jobId":       job_id,
    }


@app.get("/api/jobs/{job_id}/files")
async def list_job_files(
    job_id: str,
    user: CurrentUser = Depends(require_auth),
):
    """List files in a job's input directory."""
    input_dir = JOBS_DIR / job_id / "input"
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

# ── Serve frontend ───────────────────────────────────────────

# Serve the frontend directory
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRONTEND_DIR.mkdir(exist_ok=True)


@app.get("/")
async def serve_index():
    """Serve the main dashboard HTML."""
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        return JSONResponse(
            status_code=200,
            content={"status": "dashboard_not_built", "message": "Frontend not yet built. Navigate to /api/ endpoints."}
        )
    return FileResponse(str(index_path))


# Mount static files (JS, CSS, images)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")

print(f"📁 Frontend served from: {FRONTEND_DIR}")
print(f"📁 Jobs directory: {JOBS_DIR}")