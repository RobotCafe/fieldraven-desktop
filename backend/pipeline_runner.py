"""
Pipeline runner: manages one SplatPipe background thread per accepted job.
"""
import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

from . import queue_manager

JOBS_DIR = Path("C:/FieldRaven/Jobs")

VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.insv'}
IMAGE_EXTS  = {'.jpg', '.jpeg', '.png'}
INSP_EXT    = {'.insp'}

# Insta360 Media SDK — needed at runtime by insp_stitch.exe
_SDK_BIN = Path("C:/Users/DenmanNic/Projects/Windows_CameraSDK-2.1.1_MediaSDK-3.1.3/MediaSDK-3.1.3-20260128-win64_1769600100370/MediaSDK-3.1.3-20260128-win64/MediaSDK/bin")
_INSP_STITCH_EXE = Path(__file__).resolve().parent.parent / "tools" / "insp_stitch.exe"

# Maps pipeline stage → (low%, high%) of overall 0-100 progress
# Two variants: with and without a preceding stitch step
_STAGE_RANGE = {
    "frame_extraction": (5,  20),
    "view_extraction":  (20, 50),
    "vggt_alignment":   (50, 85),
    "colmap_export":    (85, 97),
}
_STAGE_RANGE_POST_STITCH = {
    "frame_extraction": (47, 57),
    "view_extraction":  (57, 75),
    "vggt_alignment":   (75, 92),
    "colmap_export":    (92, 97),
}

_cancel_events: dict[str, threading.Event] = {}
_threads:       dict[str, threading.Thread] = {}


# ── Public API ────────────────────────────────────────────────

def start(job_id: str, job_data: dict) -> bool:
    """Spawn a pipeline thread for job_id. Returns False if already running."""
    if job_id in _threads and _threads[job_id].is_alive():
        print(f"⚠️  Pipeline already running for {job_id}")
        return False
    cancel = threading.Event()
    _cancel_events[job_id] = cancel
    t = threading.Thread(
        target=_worker, args=(job_id, job_data, cancel),
        daemon=True, name=f"pipe-{job_id[:8]}"
    )
    _threads[job_id] = t
    t.start()
    print(f"🚀 Pipeline started for {job_id}")
    return True


def cancel(job_id: str) -> bool:
    """Signal the pipeline for job_id to stop gracefully."""
    ev = _cancel_events.get(job_id)
    if ev:
        ev.set()
        print(f"🛑 Cancel requested for {job_id}")
        return True
    return False


def is_running(job_id: str) -> bool:
    t = _threads.get(job_id)
    return t is not None and t.is_alive()


def find_output_glb(job_id: str) -> Optional[Path]:
    """Locate the vggt_scene.glb produced by the pipeline, or None."""
    training_dir = JOBS_DIR / job_id / "04_training"
    if not training_dir.exists():
        return None
    for sub in ["postshot_input", "brush_input", "vggt_output"]:
        glb = training_dir / sub / "vggt_scene.glb"
        if glb.exists():
            return glb
    return None


# ── Internal helpers ──────────────────────────────────────────

def _find_primary_input(job_id: str) -> Optional[str]:
    """Return path to a video file or image folder in the job's input dir."""
    input_dir = JOBS_DIR / job_id / "input"
    if not input_dir.exists():
        return None
    files = sorted(input_dir.iterdir())
    for f in files:
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            return str(f)
    if any(f.is_file() and f.suffix.lower() in IMAGE_EXTS for f in files):
        return str(input_dir)
    return None


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


def _build_settings(job_data: dict):
    """Construct PipelineSettings from the SplatPipe INI + Firebase job overrides."""
    from splatpipe_core import PipelineSettings  # type: ignore
    from . import splat_config

    cfg = splat_config.load()
    s   = PipelineSettings()

    # ── Extraction ────────────────────────────────────────────────
    s.extraction_method = cfg.get("extraction_method", s.extraction_method)
    s.interval_value    = float(cfg.get("interval_value", s.interval_value))
    s.interval_unit     = cfg.get("interval_unit", s.interval_unit)
    s.frame_count       = int(cfg.get("frame_count", s.frame_count))
    s.frame_format      = cfg.get("frame_format", s.frame_format)
    raw_pitch = cfg.get("pitch_angles_str", "")
    if raw_pitch:
        s.pitch_angles = [float(x.strip()) for x in raw_pitch.split(",") if x.strip()]
    s.yaw_steps         = int(cfg.get("yaw_steps", s.yaw_steps))
    s.fov               = float(cfg.get("fov", s.fov))

    # ── Alignment / VGGT ─────────────────────────────────────────
    s.run_vggt                    = _to_bool(cfg.get("run_vggt", s.run_vggt))
    s.run_postshot                = _to_bool(cfg.get("run_postshot", s.run_postshot))
    s.run_brush                   = _to_bool(cfg.get("run_brush", s.run_brush))
    s.conf_threshold              = float(cfg.get("vggt_conf_threshold", s.conf_threshold))
    s.mask_sky                    = _to_bool(cfg.get("vggt_mask_sky", s.mask_sky))
    s.mask_black_bg               = _to_bool(cfg.get("vggt_mask_black_bg", s.mask_black_bg))
    s.mask_white_bg               = _to_bool(cfg.get("vggt_mask_white_bg", s.mask_white_bg))
    s.prediction_mode             = cfg.get("vggt_prediction_mode", s.prediction_mode)
    s.temporal_sequencing         = _to_bool(cfg.get("vggt_temporal_sequencing", s.temporal_sequencing))
    s.enable_sparse               = _to_bool(cfg.get("vggt_enable_sparse", s.enable_sparse))
    s.sparse_target_points        = int(cfg.get("vggt_sparse_target", s.sparse_target_points))
    s.use_anchor_rig              = _to_bool(cfg.get("vggt_use_anchor_rig", s.use_anchor_rig))
    s.anchor_view                 = cfg.get("vggt_anchor_view", s.anchor_view)
    s.rig_optimization_min_points = int(cfg.get("vggt_rig_optimization_min_points", s.rig_optimization_min_points))
    s.sky_sensitivity_threshold   = int(float(cfg.get("sky_sensitivity_threshold", s.sky_sensitivity_threshold)))

    # ── Postshot ──────────────────────────────────────────────────
    s.postshot_profile          = cfg.get("postshot_profile", s.postshot_profile)
    s.postshot_max_image_size   = int(cfg.get("postshot_max_image_size", s.postshot_max_image_size))
    s.postshot_train_steps      = int(cfg.get("postshot_train_steps", s.postshot_train_steps))
    s.postshot_max_splats       = int(cfg.get("postshot_max_splats", s.postshot_max_splats))
    s.postshot_anti_aliasing    = _to_bool(cfg.get("postshot_anti_aliasing", s.postshot_anti_aliasing))
    s.postshot_show_train_error = _to_bool(cfg.get("postshot_show_train_error", s.postshot_show_train_error))
    s.postshot_store_context    = _to_bool(cfg.get("postshot_store_context", s.postshot_store_context))
    s.postshot_export_ply       = _to_bool(cfg.get("postshot_export_ply", s.postshot_export_ply))
    s.postshot_alpha_mask       = _to_bool(cfg.get("postshot_alpha_mask", s.postshot_alpha_mask))
    s.postshot_sky_model        = _to_bool(cfg.get("postshot_sky_model", s.postshot_sky_model))

    # ── Brush ─────────────────────────────────────────────────────
    s.brush_total_steps    = int(cfg.get("brush_total_steps", s.brush_total_steps))
    s.brush_max_splats     = int(cfg.get("brush_max_splats", s.brush_max_splats))
    s.brush_max_resolution = int(cfg.get("brush_max_resolution", s.brush_max_resolution))
    s.brush_seed           = int(cfg.get("brush_seed", s.brush_seed))
    s.brush_rerun_logging  = _to_bool(cfg.get("brush_rerun_logging", s.brush_rerun_logging))
    s.brush_spawn_viewer   = _to_bool(cfg.get("brush_spawn_viewer", s.brush_spawn_viewer))

    # ── Paths ─────────────────────────────────────────────────────
    s.ffmpeg_path        = cfg.get("ffmpeg_path") or s.ffmpeg_path
    s.postshot_path      = cfg.get("postshot_path") or s.postshot_path
    s.brush_path         = cfg.get("brush_path") or s.brush_path
    s.fusion2sphere_path = cfg.get("fusion2sphere_path") or s.fusion2sphere_path

    # ── Per-job Firebase overrides ────────────────────────────────
    js = job_data.get("settings") or {}
    if js.get("extractionMethod"): s.extraction_method = js["extractionMethod"]
    if js.get("intervalValue"):    s.interval_value    = float(js["intervalValue"])
    if js.get("intervalUnit"):     s.interval_unit     = js["intervalUnit"]
    if js.get("frameCount"):       s.frame_count       = int(js["frameCount"])
    if js.get("pitchAngles"):      s.pitch_angles      = [float(p) for p in js["pitchAngles"]]
    if js.get("yawSteps"):         s.yaw_steps         = int(js["yawSteps"])
    if js.get("fov"):              s.fov               = float(js["fov"])

    return s


# ── Insta360 stitch step ──────────────────────────────────────

def _stitch_insp_files(job_id: str, cancel_event: threading.Event) -> int:
    """Convert all .insp files in the job input dir to equirectangular JPEGs.
    Stitched files land in the same input dir; skips files already stitched.
    Returns number of files successfully stitched."""
    input_dir = JOBS_DIR / job_id / "input"
    insp_files = sorted(input_dir.glob("*.insp"))
    if not insp_files:
        return 0

    if not _INSP_STITCH_EXE.exists():
        raise FileNotFoundError(f"insp_stitch.exe not found at {_INSP_STITCH_EXE}. Run tools/build_insp_stitch.bat first.")

    env = os.environ.copy()
    env["PATH"] = str(_SDK_BIN) + ";" + env.get("PATH", "")

    total    = len(insp_files)
    stitched = 0

    for i, insp in enumerate(insp_files):
        if cancel_event.is_set():
            break

        out_jpg = input_dir / (insp.stem + ".jpg")
        if out_jpg.exists():
            stitched += 1
            continue

        pct = int(5 + (i / total) * 40)  # 5-45% of overall progress
        queue_manager.update_job_progress(
            job_id, pct, f"Stitching {i+1}/{total}: {insp.name}"
        )

        result = subprocess.run(
            [str(_INSP_STITCH_EXE), str(insp), str(out_jpg)],
            capture_output=True, text=True, env=env,
        )
        if result.returncode == 0:
            stitched += 1
            print(f"✅ Stitched: {insp.name}")
        else:
            print(f"⚠️ Stitch failed for {insp.name}: {result.stderr.strip()}")

    return stitched


# ── Worker thread ─────────────────────────────────────────────

def _worker(job_id: str, job_data: dict, cancel_event: threading.Event):
    try:
        queue_manager.update_job_progress(job_id, 1, "Initialising pipeline…")

        # Stitch any .insp files to equirectangular JPEGs before the pipeline runs
        input_dir = JOBS_DIR / job_id / "input"
        insp_count = len(list(input_dir.glob("*.insp"))) if input_dir.exists() else 0
        if insp_count:
            queue_manager.update_job_progress(job_id, 3, f"Found {insp_count} Insta360 files — stitching…")
            stitched = _stitch_insp_files(job_id, cancel_event)
            if cancel_event.is_set():
                queue_manager.fail_job(job_id, "Cancelled by user")
                return
            if stitched == 0:
                queue_manager.fail_job(job_id, "Stitch step produced no output — check MediaSDK setup")
                return
            queue_manager.update_job_progress(job_id, 45, f"Stitched {stitched}/{insp_count} files")

        input_path = _find_primary_input(job_id)
        if not input_path:
            queue_manager.fail_job(job_id, "No input files found in job input directory")
            return

        queue_manager.update_job_progress(job_id, 2 if not insp_count else 46, f"Input: {Path(input_path).name}")

        # Heavy ML deps are only imported here — server startup stays light
        from splatpipe_core import run_pipeline  # type: ignore

        settings = _build_settings(job_data)

        stage_map = _STAGE_RANGE_POST_STITCH if insp_count else _STAGE_RANGE

        def on_progress(sp):
            lo, hi = stage_map.get(sp.stage.value, (0, 97))
            overall = int(lo + (sp.progress / 100) * (hi - lo))
            queue_manager.update_job_progress(
                job_id, overall, sp.message[:200],
                extra={"currentStage": sp.stage.value},
            )

        result = run_pipeline(
            job_id=job_id,
            input_path=input_path,
            settings=settings,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

        if cancel_event.is_set():
            queue_manager.fail_job(job_id, "Cancelled by user")
            return

        if result.success:
            glb = find_output_glb(job_id)
            queue_manager.complete_job(
                job_id,
                output_path=result.output_dir,
                output_format="colmap" if result.colmap_dir else "views",
                preview_url=f"/api/jobs/{job_id}/output/glb" if glb else None,
            )
        else:
            queue_manager.fail_job(job_id, result.error or "Pipeline failed")

    except Exception as exc:
        import traceback
        traceback.print_exc()
        queue_manager.fail_job(job_id, f"Unexpected error: {exc}")
    finally:
        _cancel_events.pop(job_id, None)
        _threads.pop(job_id, None)
