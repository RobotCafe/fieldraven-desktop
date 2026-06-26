"""
Main pipeline orchestrator for SplatPipe.

Stages:
  1. Frame extraction   — video → JPEG frames via FFmpeg
  2. View extraction    — equirectangular frames → perspective crop views
  3. VGGT alignment     — VGGT inference: camera poses + 3D point cloud + GLB
  4. COLMAP export      — write cameras.txt / images.txt / points3D.txt
"""
import os
import re
import subprocess
import sys
import shutil
import time
import threading
from pathlib import Path
from typing import Callable, List, Optional

from .types import PipelineResult, PipelineStage, StageProgress
from .settings import PipelineSettings

_WIN_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0


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
    """Return a callable that fires on_progress and logs to stdout.

    Firebase writes are deduplicated: only pushes when (stage, pct) changes.
    Without this, every COLMAP log line triggers a synchronous Firebase write,
    filling the stdout pipe buffer and stalling the COLMAP subprocess.
    """
    _last: list = [None]  # (stage, pct) of last Firebase push

    def report(stage: PipelineStage, pct: int, message: str, detail: str = None):
        print(f"  [{stage.value}] {pct}% — {message}")
        if on_progress:
            key = (stage, pct)
            if key != _last[0]:
                _last[0] = key
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


# ── RealityScan stage ─────────────────────────────────────────

def _run_realityscan(
    views_dir: Path,
    alignment_dir: Path,
    settings: PipelineSettings,
    report,
    cancel_event: threading.Event,
) -> bool:
    """Launch RealityScan, stream its stdout line-by-line, wait for exit."""
    if not settings.rs_path:
        raise RuntimeError("rs_path is not configured — set RealityScan.exe path in Settings")
    rs_exe = Path(settings.rs_path)
    if not rs_exe.exists():
        raise FileNotFoundError(f"RealityScan not found: {rs_exe}")

    if not settings.rs_settings_path:
        raise RuntimeError("rs_settings_path is not configured — set the RS settings folder in Settings")
    rs_settings = Path(settings.rs_settings_path)
    if not rs_settings.exists():
        raise FileNotFoundError(f"RS settings folder not found: {rs_settings}")

    colmap_xml_path = rs_settings / "360_Pipe_COLMAP.xml"
    if not colmap_xml_path.exists():
        raise FileNotFoundError(
            f"360_Pipe_COLMAP.xml not found in RS settings folder: {rs_settings}\n"
            f"  Expected: {colmap_xml_path}"
        )

    alignment_dir.mkdir(parents=True, exist_ok=True)
    colmap_out = alignment_dir / "COLMAP_for_Brush"

    # Clear any stale export from a previous run so a fresh failure is detectable
    if colmap_out.exists():
        shutil.rmtree(str(colmap_out))
    colmap_out.mkdir(parents=True, exist_ok=True)

    project_file = str(alignment_dir / "RS.rcproj")
    colmap_txt   = str(colmap_out / "RS.txt")
    colmap_xml   = str(colmap_xml_path)

    command = [
        str(rs_exe),
        "-addFolder", str(views_dir),
        "-align",
        "-save", project_file,
        "-exportRegistration", colmap_txt, colmap_xml,
        "-quit",
    ]

    report(PipelineStage.REALITYSCAN, 0,
           f"Starting RealityScan alignment ({views_dir.name})…")
    print(f"  [realityscan] CMD: {' '.join(command)}")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=_WIN_NO_WINDOW,
    )

    line_count = 0
    for raw in iter(process.stdout.readline, ""):
        if cancel_event.is_set():
            process.kill()
            return False
        line = raw.strip()
        if not line:
            continue
        line_count += 1
        pct = min(90, 2 + line_count)
        report(PipelineStage.REALITYSCAN, pct, f"RS: {line[:200]}")

    process.stdout.close()
    rc = process.wait()
    if rc != 0:
        raise RuntimeError(f"RealityScan exited with code {rc}")
    return True


# ── Brush training stage ──────────────────────────────────────

def _run_brush_training(
    training_dir: Path,
    settings: PipelineSettings,
    report,
    cancel_event: threading.Event,
) -> bool:
    """Launch Brush, monitor stdout + PLY file exports for progress."""
    import queue as _queue

    if not settings.brush_path:
        raise RuntimeError("brush_path is not configured — set Brush.exe path in Settings")
    brush_exe = Path(settings.brush_path)
    if not brush_exe.exists():
        raise FileNotFoundError(f"Brush not found: {brush_exe}")

    brush_input = training_dir / "brush_input"
    if not brush_input.exists():
        raise FileNotFoundError(f"Brush input directory not found: {brush_input}")

    total_steps  = settings.brush_total_steps
    export_every = settings.brush_export_every

    command = [
        str(brush_exe),
        str(brush_input),
        "--total-steps",    str(total_steps),
        "--max-splats",     str(settings.brush_max_splats),
        "--max-resolution", str(settings.brush_max_resolution),
        "--seed",           str(settings.brush_seed),
        "--export-path",    str(training_dir),
        "--export-every",   str(export_every),
    ]
    if settings.brush_rerun_logging:
        command.append("--rerun-enabled")
    if settings.brush_spawn_viewer:
        command.append("--with-viewer")

    creation_flags = 0 if settings.brush_spawn_viewer else _WIN_NO_WINDOW

    # Kill any Brush instance left over from a previous job (e.g. a viewer
    # window that stayed open after we moved on at the final training step)
    # so it doesn't hold onto GPU memory while this job trains.
    subprocess.run(
        ["taskkill", "/IM", brush_exe.name, "/F"],
        capture_output=True, creationflags=_WIN_NO_WINDOW,
    )

    report(PipelineStage.BRUSH_TRAINING, 0,
           f"Starting Brush training ({total_steps:,} steps, export every {export_every:,})…")
    print(f"  [brush_training] CMD: {' '.join(command)}")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creation_flags,
        cwd=str(training_dir),
    )

    stdout_q = _queue.Queue()

    def _drain():
        try:
            for line in iter(process.stdout.readline, ""):
                if line.strip():
                    stdout_q.put(line.strip())
        finally:
            stdout_q.put(None)  # sentinel

    threading.Thread(target=_drain, daemon=True).start()

    current_pct = [0]
    last_poll   = time.time()
    reached_final_step = False

    while process.poll() is None:
        if cancel_event.is_set():
            process.kill()
            return False

        # Drain stdout non-blocking
        try:
            while True:
                line = stdout_q.get_nowait()
                if line is None:
                    break
                print(f"  [brush] {line}")
                report(PipelineStage.BRUSH_TRAINING, current_pct[0], f"Brush: {line[:200]}")
        except _queue.Empty:
            pass

        # Poll PLY files every 10s for step-based progress
        if time.time() - last_poll >= 10.0:
            last_poll = time.time()
            try:
                max_step = 0
                for f in training_dir.glob("*.ply"):
                    m = re.search(r'[_\-](\d+)\.ply$', f.name, re.IGNORECASE)
                    if m:
                        max_step = max(max_step, int(m.group(1)))
                if max_step >= total_steps:
                    # Final export written — training is done even if the Brush
                    # process itself stays alive (e.g. --with-viewer keeps its
                    # window open). Stop waiting on the process and move on.
                    reached_final_step = True
                    break
                if max_step > 0:
                    pct = min(99, int(max_step / total_steps * 100))
                    current_pct[0] = pct
                    report(PipelineStage.BRUSH_TRAINING, pct,
                           f"Brush training: step {max_step:,}/{total_steps:,} ({pct}%)")
                else:
                    report(PipelineStage.BRUSH_TRAINING, max(1, current_pct[0]),
                           "Brush training in progress…")
            except Exception:
                pass

        time.sleep(2)

    if reached_final_step:
        ply_files = list(training_dir.glob("*.ply"))
        print(f"  [brush_training] Reached final step {total_steps:,} — "
              f"marking complete (Brush process/viewer left running)")
    else:
        rc = process.wait()
        ply_files = list(training_dir.glob("*.ply"))

        if not ply_files:
            raise RuntimeError(
                f"Brush exited with code {rc} and produced no PLY output. "
                f"Verify brush_input/ contains a valid COLMAP structure "
                f"(cameras.txt/images.txt + images/ folder)."
            )

    report(PipelineStage.BRUSH_TRAINING, 100,
           f"Brush training complete — {len(ply_files)} splat file(s) exported")
    return True


# ── Main orchestrator ─────────────────────────────────────────

def run_pipeline(
    job_id: str,
    input_path: str,
    settings: PipelineSettings,
    on_progress: Optional[Callable[[StageProgress], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    start_from: str = '',
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
        import video_extraction    # type: ignore
        import panorama_processing # type: ignore
    except ImportError as exc:
        return PipelineResult(
            success=False, job_id=job_id, output_dir="",
            error=f"SplatPipe modules not importable from '{settings.vggt_app_path}': {exc}",
        )

    # ── Directory layout ──────────────────────────────────────
    # Use the user-selected project_dir when set (FieldRaven flow), otherwise
    # fall back to the shared jobs base dir keyed by job_id (standalone use).
    if settings.project_dir:
        job_dir = Path(settings.project_dir)
    else:
        job_dir = Path(settings.jobs_base_dir) / job_id
    frames_dir = job_dir / "01_frames"
    views_dir = job_dir / "02_views"
    training_dir = job_dir / "04_training"

    for d in [frames_dir, views_dir, job_dir / "03_alignment", training_dir]:
        d.mkdir(parents=True, exist_ok=True)

    input_p = Path(input_path)
    video_extensions = {'.mp4', '.mov', '.avi', '.insv', '.mkv'}
    is_video = input_p.is_file() and input_p.suffix.lower() in video_extensions

    _img_exts = {'.jpg', '.jpeg', '.png'}

    # Determine which stages to skip when resuming from a saved state.
    # Each key implies all earlier stages are also skipped.
    _SKIP_STAGE1 = {'view_extraction', 'realityscan', 'brush_training', 'vggt_alignment', 'colmap_export', 'colmap_alignment'}
    _SKIP_STAGE2 = {'realityscan', 'brush_training', 'vggt_alignment', 'colmap_export', 'colmap_alignment'}
    skip_stage1  = start_from in _SKIP_STAGE1
    skip_stage2  = start_from in _SKIP_STAGE2
    skip_colmap  = start_from == 'brush_training'
    skip_rs      = start_from == 'brush_training'

    # ═══════════════════════════════════════════════════════════
    # STAGE 1 — Frame Extraction
    # ═══════════════════════════════════════════════════════════
    if cancel_event.is_set():
        return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

    n_frames = 0
    if skip_stage1:
        report(PipelineStage.FRAME_EXTRACTION, 100, "Frame extraction skipped — resuming from saved state")
        # Prefer frames_dir (video case); fall back to input folder (image-folder case)
        source_dir = str(frames_dir) if any(
            f.suffix.lower() in _img_exts for f in frames_dir.rglob("*") if f.is_file()
        ) else str(input_p)
        n_frames = sum(1 for f in Path(source_dir).iterdir() if f.is_file() and f.suffix.lower() in _img_exts)
    elif is_video:
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

    total_views = 0
    if skip_stage2:
        report(PipelineStage.VIEW_EXTRACTION, 100, "View extraction skipped — resuming from saved state")
        total_views = sum(1 for f in views_dir.rglob("*") if f.is_file() and f.suffix.lower() in _img_exts) if views_dir.exists() else 0
    else:
        frame_paths = sorted(
            p for p in Path(source_dir).iterdir()
            if p.suffix.lower() in _img_exts
        )

        if not frame_paths:
            return PipelineResult(
                success=False, job_id=job_id, output_dir=str(job_dir),
                error=f"No image frames found in {source_dir}",
            )

        n_frames = len(frame_paths)
        views_per_frame = len(settings.pitch_angles) * settings.yaw_steps + (1 if getattr(settings, "horizon_ref", False) else 0)
        total_views = n_frames * views_per_frame
        view_counter = [0]   # mutable counter shared across loop closures

        report(PipelineStage.VIEW_EXTRACTION, 0,
               f"Rendering views for {n_frames} frames "
               f"({views_per_frame} views each)…")

        for fi, frame_path in enumerate(frame_paths):
            if cancel_event.is_set():
                return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

            def _view_cb(current_view, total_per_frame, _fi=fi):
                if current_view < total_per_frame:  # skip the per-frame completion ping
                    view_counter[0] += 1
                pct = min(int(view_counter[0] / total_views * 100), 100)
                display = min(current_view + 1, total_per_frame)
                report(
                    PipelineStage.VIEW_EXTRACTION, pct,
                    f"Frame {_fi + 1}/{n_frames} — view {display}/{total_per_frame}",
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
                horizon_ref=getattr(settings, "horizon_ref", False),
            )

        if cancel_event.is_set():
            return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

        report(PipelineStage.VIEW_EXTRACTION, 100,
               f"View extraction complete — {total_views} views rendered")

    # ═══════════════════════════════════════════════════════════
    # STAGE 3 — COLMAP alignment  (when run_colmap=True)
    # ═══════════════════════════════════════════════════════════
    if settings.run_colmap:
        if cancel_event.is_set():
            return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

        from .colmap_runner import run_colmap_pipeline  # type: ignore

        colmap_dir      = job_dir / "03_alignment" / "colmap"
        brush_input_dir = training_dir / "brush_input"

        if skip_colmap:
            report(PipelineStage.COLMAP_ALIGNMENT, 100,
                   "COLMAP alignment skipped — resuming from saved brush_input/")
        else:
            run_colmap_pipeline(
                frames_dir=frames_dir,
                views_dir=views_dir,
                colmap_dir=colmap_dir,
                brush_input_dir=brush_input_dir,
                settings=settings,
                report=report,
                cancel_event=cancel_event,
                project_dir=job_dir,
            )

            if cancel_event.is_set():
                return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

        colmap_files = list(brush_input_dir.glob("*.txt")) if brush_input_dir.exists() else []
        if not colmap_files:
            return PipelineResult(
                success=False, job_id=job_id, output_dir=str(job_dir),
                error="COLMAP produced no text files in brush_input/. "
                      "Check that pycolmap is installed and reconstruction succeeded.",
            )

        report(PipelineStage.COLMAP_ALIGNMENT, 100,
               f"COLMAP complete — {len(colmap_files)} file(s) in brush_input/")

        if not settings.run_brush and not skip_colmap:
            return PipelineResult(
                success=True, job_id=job_id, output_dir=str(job_dir),
                stats={"frames_extracted": n_frames, "views_rendered": total_views},
            )

        _run_brush_training(training_dir, settings, report, cancel_event)
        return PipelineResult(
            success=True, job_id=job_id, output_dir=str(job_dir),
            stats={"frames_extracted": n_frames, "views_rendered": total_views},
        )

    # ═══════════════════════════════════════════════════════════
    # STAGE 3 — RealityScan alignment  (when run_vggt=False)
    # ═══════════════════════════════════════════════════════════
    if not settings.run_vggt:
        if not settings.run_brush:
            # Nothing further to do — caller just wanted views
            report(PipelineStage.VGGT_ALIGNMENT, 100, "Alignment skipped — no training selected")
            return PipelineResult(
                success=True, job_id=job_id, output_dir=str(job_dir),
                stats={"frames_extracted": n_frames, "views_rendered": total_views},
            )

        alignment_dir   = job_dir / "03_alignment"
        brush_input_dir = training_dir / "brush_input"
        colmap_src      = alignment_dir / "COLMAP_for_Brush"

        if settings.use_rig_xmp:
            try:
                from xmp_rig_export import export_all_frame_rigs  # type: ignore
                report(PipelineStage.REALITYSCAN, 0, "Generating XMP rig sidecar files…")
                export_all_frame_rigs(str(views_dir), settings.pitch_angles, settings.yaw_steps,
                                      horizon_ref=getattr(settings, "horizon_ref", False))
                report(PipelineStage.REALITYSCAN, 2, "XMP rig files generated — starting RS alignment")
            except ImportError as exc:
                print(f"  [xmp_rig] Warning: xmp_rig_export not importable: {exc}")

        if skip_rs:
            # Resume from Brush: verify existing COLMAP export and copy to brush_input
            colmap_files = list(colmap_src.iterdir()) if colmap_src.exists() else []
            print(f"  [resume] COLMAP_for_Brush has {len(colmap_files)} item(s): "
                  f"{[f.name for f in colmap_files[:10]]}")
            if not colmap_files:
                return PipelineResult(
                    success=False, job_id=job_id, output_dir=str(job_dir),
                    error=(
                        "Cannot resume from Brush: COLMAP_for_Brush is missing or empty. "
                        "RealityScan must have completed successfully before resuming from Brush."
                    ),
                )
            report(PipelineStage.REALITYSCAN, 100,
                   f"RealityScan output verified ({len(colmap_files)} items) — resuming from Brush…")
            if brush_input_dir.exists():
                shutil.rmtree(str(brush_input_dir))
            shutil.copytree(str(colmap_src), str(brush_input_dir))
            print(f"  [resume] Copied {len(colmap_files)} items → {brush_input_dir}")
        else:
            _run_realityscan(views_dir, alignment_dir, settings, report, cancel_event)

            if cancel_event.is_set():
                return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

            # Log every file RS produced so failures are diagnosable from the console
            print(f"  [realityscan] Output contents of {alignment_dir}:")
            all_rs_files = sorted(alignment_dir.rglob("*"))
            for f in all_rs_files[:40]:
                size = f"{f.stat().st_size:,} B" if f.is_file() else "<dir>"
                print(f"    {f.relative_to(alignment_dir)}  {size}")
            if len(all_rs_files) > 40:
                print(f"    … and {len(all_rs_files) - 40} more items")

            # Transfer RS COLMAP output → brush_input
            colmap_files = list(colmap_src.iterdir()) if colmap_src.exists() else []
            print(f"  [realityscan] COLMAP_for_Brush has {len(colmap_files)} item(s): "
                  f"{[f.name for f in colmap_files[:10]]}")

            if not colmap_files:
                candidates = [p for p in alignment_dir.rglob("cameras.txt")]
                hint = f" (found cameras.txt at {candidates[0].parent})" if candidates else ""
                return PipelineResult(
                    success=False, job_id=job_id, output_dir=str(job_dir),
                    error=(
                        f"RealityScan produced no COLMAP export in COLMAP_for_Brush/{hint}. "
                        f"Check that 360_Pipe_COLMAP.xml is valid and RS alignment succeeded."
                    ),
                )

            report(PipelineStage.REALITYSCAN, 100, "RealityScan complete — preparing Brush input…")
            if brush_input_dir.exists():
                shutil.rmtree(str(brush_input_dir))
            shutil.copytree(str(colmap_src), str(brush_input_dir))
            print(f"  [realityscan] Copied {len(colmap_files)} items → {brush_input_dir}")

        # ═══════════════════════════════════════════════════════
        # STAGE 4 — Brush training
        # ═══════════════════════════════════════════════════════
        print(f"  [pipeline] Calling Brush training in {training_dir}")
        _run_brush_training(training_dir, settings, report, cancel_event)

        if cancel_event.is_set():
            return PipelineResult(success=False, job_id=job_id, output_dir=str(job_dir), error="Cancelled")

        return PipelineResult(
            success=True, job_id=job_id, output_dir=str(job_dir),
            stats={"frames_extracted": n_frames, "views_rendered": total_views},
        )

    # ═══════════════════════════════════════════════════════════
    # STAGE 3 — VGGT Alignment  (when run_vggt=True)
    # ═══════════════════════════════════════════════════════════
    try:
        from vggt_training import (  # type: ignore
            run_full_pipeline as _vggt_pipeline,
            write_colmap_files,
        )
    except ImportError as exc:
        return PipelineResult(
            success=False, job_id=job_id, output_dir=str(job_dir),
            error=f"VGGT module not importable: {exc}",
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
