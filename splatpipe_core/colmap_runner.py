"""
COLMAP alignment engine for the SplatPipe pipeline.

Two modes:
  "spherical"  — feed raw equirec frames directly using COLMAP's native
                  EQUIRECTANGULAR camera model.
  "rig"        — reorganize already-extracted 02_views/ into per-sensor
                  subdirectories, generate a rig config from known yaw/pitch
                  offsets, and run COLMAP with full rig constraints.

Both modes output cameras.txt / images.txt / points3D.txt into brush_input/
in exactly the format expected by Brush.

**Python environment split**:
  • This module runs under Python 3.13 (server process).
  • All pycolmap work for rig mode is delegated to colmap_worker.py which
    runs under Python 3.14 / pycolmap 4.0.4 via subprocess.  pycolmap 3.12.4
    (Python 3.13) is missing FeatureMatchingOptions and has broken
    camera_params/camera_model bindings — neither matching nor extraction
    can be done reliably there.
  • Spherical mode is NOT delegated (EQUIRECTANGULAR model absent from
    pycolmap 4.0.4 Windows wheel; fail-fast check is preserved).

Math mirrors colmap/python/examples/panorama_sfm.py.
"""
import subprocess
import json
import queue
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .types import PipelineStage, StageProgress
from .settings import PipelineSettings

# Python 3.14 executable that carries pycolmap 4.0.4
_PYTHON_314 = "C:\\Python314\\python.exe"
_WORKER     = str(Path(__file__).parent / "colmap_worker.py")
_VISUALIZER = str(Path(__file__).parent.parent / "tools" / "visualize_cameras.py")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _first_image_size(image_dir: Path) -> tuple[int, int]:
    """Return (width, height) of the first .jpg/.png found recursively."""
    from PIL import Image
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        for p in sorted(image_dir.rglob(ext)):
            try:
                with Image.open(p) as im:
                    return im.size  # (width, height)
            except Exception:
                continue
    raise RuntimeError(f"No readable images found under {image_dir}")


def _write_colmap_stage(project_dir: Path, stage: str, data: dict) -> None:
    """Append a stage record to fieldraven.json (mirrors pipeline_runner._write_stage_progress)."""
    from datetime import datetime
    config_path = project_dir / "fieldraven.json"
    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    config.setdefault("stages", {})[stage] = {
        **data,
        "completedAt": datetime.now().isoformat(timespec="seconds"),
    }
    config["savedAt"] = datetime.now().isoformat(timespec="seconds")
    try:
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠️  Could not write fieldraven.json: {e}")


def _reorganize_views(views_dir: Path, image_dir: Path) -> int:
    """
    Reorganize extracted views into per-sensor subdirs for COLMAP.

    02_views/frame_000001/frame_000001_view_00_p-30_y00.jpg
        → colmap/images/pano_camera0/frame_000001.jpg

    Returns the total number of sensors (view count per frame).
    Skips immediately if sensor directories already exist and are populated.
    """
    # Fast-path: sensor dirs already built from a previous run
    if image_dir.exists():
        sensors = sorted(
            d for d in image_dir.iterdir()
            if d.is_dir() and d.name.startswith("pano_camera")
        )
        if sensors and any(sensors[0].glob("*.jpg")):
            return len(sensors)

    max_sensor_idx = -1
    _pat = re.compile(r'^(.+)_view_(\d+)_p')
    for frame_dir in sorted(views_dir.iterdir()):
        if not frame_dir.is_dir():
            continue
        for img in sorted(frame_dir.glob("*.jpg")):
            m = _pat.match(img.stem)
            if not m:
                continue
            frame_name, view_idx = m.group(1), int(m.group(2))
            sensor_dir = image_dir / f"pano_camera{view_idx}"
            sensor_dir.mkdir(parents=True, exist_ok=True)
            dst = sensor_dir / f"{frame_name}.jpg"
            if not dst.exists():
                shutil.copy2(img, dst)
            max_sensor_idx = max(max_sensor_idx, view_idx)
    return max_sensor_idx + 1


def _cam_from_pano(yaw_deg: float, pitch_deg: float):
    """
    cam_from_pano rotation — the exact transpose of panorama_processing.py's
    look_at_rotation(), which is the actual formula used to extract each
    sensor's perspective-view pixels from the source panorama. Must stay
    byte-identical to that ground truth: the previous Ry(-yaw) @ Rx(-pitch)
    composition only coincidentally matched it at yaw=0°/180°, producing a
    cosine-modulated pitch error on the other siblings (visually confirmed
    in COLMAP GUI — see COLMAP_POSE_CORRECTION_BRIEF.md).
    """
    yaw = np.radians(yaw_deg)
    pitch = np.radians(pitch_deg)
    direction = np.array([
        np.sin(yaw) * np.cos(pitch),
        np.sin(pitch),
        np.cos(yaw) * np.cos(pitch),
    ])
    direction = direction / np.linalg.norm(direction)
    up = np.array([0.0, 1.0, 0.0])
    right = np.cross(up, direction)
    right = right / np.linalg.norm(right)
    true_up = np.cross(direction, right)
    R_world_from_cam = np.stack([right, true_up, direction], axis=1)
    return R_world_from_cam.T


def _virtual_rotations(yaw_steps: int, pitch_angles: list, horizon_ref: bool = False) -> list:
    """
    Return cam_from_pano rotation matrices for each virtual sensor.

    When horizon_ref=True, prepends identity (pitch=0°, yaw=0°) as sensor 0.
    This prevents pitch cancellation in cam_from_rig[i] = R_i @ ref.T —
    with ref=identity, the -10° pitch is preserved in all other sensors.
    """
    rots = []
    if horizon_ref:
        rots.append(_cam_from_pano(0.0, 0.0))  # identity — becomes rig reference
    yaws = np.linspace(0, 360, yaw_steps, endpoint=False)
    for p in pitch_angles:
        offset = (360 / yaw_steps / 2) if p > 0 else 0.0
        for y in yaws + offset:
            # Sign of p is inverted here: panorama_processing.py's pitch_angles
            # (e.g. -10.0) is the extraction-pipeline convention, but COLMAP's
            # rig/world DIAG-pitch convention is the opposite sign. Confirmed by
            # direct COLMAP GUI inspection and by DIAG-pitch readout (siblings
            # measure ~+10 DIAG with this flip, matching documented expectation
            # vs ~-10 without it). See COLMAP_POSE_CORRECTION_BRIEF.md, Problem 11.
            rots.append(_cam_from_pano(y, -p))
    return rots


def _compute_rig_params(settings: PipelineSettings, image_size: int) -> dict:
    """
    Compute rig geometry as plain Python dicts — no pycolmap objects.

    Returns a dict that can be JSON-serialized and passed to the worker.
    The 'rotations' field holds cam_from_rig matrices (sensor 0 is identity =
    reference sensor; sensor i>0 has R_i @ ref.T pre-applied).
    """
    cams_from_pano = _virtual_rotations(
        settings.yaw_steps, settings.pitch_angles,
        horizon_ref=getattr(settings, "horizon_ref", False),
    )
    focal = image_size / (2.0 * np.tan(np.deg2rad(settings.fov) / 2.0))
    ref   = cams_from_pano[0]

    # cam_from_rig[i] = R_i @ ref.T
    # For i=0 (ref sensor): ref @ ref.T = I (worker treats this as cam_from_rig=None)
    cam_from_rig = [(R @ ref.T).tolist() for R in cams_from_pano]
    prefixes     = [f"pano_camera{i}/" for i in range(len(cams_from_pano))]

    return {
        "focal":          focal,
        "image_size":     image_size,
        "n_sensors":      len(cams_from_pano),
        "rotations":      cam_from_rig,
        "image_prefixes": prefixes,
    }


def _select_best_reconstruction(recs: dict):
    """Return the reconstruction with the most registered images."""
    if not recs:
        raise RuntimeError("COLMAP produced no reconstructions")
    return max(recs.values(), key=lambda r: len(r.images))


def _finalize_brush_input(
    best_rec,
    colmap_dir: Path,
    brush_input_dir: Path,
    image_dir: Path,
) -> None:
    """Write COLMAP text format → brush_input/, copy images/. (spherical mode)"""
    text_path = colmap_dir / "sparse_txt"
    text_path.mkdir(parents=True, exist_ok=True)
    best_rec.write_text(str(text_path))
    _copy_to_brush_input(text_path, brush_input_dir, image_dir)


def _copy_to_brush_input(
    sparse_txt_dir: Path,
    brush_input_dir: Path,
    image_dir: Path,
) -> None:
    """Copy sparse_txt → brush_input/ and link images/. No pycolmap needed."""
    if brush_input_dir.exists():
        shutil.rmtree(str(brush_input_dir))
    shutil.copytree(str(sparse_txt_dir), str(brush_input_dir))

    images_dst = brush_input_dir / "images"
    if not images_dst.exists() and image_dir.exists():
        shutil.copytree(str(image_dir), str(images_dst))


def _quat_to_mat(qw: float, qx: float, qy: float, qz: float) -> np.ndarray:
    """Quaternion (w, x, y, z) → 3×3 rotation matrix."""
    return np.array([
        [1 - 2*(qy*qy + qz*qz),   2*(qx*qy - qw*qz),   2*(qx*qz + qw*qy)],
        [  2*(qx*qy + qw*qz), 1 - 2*(qx*qx + qz*qz),   2*(qy*qz - qw*qx)],
        [  2*(qx*qz - qw*qy),   2*(qy*qz + qw*qx), 1 - 2*(qx*qx + qy*qy)],
    ], dtype=np.float64)


def _mat_to_quat(R: np.ndarray) -> tuple:
    """3×3 rotation matrix → quaternion (w, x, y, z). Shepperd method."""
    trace = R[0, 0] + R[1, 1] + R[2, 2]
    if trace > 0:
        s  = 0.5 / np.sqrt(trace + 1.0)
        qw = 0.25 / s
        qx = (R[2, 1] - R[1, 2]) * s
        qy = (R[0, 2] - R[2, 0]) * s
        qz = (R[1, 0] - R[0, 1]) * s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s  = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        qw = (R[2, 1] - R[1, 2]) / s
        qx = 0.25 * s
        qy = (R[0, 1] + R[1, 0]) / s
        qz = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s  = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        qw = (R[0, 2] - R[2, 0]) / s
        qx = (R[0, 1] + R[1, 0]) / s
        qy = 0.25 * s
        qz = (R[1, 2] + R[2, 1]) / s
    else:
        s  = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        qw = (R[1, 0] - R[0, 1]) / s
        qx = (R[0, 2] + R[2, 0]) / s
        qy = (R[1, 2] + R[2, 1]) / s
        qz = 0.25 * s
    return float(qw), float(qx), float(qy), float(qz)


def _apply_global_level_correction(sparse_txt_dir: Path, cam_from_rig: list) -> tuple:
    """
    Level the whole reconstruction with ONE rotation applied identically to
    every rig frame and every 3D point -- not a per-frame override.

    COLMAP has no concept of gravity: the world frame it settles on can be
    tilted by some arbitrary bias relative to true up. The previous approach
    (forcing every individual frame's anchor to an assumed-constant pitch and
    zero roll) discarded real, legitimate per-frame tilt variation -- the
    camera genuinely wasn't level at every single moment of a handheld/pole-
    mounted capture -- and introduced reprojection error across every frame
    as a result (mean ~81px vs ~1.3px healthy, measured directly). See
    COLMAP_POSE_CORRECTION_BRIEF.md Problem 14.

    This instead averages every frame's current ANCHOR orientation into one
    representative "mean" orientation, computes the single rotation that
    would bring that mean to level (same mean yaw, zero pitch, zero roll),
    and applies that exact rotation matrix to every frame's anchor and to
    every point3D. Because every anchor and every point is multiplied by the
    same matrix, all relative geometry (each frame's real tilt relative to
    every other frame, and the point cloud's shape) is preserved exactly;
    only the shared reference for "what counts as level" shifts.

    cam_from_rig: list of 3x3 rotation matrices, index 0 = identity (anchor),
        same matrices passed to apply_rig_config -- siblings are NOT rotated
        directly; they're re-derived from the corrected anchor via these
        fixed matrices, same as the rig-build step itself. This matters
        because pycolmap's rig-aware reader recomputes every sibling's pose
        from frames.txt's RIG_FROM_WORLD combined with rigs.txt's untouched
        sensor_from_rig -- rotating a sibling's own images.txt entry directly
        would silently be ignored by every pycolmap-based consumer (and was
        confirmed empirically to reintroduce ~168px reprojection error: since
        rotation matrices don't commute, R_g @ sibling_old != cam_from_rig[i]
        @ (R_g @ anchor_old), which is what pycolmap actually reconstructs).

    Also patches frames.txt's RIG_FROM_WORLD (the pose pycolmap actually
    uses) and points3D.txt.

    Returns (n_cameras_corrected, n_points_corrected).
    """
    images_txt = sparse_txt_dir / "images.txt"
    points_txt = sparse_txt_dir / "points3D.txt"
    if not images_txt.exists():
        return 0, 0

    cam_from_rig_mats = [np.array(R, dtype=np.float64) for R in cam_from_rig]

    lines = images_txt.read_text(encoding="utf-8").splitlines()

    # First pass: locate each image's pose line and group by rig frame.
    records: list[dict] = []
    frames: dict[str, dict[int, int]] = {}   # frame_stem -> {sensor_idx: record index}
    data_line = False
    for idx, line in enumerate(lines):
        if line.startswith("#") or not line.strip():
            continue
        if not data_line:
            parts = line.split()
            if len(parts) >= 10:
                sensor_name = Path(parts[9]).parent.name
                try:
                    sensor_idx = int(sensor_name.replace("pano_camera", ""))
                except ValueError:
                    data_line = True
                    continue
                frame_stem = Path(parts[9]).stem
                rec_idx = len(records)
                records.append({"line_idx": idx, "parts": parts})
                frames.setdefault(frame_stem, {})[sensor_idx] = rec_idx
            data_line = True
        else:
            data_line = False

    # Collect each frame's current anchor rotation/center to fit the mean
    # orientation and the rotation pivot.
    anchor_R: list[np.ndarray] = []
    anchor_centers: list[np.ndarray] = []
    for frame_stem, sensors in frames.items():
        if 0 not in sensors:
            continue
        anchor_parts = records[sensors[0]]["parts"]
        qw, qx, qy, qz = (float(anchor_parts[i]) for i in (1, 2, 3, 4))
        R_old = _quat_to_mat(qw, qx, qy, qz)
        t_old = np.array([float(anchor_parts[i]) for i in (5, 6, 7)])
        anchor_R.append(R_old)
        anchor_centers.append(-R_old.T @ t_old)

    if not anchor_R:
        return 0, 0

    # Mean rotation (chordal/L2 mean): average the matrices elementwise, then
    # project back onto SO(3) via SVD -- the standard closest-rotation fit,
    # accurate enough for the small (a few degrees) spread we're averaging.
    R_avg = np.mean(np.stack(anchor_R), axis=0)
    U, _, Vt = np.linalg.svd(R_avg)
    R_mean = U @ Vt
    if np.linalg.det(R_mean) < 0:
        U[:, -1] *= -1
        R_mean = U @ Vt

    # The single correction rotation: same mean yaw, zero pitch, zero roll.
    # Same right/down/fwd construction used elsewhere in this file, just
    # with target pitch/roll fixed at zero instead of per-frame yaw-coupled.
    fwd_mean = R_mean.T[:, 2]
    yaw = np.arctan2(fwd_mean[0], fwd_mean[2])
    cos_y, sin_y = np.cos(yaw), np.sin(yaw)
    right = np.array([cos_y, 0.0, -sin_y])
    down  = np.array([0.0,   1.0,  0.0])
    fwd   = np.array([sin_y, 0.0,  cos_y])
    R_target_mean = np.column_stack([right, down, fwd]).T

    # R_g rotates the WORLD frame: positions transform as R_g @ position, but
    # cam_from_world is an orientation/basis-change, not a position, so it
    # transforms as R_old @ R_g.T (conjugation), not R_g @ R_old -- solving
    # R_target_mean = R_mean @ R_g.T for R_g gives this.
    R_g   = R_target_mean.T @ R_mean
    pivot = np.mean(np.stack(anchor_centers), axis=0)

    frame_rig_poses: dict[str, tuple] = {}
    n_corrected = 0
    for frame_stem, sensors in frames.items():
        if 0 not in sensors:
            continue
        anchor_parts = records[sensors[0]]["parts"]
        qw, qx, qy, qz = (float(anchor_parts[i]) for i in (1, 2, 3, 4))
        R_anchor_old = _quat_to_mat(qw, qx, qy, qz)
        t_anchor_old = np.array([float(anchor_parts[i]) for i in (5, 6, 7)])
        center_old   = -R_anchor_old.T @ t_anchor_old

        R_anchor_new = R_anchor_old @ R_g.T
        center_new   = R_g @ (center_old - pivot) + pivot
        t_anchor_new = -R_anchor_new @ center_new
        frame_rig_poses[frame_stem] = (R_anchor_new, t_anchor_new)

        for sensor_idx, rec_idx in sensors.items():
            if sensor_idx >= len(cam_from_rig_mats):
                continue
            R_new = cam_from_rig_mats[sensor_idx] @ R_anchor_new
            t_new = -R_new @ center_new

            parts = records[rec_idx]["parts"]
            nw, nx, ny, nz = _mat_to_quat(R_new)
            parts[1] = f"{nw:.10f}"
            parts[2] = f"{nx:.10f}"
            parts[3] = f"{ny:.10f}"
            parts[4] = f"{nz:.10f}"
            parts[5] = f"{t_new[0]:.10f}"
            parts[6] = f"{t_new[1]:.10f}"
            parts[7] = f"{t_new[2]:.10f}"
            lines[records[rec_idx]["line_idx"]] = " ".join(parts)
            n_corrected += 1

    images_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _patch_frames_rig_from_world(sparse_txt_dir, frame_rig_poses)

    n_points = 0
    if points_txt.exists():
        plines = points_txt.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        for line in plines:
            if line.startswith("#") or not line.strip():
                out.append(line)
                continue
            tokens = line.split()
            xyz_old = np.array([float(tokens[1]), float(tokens[2]), float(tokens[3])])
            xyz_new = R_g @ (xyz_old - pivot) + pivot
            tokens[1] = f"{xyz_new[0]:.6f}"
            tokens[2] = f"{xyz_new[1]:.6f}"
            tokens[3] = f"{xyz_new[2]:.6f}"
            out.append(" ".join(tokens))
            n_points += 1
        points_txt.write_text("\n".join(out) + "\n", encoding="utf-8")

    return n_corrected, n_points


def _patch_frames_rig_from_world(sparse_txt_dir: Path, frame_rig_poses: dict) -> int:
    """
    Overwrite frames.txt's RIG_FROM_WORLD per frame with the corrected anchor
    pose. This is the pose pycolmap actually uses (see the long comment on
    _apply_global_level_correction) -- without this, images.txt can be
    perfectly corrected and pycolmap will still compute geometry from the
    stale, uncorrected rig_from_world.

    frame_rig_poses: frame_stem -> (R_new, t_new) as built by the caller
        (R_new/t_new for sensor_idx 0, i.e. the rig reference sensor's
        corrected cam_from_world).

    Returns count of frame lines patched.
    """
    frames_txt = sparse_txt_dir / "frames.txt"
    images_txt = sparse_txt_dir / "images.txt"
    if not frames_txt.exists() or not images_txt.exists():
        return 0

    # Map IMAGE_ID -> frame_stem from images.txt's pose lines.
    image_id_to_stem: dict[int, str] = {}
    data_line = False
    for line in images_txt.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if not data_line:
            parts = line.split()
            if len(parts) >= 10:
                image_id_to_stem[int(parts[0])] = Path(parts[9]).stem
            data_line = True
        else:
            data_line = False

    lines = frames_txt.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    n_patched = 0
    for line in lines:
        if line.startswith("#") or not line.strip():
            out.append(line)
            continue
        tokens = line.split()
        # FRAME_ID RIG_ID QW QX QY QZ TX TY TZ NUM_DATA_IDS (SENSOR_TYPE SENSOR_ID DATA_ID)*
        num_data_ids = int(tokens[9])
        data_ids = tokens[10 : 10 + num_data_ids * 3]
        frame_stem = None
        for j in range(0, len(data_ids), 3):
            data_id = int(data_ids[j + 2])
            if data_id in image_id_to_stem:
                frame_stem = image_id_to_stem[data_id]
                break
        pose = frame_rig_poses.get(frame_stem) if frame_stem else None
        if pose is not None:
            R_new, t_new = pose
            nw, nx, ny, nz = _mat_to_quat(R_new)
            tokens[2:9] = [
                f"{nw:.10f}", f"{nx:.10f}", f"{ny:.10f}", f"{nz:.10f}",
                f"{t_new[0]:.10f}", f"{t_new[1]:.10f}", f"{t_new[2]:.10f}",
            ]
            n_patched += 1
        out.append(" ".join(tokens))

    frames_txt.write_text("\n".join(out) + "\n", encoding="utf-8")
    return n_patched


def _measure_anchor_pitch(sparse_txt_dir: Path) -> float:
    """Measure the mean DIAG pitch of all pano_camera0 images in images.txt.

    DIAG pitch = arcsin(Y_world) where Y_world is the Y component of the camera
    forward axis in COLMAP world coordinates (Y-down convention).
    Positive = camera pointing down, negative = pointing up.
    Returns 0.0 if no anchor images found.
    """
    images_txt = sparse_txt_dir / "images.txt"
    if not images_txt.exists():
        return 0.0
    pitches: list[float] = []
    data_line = False
    for raw in images_txt.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not data_line:
            parts = line.split()
            if len(parts) >= 10 and parts[9].startswith("pano_camera0/"):
                qw, qx, qy, qz = (float(parts[i]) for i in (1, 2, 3, 4))
                R   = _quat_to_mat(qw, qx, qy, qz)   # cam_from_world
                fwd = R.T[:, 2]                        # forward axis in COLMAP world
                pitches.append(float(np.degrees(np.arcsin(np.clip(fwd[1], -1.0, 1.0)))))
            data_line = True
        else:
            data_line = False
    return float(np.mean(pitches)) if pitches else 0.0


def _generate_visualizer(colmap_dir: Path, pitch_deg: float, correction_deg: float = 0.0) -> None:
    """
    Run visualize_cameras.py under Python 3.14 to generate cameras.html,
    then open it automatically in the default browser.
    """
    import webbrowser
    sparse_txt = colmap_dir / "sparse_txt"
    out_html   = colmap_dir / "cameras.html"
    if not Path(_VISUALIZER).exists() or not sparse_txt.exists():
        return
    subprocess.run(
        [_PYTHON_314, _VISUALIZER, str(sparse_txt), str(out_html),
         str(pitch_deg), str(correction_deg)],
        check=False,  # visualizer failure must not abort the pipeline
    )
    if out_html.exists():
        webbrowser.open(out_html.as_uri())
        print(f"  [colmap] Opened camera visualizer: {out_html}")


# ── Spherical mode ────────────────────────────────────────────────────────────

def _run_spherical(
    frames_dir: Path,
    colmap_dir: Path,
    brush_input_dir: Path,
    settings: PipelineSettings,
    report: Callable,
    cancel_event: threading.Event,
) -> None:
    import pycolmap

    # EQUIRECTANGULAR model was added in COLMAP ≥3.9 but is absent from some
    # pycolmap Windows wheels (including 4.0.4/cp314).  Fail early.
    available = {m for m in pycolmap.CameraModelId.__members__ if m != "INVALID"}
    if "EQUIRECTANGULAR" not in available:
        raise RuntimeError(
            "COLMAP spherical mode requires the EQUIRECTANGULAR camera model, "
            "which is not available in the installed pycolmap build "
            f"(available: {sorted(available)}). "
            "Use rig mode (colmap_mode='rig') instead."
        )

    colmap_dir.mkdir(parents=True, exist_ok=True)
    database_path = colmap_dir / "database.db"
    sparse_path   = colmap_dir / "sparse"
    sparse_path.mkdir(parents=True, exist_ok=True)

    report(PipelineStage.COLMAP_ALIGNMENT, 5,
           "COLMAP: extracting features (spherical / EQUIRECTANGULAR)…")
    pycolmap.extract_features(
        database_path,
        frames_dir,
        reader_options=pycolmap.ImageReaderOptions(camera_model="EQUIRECTANGULAR"),
        camera_mode=pycolmap.CameraMode.SINGLE,
    )
    if cancel_event.is_set(): return

    report(PipelineStage.COLMAP_ALIGNMENT, 30,
           f"COLMAP: matching features ({settings.colmap_matcher})…")
    # NOTE: spherical mode still runs in Python 3.13 — FeatureMatchingOptions
    # doesn't exist there; fall back to SiftMatchingOptions for now.
    try:
        mo = pycolmap.FeatureMatchingOptions()
    except AttributeError:
        mo = pycolmap.SiftMatchingOptions()
    _run_matcher_legacy(database_path, settings.colmap_matcher, mo)
    if cancel_event.is_set(): return

    report(PipelineStage.COLMAP_ALIGNMENT, 55, "COLMAP: incremental mapping…")
    recs = pycolmap.incremental_mapping(database_path, frames_dir, sparse_path)
    if cancel_event.is_set(): return

    report(PipelineStage.COLMAP_ALIGNMENT, 90,
           f"COLMAP: {len(recs)} reconstruction(s) — finalising brush input…")
    best = _select_best_reconstruction(recs)
    _finalize_brush_input(best, colmap_dir, brush_input_dir, frames_dir)


# ── Perspective-rig mode (full pipeline delegated to Python 3.14 worker) ──────

def _run_perspective_rig(
    views_dir: Path,
    colmap_dir: Path,
    brush_input_dir: Path,
    settings: PipelineSettings,
    report: Callable,
    cancel_event: threading.Event,
    project_dir: Optional[Path] = None,
) -> None:
    image_dir     = colmap_dir / "images"
    database_path = colmap_dir / "database.db"
    sparse_path   = colmap_dir / "sparse"
    sparse_txt    = colmap_dir / "sparse_txt"

    # Check whether sensor dirs are already built (from a previous run)
    sensors_exist = (
        image_dir.exists() and
        any(d.is_dir() and d.name.startswith("pano_camera") for d in image_dir.iterdir())
    )
    if sensors_exist:
        report(PipelineStage.COLMAP_ALIGNMENT, 5,
               "COLMAP: per-sensor directories already exist — skipping reorganisation…")
    else:
        report(PipelineStage.COLMAP_ALIGNMENT, 5,
               "COLMAP: reorganising views into per-sensor directories…")

    n_sensors = _reorganize_views(views_dir, image_dir)
    if n_sensors == 0:
        raise RuntimeError("No view images found in 02_views/ for COLMAP rig mode")

    if not sensors_exist and project_dir:
        _write_colmap_stage(project_dir, "colmap_reorganized", {
            "done": True, "sensors": n_sensors,
        })
    if cancel_event.is_set(): return

    # Compute rig geometry in pure numpy — no pycolmap objects.
    pano_w, pano_h = _first_image_size(image_dir)
    rig_params = _compute_rig_params(settings, pano_h)
    if cancel_event.is_set(): return

    report(PipelineStage.COLMAP_ALIGNMENT, 12,
           f"COLMAP: spawning Python 3.14 / pycolmap 4 worker "
           f"({n_sensors} sensors, {settings.colmap_matcher} matcher)…")

    sparse_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "database_path":   str(database_path),
        "image_path":      str(image_dir),
        "sparse_path":     str(sparse_path),
        "sparse_txt_path": str(sparse_txt),
        "colmap_matcher":  settings.colmap_matcher,
        "colmap_bin":      settings.colmap_bin or "",
        "colmap_mapper":   getattr(settings, "colmap_mapper", "incremental"),
        "vocab_tree_path": getattr(settings, "colmap_vocab_tree", "") or "",
        "rig":             rig_params,
    }

    # ── spawn worker ──────────────────────────────────────────────────────────
    # -P: don't prepend the script directory to sys.path (Python 3.11+).
    # Without it, splatpipe_core/ lands at sys.path[0] and types.py shadows
    # the stdlib 'types' module, breaking every downstream import in Python 3.14.
    process = subprocess.Popen(
        [_PYTHON_314, "-P", _WORKER, json.dumps(payload)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Stream stderr in a background thread so pycolmap's internal progress
    # (image registration, BA iterations, etc.) is forwarded to the UI.
    _stderr_q: queue.SimpleQueue[str] = queue.SimpleQueue()

    def _pipe_stderr():
        for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                _stderr_q.put(line)

    _stderr_thread = threading.Thread(target=_pipe_stderr, daemon=True)
    _stderr_thread.start()

    # GLOG prefix pattern: "I20260625 14:23:45.123456 12345 file.cc:512] "
    _GLOG = re.compile(r'^[IWEF]\d{8} [\d:.]+ +\d+ [^]]+\] ')

    def _forward_stderr(current_pct: int) -> None:
        while not _stderr_q.empty():
            raw_msg = _stderr_q.get_nowait()
            msg = _GLOG.sub("", raw_msg).strip()
            if msg:
                report(PipelineStage.COLMAP_ALIGNMENT, current_pct, f"[colmap] {msg}")

    worker_lines   = []
    current_pct    = 12
    _last_report_t = 0.0   # time of last Firestore write
    _last_report_p = -1    # pct at last write — always write on pct change
    _FB_INTERVAL   = 3.0   # seconds between Firebase writes during log-spam phases
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
            # Format: WORKER_PROGRESS:<pct>:<message>
            parts = clean.split(":", 2)
            try:
                current_pct = int(parts[1])
                msg = parts[2] if len(parts) > 2 else ""
            except (ValueError, IndexError):
                msg = clean
            _forward_stderr(current_pct)
            # Rate-limit Firestore writes: only push if pct changed or 3s elapsed.
            # Prevents ~600 sequential writes during COLMAP matching (one per image)
            # which caused a 2+ minute write backlog that made the app appear hung.
            now = time.time()
            if current_pct != _last_report_p or (now - _last_report_t) >= _FB_INTERVAL:
                report(PipelineStage.COLMAP_ALIGNMENT, current_pct, msg)
                _last_report_t = now
                _last_report_p = current_pct
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
        # Worker prints error as JSON to stdout; try to decode it for a useful message.
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
            f"PyCOLMAP v4 worker exited with code {process.returncode}:\n"
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

    # Global level correction: COLMAP has no concept of gravity, so its world
    # frame can end up tilted by some arbitrary bias relative to true up. Fix
    # that bias with ONE rotation applied identically to every camera and
    # every point -- not a per-frame override (see Problem 14 in the brief
    # for why per-frame forcing was wrong and is no longer done).
    if getattr(settings, "colmap_correct_pitch", True):
        anchor_pitch_before = _measure_anchor_pitch(sparse_txt)

        report(PipelineStage.COLMAP_ALIGNMENT, 91,
               f"Leveling reconstruction as a single rigid rotation "
               f"(mean anchor tilt ~{anchor_pitch_before:.2f}° from horizontal)…")
        n_cams, n_pts_corrected = _apply_global_level_correction(
            sparse_txt, cam_from_rig=rig_params["rotations"],
        )

        post_pitch = _measure_anchor_pitch(sparse_txt)
        report(PipelineStage.COLMAP_ALIGNMENT, 91,
               f"Leveling applied to {n_cams} cameras, {n_pts_corrected} points — "
               f"mean anchor tilt now {post_pitch:.3f}° (expected ~0°)")
    else:
        report(PipelineStage.COLMAP_ALIGNMENT, 91, "Leveling skipped — using raw COLMAP poses.")

    # Optional: refine level using COLMAP's own scene-geometry aligner.
    # Runs after the rig-reference correction so the two stack rather than conflict.
    # Non-fatal — if the aligner fails the rig-reference correction is still in place.
    if getattr(settings, "colmap_orientation_align", False) and getattr(settings, "colmap_bin", None):
        report(PipelineStage.COLMAP_ALIGNMENT, 92,
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
                report(PipelineStage.COLMAP_ALIGNMENT, 92,
                       "Scene-geometry orientation refinement complete")
            else:
                report(PipelineStage.COLMAP_ALIGNMENT, 92,
                       f"Orientation aligner failed (non-fatal): {(result.stderr or '')[:300]}")
        except Exception as exc:
            report(PipelineStage.COLMAP_ALIGNMENT, 92,
                   f"Orientation aligner error (non-fatal): {exc}")

    # ── finalise brush_input/ (file copies only — no pycolmap) ───────────────
    report(PipelineStage.COLMAP_ALIGNMENT, 93,
           f"COLMAP: {n_imgs} images, {n_pts} points — finalising brush input…")
    _copy_to_brush_input(sparse_txt, brush_input_dir, image_dir)


# ── Legacy matcher dispatcher (for spherical mode on Python 3.13) ─────────────

def _run_matcher_legacy(database_path: Path, matcher: str, options) -> None:
    import pycolmap
    # In pycolmap 3.12.4 the per-method option types are SequentialMatchingOptions,
    # ExhaustiveMatchingOptions, etc.  In 4.x everything is FeatureMatchingOptions.
    # We just pass whatever options object was constructed by the caller.
    if matcher == "sequential":
        pycolmap.match_sequential(database_path, options)
    elif matcher == "exhaustive":
        pycolmap.match_exhaustive(database_path, options)
    elif matcher == "vocabtree":
        pycolmap.match_vocabtree(database_path, options)
    else:
        raise ValueError(f"Unknown COLMAP matcher: {matcher!r}")


# ── Public entry point ────────────────────────────────────────────────────────

def run_colmap_pipeline(
    frames_dir: Path,
    views_dir: Path,
    colmap_dir: Path,
    brush_input_dir: Path,
    settings: PipelineSettings,
    report: Callable[[PipelineStage, int, str], None],
    cancel_event: threading.Event,
    project_dir: Optional[Path] = None,
) -> None:
    """
    Run the COLMAP alignment pipeline and populate brush_input_dir.

    Args:
        frames_dir:      01_frames/ (equirec images) — used for spherical mode
        views_dir:       02_views/ (per-frame subdirs of perspective crops) — rig mode
        colmap_dir:      03_alignment/colmap/ — working directory
        brush_input_dir: 04_training/brush_input/ — output destination
        settings:        pipeline configuration (colmap_mode, colmap_matcher, …)
        report:          progress callback (stage, pct, message)
        cancel_event:    set to abort
    """
    report(PipelineStage.COLMAP_ALIGNMENT, 0,
           f"Starting COLMAP alignment (mode={settings.colmap_mode})…")

    colmap_dir.mkdir(parents=True, exist_ok=True)

    if settings.colmap_mode == "spherical":
        try:
            import pycolmap  # noqa: F401 — fail early if not installed
        except ImportError:
            raise RuntimeError("pycolmap is not installed. Run: pip install pycolmap")
        _run_spherical(frames_dir, colmap_dir, brush_input_dir,
                       settings, report, cancel_event)
    else:
        _run_perspective_rig(views_dir, colmap_dir, brush_input_dir,
                             settings, report, cancel_event,
                             project_dir=project_dir)

    if cancel_event.is_set():
        return

    # ── GPS geo-registration (optional) ──────────────────────────
    # If GPS sidecars exist in the input dir and gps_priors_colmap is enabled,
    # run COLMAP model_aligner to fit the reconstruction to real-world GPS
    # coordinates (correcting scale, orientation, and absolute position).
    if getattr(settings, "gps_priors_colmap", False) and settings.colmap_bin:
        sparse_txt = colmap_dir / "sparse_txt"
        input_dir  = colmap_dir.parent.parent / "import from camera"
        if not input_dir.exists():
            input_dir = colmap_dir.parent.parent / "imported photos"
        if sparse_txt.exists() and input_dir.exists():
            import json as _json
            # Collect anchor-sensor image names and their GPS (only view_00 per frame)
            ref_lines = []
            for gps_file in sorted(input_dir.glob("*.gps.json")):
                try:
                    gps = _json.loads(gps_file.read_text(encoding="utf-8"))
                    stem = gps_file.stem  # e.g. IMG_xxx_00_040
                    # anchor view is view_00 (first view per frame)
                    view_name = f"pano_camera0/{stem}.jpg"
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
                report(PipelineStage.COLMAP_ALIGNMENT, 96,
                       f"GPS geo-registration — {len(ref_lines)} reference points…")
                try:
                    import subprocess as _sp
                    result = _sp.run([
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
                        # Replace sparse_txt with geo-registered version
                        import shutil as _shutil
                        for f in georeg_dir.iterdir():
                            _shutil.copy2(str(f), str(sparse_txt / f.name))
                        report(PipelineStage.COLMAP_ALIGNMENT, 97,
                               "GPS geo-registration applied — reconstruction is now in ECEF/GPS coordinates")
                    else:
                        report(PipelineStage.COLMAP_ALIGNMENT, 97,
                               f"GPS geo-registration failed (non-fatal): {result.stderr[:200]}")
                except Exception as e:
                    report(PipelineStage.COLMAP_ALIGNMENT, 97,
                           f"GPS geo-registration skipped: {e}")
            else:
                report(PipelineStage.COLMAP_ALIGNMENT, 97,
                       "GPS geo-registration: no .gps.json sidecars found in input dir")

    txt_count = len(list(brush_input_dir.glob("*.txt"))) if brush_input_dir.exists() else 0
    report(PipelineStage.COLMAP_ALIGNMENT, 98,
           f"COLMAP alignment complete — {txt_count} text files in brush_input/")

    # Generate camera visualizer HTML (Python 3.14 subprocess)
    if getattr(settings, "colmap_visualize", False) and settings.colmap_mode != "spherical":
        pitch = settings.pitch_angles[0] if settings.pitch_angles else -10.0
        report(PipelineStage.COLMAP_ALIGNMENT, 99, "Generating camera visualizer…")
        # correction_deg=0.0 because the bias is already corrected in sparse_txt
        _generate_visualizer(colmap_dir, float(pitch), correction_deg=0.0)

    report(PipelineStage.COLMAP_ALIGNMENT, 100,
           f"COLMAP alignment complete — {txt_count} text files in brush_input/")
