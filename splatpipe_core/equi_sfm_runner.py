# splatpipe_core/equi_sfm_runner.py
"""
EquiSfM alignment — COLMAP EQUIRECTANGULAR SfM on raw panoramic images.

Pipeline:
  1. Stage source equirectangular panos to a flat directory
  2. Run COLMAP EQUIRECTANGULAR SfM via Python 3.14 / pycolmap 4.1.0 worker
     → per-pano cam_from_world poses + sparse 3D point cloud
  3. Expand per-pano poses → per-sensor poses via known rig geometry
       R_j = abs_rots[j] @ R_pano
       t_j = abs_rots[j] @ t_pano
  4. Write sensor-level COLMAP text format (cameras.txt / images.txt / points3D.txt)
     with color-sampled 3D points
  5. Copy to brush_input/ for 3DGS training

Advantages over Pi3/RigSfM:
  • No GluMap / Pi3 dependency
  • Native 360° spherical matching — no stitch-seam artefacts at the horizon
  • Much simpler: no anchor staging, no rig expansion of matched images, no per-sensor BA
  • Still produces per-sensor views for 3DGS training via the existing view_extraction step
"""
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .types import PipelineStage
from .settings import PipelineSettings
from .colmap_runner import _mat_to_quat, _virtual_rotations, _quat_to_mat
from .gluemap_runner import _sample_and_write_colored_recon


_PYTHON_314 = "C:\\Python314\\python.exe"
_WORKER     = str(Path(__file__).parent / "equi_sfm_worker.py")
_VISUALIZER = str(Path(__file__).parent.parent / "tools" / "visualize_cameras.py")
_ANSI       = re.compile(r"\x1b\[[0-9;]*[mA-Za-z]")


# ── helpers ───────────────────────────────────────────────────────────────────

def _abs_rotations(settings: PipelineSettings) -> list[np.ndarray]:
    """cam_from_pano rotation matrices for every sensor, horizon_ref first."""
    return _virtual_rotations(
        settings.yaw_steps, settings.pitch_angles,
        horizon_ref=getattr(settings, "horizon_ref", True),
    )


def _stage_panos(source_dir: Path, stage_dir: Path) -> int:
    """
    Copy equirectangular JPEGs from source_dir into a flat stage_dir.

    Returns count of staged images.  Skips already-staged files.
    """
    stage_dir.mkdir(parents=True, exist_ok=True)
    exts = {".jpg", ".jpeg", ".JPG", ".JPEG"}
    n = 0
    for src in sorted(source_dir.iterdir()):
        if src.is_file() and src.suffix in exts:
            dst = stage_dir / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
            n += 1
    return n


def _read_pano_points(sparse_txt: Path) -> list[tuple]:
    """
    Read points3D.txt from pano-level sparse_txt.

    Returns list of (point3d_id, x, y, z, r, g, b, error).
    """
    pts = []
    pts_file = sparse_txt / "points3D.txt"
    if not pts_file.exists():
        return pts
    for line in pts_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        if len(tokens) < 8:
            continue
        try:
            pts.append((
                int(tokens[0]),
                float(tokens[1]), float(tokens[2]), float(tokens[3]),
                int(tokens[4]), int(tokens[5]), int(tokens[6]),
                float(tokens[7]),
            ))
        except (ValueError, IndexError):
            continue
    return pts


def _write_sensor_sparse_txt(
    sensor_dir: Path,
    all_poses: dict[str, tuple[np.ndarray, np.ndarray]],
    pano_points: list[tuple],
    focal: float,
    image_w: int,
    image_h: int,
) -> None:
    """
    Write a COLMAP text-format reconstruction with per-sensor layout.

    all_poses: {"pano_camera{j}/{frame}.jpg": (R_3x3, t_3)}
    pano_points: [(id, x, y, z, r, g, b, err), ...]
    """
    sensor_dir.mkdir(parents=True, exist_ok=True)

    cx = image_w / 2.0
    cy = image_h / 2.0

    # ── cameras.txt: one shared PINHOLE camera ─────────────────────────────
    (sensor_dir / "cameras.txt").write_text(
        "# Camera list with one line of data per camera:\n"
        "#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n"
        f"1 PINHOLE {image_w} {image_h} {focal:.6f} {focal:.6f} {cx:.6f} {cy:.6f}\n",
        encoding="utf-8",
    )

    # ── images.txt: one entry per sensor × frame ────────────────────────────
    img_lines = [
        "# Image list with two lines of data per image:\n"
        "#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n"
        "#   POINTS2D[] as (X, Y, POINT3D_ID)\n"
    ]
    for img_id, (name, (R, t)) in enumerate(sorted(all_poses.items()), start=1):
        qw, qx, qy, qz = _mat_to_quat(R)
        tx, ty, tz = float(t[0]), float(t[1]), float(t[2])
        img_lines.append(
            f"{img_id} {qw:.10f} {qx:.10f} {qy:.10f} {qz:.10f} "
            f"{tx:.10f} {ty:.10f} {tz:.10f} 1 {name}\n"
        )
        img_lines.append("\n")  # empty POINTS2D line

    (sensor_dir / "images.txt").write_text("".join(img_lines), encoding="utf-8")

    # ── points3D.txt: 3D points from pano recon, no per-sensor tracks ──────
    # Brush / PostShot use these for scene-scale initialisation; they don't
    # require complete track references for training to succeed.
    pt_lines = [
        "# 3D point list with one line of data per point:\n"
        "#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)\n"
    ]
    for pt_id, x, y, z, r, g, b, err in pano_points:
        pt_lines.append(f"{pt_id} {x:.6f} {y:.6f} {z:.6f} {r} {g} {b} {err:.6f}\n")
    (sensor_dir / "points3D.txt").write_text("".join(pt_lines), encoding="utf-8")


# ── main entry point ──────────────────────────────────────────────────────────

def run_equisfm_pipeline(
    source_dir: Path,
    views_dir: Path,
    colmap_dir: Path,
    brush_input_dir: Path,
    settings: PipelineSettings,
    report: Callable[[PipelineStage, int, str], None],
    cancel_event: threading.Event,
) -> None:
    """
    Run EquiSfM alignment and populate brush_input_dir.

    Args:
        source_dir:      flat directory of equirectangular pano JPEGs
                         (01_frames/, 'import from camera/', or user image folder)
        views_dir:       02_views/ — already-rendered per-sensor images
        colmap_dir:      03_alignment/colmap/ — working dir for DB / sparse output
        brush_input_dir: 04_training/brush_input/ — final output for 3DGS training
        settings:        PipelineSettings
        report:          progress callback (stage, pct, message)
        cancel_event:    set to abort
    """
    from .colmap_runner import _reorganize_views

    stage = PipelineStage.EQUISFM_ALIGNMENT
    report(stage, 0, "EquiSfM: preparing directories…")

    equisfm_dir = colmap_dir.parent / "equisfm"
    equisfm_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Ensure per-sensor views exist (for 3DGS training images) ──────────
    image_dir = colmap_dir / "images"
    sensors_exist = (
        image_dir.exists()
        and any(
            d.is_dir() and d.name.startswith("pano_camera")
            for d in image_dir.iterdir()
        )
    ) if image_dir.exists() else False

    if sensors_exist:
        n_sensors = sum(
            1 for d in image_dir.iterdir()
            if d.is_dir() and d.name.startswith("pano_camera")
        )
        report(stage, 3, f"EquiSfM: {n_sensors} sensor dirs already exist — reusing")
    else:
        report(stage, 2, "EquiSfM: reorganising sensor views…")
        n_sensors = _reorganize_views(views_dir, image_dir)
        if n_sensors == 0:
            raise RuntimeError("No view images found in 02_views/ for EquiSfM")
        report(stage, 3, f"EquiSfM: reorganised into {n_sensors} sensor dirs")

    if cancel_event.is_set():
        return

    # ── 2. Stage pano images to a flat dir for COLMAP ─────────────────────
    pano_stage_dir = equisfm_dir / "panos"
    pano_recon_dir = equisfm_dir / "recon"
    db_path        = equisfm_dir / "database.db"
    sparse_txt_dir = equisfm_dir / "sparse_txt"

    # Check if recon already exists (resume support)
    recon_done = sparse_txt_dir.exists() and (sparse_txt_dir / "images.txt").exists()
    if recon_done:
        report(stage, 30, "EquiSfM: prior COLMAP reconstruction found — reusing")
        # Read poses from existing images.txt
        pano_poses = _parse_pano_sparse_txt(sparse_txt_dir)
    else:
        report(stage, 5, f"EquiSfM: staging pano images from {source_dir.name}…")
        n_staged = _stage_panos(source_dir, pano_stage_dir)
        if n_staged == 0:
            raise RuntimeError(
                f"EquiSfM: no JPEG panoramas found in {source_dir}. "
                "Ensure frame extraction or camera import has run first."
            )
        report(stage, 8, f"EquiSfM: {n_staged} pano images staged → spawning worker")

        if cancel_event.is_set():
            return

        # ── 3. Spawn Python 3.14 EQUIRECTANGULAR SfM worker ──────────────────
        matcher = getattr(settings, "equisfm_matcher", "sequential")
        payload = json.dumps({
            "database_path": str(db_path),
            "pano_dir":      str(pano_stage_dir),
            "output_dir":    str(pano_recon_dir),
            "matcher":       matcher,
        })

        proc = subprocess.Popen(
            [_PYTHON_314, "-P", _WORKER, payload],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )

        last_json = ""
        _last_t   = time.time()
        current_pct = 8

        while True:
            raw = proc.stdout.readline()
            if not raw and proc.poll() is not None:
                break
            if not raw:
                continue
            line = _ANSI.sub("", raw).rstrip()
            if not line:
                continue
            print(f"  [equisfm_worker] {line}", flush=True)

            if line.startswith("WORKER_PROGRESS:"):
                parts = line.split(":", 2)
                try:
                    w_pct = int(parts[1])
                    current_pct = 8 + int(w_pct * 0.52)
                except (ValueError, IndexError):
                    pass
                msg = parts[2] if len(parts) > 2 else line
                if time.time() - _last_t >= 2.0:
                    report(stage, current_pct, msg)
                    _last_t = time.time()
            elif line.startswith("{"):
                last_json = line

            if cancel_event.is_set():
                proc.terminate()
                return

        proc.wait()

        result: dict = {}
        try:
            result = json.loads(last_json)
        except Exception:
            pass

        if proc.returncode != 0 and not result.get("success"):
            raise RuntimeError(
                f"EquiSfM worker failed: {result.get('error', 'check log for details')}"
            )

        n_pano_imgs = result.get("images", 0)
        n_pts       = result.get("points3D", 0)
        report(stage, 60, f"EquiSfM: {n_pano_imgs} panos registered, {n_pts:,} 3D points")

        # Worker wrote sparse_txt under pano_recon_dir/sparse_txt; move it up
        worker_sparse = pano_recon_dir / "sparse_txt"
        if worker_sparse.exists() and not sparse_txt_dir.exists():
            shutil.copytree(str(worker_sparse), str(sparse_txt_dir))

        if cancel_event.is_set():
            return

        pano_poses = _parse_pano_sparse_txt(sparse_txt_dir)

    if not pano_poses:
        raise RuntimeError(
            "EquiSfM: no pano poses recovered from reconstruction. "
            "Check that the EQUIRECTANGULAR SfM worker succeeded."
        )

    report(stage, 62, f"EquiSfM: {len(pano_poses)} pano poses read — expanding rig…")

    # ── 4. Expand pano poses → per-sensor poses ────────────────────────────
    abs_rots = _abs_rotations(settings)
    all_poses = _expand_poses(pano_poses, abs_rots)
    report(stage, 65,
           f"EquiSfM: {len(pano_poses)} panos × {len(abs_rots)} sensors "
           f"= {len(all_poses)} sensor poses")

    if cancel_event.is_set():
        return

    # ── 5. Determine camera intrinsics from actual sensor images ──────────
    try:
        from PIL import Image as _PIL
        first_img = next(image_dir.rglob("*.jpg"))
        iw, ih = _PIL.open(first_img).size
    except Exception:
        iw = ih = settings.colmap_image_width or 1920

    focal = iw / (2.0 * np.tan(np.deg2rad(settings.fov) / 2.0))

    # ── 6. Write sensor-level COLMAP text format ───────────────────────────
    report(stage, 67, "EquiSfM: writing sensor-level COLMAP text format…")
    sensor_sparse_dir = equisfm_dir / "sensor_sparse_txt"
    pano_pts = _read_pano_points(sparse_txt_dir)
    _write_sensor_sparse_txt(sensor_sparse_dir, all_poses, pano_pts, focal, iw, ih)
    report(stage, 72,
           f"EquiSfM: wrote {len(all_poses)} image poses, {len(pano_pts):,} 3D points")

    if cancel_event.is_set():
        return

    # ── 7. Build brush_input/ ──────────────────────────────────────────────
    report(stage, 90, "EquiSfM: copying reconstruction → brush_input/…")
    if brush_input_dir.exists():
        shutil.rmtree(str(brush_input_dir))
    brush_input_dir.mkdir(parents=True, exist_ok=True)

    for f in sensor_sparse_dir.iterdir():
        shutil.copy2(str(f), str(brush_input_dir / f.name))

    images_dst = brush_input_dir / "images"
    if not images_dst.exists() and image_dir.exists():
        shutil.copytree(str(image_dir), str(images_dst))

    # Color-sample 3D points from sensor views using the pano-level reconstruction
    # (images/ must exist at brush_input/images/ before this call)
    report(stage, 95, "EquiSfM: color-sampling 3D points from sensor views…")
    try:
        _sample_and_write_colored_recon(
            sensor_sparse_dir, brush_input_dir, report, stage
        )
    except Exception as _ce:
        print(f"  [equisfm] Color sampling failed ({_ce}) — using pano colors", flush=True)

    # ── 8. Open visualizer ─────────────────────────────────────────────────
    _viz_html = equisfm_dir / "cameras.html"
    if Path(_VISUALIZER).exists() and sensor_sparse_dir.exists():
        anchor_sensor = "pano_camera0"
        subprocess.run(
            [_PYTHON_314, _VISUALIZER, str(sensor_sparse_dir), str(_viz_html),
             "0", "0", anchor_sensor, str(image_dir)],
            check=False,
        )
        if _viz_html.exists():
            import webbrowser
            webbrowser.open(_viz_html.as_uri())

    n_files = (
        len(list(brush_input_dir.glob("*.txt")))
        + len(list(brush_input_dir.glob("*.bin")))
    )
    report(stage, 100,
           f"EquiSfM complete — {n_files} reconstruction files in brush_input/")


# ── pose helpers ──────────────────────────────────────────────────────────────

def _parse_pano_sparse_txt(
    sparse_txt: Path,
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """
    Read per-pano cam_from_world poses from an images.txt written by pycolmap.

    Returns {image_name: (R_3x3, t_3)}.
    """
    images_txt = sparse_txt / "images.txt"
    if not images_txt.exists():
        return {}

    poses: dict = {}
    data_line = False
    for line in images_txt.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not data_line:
            tokens = line.split()
            if len(tokens) >= 10:
                # IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME
                try:
                    qw, qx, qy, qz = (float(tokens[i]) for i in (1, 2, 3, 4))
                    tx, ty, tz     = (float(tokens[i]) for i in (5, 6, 7))
                    name = tokens[9]
                    R = _quat_to_mat(qw, qx, qy, qz)
                    t = np.array([tx, ty, tz], dtype=np.float64)
                    poses[name] = (R, t)
                except (ValueError, IndexError):
                    pass
            data_line = True
        else:
            data_line = False

    print(f"  [equisfm] Read {len(poses)} pano poses from {images_txt.name}", flush=True)
    return poses


def _expand_poses(
    pano_poses: dict[str, tuple[np.ndarray, np.ndarray]],
    abs_rots: list[np.ndarray],
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """
    Expand pano cam_from_world poses → per-sensor poses.

    For sensor j with extraction rotation abs_rots[j] (cam_from_pano):
        R_j = abs_rots[j] @ R_pano
        t_j = abs_rots[j] @ t_pano

    The pano body frame in COLMAP's EQUIRECTANGULAR model is the same convention
    as our _cam_from_pano(yaw=0, pitch=0) = identity, so no correction needed.
    """
    n_sensors = len(abs_rots)
    all_poses: dict = {}

    for pano_name, (R_pano, t_pano) in pano_poses.items():
        frame_stem = Path(pano_name).stem
        frame_file = f"{frame_stem}.jpg"
        for j, R_j_abs in enumerate(abs_rots):
            R_j = R_j_abs @ R_pano
            t_j = R_j_abs @ t_pano
            all_poses[f"pano_camera{j}/{frame_file}"] = (R_j, t_j)

    print(
        f"  [equisfm] Expanded {len(pano_poses)} pano poses × {n_sensors} sensors"
        f" = {len(all_poses)} sensor poses",
        flush=True,
    )
    return all_poses
