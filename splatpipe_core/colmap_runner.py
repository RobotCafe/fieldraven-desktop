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
    """cam_from_pano rotation: R_Y(-yaw) @ R_X(-pitch). Mirrors panorama_sfm.py."""
    p = np.radians(-pitch_deg)
    y = np.radians(-yaw_deg)
    Rx = np.array([[1, 0, 0],
                   [0, np.cos(p), -np.sin(p)],
                   [0, np.sin(p),  np.cos(p)]])
    Ry = np.array([[ np.cos(y), 0, np.sin(y)],
                   [0,          1, 0         ],
                   [-np.sin(y), 0, np.cos(y)]])
    return Ry @ Rx


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
            rots.append(_cam_from_pano(y, p))
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


def _fix_images(path: Path, Rg: np.ndarray) -> None:
    """Apply Rg to camera rotation matrices in images.txt (in-place)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out   = []
    data_line = False  # alternates: True for image header, False for keypoints
    for line in lines:
        if line.startswith("#") or not line.strip():
            out.append(line)
            continue
        if not data_line:
            # IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME
            parts = line.split()
            qw, qx, qy, qz = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
            R_new = _quat_to_mat(qw, qx, qy, qz) @ Rg
            nw, nx, ny, nz = _mat_to_quat(R_new)
            parts[1], parts[2], parts[3], parts[4] = (
                f"{nw:.10f}", f"{nx:.10f}", f"{ny:.10f}", f"{nz:.10f}"
            )
            out.append(" ".join(parts))
            data_line = True
        else:
            out.append(line)  # keypoints line — pass through
            data_line = False
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _fix_points3D(path: Path, Rg: np.ndarray) -> None:
    """Apply Rg.T to 3D point coordinates in points3D.txt (in-place)."""
    RgT   = Rg.T
    lines = path.read_text(encoding="utf-8").splitlines()
    out   = []
    for line in lines:
        if line.startswith("#") or not line.strip():
            out.append(line)
            continue
        parts    = line.split()
        xyz      = RgT @ np.array([float(parts[1]), float(parts[2]), float(parts[3])])
        parts[1] = f"{xyz[0]:.6f}"
        parts[2] = f"{xyz[1]:.6f}"
        parts[3] = f"{xyz[2]:.6f}"
        out.append(" ".join(parts))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _correct_camera_pitches_from_extraction(
    sparse_txt_dir: Path,
    anchor_diag_pitch: float = 0.0,
    sibling_diag_pitch: float = 10.0,
) -> int:
    """
    For each camera in images.txt: keep the COLMAP-estimated yaw (azimuthal
    direction in the XZ plane) but SET the pitch to the known extraction angle
    and SET roll to zero.

    This directly encodes the truth we already know — the exact angle at which
    each perspective image was extracted from the 360° sphere — rather than
    trying to reverse-engineer a world-frame bias we can only approximate.

    anchor_diag_pitch: DIAG pitch target for pano_camera0 (0.0 = horizontal)
    sibling_diag_pitch: DIAG pitch target for pano_camera1+ (positive = downward
        in COLMAP Y-down; use abs(extraction_pitch_deg) e.g. 10.0 for -10°)

    Returns count of cameras corrected.
    """
    images_txt = sparse_txt_dir / "images.txt"
    if not images_txt.exists():
        return 0

    lines = images_txt.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    data_line = False
    n_corrected = 0

    for line in lines:
        if line.startswith("#") or not line.strip():
            out.append(line)
            continue
        if not data_line:
            parts = line.split()
            if len(parts) >= 10:
                sensor_name = Path(parts[9]).parent.name  # "pano_camera0"
                try:
                    sensor_idx = int(sensor_name.replace("pano_camera", ""))
                except ValueError:
                    out.append(line)
                    data_line = True
                    continue

                target_P = np.radians(
                    anchor_diag_pitch if sensor_idx == 0 else sibling_diag_pitch
                )

                # Yaw (azimuth in COLMAP XZ plane) from current COLMAP rotation
                qw, qx, qy, qz = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                R_cur = _quat_to_mat(qw, qx, qy, qz)
                fwd_cur = R_cur.T[:, 2]          # current forward in COLMAP world
                yaw = np.arctan2(fwd_cur[0], fwd_cur[2])

                # Build new cam_from_world rotation: same yaw, target pitch, zero roll.
                # In COLMAP (Y-down, Z-forward):
                #   right = [cos(ψ),        0,          -sin(ψ)        ]
                #   down  = [-sin(P)sin(ψ), cos(P),     -sin(P)cos(ψ)  ]
                #   fwd   = [sin(ψ)cos(P),  sin(P),      cos(ψ)cos(P)  ]
                # R.T = [right | down | fwd]  (columns = camera axes in world)
                cos_P, sin_P = np.cos(target_P), np.sin(target_P)
                cos_y, sin_y = np.cos(yaw),      np.sin(yaw)

                right = np.array([ cos_y,           0.0,    -sin_y          ])
                down  = np.array([-sin_P * sin_y,   cos_P,  -sin_P * cos_y  ])
                fwd   = np.array([ sin_y * cos_P,   sin_P,   cos_y * cos_P  ])

                RT = np.column_stack([right, down, fwd])   # R.T
                R_new = RT.T                               # cam_from_world

                # Preserve camera position: center = -R_old.T @ t  →  t_new = -R_new @ center
                t_old  = np.array([float(parts[5]), float(parts[6]), float(parts[7])])
                center = -R_cur.T @ t_old
                t_new  = -R_new @ center

                nw, nx, ny, nz = _mat_to_quat(R_new)
                parts[1] = f"{nw:.10f}"
                parts[2] = f"{nx:.10f}"
                parts[3] = f"{ny:.10f}"
                parts[4] = f"{nz:.10f}"
                parts[5] = f"{t_new[0]:.10f}"
                parts[6] = f"{t_new[1]:.10f}"
                parts[7] = f"{t_new[2]:.10f}"
                n_corrected += 1
            out.append(" ".join(parts))
            data_line = True
        else:
            out.append(line)
            data_line = False

    images_txt.write_text("\n".join(out) + "\n", encoding="utf-8")
    return n_corrected


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


def _apply_gravity_alignment(sparse_txt_dir: Path, correction_deg: float) -> None:
    """Apply R_X(correction_deg) to all camera rotations and 3D points in sparse_txt/."""
    theta = np.radians(correction_deg)
    Rg = np.array([
        [1, 0,              0             ],
        [0, np.cos(theta), -np.sin(theta) ],
        [0, np.sin(theta),  np.cos(theta) ],
    ], dtype=np.float64)
    _fix_images(sparse_txt_dir / "images.txt", Rg)
    _fix_points3D(sparse_txt_dir / "points3D.txt", Rg)


def _generate_visualizer(colmap_dir: Path, pitch_deg: float, correction_deg: float = 0.0) -> None:
    """
    Run visualize_cameras.py under Python 3.14 to generate cameras.html.

    Uses Python 3.14 because the visualizer calls image.cam_from_world() and
    image.has_pose — pycolmap 4.x API that doesn't exist in 3.12.4.
    """
    sparse_txt = colmap_dir / "sparse_txt"
    out_html   = colmap_dir / "cameras.html"
    if not Path(_VISUALIZER).exists() or not sparse_txt.exists():
        return
    subprocess.run(
        [_PYTHON_314, _VISUALIZER, str(sparse_txt), str(out_html),
         str(pitch_deg), str(correction_deg)],
        check=False,  # visualizer failure must not abort the pipeline
    )


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
            # Format: WORKER_PROGRESS:<pct>:<message>
            parts = clean.split(":", 2)
            try:
                current_pct = int(parts[1])
                msg = parts[2] if len(parts) > 2 else ""
            except (ValueError, IndexError):
                msg = clean
            _forward_stderr(current_pct)
            report(PipelineStage.COLMAP_ALIGNMENT, current_pct, msg)
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

    # Scene-tilt correction: COLMAP biases the world frame toward the mean pitch
    # of all extracted cameras. Because siblings outnumber the anchor, the world
    # tilts upward. We compute the exact correction from the known extraction angles
    # (sibling_pitch and sensor count) — no measurement from the reconstruction needed.
    if getattr(settings, "colmap_gravity_align", True) and settings.pitch_angles:
        sibling_pitch = float(settings.pitch_angles[0])
        # sibling_diag: positive DIAG value (pointing down in COLMAP Y-down)
        # e.g. extraction pitch -10° → DIAG target +10°
        sibling_diag = abs(sibling_pitch)

        # Measure anchor pitch now so we can apply the same rotation to points3D
        # to keep the point cloud approximately consistent with corrected camera poses.
        anchor_pitch_before = _measure_anchor_pitch(sparse_txt)
        points_correction_deg = -anchor_pitch_before  # approx global shift for points

        report(PipelineStage.COLMAP_ALIGNMENT, 91,
               f"Per-camera pitch correction: anchor→0°, siblings→{sibling_diag:.1f}° DIAG "
               f"(COLMAP world tilted ~{anchor_pitch_before:.2f}° from horizontal)…")
        n_cams = _correct_camera_pitches_from_extraction(
            sparse_txt,
            anchor_diag_pitch=0.0,
            sibling_diag_pitch=sibling_diag,
        )
        # Apply approximate global rotation to points3D for consistency.
        # All cameras received roughly the same pitch shift (~anchor_pitch_before),
        # so rotating the point cloud by the same amount keeps projections valid.
        if abs(points_correction_deg) > 0.5:
            theta = np.radians(points_correction_deg)
            Rg = np.array([
                [1, 0,              0             ],
                [0, np.cos(theta), -np.sin(theta) ],
                [0, np.sin(theta),  np.cos(theta) ],
            ], dtype=np.float64)
            _fix_points3D(sparse_txt / "points3D.txt", Rg)

        post_pitch = _measure_anchor_pitch(sparse_txt)
        report(PipelineStage.COLMAP_ALIGNMENT, 91,
               f"Per-camera correction applied to {n_cams} cameras — "
               f"anchor DIAG: {post_pitch:.3f}° (expected 0°)")
    else:
        report(PipelineStage.COLMAP_ALIGNMENT, 91, "Scene-tilt correction skipped.")

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
