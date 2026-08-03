"""
COLMAP Fisheye alignment engine — cloned from colmap_runner.py's rig mode,
but operating on genuine raw, un-stitched per-lens fisheye frames instead of
synthetic pinhole crops rendered from a stitched equirectangular panorama.

Why this can't just reuse 02_views/: colmap_runner.py's rig mode consumes
02_views/, which panorama_processing renders as perspective crops FROM
01_frames/ (already-stitched equirectangular panoramas -- confirmed via
pipeline.py's ensure_frames_extracted() calling video_extraction on the
source video directly, and ensure_views_extracted() cropping perspective
views from those frames). Those crops have zero real fisheye distortion
left for a calibration profile to correct.

This mode instead requires the caller to point `colmap_fisheye_raw_dir` at
a folder already containing `front/` and `back/` subfolders of raw fisheye
frames -- e.g. frames extracted directly from the camera's raw (pre-stitch)
video, or a folder built up via FieldRaven mobile's live calibration
capture flow. There is currently no automated step anywhere in this
pipeline that derives raw per-lens frames from a normal job's video import
(that import always produces already-stitched equirect frames) -- teaching
frame extraction to do that is a separate, camera-format-specific task.

Two lenses only (X4 front/back), fixed rig geometry -- not a computed
yaw/pitch sweep like the pano-crop case. Front is the rig reference sensor
(identity); back is offset by a fixed ~180° yaw, a physical constant of the
X4's lens placement, not a per-job setting.

**Python environment split** (same as colmap_runner.py): this module runs
under Python 3.13 (server process); all pycolmap work is delegated to
colmap_fisheye_worker.py under Python 3.14 / pycolmap 4.x via subprocess.
"""
import subprocess
import json
import queue
import re
import shutil
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .types import PipelineStage
from .settings import PipelineSettings

_PYTHON_314 = "C:\\Python314\\python.exe"
_WORKER     = str(Path(__file__).parent / "colmap_fisheye_worker.py")

_IMG_EXTS = {".jpg", ".jpeg", ".png"}


def _first_image_size(image_dir: Path) -> tuple[int, int]:
    from PIL import Image
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        for p in sorted(image_dir.rglob(ext)):
            try:
                with Image.open(p) as im:
                    return im.size  # (width, height)
            except Exception:
                continue
    raise RuntimeError(f"No readable images found under {image_dir}")


def _copy_raw_fisheye_frames(raw_dir: Path, image_dir: Path) -> int:
    """
    Copy front/back raw fisheye frames from raw_dir into image_dir in the
    per-sensor subfolder layout COLMAP's PER_FOLDER camera mode expects.

    raw_dir/front/*.jpg, raw_dir/back/*.jpg  →  image_dir/front/*.jpg, image_dir/back/*.jpg

    Returns the number of sensors found (0, 1, or 2). Skips immediately if
    image_dir already has both subfolders populated from a previous run.
    """
    front_src, back_src = raw_dir / "front", raw_dir / "back"
    front_dst, back_dst = image_dir / "front", image_dir / "back"

    if front_dst.exists() and back_dst.exists() and any(front_dst.glob("*.jpg")) and any(back_dst.glob("*.jpg")):
        return 2

    if not front_src.exists() or not back_src.exists():
        raise RuntimeError(
            f"colmap_fisheye_raw_dir must contain 'front/' and 'back/' subfolders "
            f"of raw fisheye frames — checked {raw_dir}"
        )

    n_sensors = 0
    for src, dst in ((front_src, front_dst), (back_src, back_dst)):
        dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for img in sorted(src.iterdir()):
            if img.suffix.lower() in _IMG_EXTS:
                shutil.copy2(img, dst / img.name)
                copied += 1
        if copied:
            n_sensors += 1
    return n_sensors


def _fixed_dual_lens_rig() -> dict:
    """
    Fixed 2-sensor rig geometry for the X4's front/back fisheye lenses --
    a physical constant of the hardware, unlike the pano-crop case's
    computed yaw_steps/pitch_angles sweep. Front is the reference sensor
    (identity cam_from_rig); back is rotated 180° about the vertical (yaw)
    axis relative to front.
    """
    identity = np.eye(3, dtype=np.float64)
    yaw_180 = np.array([
        [-1.0, 0.0,  0.0],
        [ 0.0, 1.0,  0.0],
        [ 0.0, 0.0, -1.0],
    ], dtype=np.float64)
    return {
        "rotations":      [identity.tolist(), yaw_180.tolist()],
        "image_prefixes": ["front/", "back/"],
    }


def run_colmap_fisheye_pipeline(
    raw_dir: Path,
    colmap_dir: Path,
    brush_input_dir: Path,
    settings: PipelineSettings,
    report: Callable[[PipelineStage, int, str], None],
    cancel_event: threading.Event,
) -> None:
    """
    Run the COLMAP Fisheye alignment pipeline and populate brush_input_dir.

    Args:
        raw_dir:         folder containing front/ and back/ subfolders of raw,
                          un-stitched fisheye frames (settings.colmap_fisheye_raw_dir)
        colmap_dir:      03_alignment/colmap_fisheye/ — working directory
        brush_input_dir: 04_training/brush_input/ — output destination
        settings:        pipeline configuration (colmap_fisheye_matcher,
                          colmap_fisheye_front_profile, colmap_fisheye_back_profile, …)
        report:          progress callback (stage, pct, message)
        cancel_event:    set to abort
    """
    report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 0, "Starting COLMAP Fisheye alignment…")

    front_profile_name = getattr(settings, "colmap_fisheye_front_profile", "")
    back_profile_name  = getattr(settings, "colmap_fisheye_back_profile", "")
    if not front_profile_name or not back_profile_name:
        raise RuntimeError(
            "COLMAP Fisheye mode requires both a front and back lens calibration "
            "profile — save them from the Lens Calibration tab first."
        )

    from .calibration_profiles import load_profile
    front_profile = load_profile(front_profile_name)
    back_profile  = load_profile(back_profile_name)
    if front_profile is None or back_profile is None:
        raise RuntimeError(
            f"Calibration profile not found: "
            f"{'front:' + front_profile_name if front_profile is None else ''} "
            f"{'back:' + back_profile_name if back_profile is None else ''}".strip()
        )

    colmap_dir.mkdir(parents=True, exist_ok=True)
    image_dir     = colmap_dir / "images"
    database_path = colmap_dir / "database.db"
    sparse_path   = colmap_dir / "sparse"
    sparse_txt    = colmap_dir / "sparse_txt"

    report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 5,
           f"Copying raw fisheye frames from {raw_dir}…")
    n_sensors = _copy_raw_fisheye_frames(raw_dir, image_dir)
    if n_sensors != 2:
        raise RuntimeError(
            f"Expected front/ and back/ raw fisheye frame folders under {raw_dir}, "
            f"found {n_sensors} populated sensor folder(s)."
        )
    if cancel_event.is_set(): return

    img_w, img_h = _first_image_size(image_dir)
    rig = _fixed_dual_lens_rig()
    rig["image_width"]  = img_w
    rig["image_height"] = img_h
    rig["front_params"] = [
        front_profile["fx"], front_profile["fy"], front_profile["cx"], front_profile["cy"],
        front_profile["k1"], front_profile["k2"], front_profile["k3"], front_profile["k4"],
    ]
    rig["back_params"] = [
        back_profile["fx"], back_profile["fy"], back_profile["cx"], back_profile["cy"],
        back_profile["k1"], back_profile["k2"], back_profile["k3"], back_profile["k4"],
    ]

    report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 12,
           f"Spawning Python 3.14 / pycolmap worker (2 sensors, "
           f"{settings.colmap_fisheye_matcher} matcher)…")

    sparse_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "database_path":   str(database_path),
        "image_path":      str(image_dir),
        "sparse_path":     str(sparse_path),
        "sparse_txt_path": str(sparse_txt),
        "colmap_matcher":  settings.colmap_fisheye_matcher,
        "colmap_bin":      settings.colmap_bin or "",
        "colmap_mapper":   getattr(settings, "colmap_mapper", "incremental"),
        "vocab_tree_path":    getattr(settings, "colmap_vocab_tree", "") or "",
        "vocab_tree_enabled": getattr(settings, "colmap_vocab_tree_enabled", True),
        "rig":             rig,
    }

    # ── spawn worker (mirrors colmap_runner.py's _run_perspective_rig) ───────
    process = subprocess.Popen(
        [_PYTHON_314, "-P", _WORKER, json.dumps(payload)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    _stderr_q: queue.SimpleQueue[str] = queue.SimpleQueue()

    def _pipe_stderr():
        for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                _stderr_q.put(line)

    _stderr_thread = threading.Thread(target=_pipe_stderr, daemon=True)
    _stderr_thread.start()

    _GLOG = re.compile(r'^[IWEF]\d{8} [\d:.]+ +\d+ [^]]+\] ')

    def _forward_stderr(current_pct: int) -> None:
        while not _stderr_q.empty():
            raw_msg = _stderr_q.get_nowait()
            msg = _GLOG.sub("", raw_msg).strip()
            if msg:
                report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, current_pct, f"[colmap] {msg}")

    worker_lines = []
    current_pct  = 12
    while True:
        raw = process.stdout.readline()
        if not raw and process.poll() is not None:
            break
        if not raw:
            _forward_stderr(current_pct)
            continue
        line = raw.decode("utf-8", errors="replace")
        clean = line.strip()
        if clean.startswith("WORKER_PROGRESS:"):
            parts = clean.split(":", 2)
            try:
                current_pct = int(parts[1])
                msg = parts[2] if len(parts) > 2 else ""
            except (ValueError, IndexError):
                msg = clean
            _forward_stderr(current_pct)
            report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, current_pct, msg)
            if cancel_event.is_set():
                process.terminate()
                return
        elif clean:
            worker_lines.append(clean)
        _forward_stderr(current_pct)

    _stderr_thread.join(timeout=2)
    _forward_stderr(current_pct)
    process.wait()

    if process.returncode != 0:
        stderr_lines = []
        while not _stderr_q.empty():
            stderr_lines.append(_stderr_q.get_nowait())
        worker_error = ""
        if worker_lines:
            try:
                err_result = json.loads(worker_lines[-1])
                worker_error = err_result.get("error", "")
                tb = err_result.get("traceback", "")
                if tb:
                    worker_error = f"{worker_error}\n{tb}"
            except (json.JSONDecodeError, TypeError):
                worker_error = "\n".join(worker_lines[-5:])
        raise RuntimeError(
            f"COLMAP Fisheye worker exited with code {process.returncode}:\n"
            + (worker_error or "\n".join(stderr_lines))
        )

    try:
        result = json.loads(worker_lines[-1])
        if not result.get("success"):
            raise RuntimeError(
                f"Worker reported failure:\n{result.get('error')}\n"
                f"{result.get('traceback', '')}"
            )
        n_imgs = result.get("images", "?")
        n_pts  = result.get("points3D", "?")
    except (IndexError, json.JSONDecodeError):
        raise RuntimeError(
            "Worker did not return a valid JSON completion status. "
            f"Last lines: {worker_lines[-3:]}"
        )

    if cancel_event.is_set(): return

    # No global level-correction pass in v1 (see module docstring) — that
    # logic in colmap_runner.py hardcodes "pano_cameraN" folder-name parsing
    # and isn't generic to front/back naming. Revisit if leveling turns out
    # to matter for this mode once real results are in.

    report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 93,
           f"COLMAP Fisheye: {n_imgs} images, {n_pts} points — finalising brush input…")
    if brush_input_dir.exists():
        shutil.rmtree(str(brush_input_dir))
    shutil.copytree(str(sparse_txt), str(brush_input_dir))
    images_dst = brush_input_dir / "images"
    if not images_dst.exists() and image_dir.exists():
        shutil.copytree(str(image_dir), str(images_dst))
