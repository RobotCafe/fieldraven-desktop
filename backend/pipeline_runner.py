"""
Pipeline runner: manages one SplatPipe background thread per accepted job.
"""
import json
import os
import subprocess
import sys
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
_INSV_STITCH_EXE = Path(__file__).resolve().parent.parent / "tools" / "insv_stitch.exe"

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
_STAGE_RANGE_RS_POSTSHOT = {
    "frame_extraction":   (5,  20),
    "view_extraction":    (20, 45),
    "realityscan":        (45, 65),
    "postshot_training":  (65, 97),
}
_STAGE_RANGE_RS_POSTSHOT_POST_STITCH = {
    "frame_extraction":   (47, 57),
    "view_extraction":    (57, 70),
    "realityscan":        (70, 82),
    "postshot_training":  (82, 97),
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
_STAGE_RANGE_COLMAP_FISHEYE = {
    "frame_extraction":          (5,  20),
    "colmap_fisheye_alignment":  (20, 80),
    "brush_training":            (80, 97),
}
_STAGE_RANGE_COLMAP_FISHEYE_POST_STITCH = {
    "frame_extraction":          (47, 57),
    "colmap_fisheye_alignment":  (57, 88),
    "brush_training":            (88, 97),
}
_STAGE_RANGE_GLUEMAP = {
    "frame_extraction":   (5,  20),
    "view_extraction":    (20, 45),
    "gluemap_alignment":  (45, 82),
    "brush_training":     (82, 97),
}
_STAGE_RANGE_GLUEMAP_POST_STITCH = {
    "frame_extraction":   (47, 57),
    "view_extraction":    (57, 72),
    "gluemap_alignment":  (72, 88),
    "brush_training":     (88, 97),
}
_STAGE_RANGE_RIGSFM = {
    "frame_extraction":   (5,  20),
    "view_extraction":    (20, 45),
    "rigsfm_alignment":   (45, 82),
    "brush_training":     (82, 97),
}
_STAGE_RANGE_RIGSFM_POST_STITCH = {
    "frame_extraction":   (47, 57),
    "view_extraction":    (57, 72),
    "rigsfm_alignment":   (72, 88),
    "brush_training":     (88, 97),
}
_STAGE_RANGE_EQUISFM = {
    "frame_extraction":   (5,  20),
    "view_extraction":    (20, 45),
    "equisfm_alignment":  (45, 82),
    "brush_training":     (82, 97),
}
_STAGE_RANGE_EQUISFM_POST_STITCH = {
    "frame_extraction":   (47, 57),
    "view_extraction":    (57, 72),
    "equisfm_alignment":  (72, 88),
    "brush_training":     (88, 97),
}

_cancel_events: dict[str, threading.Event] = {}
_threads:       dict[str, threading.Thread] = {}


# ── Public API ────────────────────────────────────────────────

def start(job_id: str, job_data: dict) -> bool:
    """Spawn a pipeline thread for job_id. Returns False if already running."""
    if job_id in _threads and _threads[job_id].is_alive():
        # If a cancel is in-flight, wait briefly for the thread to finish cleanup
        # before allowing a re-run.  This lets the user cancel and immediately
        # re-run without getting a 409 "already running" error.
        cancel_ev = _cancel_events.get(job_id)
        if cancel_ev and cancel_ev.is_set():
            print(f"⏳ Cancel in-flight for {job_id} — waiting for thread to finish…")
            _threads[job_id].join(timeout=15.0)
            if _threads[job_id].is_alive():
                print(f"⚠️  Pipeline still cleaning up for {job_id}, try again shortly")
                return False
            print(f"✅ Previous thread cleaned up — starting fresh for {job_id}")
        else:
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


def _import_reference(proj_root: Path) -> Optional[dict]:
    """Read the 'importReference' block from fieldraven.json, if this project was
    imported in reference mode (source left in place, not copied in) rather than
    copy mode. Returns None for every pre-existing project (no such block) and for
    any project explicitly imported in copy mode -- fully backward compatible."""
    config_path = proj_root / "fieldraven.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    ref = config.get("importReference")
    return ref if isinstance(ref, dict) and ref.get("mode") == "reference" else None


def _write_import_reference(project_dir: Path, kind: str, source_path: Path) -> None:
    """Record that this project's import was left in place rather than copied in.
    kind is 'video' or 'folder'; source_path is the original file/folder location.
    Read back by _import_reference() -- see that function and _input_dir()/
    _find_primary_input() for how each consumer resolves the reference."""
    config_path = project_dir / "fieldraven.json"
    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    config["importReference"] = {
        "mode": "reference", "kind": kind, "sourcePath": str(source_path),
    }
    config["savedAt"] = datetime.now().isoformat(timespec="seconds")
    try:
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠️  Could not write importReference to fieldraven.json: {e}")


def _input_dir(proj_root: Path) -> Path:
    """Return the project's source-photos directory: 'import from camera' (camera/cloud
    imports) or 'imported photos' (a local folder imported via /api/project/import-folder),
    whichever exists. Defaults to 'import from camera' when neither exists yet.

    A folder-kind reference import (source left in its original location, never
    copied in) overrides this entirely -- the referenced folder itself IS the input
    dir, so every consumer of this function (gallery listing, EXIF/session-time
    derivation, the stale-output check) transparently reads the real files with no
    changes needed on their end. Falls through to the normal local-folder logic if
    the referenced path no longer exists, so a moved/deleted source fails clearly
    downstream ("no input files found") rather than silently.
    """
    ref = _import_reference(proj_root)
    if ref and ref.get("kind") == "folder":
        ref_path = Path(ref.get("sourcePath", ""))
        if ref_path.is_dir():
            return ref_path

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
    proj_root = _job_root(job_id, job_data)
    input_dir = _input_dir(proj_root)

    ref = _import_reference(proj_root)
    if ref and ref.get("kind") == "video":
        # Referenced video (never copied in) -- a locally-stitched .insv output
        # still takes priority if it already exists (same priority as the local-
        # copy case below), otherwise fall back to the external source directly.
        ref_path = Path(ref.get("sourcePath", ""))
        if ref_path.suffix.lower() == ".insv" and input_dir.exists():
            stitched = input_dir / (ref_path.stem + "_equirect.mp4")
            if stitched.exists():
                return str(stitched)
        if ref_path.is_file():
            return str(ref_path)
        # Referenced source no longer exists -- fall through to the scan below,
        # which will correctly find nothing local either and report a clear
        # failure rather than silently proceeding on stale state.

    if not input_dir.exists():
        return None
    files = sorted(input_dir.iterdir())
    for f in files:
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            if f.suffix.lower() == ".insv":
                # Raw .insv is dual-fisheye, not equirectangular -- prefer the
                # stitched output (_stitch_insv_files) if it already exists so
                # frame extraction never runs against unstitched footage.
                stitched = input_dir / (f.stem + "_equirect.mp4")
                if stitched.exists():
                    return str(stitched)
            return str(f)
    if any(f.is_file() and f.suffix.lower() in IMAGE_EXTS for f in files):
        return str(input_dir)
    return None


def _find_raw_fisheye_sources(job_id: str, job_data: Optional[dict] = None) -> Optional[dict]:
    """Resolve the job's own raw, un-stitched Insta360 source for auto-deriving
    colmap_fisheye_raw_dir (see splatpipe_core/fisheye_frame_extractor.py). Mirrors
    _stitch_insv_files'/_stitch_insp_files' own resolution logic (lines above) rather
    than inventing new logic. Returns None if this job has no raw .insv/.insp at all
    (non-Insta360 source, or a job relying solely on a manually-pointed / mobile
    live-capture raw_dir) -- the caller no-ops in that case."""
    proj_root = _job_root(job_id, job_data)
    input_dir = _input_dir(proj_root)

    video_file = (job_data or {}).get("videoFile", "")
    if video_file.lower().endswith(".insv"):
        candidate = input_dir / video_file
        if candidate.exists():
            return {"kind": "insv", "paths": [candidate]}
        ref = _import_reference(proj_root)
        if ref and ref.get("kind") == "video":
            ref_path = Path(ref.get("sourcePath", ""))
            if ref_path.name == video_file and ref_path.exists():
                return {"kind": "insv", "paths": [ref_path]}

    if input_dir.exists():
        insp_files = sorted(input_dir.glob("*.insp"))
        if insp_files:
            return {"kind": "insp", "paths": insp_files}

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

    # Merge fieldraven.json["settings"] from the project directory into cfg so
    # project-level settings (e.g. rigsfm_quad_anchors) are picked up.
    _proj_dir = job_data.get("projectDir")
    if _proj_dir:
        _fj_path = Path(_proj_dir) / "fieldraven.json"
        if _fj_path.exists():
            try:
                _fj = json.loads(_fj_path.read_text(encoding="utf-8"))
                _fj_settings = _fj.get("settings") or {}
                cfg.update({k: str(v) for k, v in _fj_settings.items()
                            if isinstance(v, (str, bool, int, float))})
            except Exception as _fj_err:
                print(f"⚠️  Could not read fieldraven.json settings: {_fj_err}")

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
    s.colmap_orientation_align    = _to_bool(cfg.get("colmap_orientation_align", s.colmap_orientation_align))
    s.colmap_mapper               = cfg.get("colmap_mapper", s.colmap_mapper)
    s.colmap_vocab_tree           = cfg.get("colmap_vocab_tree", s.colmap_vocab_tree)
    s.colmap_vocab_tree_enabled   = _to_bool(cfg.get("colmap_vocab_tree_enabled", s.colmap_vocab_tree_enabled))
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
    s.colmap_bin            = cfg.get("colmap_bin") or s.colmap_bin
    s.rigsfm_quad_anchors   = _to_bool(cfg.get("rigsfm_quad_anchors", s.rigsfm_quad_anchors))
    s.colmap_vocab_tree     = cfg.get("colmap_vocab_tree", s.colmap_vocab_tree) or s.colmap_vocab_tree
    s.colmap_vocab_tree_enabled = _to_bool(cfg.get("colmap_vocab_tree_enabled", s.colmap_vocab_tree_enabled))
    if cfg.get("colmap_image_width"):
        try:
            s.colmap_image_width = int(float(cfg["colmap_image_width"]))
        except (ValueError, TypeError):
            pass

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
        if "export_xmp" in ui:        s.use_rig_xmp        = _to_bool(ui["export_xmp"])
        if "gps_priors_rs" in ui:     s.gps_priors_rs      = _to_bool(ui["gps_priors_rs"])
        if "gps_priors_colmap" in ui: s.gps_priors_colmap  = _to_bool(ui["gps_priors_colmap"])
        if "skip_realityscan" in ui:  s.skip_realityscan  = _to_bool(ui["skip_realityscan"])
        if "run_colmap" in ui:        s.run_colmap        = _to_bool(ui["run_colmap"])
        if "colmap_mode" in ui:       s.colmap_mode       = ui["colmap_mode"]
        if "colmap_matcher" in ui:    s.colmap_matcher    = ui["colmap_matcher"]
        if "brush_rerun_logging" in ui: s.brush_rerun_logging = _to_bool(ui["brush_rerun_logging"])
        if "colmap_visualize" in ui:    s.colmap_visualize    = _to_bool(ui["colmap_visualize"])
        if "colmap_correct_pitch" in ui:       s.colmap_correct_pitch       = _to_bool(ui["colmap_correct_pitch"])
        if "colmap_orientation_align" in ui:   s.colmap_orientation_align   = _to_bool(ui["colmap_orientation_align"])
        if "colmap_mapper" in ui:              s.colmap_mapper              = ui["colmap_mapper"]
        if "colmap_vocab_tree" in ui:          s.colmap_vocab_tree          = ui["colmap_vocab_tree"]
        if "colmap_vocab_tree_enabled" in ui:   s.colmap_vocab_tree_enabled  = _to_bool(ui["colmap_vocab_tree_enabled"])
        if "run_gluemap" in ui:                s.run_gluemap                = _to_bool(ui["run_gluemap"])
        if "gluemap_backbone" in ui:           s.gluemap_backbone           = ui["gluemap_backbone"]
        if "gluemap_skip_doppelgangers" in ui: s.gluemap_skip_doppelgangers = _to_bool(ui["gluemap_skip_doppelgangers"])
        if "gluemap_coarse_only" in ui:        s.gluemap_coarse_only        = _to_bool(ui["gluemap_coarse_only"])
        if "gluemap_is_sequential" in ui:      s.gluemap_is_sequential      = _to_bool(ui["gluemap_is_sequential"])
        if "gluemap_num_neighbors" in ui:      s.gluemap_num_neighbors      = int(float(ui["gluemap_num_neighbors"]))
        if "gluemap_batch_size" in ui:         s.gluemap_batch_size         = int(float(ui["gluemap_batch_size"]))
        if "gluemap_num_track_per_img" in ui:  s.gluemap_num_track_per_img  = int(float(ui["gluemap_num_track_per_img"]))
        if "gluemap_wsl_home" in ui:           s.gluemap_wsl_home           = ui["gluemap_wsl_home"]
        if "gluemap_wsl_distro" in ui:         s.gluemap_wsl_distro         = ui["gluemap_wsl_distro"]
        if "run_rigsfm" in ui:                 s.run_rigsfm                 = _to_bool(ui["run_rigsfm"])
        if "rigsfm_anchor_sensor" in ui:       s.rigsfm_anchor_sensor       = int(float(ui["rigsfm_anchor_sensor"]))
        if "rigsfm_matcher" in ui:             s.rigsfm_matcher             = ui["rigsfm_matcher"]
        if "rigsfm_quad_anchors" in ui:        s.rigsfm_quad_anchors        = _to_bool(ui["rigsfm_quad_anchors"])
        if "run_colmap_fisheye" in ui:          s.run_colmap_fisheye          = _to_bool(ui["run_colmap_fisheye"])
        if "colmap_fisheye_use_calibration" in ui: s.colmap_fisheye_use_calibration = _to_bool(ui["colmap_fisheye_use_calibration"])
        if "colmap_fisheye_matcher" in ui:       s.colmap_fisheye_matcher       = ui["colmap_fisheye_matcher"]
        if "colmap_fisheye_front_profile" in ui: s.colmap_fisheye_front_profile = ui["colmap_fisheye_front_profile"]
        if "colmap_fisheye_back_profile" in ui:  s.colmap_fisheye_back_profile  = ui["colmap_fisheye_back_profile"]
        if "colmap_fisheye_raw_dir" in ui:        s.colmap_fisheye_raw_dir       = ui["colmap_fisheye_raw_dir"]
        if "colmap_fisheye_fov_deg" in ui:          s.colmap_fisheye_fov_deg          = float(ui["colmap_fisheye_fov_deg"])
        if "colmap_fisheye_raw_fov_deg" in ui:      s.colmap_fisheye_raw_fov_deg      = float(ui["colmap_fisheye_raw_fov_deg"])
        if "colmap_fisheye_raw_swap_lenses" in ui:  s.colmap_fisheye_raw_swap_lenses  = _to_bool(ui["colmap_fisheye_raw_swap_lenses"])
        if "run_equisfm" in ui:                s.run_equisfm                = _to_bool(ui["run_equisfm"])
        if "equisfm_matcher" in ui:            s.equisfm_matcher            = ui["equisfm_matcher"]
        if "equisfm_mapper" in ui:             s.equisfm_mapper             = ui["equisfm_mapper"]
        if "equisfm_triangulate" in ui:        s.equisfm_triangulate        = _to_bool(ui["equisfm_triangulate"])
        if "colmap_image_width" in ui:         s.colmap_image_width         = int(float(ui["colmap_image_width"]))
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

    # ── Validate outputs: a valid equirectangular is always several MB; <50 KB
    #    means the stitch process produced a truncated or empty file.
    #    Check input_dir directly — the .thumbs/ sub-folder is never consulted.
    _MIN_JPG = 50_000
    bad = [
        f for f in insp_files
        if not (input_dir / (f.stem + ".jpg")).exists()
        or (input_dir / (f.stem + ".jpg")).stat().st_size < _MIN_JPG
    ]
    if bad and not cancel_event.is_set():
        print(f"🔄 {len(bad)} file(s) have missing/truncated output — retrying…")
        queue_manager.update_job_progress(job_id, 46, f"Retrying {len(bad)} failed conversion(s)…")
        for f in bad:
            bad_out = input_dir / (f.stem + ".jpg")
            if bad_out.exists():
                bad_out.unlink()
            # Also drop any stale thumbnail so it gets rebuilt from the fresh JPEG
            stale_thumb = input_dir / ".thumbs" / (f.stem + ".jpg")
            if stale_thumb.exists():
                stale_thumb.unlink()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures2 = {pool.submit(_stitch_one, insp): insp for insp in bad}
            for future in _as_completed(futures2):
                if cancel_event.is_set():
                    break
                insp = futures2[future]
                success = future.result()
                with lock:
                    if success:
                        ok[0] += 1
        still_bad = [
            f for f in bad
            if not (input_dir / (f.stem + ".jpg")).exists()
            or (input_dir / (f.stem + ".jpg")).stat().st_size < _MIN_JPG
        ]
        if still_bad:
            print(f"❌ {len(still_bad)} file(s) could not be stitched after retry: "
                  + ", ".join(f.name for f in still_bad))
        else:
            print(f"✅ All retry conversions succeeded")

    return ok[0]


def _stitch_insv_files(job_id: str, cancel_event: threading.Event, job_data: Optional[dict] = None) -> int:
    """Convert any raw (unstitched) .insv videos in the job input dir to
    equirectangular .mp4 files, so frame extraction (ffmpeg) always runs
    against already-stitched footage exactly like it does for images.
    Output lands alongside the source as '{stem}_equirect.mp4'; the raw
    .insv is left in place (matching the .insp -> .jpg convention) and
    _find_primary_input() prefers the stitched output once it exists.
    Returns number of files successfully stitched."""
    proj_root = _job_root(job_id, job_data)
    input_dir = _input_dir(proj_root)
    # Only stitch the one video this job actually owns (job_data['videoFile'],
    # set once at job creation) -- NOT every .insv sitting in the input dir.
    # A project folder can hold other raw clips (e.g. multiple imports from the
    # same card) that were never part of this job; globbing *.insv would stitch
    # them too on every Run Pipeline click, which is exactly the "stitch should
    # only happen at import" bug this guards against.
    video_file = (job_data or {}).get("videoFile", "")
    if not video_file.lower().endswith(".insv"):
        return 0
    candidate = input_dir / video_file
    if not candidate.exists():
        # Referenced (never-copied) source -- read the raw .insv from its
        # original location; the stitched output below still always writes
        # into input_dir (project-owned), never back into the source folder.
        ref = _import_reference(proj_root)
        if ref and ref.get("kind") == "video":
            ref_path = Path(ref.get("sourcePath", ""))
            if ref_path.name == video_file and ref_path.exists():
                candidate = ref_path
    insv_files = [candidate] if candidate.exists() else []
    if not insv_files:
        return 0

    if not _INSV_STITCH_EXE.exists():
        raise FileNotFoundError(f"insv_stitch.exe not found at {_INSV_STITCH_EXE}. Run tools/build_insv_stitch.bat first.")

    env = os.environ.copy()
    env["PATH"] = str(_SDK_BIN) + ";" + env.get("PATH", "")

    from . import splat_config
    cfg = splat_config.load()
    extra_args: list[str] = []

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

    to_stitch = [f for f in insv_files if not (input_dir / (f.stem + "_equirect.mp4")).exists()]
    already_done = len(insv_files) - len(to_stitch)
    print(f"ℹ️  Video stitch: {len(insv_files)} file(s), {len(to_stitch)} to convert, cuda={use_cuda}, args={extra_args}")

    ok = already_done
    for insv in to_stitch:
        if cancel_event.is_set():
            break
        out_mp4 = input_dir / (insv.stem + "_equirect.mp4")
        queue_manager.update_job_progress(job_id, 3, f"Stitching video {insv.name}…")

        proc = subprocess.Popen(
            [str(_INSV_STITCH_EXE), str(insv), str(out_mp4)] + extra_args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env,
        )
        last_pct = -1
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("PROGRESS:"):
                try:
                    pct = int(line.split(":", 1)[1])
                except ValueError:
                    pct = None
                if pct is not None and pct != last_pct:
                    last_pct = pct
                    # Video stitching is one long-running file, not many quick
                    # ones -- map its own 0-100 into the same 3-45 band
                    # _stitch_insp_files uses for its per-file counting.
                    queue_manager.update_job_progress(
                        job_id, int(3 + pct * 0.42), f"Stitching {insv.name}: {pct}%"
                    )
            else:
                print(f"  [{insv.name}] {line}")
            if cancel_event.is_set():
                proc.terminate()
                break
        proc.wait()

        if proc.returncode == 0 and out_mp4.exists():
            print(f"✅ Stitched video: {insv.name}")
            ok += 1
        else:
            print(f"⚠️ Video stitch failed: {insv.name}")

    return ok


def _extract_frames_files(job_id: str, cancel_event: threading.Event,
                           job_data: Optional[dict] = None, ui_settings: Optional[dict] = None) -> dict:
    """Extract frames for a video job into 01_frames/, skipping the work entirely
    if a matching extraction (same source content + same settings) already exists.
    Shared with the real pipeline run via pipeline.ensure_frames_extracted() -- built
    through _build_settings() exactly like /start does, so the button and a later
    Run Pipeline always agree on what counts as "already extracted"."""
    from splatpipe_core import pipeline as core_pipeline  # type: ignore

    settings = _build_settings({**(job_data or {}), "_ui_settings": ui_settings or {}})
    core_pipeline._load_splatpipe(settings.vggt_app_path)

    input_path = _find_primary_input(job_id, job_data)
    if not input_path or not Path(input_path).is_file():
        return {"success": False, "error": "No video file found for this job"}

    proj_root = _job_root(job_id, job_data)
    frames_dir = proj_root / "01_frames"

    last_pct = [-1]

    def _progress_cb(current, total, ts=None):
        pct = int(current / total * 100) if total else 0
        if pct != last_pct[0]:
            last_pct[0] = pct
            msg = f"Extracting frame {current}/{total}" + (f" at {ts:.1f}s" if ts else "")
            queue_manager.update_job_progress(job_id, pct, msg)

    ok, n_frames, already_done = core_pipeline.ensure_frames_extracted(
        video_path=input_path,
        frames_dir=frames_dir,
        settings=settings,
        progress_callback=_progress_cb,
        cancel_event=cancel_event,
    )
    if not ok:
        return {"success": False, "error": "Frame extraction failed or produced no frames"}
    return {"success": True, "n_frames": n_frames, "already_done": already_done}


# ── Worker thread ─────────────────────────────────────────────

def _worker(job_id: str, job_data: dict, cancel_event: threading.Event):
    # Open a fresh log file for this run and splice into the global stdout tee.
    _log_dir = Path(__file__).parent.parent / "server_logs"
    _log_dir.mkdir(exist_ok=True)
    _run_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    _run_log_path = _log_dir / f"pipeline_{_run_stamp}_{job_id[:8]}.log"
    _run_log = open(str(_run_log_path), "w", encoding="utf-8", buffering=1)
    if hasattr(sys.stdout, "add_stream"):
        sys.stdout.add_stream(_run_log)
    if hasattr(sys.stderr, "add_stream"):
        sys.stderr.add_stream(_run_log)

    _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'═'*52}\n PIPELINE START  job={job_id}  {_ts}\n{'═'*52}")
    try:
        queue_manager.update_job_progress(job_id, 1, "Initialising pipeline…", milestone=True)

        # Persist mobile job metadata into fieldraven.json so the resume path
        # always has userId, userJobId, and exact session times without Firestore.
        _uid  = job_data.get("userId")  or job_data.get("user_id")
        _ujid = job_data.get("userJobId") or job_data.get("user_job_id")
        if _uid or _ujid:
            try:
                _write_stage_progress(_job_root(job_id, job_data), "mobile_ids", {
                    "userId": _uid, "userJobId": _ujid,
                })
            except Exception:
                pass
        if _uid and _ujid:
            try:
                _db_early = firebase_client.get_db()
                _snap = (_db_early.collection("users")
                         .document(_uid).collection("jobs").document(_ujid).get())
                if _snap.exists:
                    _md   = _snap.to_dict() or {}
                    _s_ms = _md.get("startTime")
                    _e_ms = _md.get("endTime")
                    if _s_ms and _e_ms:
                        _write_stage_progress(_job_root(job_id, job_data), "session_times", {
                            "startMs": int(_s_ms), "endMs": int(_e_ms),
                        })
                        print("  [mobile] Session times persisted to fieldraven.json")
            except Exception as _e_persist:
                print(f"  [mobile] Could not persist session times: {_e_persist}")

        # Stitch any .insp files to equirectangular JPEGs before the pipeline runs
        input_dir = _input_dir(_job_root(job_id, job_data))
        insp_count = len(list(input_dir.glob("*.insp"))) if input_dir.exists() else 0
        if insp_count:
            queue_manager.update_job_progress(job_id, 3, f"Found {insp_count} Insta360 files — stitching…", milestone=True)
            stitched = _stitch_insp_files(job_id, cancel_event, job_data)
            if cancel_event.is_set():
                queue_manager.fail_job(job_id, "Cancelled by user")
                return
            if stitched == 0:
                queue_manager.fail_job(job_id, "Stitch step produced no output — check MediaSDK setup")
                return
            queue_manager.update_job_progress(job_id, 45, f"Stitched {stitched}/{insp_count} files", milestone=True)
            proj_root = _job_root(job_id, job_data)
            _write_stage_progress(proj_root, "import", {"stitched": stitched, "total": insp_count})

        # Stitch this job's own raw .insv video to equirectangular .mp4 before
        # the pipeline runs -- frame extraction assumes its input is already
        # equirectangular (it only crops panorama -> perspective views, it
        # never un-fisheyes anything), so this must happen before
        # _find_primary_input() / run_pipeline() regardless of whether the
        # frontend's own immediate-post-import stitch trigger already ran.
        # Scoped to job_data['videoFile'] specifically (not a directory-wide
        # *.insv glob) -- a project folder can hold other raw clips that were
        # never part of this job, and stitching should only ever happen at
        # import time, not get re-triggered against unrelated files every time
        # Run Pipeline is clicked.
        _video_file = (job_data or {}).get("videoFile", "")
        _should_stitch = False
        if _video_file.lower().endswith(".insv"):
            if (input_dir / _video_file).exists():
                _should_stitch = True
            else:
                # Referenced (never-copied) source -- the raw .insv lives at
                # the external reference path, not in input_dir.
                # _stitch_insv_files() already checks the reference itself;
                # this gate just needs to not skip the whole block on that
                # account.
                _ref = _import_reference(_job_root(job_id, job_data))
                if _ref and _ref.get("kind") == "video" and Path(_ref.get("sourcePath", "")).exists():
                    _should_stitch = True
        if _should_stitch:
            queue_manager.update_job_progress(job_id, 3, "Found Insta360 video — stitching…", milestone=True)
            stitched_v = _stitch_insv_files(job_id, cancel_event, job_data)
            if cancel_event.is_set():
                queue_manager.fail_job(job_id, "Cancelled by user")
                return
            if stitched_v == 0:
                queue_manager.fail_job(job_id, "Video stitch step produced no output — check MediaSDK setup")
                return
            queue_manager.update_job_progress(job_id, 45, "Stitched video", milestone=True)
            proj_root = _job_root(job_id, job_data)
            _write_stage_progress(proj_root, "import", {"stitched": stitched_v, "total": 1})

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

        # ── COLMAP Fisheye: auto-derive raw_dir from the job's own raw .insv/.insp
        # if the user hasn't manually pointed at some other folder. Silent no-op for
        # non-Insta360 jobs or when a manual/mobile-live-capture folder is already set.
        if getattr(settings, "run_colmap_fisheye", False) and not (settings.colmap_fisheye_raw_dir or "").strip():
            _raw_sources = _find_raw_fisheye_sources(job_id, job_data)
            if _raw_sources:
                from splatpipe_core import fisheye_frame_extractor
                from splatpipe_core.calibration_profiles import load_profile as _load_calib_profile

                _front_profile = _back_profile = None
                if getattr(settings, "colmap_fisheye_use_calibration", True):
                    _front_profile = _load_calib_profile(settings.colmap_fisheye_front_profile)
                    _back_profile = _load_calib_profile(settings.colmap_fisheye_back_profile)

                _extraction_settings = {
                    "extraction_method": settings.extraction_method,
                    "interval_value": settings.interval_value,
                    "interval_unit": settings.interval_unit,
                    "frame_count": settings.frame_count,
                    "ffmpeg_path": settings.ffmpeg_path,
                }
                queue_manager.update_job_progress(job_id, 3, "Deriving raw fisheye frames from source…", milestone=True)

                def _fisheye_progress(pct: int, msg: str) -> None:
                    queue_manager.update_job_progress(job_id, int(3 + pct * 0.42), msg)

                try:
                    _fisheye_raw_dir = fisheye_frame_extractor.ensure_fisheye_raw_frames(
                        raw_sources=_raw_sources["paths"],
                        source_kind=_raw_sources["kind"],
                        out_dir=proj_root / "01_frames_fisheye",
                        fov_deg=getattr(settings, "colmap_fisheye_fov_deg", 130.0),
                        raw_fov_deg=getattr(settings, "colmap_fisheye_raw_fov_deg", 190.0),
                        swap_lenses=getattr(settings, "colmap_fisheye_raw_swap_lenses", False),
                        front_profile=_front_profile,
                        back_profile=_back_profile,
                        extraction_settings=_extraction_settings,
                        cancel_event=cancel_event,
                        progress_cb=_fisheye_progress,
                    )
                except Exception as exc:
                    queue_manager.fail_job(job_id, f"Raw fisheye frame extraction failed: {exc}")
                    return
                if cancel_event.is_set():
                    queue_manager.fail_job(job_id, "Cancelled by user")
                    return
                settings.colmap_fisheye_raw_dir = str(_fisheye_raw_dir)

        print(f"⚙️  Pipeline settings: run_vggt={settings.run_vggt} run_brush={settings.run_brush} "
              f"run_postshot={settings.run_postshot} "
              f"yaw_steps={settings.yaw_steps} pitch_angles={settings.pitch_angles}")
        queue_manager.update_job_progress(job_id, 2,
            f"Settings: vggt={settings.run_vggt} brush={settings.run_brush} "
            f"postshot={settings.run_postshot} "
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

        use_colmap      = getattr(settings, "run_colmap",   False)
        use_colmap_fisheye = getattr(settings, "run_colmap_fisheye", False)
        use_gluemap     = getattr(settings, "run_gluemap",  False)
        use_rigsfm      = getattr(settings, "run_rigsfm",   False)
        use_equisfm     = getattr(settings, "run_equisfm",  False)
        _no_sfm         = not settings.run_vggt and not use_colmap and not use_colmap_fisheye and not use_gluemap and not use_rigsfm and not use_equisfm
        use_rs_brush    = _no_sfm and settings.run_brush and not settings.run_postshot
        use_rs_postshot = _no_sfm and settings.run_postshot and not settings.run_brush
        if use_equisfm:
            stage_map = _STAGE_RANGE_EQUISFM_POST_STITCH if insp_count else _STAGE_RANGE_EQUISFM
            print(f"  → Stage map: EquiSfM {'(post-stitch)' if insp_count else '(direct)'}")
        elif use_rigsfm:
            stage_map = _STAGE_RANGE_RIGSFM_POST_STITCH if insp_count else _STAGE_RANGE_RIGSFM
            print(f"  → Stage map: RigSfM {'(post-stitch)' if insp_count else '(direct)'}")
        elif use_rs_brush:
            stage_map = _STAGE_RANGE_RS_BRUSH_POST_STITCH if insp_count else _STAGE_RANGE_RS_BRUSH
            print(f"  → Stage map: RS+Brush {'(post-stitch)' if insp_count else '(direct)'}")
        elif use_rs_postshot:
            stage_map = _STAGE_RANGE_RS_POSTSHOT_POST_STITCH if insp_count else _STAGE_RANGE_RS_POSTSHOT
            print(f"  → Stage map: RS+PostShot {'(post-stitch)' if insp_count else '(direct)'}")
        elif use_colmap:
            stage_map = _STAGE_RANGE_COLMAP_POST_STITCH if insp_count else _STAGE_RANGE_COLMAP
            print(f"  → Stage map: COLMAP {'(post-stitch)' if insp_count else '(direct)'}")
        elif use_colmap_fisheye:
            stage_map = _STAGE_RANGE_COLMAP_FISHEYE_POST_STITCH if insp_count else _STAGE_RANGE_COLMAP_FISHEYE
            print(f"  → Stage map: COLMAP Fisheye {'(post-stitch)' if insp_count else '(direct)'}")
        elif use_gluemap:
            stage_map = _STAGE_RANGE_GLUEMAP_POST_STITCH if insp_count else _STAGE_RANGE_GLUEMAP
            print(f"  → Stage map: GlueMap {'(post-stitch)' if insp_count else '(direct)'}")
        else:
            stage_map = _STAGE_RANGE_POST_STITCH if insp_count else _STAGE_RANGE
            print(f"  → Stage map: VGGT {'(post-stitch)' if insp_count else '(direct)'}")

        _last_stage = [None]  # mutable cell for closure

        def on_progress(sp):
            lo, hi = stage_map.get(sp.stage.value, (0, 97))
            overall = int(lo + (sp.progress / 100) * (hi - lo))
            if sp.stage.value != _last_stage[0]:
                _last_stage[0] = sp.stage.value
                _sep = f"── {sp.stage.value} " + "─" * max(1, 44 - len(sp.stage.value))
                print(f"\n{_sep}")
            print(f"  → {sp.stage.value} {sp.progress}% → overall {overall}% [{lo}–{hi}]")
            _mode = ("equisfm"  if use_equisfm
                     else "rigsfm"   if use_rigsfm
                     else "rs_brush" if use_rs_brush
                     else "rs_brush" if use_rs_postshot
                     else "colmap"   if use_colmap
                     else "colmap_fisheye" if use_colmap_fisheye
                     else "gluemap"  if use_gluemap
                     else "vggt")
            extra = {
                "currentStage": sp.stage.value,
                "pipelineMode": _mode,
            }
            # Write to Firestore only when the stage changes (milestone).
            # All other progress ticks update in-memory only — no Firebase I/O.
            stage_changed = sp.stage.value != _last_stage[0]
            if stage_changed:
                _last_stage[0] = sp.stage.value
            queue_manager.update_job_progress(
                job_id, overall, sp.message[:200],
                extra=extra,
                milestone=stage_changed,
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

                    # Upload camera frustum data (works for all pipeline paths)
                    try:
                        _build_and_upload_cameras_json(proj_root, job_id)
                    except Exception as _cam_exc:
                        print(f"  [cameras] Non-fatal: {_cam_exc}")

                    # Determine the actual field-session date (not the upload date)
                    _db = firebase_client.get_db()
                    _session_date = None
                    try:
                        _session_date = _determine_session_date(job_id, job_data, proj_root, _db)
                    except Exception as _sd_exc:
                        print(f"  [session] Non-fatal: {_sd_exc}")

                    # Publish to the public gallery Firestore collection
                    _pipeline_mode = ("rs_brush" if use_rs_brush
                              else "colmap"   if use_colmap
                              else "colmap_fisheye" if use_colmap_fisheye
                              else "gluemap"  if use_gluemap
                              else "vggt")
                    _publish_to_gallery(job_id, job_data, r2_url, gaussian_count, thumbnail_url,
                                        pipeline_mode=_pipeline_mode,
                                        session_date=_session_date)

                    # Write GPS capture location from mobile job doc
                    try:
                        _write_capture_location(job_id, job_data, _db)
                    except Exception as _loc_exc:
                        print(f"  [location] Non-fatal: {_loc_exc}")

                    # Upload the standalone cameras.html point-cloud/camera
                    # visualizer, if the alignment stage generated one, as a
                    # second, separately-openable viewer alongside the splat.
                    try:
                        _upload_and_link_cameras_html(proj_root, job_id, _db)
                    except Exception as _viz_exc:
                        print(f"  [cameras] Non-fatal: {_viz_exc}")

                    # Auto-fetch Garmin/Coros activity using exact session times.
                    # Requires session start/end — mobile job doc, or (for the
                    # From Camera flow) EXIF-derived times, see _derive_session_times
                    # in server.py. Both fetchers are independent and non-fatal;
                    # run both since a user may have activity history in either.
                    _session_start, _session_end = _get_session_times(job_id, job_data, _db)
                    if _session_start and _session_end:
                        try:
                            from splatpipe_core import garmin_fetcher
                            garmin_fetcher.fetch_and_store(
                                job_id, _session_start.astimezone().date(), _db,
                                session_start=_session_start,
                                session_end=_session_end,
                            )
                        except Exception as _g_exc:
                            print(f"  [garmin] Non-fatal: {_g_exc}")
                        try:
                            from splatpipe_core import coros_fetcher
                            coros_fetcher.fetch_and_store(
                                job_id, _session_start.astimezone().date(), _db,
                                session_start=_session_start,
                                session_end=_session_end,
                            )
                        except Exception as _c_exc:
                            print(f"  [coros] Non-fatal: {_c_exc}")
                    else:
                        print("  [garmin/coros] Skipping — no session times available")
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
        _ts2 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'═'*52}\n PIPELINE FAILED  job={job_id}  {_ts2}\n{'═'*52}\n")
    else:
        _ts2 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'═'*52}\n PIPELINE COMPLETE  job={job_id}  {_ts2}\n{'═'*52}\n")
    finally:
        _cancel_events.pop(job_id, None)
        _threads.pop(job_id, None)
        if hasattr(sys.stdout, "remove_stream"):
            sys.stdout.remove_stream(_run_log)
        if hasattr(sys.stderr, "remove_stream"):
            sys.stderr.remove_stream(_run_log)
        _run_log.flush()
        _run_log.close()
        print(f"  [run-log] {_run_log_path.name}")


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
            # Video jobs have no loose stitched JPEGs in the input dir (just the
            # video file) -- fall back to an extracted frame instead.
            frames_dir = proj_root / "01_frames"
            jpegs = sorted(
                f for f in frames_dir.glob("*") if f.suffix.lower() in (".jpg", ".jpeg", ".png")
            )
        if not jpegs:
            print("  [thumbnail] No stitched JPEGs or extracted frames found, skipping thumbnail")
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
    pipeline_mode: "str | None" = None,
    session_date: "object | None" = None,
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
            "pipelineMode":  pipeline_mode or job_data.get("pipelineMode") or job_data.get("pipeline_mode") or "rs_brush",
            "createdAt":     SERVER_TIMESTAMP,
        }
        if thumbnail_url:
            doc["thumbnailUrl"] = thumbnail_url
        if session_date:
            doc["sessionDate"] = str(session_date)   # ISO "YYYY-MM-DD"
        db.collection("gallery").document(job_id).set(doc)
        print(f"  [gallery] Published to Firestore gallery/{job_id}")
    except Exception as e:
        print(f"  [gallery] Could not publish gallery doc: {e}")


# ── Mobile job helpers ────────────────────────────────────────────────────────

def _determine_session_date(job_id: str, job_data: dict, proj_root: "Path", db) -> "Optional[object]":
    """
    Return the actual field-session date (a datetime.date) in priority order:
      1. Mobile app job startTime (Firestore users/{uid}/jobs/{jobId})
      2. EXIF DateTimeOriginal from extracted frames / views
      3. Project folder mtime (last resort — reflects upload day, not capture day)

    This is stored as sessionDate on the gallery doc so Garmin matching
    and display always use the capture date, not the upload/publish date.
    """
    from datetime import date as _date, datetime as _dt

    # 1. Mobile job startTime
    doc = _get_mobile_job_doc(job_id, job_data, db)
    if doc:
        start_ms = doc.get("startTime")
        if start_ms:
            d = _dt.fromtimestamp(start_ms / 1000).date()
            print(f"  [session] Date from mobile job: {d}")
            return d

    # 2. EXIF DateTimeOriginal from the first JPEG in frames/views
    exif_date = _exif_date_from_project(proj_root)
    if exif_date:
        print(f"  [session] Date from EXIF: {exif_date}")
        return exif_date

    # 3. Folder mtime (fallback — note: this may be the upload day, not capture day)
    try:
        d = _date.fromtimestamp(proj_root.stat().st_mtime)
        print(f"  [session] Date from folder mtime (fallback): {d}")
        return d
    except Exception:
        pass

    return None


def _exif_date_from_project(proj_root: "Path") -> "Optional[object]":
    """Read DateTimeOriginal EXIF from the first JPEG found in frames/views directories."""
    from datetime import datetime as _dt
    from pathlib import Path as _P

    for subdir in ("01_frames", "02_views"):
        d = proj_root / subdir
        if not d.exists():
            continue
        jpegs = sorted(d.rglob("*.jpg"))
        if not jpegs:
            jpegs = sorted(d.rglob("*.jpeg"))
        for jpg in jpegs[:5]:
            try:
                # Try PIL/Pillow first (most reliable)
                from PIL import Image
                from PIL.ExifTags import TAGS
                img = Image.open(jpg)
                exif_data = img._getexif()
                if exif_data:
                    for tag_id, val in exif_data.items():
                        if TAGS.get(tag_id) == "DateTimeOriginal":
                            return _dt.strptime(val, "%Y:%m:%d %H:%M:%S").date()
            except Exception:
                pass
            try:
                # Fallback: read raw EXIF bytes
                with open(jpg, "rb") as f:
                    raw = f.read(65536)
                import re as _re
                m = _re.search(rb"DateTimeOriginal\x00(\d{4}:\d{2}:\d{2})", raw)
                if not m:
                    m = _re.search(rb"(\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2})", raw)
                if m:
                    date_str = m.group(1).decode()[:10].replace(":", "-")
                    from datetime import date as _date
                    parts = date_str.split("-")
                    return _date(int(parts[0]), int(parts[1]), int(parts[2]))
            except Exception:
                pass
    return None


def _get_mobile_job_doc(job_id: str, job_data: dict, db) -> "Optional[dict]":
    """Fetch the mobile app job document from Firestore users/{uid}/jobs/{userJobId}.

    The processing_queue doc ID (job_id) differs from the mobile app's job ID
    (stored as userJobId on the processing_queue doc). Try userJobId first,
    then fall back to job_id for legacy queue docs that pre-date this field.
    """
    from typing import Optional
    uid          = job_data.get("userId")
    user_job_id_ = job_data.get("userJobId")
    print(f"  [mobile] job_data keys: {list(job_data.keys())}")
    print(f"  [mobile] userId={uid!r}  userJobId={user_job_id_!r}")
    if not uid:
        print(f"  [mobile] No userId in job_data — cannot look up mobile job")
        return None
    # Prefer the actual mobile job ID (userJobId on the processing_queue doc)
    user_job_id = job_data.get("userJobId") or job_id
    try:
        snap = db.collection("users").document(uid).collection("jobs").document(user_job_id).get()
        if snap.exists:
            print(f"  [mobile] Found mobile job doc: users/{uid}/jobs/{user_job_id}")
            return snap.to_dict()
        # Fallback: try the queue doc ID itself (legacy)
        if user_job_id != job_id:
            snap2 = db.collection("users").document(uid).collection("jobs").document(job_id).get()
            if snap2.exists:
                print(f"  [mobile] Found mobile job doc (fallback): users/{uid}/jobs/{job_id}")
                return snap2.to_dict()
        print(f"  [mobile] No mobile job doc at users/{uid}/jobs/{user_job_id}")
        return None
    except Exception as e:
        print(f"  [mobile] Could not read job doc: {e}")
        return None


def _get_session_times(job_id: str, job_data: dict, db):
    """
    Return (start, end) as UTC-aware datetimes, or (None, None) if unavailable.

    Priority:
      1. fieldraven.json session_times (persisted at job start — survives resume)
      2. Mobile job doc startTime/endTime from Firestore
    """
    from datetime import datetime, timezone
    import json as _json

    def _ms_to_utc(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

    # 1. fieldraven.json — always preferred on resume
    try:
        fj_path = _job_root(job_id, job_data) / "fieldraven.json"
        if fj_path.exists():
            fj = _json.loads(fj_path.read_text(encoding="utf-8"))
            # _write_stage_progress() nests everything under "stages" — read from
            # there, not the top level, to actually match what gets written.
            st = (fj.get("stages", {}).get("session_times")
                  or fj.get("session_times") or {})
            s_ms, e_ms = st.get("startMs"), st.get("endMs")
            if s_ms and e_ms:
                start, end = _ms_to_utc(s_ms), _ms_to_utc(e_ms)
                print(f"  [mobile] Session from fieldraven.json: "
                      f"{start.astimezone().strftime('%Y-%m-%d %H:%M')} → "
                      f"{end.astimezone().strftime('%H:%M %Z')}")
                return start, end
    except Exception:
        pass

    # 2. Firestore mobile job doc
    doc = _get_mobile_job_doc(job_id, job_data, db)
    if not doc:
        return None, None
    s_ms = doc.get("startTime")
    e_ms = doc.get("endTime")
    if not (s_ms and e_ms):
        return None, None
    start, end = _ms_to_utc(s_ms), _ms_to_utc(e_ms)
    print(f"  [mobile] Session: "
          f"{start.astimezone().strftime('%Y-%m-%d %H:%M')} → "
          f"{end.astimezone().strftime('%H:%M %Z')}")
    return start, end


def _upload_and_link_cameras_html(proj_root: Path, job_id: str, db) -> None:
    """
    Find an already-generated cameras.html (tools/visualize_cameras.py output
    — point cloud, rig sensor gallery, nodal spread chart, base64-embedded
    thumbnails — written by whichever alignment stage ran, when
    colmap_visualize/equivalent is enabled) and upload it to R2 as its own
    standalone artifact, storing the URL on gallery/{job_id} as
    pointCloudViewerUrl.

    Deliberately kept as a second, separately-openable viewer rather than
    merged into the splat viewer's own lightweight cameras.json-driven
    frustum overlay (_build_and_upload_cameras_json) — the two serve
    different purposes (rich standalone point-cloud inspection vs. a quick
    in-scene frustum toggle) and can be opened side by side in separate tabs.
    """
    from . import r2_client
    if not r2_client.is_configured():
        return

    candidates = []
    for subdir in ("03_alignment/colmap", "03_alignment/gluemap",
                   "03_alignment/equisfm", "03_alignment/rigsfm"):
        p = proj_root / subdir / "cameras.html"
        if p.exists():
            candidates.append(p)
    if not candidates:
        candidates = list(proj_root.rglob("cameras.html"))
    if not candidates:
        print("  [cameras] No cameras.html found — skipping point-cloud viewer upload")
        return

    try:
        url = r2_client.upload_cameras_html(candidates[0], job_id)
        db.collection("gallery").document(job_id).update({"pointCloudViewerUrl": url})
        print(f"  [cameras] Point-cloud viewer linked: {url}")
    except Exception as e:
        print(f"  [cameras] cameras.html upload/link failed (non-fatal): {e}")


def _write_capture_location(job_id: str, job_data: dict, db) -> None:
    """
    Write captureLocation to gallery/{job_id} so the web map can show a pin.

    Priority:
      1. Mobile job doc gpsStart/gpsEnd (phone GPS during a FieldRaven field job).
      2. Manually-picked location saved to fieldraven.json at import time (the
         "From Camera" flow's map picker) -- used when the camera itself has no
         GPS. Confirmed empirically: Insta360 .insp/.insv files never carry a
         real GPS fix (GPS EXIF is always zeroed), so this is the only location
         signal for camera-only imports with no linked mobile job.
      3. Neither: leave captureLocation unset. garmin_fetcher.fetch_and_store()
         has its own later fallback using the matched activity's first route
         point, so this isn't the last chance to get a pin.
    """
    lat = lon = None

    doc = _get_mobile_job_doc(job_id, job_data, db)
    if doc:
        gps = doc.get("gpsStart") or doc.get("gpsEnd")
        if gps:
            lat = gps.get("lat")
            lon = gps.get("lon") or gps.get("lng")

    if lat is None or lon is None:
        try:
            fj_path = _job_root(job_id, job_data) / "fieldraven.json"
            if fj_path.exists():
                fj = json.loads(fj_path.read_text(encoding="utf-8"))
                manual = fj.get("stages", {}).get("manual_location") or {}
                if manual.get("lat") is not None and manual.get("lon") is not None:
                    lat, lon = manual["lat"], manual["lon"]
                    print(f"  [location] Using manually-picked location for {job_id}")
        except Exception:
            pass

    if lat is None or lon is None:
        return
    db.collection("gallery").document(job_id).update({
        "captureLocation": {"lat": round(lat, 6), "lon": round(lon, 6)}
    })
    print(f"  [location] Capture pin: {lat:.4f}, {lon:.4f}")


# ── Camera JSON export (all pipeline paths) ───────────────────────────────────

def _quat_to_mat(qw: float, qx: float, qy: float, qz: float):
    """COLMAP quaternion [qw,qx,qy,qz] → 3×3 rotation matrix (world→camera)."""
    import numpy as np
    n = (qw*qw + qx*qx + qy*qy + qz*qz) ** 0.5
    if n < 1e-10:
        return np.eye(3)
    qw, qx, qy, qz = qw/n, qx/n, qy/n, qz/n
    return np.array([
        [1-2*(qy*qy+qz*qz), 2*(qx*qy-qz*qw),   2*(qx*qz+qy*qw)  ],
        [2*(qx*qy+qz*qw),   1-2*(qx*qx+qz*qz), 2*(qy*qz-qx*qw)  ],
        [2*(qx*qz-qy*qw),   2*(qy*qz+qx*qw),   1-2*(qx*qx+qy*qy)],
    ])


def _mat_to_quat(R):
    """3×3 rotation matrix → quaternion [qw,qx,qy,qz]."""
    import numpy as np
    trace = R[0,0] + R[1,1] + R[2,2]
    if trace > 0:
        s = 0.5 / (trace + 1.0) ** 0.5
        w = 0.25 / s
        x = (R[2,1] - R[1,2]) * s
        y = (R[0,2] - R[2,0]) * s
        z = (R[1,0] - R[0,1]) * s
    elif R[0,0] > R[1,1] and R[0,0] > R[2,2]:
        s = 2.0 * (1.0 + R[0,0] - R[1,1] - R[2,2]) ** 0.5
        w = (R[2,1] - R[1,2]) / s; x = 0.25 * s
        y = (R[0,1] + R[1,0]) / s; z = (R[0,2] + R[2,0]) / s
    elif R[1,1] > R[2,2]:
        s = 2.0 * (1.0 + R[1,1] - R[0,0] - R[2,2]) ** 0.5
        w = (R[0,2] - R[2,0]) / s; x = (R[0,1] + R[1,0]) / s
        y = 0.25 * s;               z = (R[1,2] + R[2,1]) / s
    else:
        s = 2.0 * (1.0 + R[2,2] - R[0,0] - R[1,1]) ** 0.5
        w = (R[1,0] - R[0,1]) / s; x = (R[0,2] + R[2,0]) / s
        y = (R[1,2] + R[2,1]) / s; z = 0.25 * s
    return float(w), float(x), float(y), float(z)


def _parse_images_txt(images_txt: Path) -> list:
    """
    Parse COLMAP images.txt → list of (qw,qx,qy,qz,tx,ty,tz,name) tuples.
    Every other data line is POINTS2D — those are skipped.
    'name' is the full image path string (e.g. 'pano_camera0/frame_000001.jpg').
    """
    entries = []
    with images_txt.open(encoding="utf-8", errors="replace") as f:
        skip_next = False
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if skip_next:
                skip_next = False
                continue
            parts = line.split()
            if len(parts) < 10:
                continue
            try:
                qw  = float(parts[1]); qx = float(parts[2])
                qy  = float(parts[3]); qz = float(parts[4])
                tx  = float(parts[5]); ty = float(parts[6]); tz = float(parts[7])
                name = parts[9]
                entries.append((qw, qx, qy, qz, tx, ty, tz, name))
                skip_next = True
            except (ValueError, IndexError):
                continue
    return entries


def _build_and_upload_cameras_json(proj_root: Path, job_id: str) -> None:
    """
    Find images.txt anywhere under the project, convert camera poses to
    Three.js viewer space, and upload cameras.json to R2.

    Works for all pipeline paths (RS, COLMAP, GlueMap, VGGT) because
    they all produce COLMAP-format images.txt before training.

    Three.js viewer applies mesh.rotation.x = π so position transform is:
        viewer_pos = (x_colmap, -y_colmap, -z_colmap)

    The frustum mesh points in its local -Z direction. R_viewer must satisfy:
        R_viewer @ [0,0,-1] = fwd_viewer  →  R_viewer[:,2] = -fwd_viewer
    This requires an extra Rx on the right:
        R_viewer = Rx(π) @ R_cam_to_world @ Rx(π)
    """
    import json, re, tempfile, numpy as np
    from pathlib import PurePosixPath
    from . import r2_client

    def _sensor_frame(image_name: str) -> tuple[str, str]:
        """Return (sensor, frame_key) from COLMAP image path."""
        p = PurePosixPath(image_name)
        if len(p.parts) >= 2:
            return p.parts[0], p.stem          # "pano_camera5", "IMG_1234"
        m = re.match(r'^(pano_camera\d+)_(.+)$', p.stem)
        return (m.group(1), m.group(2)) if m else ('', p.stem)

    def _thumb_b64(img_path: Path, max_px: int = 96) -> str | None:
        """Return a base64 JPEG thumbnail, or None if PIL unavailable / image missing."""
        if not img_path.exists():
            return None
        try:
            import io, base64
            from PIL import Image
            with Image.open(img_path) as im:
                im.thumbnail((max_px, max_px))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="JPEG", quality=60)
                return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    if not r2_client.is_configured():
        return

    # Search order: brush_input → postshot_input → COLMAP_for_Brush → anywhere in 03_/04_
    candidates = []
    for subdir in ("04_training/brush_input", "04_training/postshot_input",
                   "03_alignment/COLMAP_for_Brush"):
        p = proj_root / subdir / "images.txt"
        if p.exists():
            candidates.append(p)
    if not candidates:
        for p in proj_root.rglob("images.txt"):
            candidates.append(p)
    if not candidates:
        print("  [cameras] images.txt not found — skipping cameras.json")
        return

    images_txt = candidates[0]
    print(f"  [cameras] Reading poses from {images_txt.relative_to(proj_root)}")

    # Images live in an "images/" sibling directory next to images.txt
    images_dir = images_txt.parent / "images"
    has_images = images_dir.is_dir()
    if has_images:
        print(f"  [cameras] Thumbnails: {images_dir.relative_to(proj_root)}")
    else:
        print("  [cameras] No images/ directory found — thumbnails skipped")

    raw = _parse_images_txt(images_txt)
    if not raw:
        print("  [cameras] No camera poses parsed — skipping")
        return

    sampled = raw
    print(f"  [cameras] {len(raw)} poses → uploading all")

    Rx = np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1]], dtype=float)

    out = []
    for (qw, qx, qy, qz, tx, ty, tz, _name) in sampled:
        R_cw = _quat_to_mat(qw, qx, qy, qz)  # world→camera
        t    = np.array([tx, ty, tz])
        C    = -R_cw.T @ t                    # camera centre in COLMAP world space

        # Apply Y-flip to position
        px = float(C[0])
        py = float(-C[1])
        pz = float(-C[2])

        # R_viewer maps from frustum local space to viewer world space.
        # The frustum mesh points in its local -Z direction. For it to point
        # along fwd_viewer, we need R_viewer[:,2] = -fwd_viewer.
        # Rx @ R_cw.T has [:,2] = +fwd_viewer (180° wrong).
        # Rx @ R_cw.T @ Rx negates columns 1&2 → [:,2] = -fwd_viewer ✓
        R_viewer = Rx @ R_cw.T @ Rx
        vqw, vqx, vqy, vqz = _mat_to_quat(R_viewer)

        sensor, frame_key = _sensor_frame(_name)
        thumb = _thumb_b64(images_dir / _name) if has_images else None

        entry: dict = {
            "px": round(px, 4), "py": round(py, 4), "pz": round(pz, 4),
            "qw": round(vqw, 5), "qx": round(vqx, 5),
            "qy": round(vqy, 5), "qz": round(vqz, 5),
            "name": _name,
            "sensor": sensor,
            "frame_key": frame_key,
        }
        if thumb:
            entry["thumb"] = thumb
        out.append(entry)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(out, tmp, separators=(",", ":"))
        tmp_path = Path(tmp.name)

    try:
        r2_client.upload_cameras_json(tmp_path, job_id)
    finally:
        tmp_path.unlink(missing_ok=True)

