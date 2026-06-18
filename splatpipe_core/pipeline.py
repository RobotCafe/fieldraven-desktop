"""
Main pipeline orchestrator for SplatPipe.

Stages:
  1. Frame extraction   — video → JPEG frames via FFmpeg
  2. View extraction    — equirectangular frames → perspective crop views
  3. VGGT alignment     — VGGT inference: camera poses + 3D point cloud + GLB
  4. COLMAP export      — write cameras.txt / images.txt / points3D.txt
"""
import os
import sys
import shutil
import threading
from pathlib import Path
from typing import Callable, List, Optional

from .types import PipelineResult, PipelineStage, StageProgress
from .settings import PipelineSettings
from . import insp_convert


# ── Module loader ─────────────────────────────────────────────

_splatpipe_loaded = False


def _load_splatpipe(vggt_app_path: str):
    """Add the SplatPipe App directory to sys.path once."""
    global _splatpipe_loaded
    if not _splatpipe_loaded:
        resolved = str(Path(vggt_app_path).resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
        _splatpipe_loaded = True


# ── Progress helper ───────────────────────────────────────────

def _make_reporter(on_progress: Optional[Callable[[StageProgress], None]]):
    """Return a callable that fires on_progress and logs to stdout."""
    def report(stage: PipelineStage, pct: int, message: str, detail: str = None):
        print(f"  [{stage.value}] {pct}% — {message}")
        if on_progress:
            on_progress(StageProgress(stage=stage, progress=pct, message=message, detail=detail))
    return report


def _estimate_vggt_progress(message: str, total_seen: int) -> int:
    """Map VGGT log messages to rough progress percentages."""
    msg = message.lower()
    if "starting vggt" in msg or "initializing" in msg:
        return 2
    if "loading" in msg and "image" in msg:
        return 10
    if "running vggt inference" in msg:
        return 15
    if "vggt inference complete" in msg:
        return 35
    if "creating 3d point cloud" in msg:
        return 40
    if "point cloud created" in msg:
        return 45
    if "expanding anchor" in msg:
        return 50
    if "rig expansion complete" in msg:
        return 55
    if "applying quality filter" in msg:
        return 60
    if "quality filtering complete" in msg:
        return 70
    if "creating 3d glb" in msg:
        return 80
    if "glb file created" in msg:
        return 90
    if "pipeline completed" in msg:
        return 95
    # Crude fallback: creep up slowly
    return min(88, 2 + total_seen)


# ── Main orchestrator ─────────────────────────────────────────

def run_pipeline(
    job_id: str,
    input_path: str,
    settings: PipelineSettings,
    on_progress: Optional[Callable[[StageProgress], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> PipelineResult:
    """
    Run the full SplatPipe pipeline for one job.

    Args:
        job_id:        Firebase job document ID (also used as the output directory name).
        input_path:    Path to a video file OR a folder of equirectangular images.
        settings:      All pipeline configuration.
        on_progress:   Callback fired on every progress update.
        cancel_event:  Set this to cancel mid-run.

    Returns:
        PipelineResult with success flag, output paths, and stats.
    """
    if cancel_event is None:
        cancel_event = threading.Event()

    report = _make_reporter(on_progress)

    # Load SplatPipe modules from the Tkinter app directory
    _load_splatpipe(settings.vggt_app_path)
    try:
        import video_extraction          # type: ignore
        import panorama_processing       # type: ignore
        from vggt_training import (      # type: ignore
            run_full_pipeline as _vggt_pipeline,
            write_colmap_files,
        )
    except ImportError as exc:
        return PipelineResult(
            success=False, job_id=job_id, output_dir="",
            error=f"SplatPipe modules not importable from '{settings.vggt_app_path}': {exc}",
        )

    # ── Directory layout ──────────────────────────────────────
    job_dir = Path(settings.jobs_base_dir) / job_id
    frames_dir = job_dir / "01_frames"
    views_dir = job_dir / "02_views"
    training_dir = job_dir / "04_training"

    for d in [frames_dir, views_dir, training_dir]:
        d.mkdir(parents=True, exist_ok=True)

    input_p = Path(input_path)
    video_extensions = {'.mp4', '.mov', '.avi', '.insv', '.mkv'}
    is_video = input_p.is_file() and input_p.suffix.lower() in video_extensions

    # ═══════════════════════════════════════════════════════════
    # PRE-STAGE — INSP Conversion (Insta360 raw dual-fisheye → equirectangular)
    # ═══════════════════════════════════════════════════════════
    # If the input directory contains .insp files, convert them to
    # equirectangular JPEGs and redirect the pipeline to use those instead.
    if input_p.is_dir():
        insp_files = insp_convert.find_insp_files(str(input_p))
        if insp_files:
            if not insp_convert.is_available(settings.fusion2sphere_path):
                return PipelineResult(
                    success=False, job_id=job_id, output_dir=str(job_dir),
                    error=(
                        f"INSP files found but fusion2sphere not found at "
                        f"'{settings.fusion2sphere_path}'. "
                        "Download from https://github.com/dorthrithil/fusion2sphere "
                        "and set the path in Settings."
                    ),
                )

            report(PipelineStage.FRAME_EXTRACTION, 0,
                   f"Converting {len(insp_files)} INSP files → equirectangular…")

            def _insp_cb(done: int, total: int, msg: str):
                pct = int(done / total * 100) if total else 0
                report(PipelineStage.FRAME_EXTRACTION, pct, msg)

            converted = insp_convert.convert_folder(
                input_dir=str(input_p),
                output_dir=str(frames_dir),
                fusion_binary=settings.fusion2sphere_path,
                lens_size=settings.insp_lens_size,
                output_width=settings.insp_output_width,
                blend_radius=settings.insp_blend_radius,
                cancel_event=cancel_event,
                progress_callback=_insp_cb,
            )

            if not converted:
                return PipelineResult(
                    success=False, job_id=job_id, output_dir=str(job_dir),
                    error="INSP conversion produced no output images",
                )

            report(PipelineStage.FRAME_EXTRACTION, 100,
                   f"INSP conversion complete — {len(converted)} panoramas")

            # Skip stage 1 — frames already in frames_dir
            source_dir = str(frames_dir)
            is_video = False
            input_p = frames_dir   # redirect so stage 1 is bypassed below

    # ═══════════════════════════════════════════════════════════
    # STAGE 1 — Frame Extraction
    # ═══════════════════════════════════════════════════════════
    if cancel_event.is_set():
        return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

    if is_video:
        report(PipelineStage.FRAME_EXTRACTION, 0, f"Extracting frames from {input_p.name}…")

        def _frame_cb(current, total, ts=None):
            pct = int(current / total * 100) if total else 0
            msg = f"Frame {current}/{total}" + (f" at {ts:.1f}s" if ts else "")
            report(PipelineStage.FRAME_EXTRACTION, pct, msg)

        ok = video_extraction.extract_frames_for_video(
            video_path=str(input_p),
            output_folder=str(frames_dir),
            extraction_method=settings.extraction_method,
            interval_value=settings.interval_value,
            interval_unit=settings.interval_unit,
            frame_count=settings.frame_count,
            frame_format=settings.frame_format,
            ffmpeg_path=settings.ffmpeg_path,
            progress_callback=_frame_cb,
            cancel_event=cancel_event,
        )
        if not ok:
            return PipelineResult(
                success=False, job_id=job_id, output_dir=str(job_dir),
                error="Frame extraction failed or produced no frames",
            )
        report(PipelineStage.FRAME_EXTRACTION, 100, "Frame extraction complete")
        source_dir = str(frames_dir)
    else:
        # Input is already an image folder
        source_dir = str(input_p)
        report(PipelineStage.FRAME_EXTRACTION, 100, f"Using image folder: {input_p.name}")

    # ═══════════════════════════════════════════════════════════
    # STAGE 2 — View Extraction (panorama → perspective crops)
    # ═══════════════════════════════════════════════════════════
    if cancel_event.is_set():
        return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

    image_exts = {'.jpg', '.jpeg', '.png'}
    frame_paths = sorted(
        p for p in Path(source_dir).iterdir()
        if p.suffix.lower() in image_exts
    )

    if not frame_paths:
        return PipelineResult(
            success=False, job_id=job_id, output_dir=str(job_dir),
            error=f"No image frames found in {source_dir}",
        )

    n_frames = len(frame_paths)
    views_per_frame = len(settings.pitch_angles) * settings.yaw_steps
    total_views = n_frames * views_per_frame
    view_counter = [0]   # mutable counter shared across loop closures

    report(PipelineStage.VIEW_EXTRACTION, 0,
           f"Rendering views for {n_frames} frames "
           f"({views_per_frame} views each)…")

    for fi, frame_path in enumerate(frame_paths):
        if cancel_event.is_set():
            return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

        def _view_cb(current_view, total_per_frame, _fi=fi):
            view_counter[0] += 1
            pct = int(view_counter[0] / total_views * 100)
            report(
                PipelineStage.VIEW_EXTRACTION, pct,
                f"Frame {_fi + 1}/{n_frames} — view {current_view + 1}/{total_per_frame}",
            )

        panorama_processing.render_views(
            pano_path=str(frame_path),
            out_root=str(views_dir),
            fov_deg=settings.fov,
            yaw_steps=settings.yaw_steps,
            pitch_angles=settings.pitch_angles,
            export_xmp=False,
            save_images=True,
            cancel_event=cancel_event,
            progress_callback=_view_cb,
        )

    if cancel_event.is_set():
        return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

    report(PipelineStage.VIEW_EXTRACTION, 100,
           f"View extraction complete — {total_views} views rendered")

    # ═══════════════════════════════════════════════════════════
    # STAGE 3 — VGGT Alignment
    # ═══════════════════════════════════════════════════════════
    if not settings.run_vggt:
        report(PipelineStage.VGGT_ALIGNMENT, 100, "VGGT skipped (direct Postshot mode)")
        return PipelineResult(
            success=True, job_id=job_id, output_dir=str(job_dir),
            stats={"views_dir": str(views_dir), "frames": n_frames},
        )

    report(PipelineStage.VGGT_ALIGNMENT, 0, "Starting VGGT pose estimation…")

    # Determine output directory based on downstream training target
    if settings.run_postshot:
        vggt_out = training_dir / "postshot_input"
    elif settings.run_brush:
        vggt_out = training_dir / "brush_input"
    else:
        vggt_out = training_dir / "vggt_output"
    vggt_out.mkdir(parents=True, exist_ok=True)

    # Copy all rendered views into a flat images/ sub-directory expected by COLMAP tools
    colmap_images_dir = vggt_out / "images"
    colmap_images_dir.mkdir(exist_ok=True)
    colmap_ext = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff'}
    for root, _, files in os.walk(str(views_dir)):
        for fname in files:
            if Path(fname).suffix.lower() in colmap_ext:
                src = Path(root) / fname
                dst = colmap_images_dir / fname
                if not dst.exists():
                    shutil.copy2(str(src), str(dst))

    # VGGT progress uses message-content heuristics (it logs strings, not percentages)
    vggt_msg_count = [0]

    def _vggt_cb(message: str) -> bool:
        vggt_msg_count[0] += 1
        pct = _estimate_vggt_progress(message, vggt_msg_count[0])
        report(PipelineStage.VGGT_ALIGNMENT, pct, message[:120])
        return not cancel_event.is_set()

    vggt_result = _vggt_pipeline(
        image_dir=str(views_dir),
        output_dir=str(vggt_out),
        progress_callback=_vggt_cb,
        cancel_event=cancel_event,
        conf_thres=settings.conf_threshold,
        mask_sky=settings.mask_sky,
        mask_black_bg=settings.mask_black_bg,
        mask_white_bg=settings.mask_white_bg,
        prediction_mode=settings.prediction_mode,
        temporal_sequencing=settings.temporal_sequencing,
        enable_sparse=settings.enable_sparse,
        sparse_target_points=settings.sparse_target_points,
        use_anchor_rig=settings.use_anchor_rig,
        anchor_view=settings.anchor_view,
        rig_optimization_min_points=settings.rig_optimization_min_points,
        show_camera=True,
        pitch_angles=settings.pitch_angles,
        yaw_steps=settings.yaw_steps,
        colmap_image_width=settings.colmap_image_width,
        colmap_image_height=settings.colmap_image_height,
    )

    if cancel_event.is_set():
        return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

    if not vggt_result.get("success"):
        return PipelineResult(
            success=False, job_id=job_id, output_dir=str(job_dir),
            error=vggt_result.get("error", "VGGT pipeline returned failure"),
        )

    report(PipelineStage.VGGT_ALIGNMENT, 95, "VGGT complete — preparing COLMAP export…")

    # ═══════════════════════════════════════════════════════════
    # STAGE 4 — COLMAP Export
    # ═══════════════════════════════════════════════════════════
    colmap_msg_count = [0]

    def _colmap_cb(message: str):
        colmap_msg_count[0] += 1
        # COLMAP export has identifiable sub-phases; give rough percentages
        msg = message.lower()
        if "cameras.txt" in msg:
            pct = 20
        elif "projecting" in msg:
            pct = 40
        elif "images.txt" in msg:
            pct = 60
        elif "points3d" in msg:
            pct = 80
        elif "complete" in msg or "saved" in msg:
            pct = 95
        else:
            pct = min(90, 10 + colmap_msg_count[0] * 2)
        report(PipelineStage.COLMAP_EXPORT, pct, message[:120])

    colmap_dir, n_points = write_colmap_files(
        output_dir=str(vggt_out),
        filtered_points=vggt_result["filtered_points"],
        filtered_colors=vggt_result["filtered_colors"],
        camera_poses_c2w=vggt_result["num_cameras_processed_poses_c2w"],
        camera_intrinsics=vggt_result["final_intrinsic"],
        image_names=vggt_result["expanded_image_names"],
        progress_callback=_colmap_cb,
        colmap_image_width=settings.colmap_image_width,
        colmap_image_height=settings.colmap_image_height,
        use_anchor_rig=settings.use_anchor_rig,
        predictions_dict=vggt_result.get("raw_predictions"),
    )

    report(PipelineStage.COLMAP_EXPORT, 100,
           f"COLMAP export complete — {n_points:,} 3D points")

    return PipelineResult(
        success=True,
        job_id=job_id,
        output_dir=str(job_dir),
        glb_path=vggt_result.get("glb_path"),
        colmap_dir=colmap_dir,
        stats={
            "frames_extracted": n_frames,
            "views_rendered": total_views,
            "point_count": n_points,
        },
    )
