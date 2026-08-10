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

Calibration is optional (settings.colmap_fisheye_use_calibration): with it
on (default), both lenses need a saved profile from the Lens Calibration tab
and their intrinsics are locked during BA. With it off, both lenses are
seeded with a rough guessed intrinsic and bundle adjustment self-calibrates
focal/principal-point/distortion instead -- lets the pipeline be exercised
end-to-end without running a real calibration first, at the cost of accuracy
(see colmap_fisheye_worker.py's calibrated flag).

Post-processing (leveling, scene-geometry orientation refinement, GPS
geo-registration, camera visualizer) mirrors colmap_runner.py's plain rig
mode and shares its settings fields (colmap_correct_pitch,
colmap_orientation_align, gps_priors_colmap, colmap_visualize) -- these are
generic COLMAP-family knobs, not alignment-mode-specific. Leveling is
re-implemented here against front/back naming via
colmap_runner._apply_global_level_correction's sensor_name_to_idx param
rather than its default pano_cameraN parsing.

**Python environment split** (same as colmap_runner.py): this module runs
under Python 3.13 (server process); all pycolmap work is delegated to
colmap_fisheye_worker.py under Python 3.14 / pycolmap 4.x via subprocess.
"""
import os
import subprocess
import json
import queue
import re
import shutil
import threading
from math import radians
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .types import PipelineStage
from .settings import PipelineSettings
from .colmap_runner import _measure_anchor_pitch, _apply_global_level_correction, _generate_visualizer
from .fisheye_frame_extractor import load_crop_geometry

# colmap_fisheye's rig has exactly 2 sensors, named by folder rather than
# index — front is always the rig reference (index 0, identity cam_from_rig).
_SENSOR_NAME_TO_IDX = {"front": 0, "back": 1}

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

    use_calibration = getattr(settings, "colmap_fisheye_use_calibration", True)

    front_profile: Optional[dict] = None
    back_profile: Optional[dict] = None
    if use_calibration:
        front_profile_name = getattr(settings, "colmap_fisheye_front_profile", "")
        back_profile_name  = getattr(settings, "colmap_fisheye_back_profile", "")
        if not front_profile_name or not back_profile_name:
            raise RuntimeError(
                "COLMAP Fisheye mode requires both a front and back lens calibration "
                "profile — save them from the Lens Calibration tab first, or turn off "
                "'Use calibrated lens profiles' to self-calibrate instead."
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

    if use_calibration:
        # If raw_dir was auto-derived by fisheye_frame_extractor.py, its crop was
        # applied AFTER the calibration profile was computed on the full, uncropped
        # image (the Lens Calibration tab is untouched and still expects pre-split,
        # uncropped single-lens images) -- so the profile's cx/cy need translating
        # into the cropped image's own coordinate frame. A manually-pointed folder
        # (e.g. mobile live-capture output) has no sidecar, so load_crop_geometry()
        # returns None and this is a no-op -- exactly today's behavior, unchanged.
        crop_geom = load_crop_geometry(raw_dir)
        if crop_geom is not None:
            front_profile = dict(front_profile)
            back_profile = dict(back_profile)
            front_profile["cx"] -= crop_geom["front"]["crop_x0"]
            front_profile["cy"] -= crop_geom["front"]["crop_y0"]
            back_profile["cx"] -= crop_geom["back"]["crop_x0"]
            back_profile["cy"] -= crop_geom["back"]["crop_y0"]
        rig_front_params = [
            front_profile["fx"], front_profile["fy"], front_profile["cx"], front_profile["cy"],
            front_profile["k1"], front_profile["k2"], front_profile["k3"], front_profile["k4"],
        ]
        rig_back_params = [
            back_profile["fx"], back_profile["fy"], back_profile["cx"], back_profile["cy"],
            back_profile["k1"], back_profile["k2"], back_profile["k3"], back_profile["k4"],
        ]
    else:
        # No real prior available — seed a rough equidistant-fisheye guess (focal
        # derived from the actual cropped frame width and the configured target FOV,
        # principal point centered, zero distortion) and let bundle adjustment
        # self-calibrate from there using RADIAL_FISHEYE, a lower-DOF model than the
        # calibrated path's OPENCV_FISHEYE (see colmap_fisheye_worker.py's
        # calibrated=False handling for why). Both lenses start from the same guess;
        # BA refines each independently since they're separate Camera objects.
        fov_deg = getattr(settings, "colmap_fisheye_fov_deg", 130.0)
        f_guess = (img_w / 2.0) / radians(fov_deg / 2.0)
        _guess = [f_guess, img_w / 2.0, img_h / 2.0, 0.0, 0.0]  # f, cx, cy, k1, k2
        rig_front_params = list(_guess)
        rig_back_params = list(_guess)

    rig = _fixed_dual_lens_rig()
    rig["image_width"]  = img_w
    rig["image_height"] = img_h
    rig["front_params"] = rig_front_params
    rig["back_params"]  = rig_back_params

    report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 12,
           f"Spawning Python 3.14 / pycolmap worker (2 sensors, "
           f"{settings.colmap_fisheye_matcher} matcher, "
           f"{'calibrated intrinsics' if use_calibration else 'self-calibrating intrinsics'})…")

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
        "calibrated":      use_calibration,
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

    # Global level correction — same one-rigid-rotation approach as plain
    # COLMAP rig mode (colmap_runner.py's _apply_global_level_correction),
    # generalized via sensor_name_to_idx to front/back naming instead of
    # pano_cameraN. Front is the rig reference (identity), so the target is
    # always 0° DIAG pitch — there's no pitch_angles sweep to account for
    # like the pano-crop case. Shares settings.colmap_correct_pitch with the
    # plain COLMAP tab (see settings.py note on shared COLMAP-family knobs).
    if getattr(settings, "colmap_correct_pitch", True):
        anchor_pitch_before = _measure_anchor_pitch(sparse_txt, anchor_prefix="front/")
        report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 90,
               f"Leveling reconstruction as a single rigid rotation "
               f"(mean front-lens tilt ~{anchor_pitch_before:.2f}°, target 0.0° DIAG)…")
        n_cams, n_pts_corrected = _apply_global_level_correction(
            sparse_txt,
            cam_from_rig=rig["rotations"],
            anchor_diag_pitch=0.0,
            sensor_name_to_idx=_SENSOR_NAME_TO_IDX,
        )
        post_pitch = _measure_anchor_pitch(sparse_txt, anchor_prefix="front/")
        report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 90,
               f"Leveling applied to {n_cams} cameras, {n_pts_corrected} points — "
               f"mean front-lens tilt now {post_pitch:.3f}° (expected ~0.0°)")
    else:
        report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 90, "Leveling skipped — using raw COLMAP poses.")

    # Optional: refine level using COLMAP's own scene-geometry aligner — fully
    # generic (operates on sparse_txt only, no sensor-naming assumptions), so
    # reused as-is via the shared settings.colmap_orientation_align toggle.
    if getattr(settings, "colmap_orientation_align", False) and getattr(settings, "colmap_bin", None):
        report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 92,
               "Refining level using scene geometry (model_orientation_aligner IMAGE_ORIENTATION)…")
        try:
            result = subprocess.run(
                [settings.colmap_bin, "model_orientation_aligner",
                 "--input_path",  str(sparse_txt),
                 "--output_path", str(sparse_txt),
                 "--method",      "IMAGE_ORIENTATION"],
                capture_output=True, text=True, timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            if result.returncode == 0:
                report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 92,
                       "Scene-geometry orientation refinement complete")
            else:
                report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 92,
                       f"Orientation aligner failed (non-fatal): {(result.stderr or '')[:300]}")
        except Exception as exc:
            report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 92,
                   f"Orientation aligner error (non-fatal): {exc}")

    report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 93,
           f"COLMAP Fisheye: {n_imgs} images, {n_pts} points — finalising brush input…")
    if brush_input_dir.exists():
        shutil.rmtree(str(brush_input_dir))
    shutil.copytree(str(sparse_txt), str(brush_input_dir))
    images_dst = brush_input_dir / "images"
    if not images_dst.exists() and image_dir.exists():
        shutil.copytree(str(image_dir), str(images_dst))

    # ── GPS geo-registration (optional, shared settings.gps_priors_colmap) ──
    # Looks for .gps.json sidecars alongside the raw front/back frames
    # (raw_dir/*.gps.json, matched by filename stem to front/ frames) — the
    # raw-fisheye-frame equivalent of colmap_runner.py's "import from camera"
    # sidecar convention. Matches its behavior exactly, including running
    # AFTER brush_input/ has already been copied (same pre-existing ordering
    # as the plain COLMAP path — not something introduced here).
    if getattr(settings, "gps_priors_colmap", False) and settings.colmap_bin:
        if sparse_txt.exists():
            ref_lines = []
            for gps_file in sorted(raw_dir.glob("*.gps.json")):
                try:
                    gps = json.loads(gps_file.read_text(encoding="utf-8"))
                    stem = gps_file.stem
                    view_name = f"front/{stem}.jpg"
                    ref_lines.append(
                        f"{view_name} {gps['lon']:.8f} {gps['lat']:.8f} {gps.get('alt', 0):.3f}"
                    )
                except Exception:
                    pass
            if ref_lines:
                ref_file = colmap_dir / "gps_ref.txt"
                ref_file.write_text("\n".join(ref_lines) + "\n", encoding="utf-8")
                georeg_dir = colmap_dir / "sparse_georeg"
                georeg_dir.mkdir(exist_ok=True)
                report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 96,
                       f"GPS geo-registration — {len(ref_lines)} reference points…")
                try:
                    result = subprocess.run([
                        settings.colmap_bin, "model_aligner",
                        "--input_path",  str(sparse_txt),
                        "--output_path", str(georeg_dir),
                        "--ref_images_path", str(ref_file),
                        "--ref_is_gps",  "1",
                        "--alignment_type", "ecef",
                        "--robust_alignment", "1",
                        "--robust_alignment_max_error", "5.0",  # 5 m GPS tolerance
                    ], capture_output=True, text=True)
                    if result.returncode == 0:
                        for f in georeg_dir.iterdir():
                            shutil.copy2(str(f), str(sparse_txt / f.name))
                        report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 97,
                               "GPS geo-registration applied — reconstruction is now in ECEF/GPS coordinates")
                    else:
                        report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 97,
                               f"GPS geo-registration failed (non-fatal): {result.stderr[:200]}")
                except Exception as e:
                    report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 97,
                           f"GPS geo-registration skipped: {e}")
            else:
                report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 97,
                       "GPS geo-registration: no .gps.json sidecars found in raw frames folder")

    # Generate camera visualizer HTML (Python 3.14 subprocess) — shared
    # settings.colmap_visualize toggle, same generic tool as plain COLMAP.
    if getattr(settings, "colmap_visualize", False):
        report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 99, "Generating camera visualizer…")
        _generate_visualizer(colmap_dir, 0.0, correction_deg=0.0,
                              image_dir=image_dir, anchor_sensor="front")

    report(PipelineStage.COLMAP_FISHEYE_ALIGNMENT, 100,
           f"COLMAP Fisheye alignment complete — {n_imgs} images, {n_pts} points.")
