"""
Pipeline runner: manages one SplatPipe background thread per accepted job.
"""
import json
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import queue_manager
from . import firebase_client

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
_STAGE_RANGE_RS_BRUSH = {
    "frame_extraction": (5,  20),
    "view_extraction":  (20, 45),
    "realityscan":      (45, 75),
    "brush_training":   (75, 97),
}
_STAGE_RANGE_RS_BRUSH_POST_STITCH = {
    "frame_extraction": (47, 57),
    "view_extraction":  (57, 70),
    "realityscan":      (70, 85),
    "brush_training":   (85, 97),
}
_STAGE_RANGE_COLMAP = {
    "frame_extraction":  (5,  20),
    "view_extraction":   (20, 45),
    "colmap_alignment":  (45, 80),
    "brush_training":    (80, 97),
}
_STAGE_RANGE_COLMAP_POST_STITCH = {
    "frame_extraction":  (47, 57),
    "view_extraction":   (57, 72),
    "colmap_alignment":  (72, 88),
    "brush_training":    (88, 97),
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


def _job_root(job_id: str, job_data: Optional[dict] = None) -> Path:
    """Return the project root directory for a job (user-selected or fallback)."""
    if job_data:
        pd = job_data.get('projectDir')
        if pd:
            return Path(pd)
    return JOBS_DIR / job_id


def _input_dir(proj_root: Path) -> Path:
    """Return the project's source-photos directory: 'import from camera' (camera/cloud
    imports) or 'imported photos' (a local folder imported via /api/project/import-folder),
    whichever exists. Defaults to 'import from camera' when neither exists yet."""
    camera_dir = proj_root / "import from camera"
    if camera_dir.exists():
        return camera_dir
    manual_dir = proj_root / "imported photos"
    if manual_dir.exists():
        return manual_dir
    return camera_dir


def _write_stage_progress(project_dir: Path, stage: str, data: dict) -> None:
    """Write a stage completion record into fieldraven.json."""
    config_path = project_dir / "fieldraven.json"
    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    stages = config.setdefault("stages", {})
    stages[stage] = {**data, "completedAt": datetime.now().isoformat(timespec="seconds")}
    config["savedAt"] = datetime.now().isoformat(timespec="seconds")
    try:
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠️  Could not write fieldraven.json: {e}")


def find_output_glb(job_id: str, job_data: Optional[dict] = None) -> Optional[Path]:
    """Locate the vggt_scene.glb produced by the pipeline, or None."""
    training_dir = _job_root(job_id, job_data) / "04_training"
    if not training_dir.exists():
        return None
    for sub in ["postshot_input", "brush_input", "vggt_output"]:
        glb = training_dir / sub / "vggt_scene.glb"
        if glb.exists():
            return glb
    return None


# ── Internal helpers ──────────────────────────────────────────

def _find_primary_input(job_id: str, job_data: Optional[dict] = None) -> Optional[str]:
    """Return path to a video file or image folder in the job's input dir."""
    input_dir = _input_dir(_job_root(job_id, job_data))
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

    cfg = dict(splat_config.load())
    s = PipelineSettings()

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
    s.horizon_ref       = _to_bool(cfg.get("horizon_ref", s.horizon_ref))

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
    s.use_rig_xmp                 = _to_bool(cfg.get("export_xmp", s.use_rig_xmp))
    s.run_colmap                  = _to_bool(cfg.get("run_colmap", s.run_colmap))
    s.colmap_mode                 = cfg.get("colmap_mode", s.colmap_mode)
    s.colmap_matcher              = cfg.get("colmap_matcher", s.colmap_matcher)
    s.colmap_visualize            = _to_bool(cfg.get("colmap_visualize", s.colmap_visualize))
    s.colmap_correct_pitch        = _to_bool(cfg.get("colmap_correct_pitch", s.colmap_correct_pitch))
    s.colmap_correct_translation  = _to_bool(cfg.get("colmap_correct_translation", s.colmap_correct_translation))
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

    # ── Brush export ──────────────────────────────────────────────
    s.brush_export_every = int(cfg.get("brush_export_every", s.brush_export_every))

    # ── Paths ─────────────────────────────────────────────────────
    s.ffmpeg_path      = cfg.get("ffmpeg_path") or s.ffmpeg_path
    s.postshot_path    = cfg.get("postshot_path") or s.postshot_path
    s.brush_path       = cfg.get("brush_path") or s.brush_path
    s.rs_path          = cfg.get("rs_path") or s.rs_path
    s.rs_settings_path = cfg.get("rs_settings_path") or s.rs_settings_path
    s.colmap_bin       = cfg.get("colmap_bin") or s.colmap_bin

    # ── Per-job Firestore overrides (from processing_queue.settings) ─
    js = job_data.get("settings") or {}
    if js.get("extractionMethod"): s.extraction_method = js["extractionMethod"]
    if js.get("intervalValue"):    s.interval_value    = float(js["intervalValue"])
    if js.get("intervalUnit"):     s.interval_unit     = js["intervalUnit"]
    if js.get("frameCount"):       s.frame_count       = int(js["frameCount"])
    if js.get("pitchAngles"):
        raw = js["pitchAngles"]
        if isinstance(raw, str):
            s.pitch_angles = [float(p.strip()) for p in raw.split(",") if p.strip()]
        else:
            s.pitch_angles = [float(p) for p in raw]
    if js.get("yawSteps"):         s.yaw_steps         = int(js["yawSteps"])
    if js.get("fov"):              s.fov               = float(js["fov"])
    if "run_vggt" in js:           s.run_vggt          = _to_bool(js["run_vggt"])
    if "run_brush" in js:          s.run_brush         = _to_bool(js["run_brush"])

    # ── UI settings (highest priority — always applied last) ──────
    ui = job_data.get("_ui_settings") or {}
    if ui:
        if "run_vggt" in ui:          s.run_vggt          = _to_bool(ui["run_vggt"])
        if "run_brush" in ui:         s.run_brush         = _to_bool(ui["run_brush"])
        if "run_postshot" in ui:      s.run_postshot      = _to_bool(ui["run_postshot"])
        if "export_xmp" in ui:        s.use_rig_xmp       = _to_bool(ui["export_xmp"])
        if "run_colmap" in ui:        s.run_colmap        = _to_bool(ui["run_colmap"])
        if "colmap_mode" in ui:       s.colmap_mode       = ui["colmap_mode"]
        if "colmap_matcher" in ui:    s.colmap_matcher    = ui["colmap_matcher"]
        if "colmap_visualize" in ui:    s.colmap_visualize    = _to_bool(ui["colmap_visualize"])
        if "colmap_correct_pitch" in ui:       s.colmap_correct_pitch       = _to_bool(ui["colmap_correct_pitch"])
        if "colmap_correct_translation" in ui: s.colmap_correct_translation = _to_bool(ui["colmap_correct_translation"])
        if "yaw_steps" in ui:         s.yaw_steps         = int(ui["yaw_steps"])
        if "fov" in ui:               s.fov               = float(ui["fov"])
        if "horizon_ref" in ui:       s.horizon_ref       = _to_bool(ui["horizon_ref"])
        if "pitch_angles_str" in ui:
            raw = ui["pitch_angles_str"]
            s.pitch_angles = [float(x.strip()) for x in raw.split(",") if x.strip() and float(x.strip()) != 0]
        if "extraction_method" in ui: s.extraction_method = ui["extraction_method"]
        if "interval_value" in ui:    s.interval_value    = float(ui["interval_value"])
        if "interval_unit" in ui:     s.interval_unit     = ui["interval_unit"]
        if "frame_count" in ui:       s.frame_count       = int(ui["frame_count"])

    return s


# ── Insta360 stitch step ──────────────────────────────────────

def _stitch_insp_files(job_id: str, cancel_event: threading.Event, job_data: Optional[dict] = None) -> int:
    """Convert all .insp files in the job input dir to equirectangular JPEGs.
    Stitched files land in the same input dir; skips files already stitched.
    Returns number of files successfully stitched."""
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    input_dir = _input_dir(_job_root(job_id, job_data))
    insp_files = sorted(input_dir.glob("*.insp"))
    if not insp_files:
        return 0

    if not _INSP_STITCH_EXE.exists():
        raise FileNotFoundError(f"insp_stitch.exe not found at {_INSP_STITCH_EXE}. Run tools/build_insp_stitch.bat first.")

    env = os.environ.copy()
    env["PATH"] = str(_SDK_BIN) + ";" + env.get("PATH", "")

    from . import splat_config
    cfg = splat_config.load()
    extra_args: list[str] = []

    # Output resolution (height = width / 2 for 2:1 equirectangular)
    out_w_str = cfg.get("insp_output_width", "")
    if out_w_str:
        try:
            w = int(out_w_str)
            extra_args += ["--width", str(w), "--height", str(w // 2)]
        except ValueError:
            pass

    stitch_type = cfg.get("insp_stitch_type", "template")
    if stitch_type and stitch_type != "template":
        extra_args += ["--stitch-type", stitch_type]
    lens_guard = cfg.get("insp_lens_guard", "none")
    if lens_guard and lens_guard != "none":
        extra_args += ["--lens-guard", lens_guard]
    if _to_bool(cfg.get("insp_flowstate", "false")):
        extra_args.append("--flowstate")
    use_cuda = _to_bool(cfg.get("insp_cuda", "false"))
    if use_cuda:
        extra_args.append("--cuda")

    workers = max(1, int(cfg.get("insp_workers", "1")))
    print(f"ℹ️  Stitch: {len(insp_files)} files, {workers} worker(s), cuda={use_cuda}, args={extra_args}")

    total = len(insp_files)
    to_stitch = [f for f in insp_files if not (input_dir / (f.stem + ".jpg")).exists()]
    already_done = total - len(to_stitch)

    done = [already_done]
    ok   = [already_done]
    lock = threading.Lock()

    def _stitch_one(insp: "Path") -> bool:
        out_jpg = input_dir / (insp.stem + ".jpg")
        result = subprocess.run(
            [str(_INSP_STITCH_EXE), str(insp), str(out_jpg)] + extra_args,
            capture_output=True, text=True, env=env,
        )
        # Print anything the exe logged (CUDA status, config, errors)
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                print(f"  [{insp.name}] {line}")
        if result.returncode == 0:
            print(f"✅ Stitched: {insp.name}")
            return True
        print(f"⚠️ Stitch failed: {insp.name}")
        return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_stitch_one, insp): insp for insp in to_stitch}
        for future in _as_completed(futures):
            if cancel_event.is_set():
                break
            insp = futures[future]
            success = future.result()
            with lock:
                done[0] += 1
                if success:
                    ok[0] += 1
                pct = int(5 + (done[0] / total) * 40)
            queue_manager.update_job_progress(
                job_id, pct, f"Stitched {done[0]}/{total}: {insp.name}"
            )

    return ok[0]


# ── Worker thread ─────────────────────────────────────────────

def _worker(job_id: str, job_data: dict, cancel_event: threading.Event):
    try:
        queue_manager.update_job_progress(job_id, 1, "Initialising pipeline…")

        # Stitch any .insp files to equirectangular JPEGs before the pipeline runs
        input_dir = _input_dir(_job_root(job_id, job_data))
        insp_count = len(list(input_dir.glob("*.insp"))) if input_dir.exists() else 0
        if insp_count:
            queue_manager.update_job_progress(job_id, 3, f"Found {insp_count} Insta360 files — stitching…")
            stitched = _stitch_insp_files(job_id, cancel_event, job_data)
            if cancel_event.is_set():
                queue_manager.fail_job(job_id, "Cancelled by user")
                return
            if stitched == 0:
                queue_manager.fail_job(job_id, "Stitch step produced no output — check MediaSDK setup")
                return
            queue_manager.update_job_progress(job_id, 45, f"Stitched {stitched}/{insp_count} files")
            proj_root = _job_root(job_id, job_data)
            _write_stage_progress(proj_root, "import", {"stitched": stitched, "total": insp_count})

        # ── Storage download (web-queued jobs only) ──────────────
        storage_prefix = job_data.get('storageInputPath')
        if storage_prefix:
            queue_manager.update_job_progress(job_id, 1, "Downloading files from cloud storage…")
            input_dir = _job_root(job_id, job_data) / "import from camera"
            input_dir.mkdir(parents=True, exist_ok=True)
            try:
                downloaded = firebase_client.download_storage_folder(
                    storage_prefix, str(input_dir)
                )
                if not downloaded and not any(input_dir.iterdir()):
                    queue_manager.fail_job(job_id, "No files found at storage path")
                    return
                queue_manager.update_job_progress(
                    job_id, 3, f"Downloaded {len(downloaded)} file(s) from cloud storage"
                )
            except Exception as exc:
                queue_manager.fail_job(job_id, f"Storage download failed: {exc}")
                return

        input_path = _find_primary_input(job_id, job_data)
        if not input_path:
            queue_manager.fail_job(job_id, "No input files found in job input directory")
            return

        queue_manager.update_job_progress(job_id, 2 if not insp_count else 46, f"Input: {Path(input_path).name}")

        # Heavy ML deps are only imported here — server startup stays light
        from splatpipe_core import run_pipeline  # type: ignore

        settings = _build_settings(job_data)
        proj_root = Path(str(_job_root(job_id, job_data)))
        settings.project_dir = str(proj_root)

        print(f"⚙️  Pipeline settings: run_vggt={settings.run_vggt} run_brush={settings.run_brush} "
              f"yaw_steps={settings.yaw_steps} pitch_angles={settings.pitch_angles}")
        queue_manager.update_job_progress(job_id, 2,
            f"Settings: vggt={settings.run_vggt} brush={settings.run_brush} "
            f"yaw={settings.yaw_steps} pitch={settings.pitch_angles}")

        # ── Stale output check ────────────────────────────────────
        # If output folders exist but don't match current settings, wipe and warn.
        # Skip this check when resuming (start_from is set) — the user explicitly chose
        # which stage to run, so we should not wipe work they want to keep.
        _resume_start = (job_data.get("_ui_settings") or {}).get("start_from", "")
        views_dir   = proj_root / "02_views"
        frames_dir  = proj_root / "01_frames"
        img_exts = {'.jpg', '.jpeg', '.png'}
        if not _resume_start and views_dir.exists():
            existing_views = sum(1 for f in views_dir.rglob("*") if f.is_file() and f.suffix.lower() in img_exts)
            _proj_input_dir = _input_dir(proj_root)
            input_count = len([
                f for f in _proj_input_dir.iterdir()
                if f.is_file() and f.suffix.lower() in img_exts
            ]) if _proj_input_dir.exists() else 0
            expected_views = input_count * (len(settings.pitch_angles) * settings.yaw_steps + (1 if getattr(settings, "horizon_ref", False) else 0))
            if existing_views > 0 and expected_views > 0 and existing_views != expected_views:
                queue_manager.update_job_progress(job_id, 2,
                    f"⚠️ Stale output detected ({existing_views} views, expected {expected_views}) — clearing and restarting")
                import shutil
                for d in [proj_root / "04_training", views_dir, frames_dir]:
                    if d.exists():
                        shutil.rmtree(str(d))

        use_colmap   = getattr(settings, "run_colmap", False)
        use_rs_brush = not settings.run_vggt and not use_colmap and settings.run_brush
        if use_rs_brush:
            stage_map = _STAGE_RANGE_RS_BRUSH_POST_STITCH if insp_count else _STAGE_RANGE_RS_BRUSH
            print(f"  → Stage map: RS+Brush {'(post-stitch)' if insp_count else '(direct)'}")
        elif use_colmap:
            stage_map = _STAGE_RANGE_COLMAP_POST_STITCH if insp_count else _STAGE_RANGE_COLMAP
            print(f"  → Stage map: COLMAP {'(post-stitch)' if insp_count else '(direct)'}")
        else:
            stage_map = _STAGE_RANGE_POST_STITCH if insp_count else _STAGE_RANGE
            print(f"  → Stage map: VGGT {'(post-stitch)' if insp_count else '(direct)'}")

        def on_progress(sp):
            lo, hi = stage_map.get(sp.stage.value, (0, 97))
            overall = int(lo + (sp.progress / 100) * (hi - lo))
            print(f"  → {sp.stage.value} {sp.progress}% → overall {overall}% [{lo}–{hi}]")
            _mode = "rs_brush" if use_rs_brush else ("colmap" if use_colmap else "vggt")
            extra = {
                "currentStage": sp.stage.value,
                "pipelineMode": _mode,
            }
            queue_manager.update_job_progress(
                job_id, overall, sp.message[:200],
                extra=extra,
            )

        start_from = (job_data.get("_ui_settings") or {}).get("start_from", "")
        if start_from:
            print(f"  → Resuming from stage: {start_from!r}")

        result = run_pipeline(
            job_id=job_id,
            input_path=input_path,
            settings=settings,
            on_progress=on_progress,
            cancel_event=cancel_event,
            start_from=start_from,
        )

        if cancel_event.is_set():
            queue_manager.fail_job(job_id, "Cancelled by user")
            return

        if result.success:
            proj_root = _job_root(job_id, job_data)
            stats = result.stats or {}
            if stats.get("frames_extracted"):
                _write_stage_progress(proj_root, "frames", {"count": stats["frames_extracted"]})
            if stats.get("views_rendered"):
                _write_stage_progress(proj_root, "views", {"count": stats["views_rendered"]})
            if result.colmap_dir:
                _write_stage_progress(proj_root, "vggt", {"colmap_dir": str(result.colmap_dir)})
            if stats.get("point_count"):
                _write_stage_progress(proj_root, "colmap", {"points": stats["point_count"]})

            # ── Convert .ply → .spz → .rad and upload to R2 ──────────
            r2_url = None
            gaussian_count = 0
            try:
                from .ply_to_splat import find_latest_ply
                from . import r2_client

                training_dir = proj_root / "04_training"
                ply_file = find_latest_ply(str(training_dir))

                if ply_file and r2_client.is_configured():
                    gaussian_count = _count_gaussians_ply(Path(ply_file))

                    queue_manager.update_job_progress(job_id, 90, "Compressing to .spz…")
                    spz_file = _convert_ply_to_spz(Path(ply_file))

                    queue_manager.update_job_progress(job_id, 95, "Building LoD tree (.rad)…")
                    rad_file = _convert_spz_to_rad(spz_file)

                    queue_manager.update_job_progress(job_id, 98, "Uploading to gallery…")
                    r2_url = r2_client.upload_rad(rad_file, job_id)

                    # Pick middle stitched JPEG as thumbnail
                    thumbnail_url = _upload_thumbnail(job_id, proj_root)

                    # Publish to the public gallery Firestore collection
                    _publish_to_gallery(job_id, job_data, r2_url, gaussian_count, thumbnail_url)
                elif ply_file and not r2_client.is_configured():
                    print("  [r2] Skipping upload — r2_config.json not configured")
            except Exception as exc:
                print(f"  ⚠️  Gallery upload failed (job still complete): {exc}")

            glb = find_output_glb(job_id, job_data)
            queue_manager.complete_job(
                job_id,
                output_path=result.output_dir,
                output_format="colmap" if result.colmap_dir else "views",
                preview_url=r2_url or (f"/api/jobs/{job_id}/output/glb" if glb else None),
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


_SPZ_SCRIPT = Path(__file__).parent.parent / "scripts" / "spz" / "convert.mjs"
_BUILD_LOD_EXE = (
    Path(__file__).parent.parent
    / "scripts" / "spark-repo" / "rust"
    / "target" / "release" / "build-lod.exe"
)


def _convert_ply_to_spz(ply_path: Path) -> Path:
    """Compress PLY → .spz (Spark WASM compressor). Output lands next to input."""
    result = subprocess.run(
        ["node", str(_SPZ_SCRIPT), str(ply_path), "--max-sh", "2"],
        capture_output=True, text=True, timeout=1800,
        cwd=str(ply_path.parent),
    )
    if result.returncode != 0:
        raise RuntimeError(f"spz compression failed:\n{result.stderr[-2000:]}")
    print(f"  [spz] {result.stdout.strip()}")
    return ply_path.with_suffix(".spz")


def _convert_spz_to_rad(spz_path: Path) -> Path:
    """Build LoD splat tree (.rad) from .spz using Spark's build-lod binary."""
    result = subprocess.run(
        [str(_BUILD_LOD_EXE), "--quality", str(spz_path)],
        capture_output=True, text=True, timeout=3600,
        cwd=str(spz_path.parent),
    )
    if result.returncode != 0:
        raise RuntimeError(f"build-lod failed:\n{result.stderr[-2000:]}")
    print(f"  [build-lod] {result.stdout.strip()}")
    return spz_path.parent / (spz_path.stem + "-lod.rad")


def _count_gaussians_ply(path: Path) -> int:
    """Count Gaussian splats by reading the 'element vertex' line in the PLY header."""
    with open(path, "rb") as f:
        for line in iter(lambda: f.readline(), b""):
            if line.startswith(b"element vertex "):
                return int(line.split()[-1])
            if line.strip() == b"end_header":
                break
    return 0


def _upload_thumbnail(job_id: str, proj_root: "Path") -> "str | None":
    """
    Pick the middle stitched JPEG from the input dir, crop + resize it,
    and upload to R2 as {job_id}/thumbnail.jpg.
    Returns the public URL or None (non-fatal).
    """
    try:
        from . import r2_client
        from PIL import Image

        input_dir = _input_dir(proj_root)
        jpegs = sorted(input_dir.glob("*.jpg"))
        if not jpegs:
            print("  [thumbnail] No stitched JPEGs found, skipping thumbnail")
            return None

        src = jpegs[len(jpegs) // 2]
        print(f"  [thumbnail] Resizing {src.name} for thumbnail")

        thumb_path = proj_root / "thumbnail.jpg"
        with Image.open(src) as img:
            img = img.convert("RGB")
            # Crop the centre horizontal band of the equirectangular image
            w, h = img.size
            img = img.crop((0, h // 3, w, h * 2 // 3))
            # Resize to max 1280px wide, keep aspect ratio
            img.thumbnail((1280, 1280), Image.LANCZOS)
            img.save(str(thumb_path), "JPEG", quality=82, optimize=True)

        return r2_client.upload_thumbnail(thumb_path, job_id)
    except ImportError:
        print("  [thumbnail] Pillow not installed, skipping (pip install Pillow)")
        return None
    except Exception as exc:
        print(f"  [thumbnail] Skipping: {exc}")
        return None


def _publish_to_gallery(
    job_id: str,
    job_data: dict,
    splat_url: str,
    gaussian_count: int,
    thumbnail_url: "str | None" = None,
) -> None:
    """Write a public gallery document to Firestore for this completed splat."""
    from google.cloud.firestore import SERVER_TIMESTAMP
    try:
        db = firebase_client.get_db()
        # Prefer the project folder name (e.g. "nile creek") over the generic
        # upload label (e.g. "Nicolas de Cosson — 2026-06-03") so the gallery
        # entry actually identifies the scene, not the upload session.
        project_dir = job_data.get('projectDir')
        display_name = (
            Path(project_dir).name if project_dir
            else job_data.get("name") or job_data.get("display_name") or "Untitled"
        )
        doc: dict = {
            "jobId":         job_id,
            "name":          display_name,
            "splatUrl":      splat_url,
            "gaussianCount": gaussian_count,
            "pipelineMode":  job_data.get("pipelineMode") or job_data.get("pipeline_mode") or "rs_brush",
            "createdAt":     SERVER_TIMESTAMP,
        }
        if thumbnail_url:
            doc["thumbnailUrl"] = thumbnail_url
        db.collection("gallery").document(job_id).set(doc)
        print(f"  [gallery] Published to Firestore gallery/{job_id}")
    except Exception as e:
        print(f"  [gallery] Could not publish gallery doc: {e}")
