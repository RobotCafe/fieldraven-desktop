"""
RigSfM alignment — rig-aware SfM via Pi3 anchor expansion.

Pipeline:
  1. Ensure per-sensor image dirs exist (reuse from COLMAP/GluMap step)
  2. Copy anchor sensor images (horizon ref = pano_camera0) to a staging dir
  3. Run GluMap/Pi3 on the 25 anchor images → global camera poses
  4. Read Pi3 COLMAP output, expand rig geometry (25 → all sensors)
  5. SIFT feature extraction + matching on all images (rigsfm_worker subprocess)
  6. Triangulate 3D point cloud from known rig poses + SIFT correspondences
  7. Bundle adjustment (ABA)
  8. Color sample 3D points → brush_input/ (text COLMAP format)

Speed: Pi3 on 25 anchors ≈ 2 min vs ≈ 27 min on 325. Total ≈ 13× faster than
full GluMap. Rig geometry is enforced from the start (not post-hoc).
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
from .colmap_runner import _cam_from_pano, _virtual_rotations, _quat_to_mat


_PYTHON_314  = "C:\\Python314\\python.exe"
_WORKER      = str(Path(__file__).parent / "rigsfm_worker.py")
_VISUALIZER  = str(Path(__file__).parent.parent / "tools" / "visualize_cameras.py")
_LOG_DIR     = Path(__file__).parent.parent / "logs"

_ANSI      = re.compile(r"\x1b\[[0-9;]*[mA-Za-z]")
_QUAD_YAWS = [0, 90, 180, 270]


def _trim_ceres_logs(keep: int = 20) -> None:
    """Delete oldest glog files in _LOG_DIR beyond `keep` count."""
    logs = sorted(
        [p for p in _LOG_DIR.iterdir() if ".log." in p.name],
        key=lambda p: p.stat().st_mtime,
    )
    for old in logs[:-keep] if len(logs) > keep else []:
        try:
            old.unlink()
        except OSError:
            pass


def _win_to_wsl(path: Path) -> str:
    s = str(path.resolve()).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        return f"/mnt/{s[0].lower()}{s[2:]}"
    return s


def _find_colmap_output(write_path: Path) -> Optional[Path]:
    for candidate in [
        write_path / "gluemap_aba",
        write_path / "refined",
        write_path / "coarse",
        write_path,
        write_path / "0",
        write_path / "sparse" / "0",
        write_path / "reconstruction",
    ]:
        if candidate.exists():
            if (candidate / "cameras.bin").exists() or (candidate / "cameras.txt").exists():
                return candidate
    for fname in ("cameras.bin", "cameras.txt"):
        for match in write_path.rglob(fname):
            return match.parent
    return None


def _abs_rotations(settings: PipelineSettings) -> list[np.ndarray]:
    """
    Return absolute cam_from_pano rotation matrices for every sensor,
    in pano_camera{j} order.  These are the raw extraction rotations,
    NOT relative to any reference sensor.
    """
    return _virtual_rotations(
        settings.yaw_steps, settings.pitch_angles,
        horizon_ref=getattr(settings, "horizon_ref", True),
    )


def _parse_pi3_poses(recon_dir: Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """
    Read COLMAP binary reconstruction written by Pi3 / GluMap.

    Returns {image_name: (R_3x3, t_3)} where (R, t) is the cam_from_world
    pose in COLMAP convention (X_cam = R @ X_world + t).
    """
    try:
        import pycolmap
        recon = pycolmap.Reconstruction()
        recon.read(str(recon_dir))
    except Exception as e:
        raise RuntimeError(f"Could not read Pi3 COLMAP output from {recon_dir}: {e}")

    poses: dict = {}
    for img in recon.images.values():
        try:
            # pycolmap 4.x: cam_from_world is Rigid3d (property or C++ method)
            cfw = img.cam_from_world
            if callable(cfw):
                cfw = cfw()
            R = np.array(cfw.rotation.matrix(), dtype=np.float64)
            t = np.array(cfw.translation, dtype=np.float64).flatten()
        except AttributeError:
            # pycolmap 0.x (qvec/tvec)
            try:
                qw, qx, qy, qz = img.qvec
            except AttributeError:
                attrs = [a for a in dir(img) if not a.startswith('_')]
                raise RuntimeError(
                    f"Cannot read pose from pycolmap Image — unknown API. "
                    f"Available attrs: {attrs}"
                )
            R = _quat_to_mat(qw, qx, qy, qz)
            t = np.array(img.tvec, dtype=np.float64).flatten()
        poses[img.name] = (R, t)

    print(f"  [rigsfm] Read {len(poses)} anchor poses from Pi3 output", flush=True)
    return poses


def _expand_rig_poses(
    anchor_poses: dict[str, tuple[np.ndarray, np.ndarray]],
    abs_rots: list[np.ndarray],
    anchor_idx: int = 0,
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """
    Expand anchor poses (n_frames) → full rig poses (n_frames x n_sensors).

    Pi3 gives (R_a, t_a): the anchor camera's cam_from_world pose.  That pose
    has the anchor sensor's extraction direction baked in.

    For any anchor index the correct per-sensor pose is:

        R_j = abs_rots[j] @ abs_rots[anchor_idx].T @ R_a
        t_j = -(R_j @ C_rig)   where  C_rig = -R_a.T @ t_a

    Derivation:
      abs_rots[anchor_idx].T @ R_a  undoes the anchor's extraction rotation,
      recovering the raw rig-body orientation in Pi3's world frame.
      abs_rots[j] then re-applies sensor j's own extraction rotation.

    This formula is correct for ALL anchor choices, including non-horizon-ref
    sensors.  The previous implementation used relative (cam_from_rig) rotations
    which introduced a spurious R_0 factor whenever anchor_idx != 0, causing
    sibling cameras to drift away from the rig centre.
    """
    R_anchor_abs = abs_rots[anchor_idx]
    n_sensors    = len(abs_rots)

    all_poses: dict = {}

    for img_name, (R_a, t_a) in anchor_poses.items():
        frame_filename = Path(img_name).name  # strip "pano_anchor/" prefix
        C_rig = -(R_a.T @ t_a)               # physical rig centre (shared across sensors)

        for j, R_j_abs in enumerate(abs_rots):
            R_j = R_j_abs @ R_anchor_abs.T @ R_a   # sensor j cam_from_world
            t_j = -(R_j @ C_rig)
            all_poses[f"pano_camera{j}/{frame_filename}"] = (R_j, t_j)

    print(
        f"  [rigsfm] Expanded {len(anchor_poses)} frames x {n_sensors} sensors"
        f" = {len(all_poses)} poses (anchor idx={anchor_idx})",
        flush=True,
    )
    return all_poses


def _stage_quad_anchors(
    frames_dir: Path,
    anchor_dir: Path,
    fov_deg: float,
    target_size: int,
) -> int:
    """
    Crop 4 horizon views (yaw 0/90/180/270°, pitch 0°) from every equirectangular
    source frame in frames_dir and write them to anchor_dir with station-first naming:

        {orig_stem}_h0.jpg  {orig_stem}_h1.jpg  {orig_stem}_h2.jpg  {orig_stem}_h3.jpg

    This sort order makes Pi3 see a full 360° spin at each station before advancing,
    giving strong within-station loop closure even for sporadic/non-sequential captures.

    Returns total number of anchor images written (n_frames * 4).
    """
    try:
        from PIL import Image as _PIL
    except ImportError:
        raise RuntimeError("Pillow is required for quad-anchor staging (pip install Pillow)")

    srcs = sorted(frames_dir.glob("*.jpg")) or sorted(frames_dir.glob("*.JPG"))
    if not srcs:
        raise RuntimeError(
            f"Quad anchor mode: no source equirectangular frames found in {frames_dir}. "
            "Ensure frame extraction has run (01_frames/ must contain .jpg files)."
        )

    anchor_dir.mkdir(parents=True, exist_ok=True)
    n_staged = 0

    for src in srcs:
        orig_stem = src.stem
        img = _PIL.open(src)
        IW, IH = img.size
        crop_w = int((fov_deg / 360.0) * IW)
        crop_h = int((fov_deg / 180.0) * IH)

        for h, yaw in enumerate(_QUAD_YAWS):
            dst = anchor_dir / f"{orig_stem}_h{h}.jpg"
            if dst.exists():
                n_staged += 1
                continue

            cx = int(((yaw + 180) % 360) / 360.0 * IW)
            cy = IH // 2
            sx = ((cx - crop_w // 2) % IW + IW) % IW
            sy = max(0, min(IH - crop_h, cy - crop_h // 2))

            if sx + crop_w <= IW:
                region = img.crop((sx, sy, sx + crop_w, sy + crop_h))
            else:
                p1w   = IW - sx
                p1    = img.crop((sx, sy, IW, sy + crop_h))
                p2    = img.crop((0,  sy, crop_w - p1w, sy + crop_h))
                region = _PIL.new("RGB", (crop_w, crop_h))
                region.paste(p1, (0, 0))
                region.paste(p2, (p1w, 0))

            region.resize((target_size, target_size), _PIL.LANCZOS).save(
                dst, "JPEG", quality=90
            )
            n_staged += 1

        img.close()

    return n_staged


def _write_quad_poses_json(
    pi3_poses: dict[str, tuple[np.ndarray, np.ndarray]],
    out_path: Path,
) -> None:
    """Write per-crop camera poses to a JSON sidecar for the cameras.html visualizer."""
    import json as _json
    _pat = re.compile(r"^(.+)_h(\d+)$")
    records = []
    for name, (R, t) in pi3_poses.items():
        stem = Path(name).stem
        m = _pat.match(stem)
        if not m:
            continue
        h_idx = int(m.group(2))
        if h_idx >= len(_QUAD_YAWS):
            continue
        records.append({
            "station":  m.group(1),
            "h_idx":    h_idx,
            "yaw_deg":  _QUAD_YAWS[h_idx],
            "center":   (-R.T @ t).tolist(),
            "forward":  R.T[:, 2].tolist(),   # camera +Z mapped to world space
        })
    records.sort(key=lambda r: (r["station"], r["h_idx"]))
    out_path.write_text(_json.dumps(records, indent=2), encoding="utf-8")
    print(f"  [rigsfm] Wrote {len(records)} Pi3 quad crop poses → {out_path.name}", flush=True)


def _stage_sensor_anchors(
    image_dir: Path,
    anchor_dir: Path,
    sensor_indices: list,
) -> int:
    """
    Stage anchor images from already-rendered per-sensor views.

    Copies images from pano_camera{sensor_indices[h]}/ to anchor_dir with
    station-first naming: {frame_stem}_h{h}.jpg  (one h per selected sensor).
    Uses the same rendered views that the rest of the pipeline already has —
    no equirectangular source files required.

    Returns total number of anchor images staged.
    """
    import shutil as _shutil
    anchor_dir.mkdir(parents=True, exist_ok=True)
    n_staged = 0
    for h, sensor_idx in enumerate(sensor_indices):
        sensor_dir = image_dir / f"pano_camera{sensor_idx}"
        if not sensor_dir.exists():
            continue
        for src in sorted(sensor_dir.glob("*.jpg")):
            dst = anchor_dir / f"{src.stem}_h{h}.jpg"
            if not dst.exists():
                _shutil.copy2(str(src), str(dst))
            n_staged += 1
    return n_staged


def _aggregate_quad_poses(
    pi3_poses: dict[str, tuple[np.ndarray, np.ndarray]],
    sensor_abs_rots: list | None = None,
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """
    Aggregate Pi3 output from 4-sensor staging into one rig pose per station.

    Images are named  pano_anchor/{orig_stem}_h{0-3}.jpg  where h is the
    index into the selected sensors.

    For each observation (R_k, t_k) with known absolute rotation R_k_abs:
        R_rig_estimate = R_k_abs.T @ R_k   (undo the baked-in sensor rotation)
        t_rig_estimate = R_k_abs.T @ t_k

    sensor_abs_rots: list of 4 abs_rot matrices for the selected sensors (from
        _abs_rotations). When None, falls back to hardcoded equirectangular-crop
        rotations at yaw 0/90/180/270° (legacy equirectangular-crop mode).

    All available estimates for a station are averaged; the mean rotation is
    re-orthogonalised via SVD to stay on SO(3).

    Returns {orig_stem + ".jpg": (R_rig, t_rig)} — one entry per station.
    """
    if sensor_abs_rots is not None:
        quad_abs_rots = [np.array(r, dtype=np.float64) for r in sensor_abs_rots]
    else:
        quad_abs_rots = [_cam_from_pano(0.0, float(y)) for y in _QUAD_YAWS]
    _pat = re.compile(r"^(.+)_h(\d+)$")

    stations: dict[str, list[tuple[np.ndarray, np.ndarray, int]]] = {}
    for name, (R, t) in pi3_poses.items():
        stem = Path(name).stem          # e.g. "IMG_20260708_182246_00_272_h0"
        m = _pat.match(stem)
        if not m:
            continue
        orig_stem = m.group(1)          # "IMG_20260708_182246_00_272"
        h_idx     = int(m.group(2))
        if h_idx >= len(_QUAD_YAWS):
            continue
        stations.setdefault(orig_stem, []).append((R, t, h_idx))

    rig_poses: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    for orig_stem, obs_list in sorted(stations.items()):
        R_list, t_list = [], []
        for R, t, h_idx in obs_list:
            R_abs = quad_abs_rots[h_idx]
            R_list.append(R_abs.T @ R)
            t_list.append(R_abs.T @ t)

        R_mean = np.mean(R_list, axis=0)
        U, _, Vt = np.linalg.svd(R_mean)
        R_rig = U @ Vt
        if np.linalg.det(R_rig) < 0:
            U[:, -1] *= -1
            R_rig = U @ Vt

        t_rig = np.mean(t_list, axis=0)
        rig_poses[f"{orig_stem}.jpg"] = (R_rig, t_rig)

    n_obs = sum(len(v) for v in stations.values())
    print(
        f"  [rigsfm] Aggregated {n_obs} quad observations → {len(rig_poses)} rig poses",
        flush=True,
    )
    return rig_poses


def _expand_rig_poses_from_rig(
    rig_poses: dict[str, tuple[np.ndarray, np.ndarray]],
    abs_rots: list[np.ndarray],
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """
    Expand rig-body cam_from_world poses → all sensors.

    Unlike _expand_rig_poses (which takes a single anchor sensor's Pi3 pose and
    undoes its baked-in rotation), this function receives poses that already
    represent the rig body directly — no anchor-specific correction needed.

        R_j = abs_rots[j] @ R_rig
        t_j = abs_rots[j] @ t_rig

    Returns {pano_camera{j}/{frame_filename}: (R_j, t_j)}.
    """
    n_sensors = len(abs_rots)
    all_poses: dict = {}
    for frame_filename, (R_rig, t_rig) in rig_poses.items():
        for j, R_j_abs in enumerate(abs_rots):
            all_poses[f"pano_camera{j}/{frame_filename}"] = (R_j_abs @ R_rig, R_j_abs @ t_rig)

    print(
        f"  [rigsfm] Expanded {len(rig_poses)} rig poses × {n_sensors} sensors"
        f" = {len(all_poses)} poses (quad anchor mode)",
        flush=True,
    )
    return all_poses


def _run_pi3_anchors(
    anchor_parent_dir: Path,
    output_dir: Path,
    settings: PipelineSettings,
    report: Callable,
    cancel_event: threading.Event,
) -> None:
    """
    Run GluMap/Pi3 on the anchor sensor folder only.

    anchor_parent_dir must contain exactly one subdirectory: pano_anchor/
    """
    backbone    = getattr(settings, "gluemap_backbone",           "pi3")
    skip_dg     = getattr(settings, "gluemap_skip_doppelgangers", True)
    n_neighbors = getattr(settings, "gluemap_num_neighbors",      100)
    batch_size  = getattr(settings, "gluemap_batch_size",         30)
    num_track   = getattr(settings, "gluemap_num_track_per_img",  1024)
    wsl_home    = getattr(settings, "gluemap_wsl_home",           "/home/decosson")
    wsl_distro  = getattr(settings, "gluemap_wsl_distro",         "Ubuntu-22.04")

    checkpoints  = f"{wsl_home}/gluemap/checkpoints"
    micromamba   = f"{wsl_home}/.local/bin/micromamba"
    backbone_ckpt = {
        "pi3":          f"{checkpoints}/pi3.safetensors",
        "pi3x":         f"{checkpoints}/pi3.safetensors",
        "vggt":         f"{checkpoints}/vggt.safetensors",
        "map_anything": f"{checkpoints}/map_anything.safetensors",
    }.get(backbone, f"{checkpoints}/pi3.safetensors")

    output_dir.mkdir(parents=True, exist_ok=True)

    inner = [
        micromamba, "run", "-n", "gluemap", "gluemap-demo",
        "--images_path",       _win_to_wsl(anchor_parent_dir),
        "--write_path",        _win_to_wsl(output_dir),
        "--intrinsics_mode",   "PER_FOLDER",
        "--chosen_model",      backbone,
        "--path_feedforward",  backbone_ckpt,
        "--path_retrieval",    f"{checkpoints}/dino_salad.ckpt",
        "--path_tracker",      f"{checkpoints}/vggsfm_v2_0_0_track_predictor.bin",
        "--path_dg",           f"{checkpoints}/checkpoint-dg+visym.pth",
        "--num_neighbors",     str(n_neighbors),
        "--batch_size",        str(batch_size),
        "--num_track_per_img", str(num_track),
        "--is_sequential",     # anchors are always ordered
    ]
    if skip_dg:
        inner.append("--skip_doppelgangers")

    wsl_cmd = ["wsl", "-d", wsl_distro, "--"] + inner

    report(PipelineStage.RIGSFM_ALIGNMENT, 10,
           f"RigSfM: Pi3 inference on anchor images ({backbone})…")
    print(f"  [rigsfm] Pi3 CMD: {' '.join(wsl_cmd)}", flush=True)

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        wsl_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace",
        cwd=str(_LOG_DIR),
    )

    _last_t = time.time()
    while True:
        raw = process.stdout.readline()
        if not raw and process.poll() is not None:
            break
        if not raw:
            continue
        line = _ANSI.sub("", raw).rstrip()
        if not line:
            continue
        print(f"  [pi3] {line}", flush=True)
        if time.time() - _last_t >= 3.0:
            report(PipelineStage.RIGSFM_ALIGNMENT, 12, f"Pi3: {line[:160]}")
            _last_t = time.time()
        if cancel_event.is_set():
            process.terminate()
            return

    process.wait()
    _trim_ceres_logs()
    if process.returncode != 0:
        raise RuntimeError(f"Pi3 (GluMap anchor pass) exited with code {process.returncode}")


def run_rigsfm_pipeline(
    views_dir: Path,
    colmap_dir: Path,
    brush_input_dir: Path,
    settings: PipelineSettings,
    report: Callable[[PipelineStage, int, str], None],
    cancel_event: threading.Event,
    project_dir: Optional[Path] = None,
    source_dir: Optional[Path] = None,
) -> None:
    """
    Run RigSfM alignment and populate brush_input_dir.

    Args:
        views_dir:       02_views/ — source for image reorganisation
        colmap_dir:      03_alignment/colmap/ — per-sensor images land here
        brush_input_dir: 04_training/brush_input/ — output destination
        settings:        PipelineSettings (gluemap_* and rigsfm_* fields used)
        report:          progress callback (stage, pct, message)
        cancel_event:    set to abort
    """
    from .colmap_runner import _reorganize_views
    from .gluemap_runner import _sample_and_write_colored_recon

    stage = PipelineStage.RIGSFM_ALIGNMENT
    report(stage, 0, "RigSfM: preparing image directories…")

    # ── 1. Ensure per-sensor image dirs exist ─────────────────────────────────
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
        report(stage, 3, f"RigSfM: {n_sensors} sensor dirs already exist — skipping reorganisation")
    else:
        report(stage, 2, "RigSfM: reorganising views into per-sensor directories…")
        n_sensors = _reorganize_views(views_dir, image_dir)
        if n_sensors == 0:
            raise RuntimeError("No view images found in 02_views/ for RigSfM")
        report(stage, 3, f"RigSfM: reorganised into {n_sensors} sensor dirs")

    if cancel_event.is_set():
        return

    # ── 2. Stage anchor images ────────────────────────────────────────────────
    quad_anchors = bool(getattr(settings, "rigsfm_quad_anchors", False))
    anchor_idx   = 0 if quad_anchors else int(getattr(settings, "rigsfm_anchor_sensor", 0))
    rigsfm_dir   = colmap_dir.parent / "rigsfm"
    anchor_dir   = rigsfm_dir / "anchors" / "pano_anchor"
    pi3_out_dir  = rigsfm_dir / "pi3_output"

    anchor_dir.mkdir(parents=True, exist_ok=True)

    _quad_sensor_indices: list[int] = []
    if quad_anchors:
        # Pick 4 sensors evenly spaced around the rig from the already-rendered views.
        # Step = n_sensors // 4 → for 13 sensors: indices [0, 3, 6, 9].
        _step = max(1, n_sensors // 4)
        _quad_sensor_indices = [i * _step for i in range(4)]
        n_anchors = _stage_sensor_anchors(image_dir, anchor_dir, _quad_sensor_indices)
        if n_anchors == 0:
            raise RuntimeError(
                f"Quad anchor mode: no rendered sensor views found in {image_dir}. "
                "Ensure view extraction has run first."
            )
        report(stage, 5,
               f"RigSfM: {n_anchors} quad anchor images staged "
               f"(sensors {_quad_sensor_indices}, {n_anchors // 4} stations × 4)")
    else:
        anchor_src = image_dir / f"pano_camera{anchor_idx}"
        if not anchor_src.exists():
            raise RuntimeError(
                f"Anchor sensor directory not found: {anchor_src}\n"
                f"Selected anchor index {anchor_idx} does not exist — "
                "check that view extraction has run and the index is within range."
            )
        n_anchors = 0
        for src in sorted(anchor_src.glob("*.jpg")):
            dst = anchor_dir / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
            n_anchors += 1
        if n_anchors == 0:
            raise RuntimeError(f"No anchor images found in {anchor_src}")
        report(stage, 5,
               f"RigSfM: {n_anchors} anchor images staged (pano_camera{anchor_idx} → pano_anchor)")

    if cancel_event.is_set():
        return

    # ── 3. Run Pi3 on anchors ─────────────────────────────────────────────────
    recon_dir = _find_colmap_output(pi3_out_dir)
    if recon_dir is not None:
        report(stage, 30, f"RigSfM: Pi3 output already exists ({recon_dir.name}) — reusing")
    else:
        _run_pi3_anchors(
            anchor_parent_dir=rigsfm_dir / "anchors",
            output_dir=pi3_out_dir,
            settings=settings,
            report=report,
            cancel_event=cancel_event,
        )
        if cancel_event.is_set():
            return
        recon_dir = _find_colmap_output(pi3_out_dir)
        if recon_dir is None:
            raise RuntimeError(
                f"Pi3 finished successfully but no COLMAP output found under {pi3_out_dir}"
            )
        report(stage, 30, "RigSfM: Pi3 anchor inference complete")

    if cancel_event.is_set():
        return

    # ── 4. Parse Pi3 poses and expand rig ────────────────────────────────────
    report(stage, 32, "RigSfM: reading Pi3 poses and expanding rig geometry…")
    pi3_poses_raw = _parse_pi3_poses(recon_dir)

    if not pi3_poses_raw:
        raise RuntimeError(
            "Pi3 registered no images — anchor alignment failed. "
            "Check that the anchor images have enough visual overlap."
        )

    abs_rots = _abs_rotations(settings)

    if quad_anchors:
        _quad_sensor_abs_rots = [abs_rots[i] for i in _quad_sensor_indices] if _quad_sensor_indices else None
        rig_poses = _aggregate_quad_poses(pi3_poses_raw, sensor_abs_rots=_quad_sensor_abs_rots)
        if not rig_poses:
            raise RuntimeError(
                "Quad anchor aggregation produced no rig poses — "
                "check that Pi3 output filenames match the expected {orig_stem}_h{0-3} pattern."
            )
        _write_quad_poses_json(pi3_poses_raw, rigsfm_dir / "pi3_quad_poses.json")
        all_poses       = _expand_rig_poses_from_rig(rig_poses, abs_rots)
        n_anchor_frames = len(rig_poses)
    else:
        all_poses       = _expand_rig_poses(pi3_poses_raw, abs_rots, anchor_idx)
        n_anchor_frames = len(pi3_poses_raw)

    # Serialize poses for the worker subprocess
    poses_json = {
        name: {"R": R.tolist(), "t": t.tolist()}
        for name, (R, t) in all_poses.items()
    }

    report(stage, 35,
           f"RigSfM: {n_anchor_frames} frames × {len(abs_rots)} sensors "
           f"= {len(all_poses)} expanded poses")

    if cancel_event.is_set():
        return

    # ── 5. SIFT extraction + matching + triangulation + BA ───────────────────
    report(stage, 37, "RigSfM: starting SIFT extraction and matching…")

    db_path    = rigsfm_dir / "colmap.db"
    sparse_txt = rigsfm_dir / "sparse_txt"

    # Determine image size from first available image
    try:
        from PIL import Image as _PIL
        image_size = max(_PIL.open(next(image_dir.rglob("*.jpg"))).size)
    except Exception:
        image_size = settings.colmap_image_width or 1920

    focal = image_size / (2.0 * np.tan(np.deg2rad(settings.fov) / 2.0))

    payload = json.dumps({
        "database_path":   str(db_path),
        "image_path":      str(image_dir),
        "sparse_txt_path": str(sparse_txt),
        "poses":           poses_json,
        "focal":           focal,
        "image_size":      image_size,
        "matcher":          getattr(settings, "rigsfm_matcher", "sequential"),
        "colmap_bin":       getattr(settings, "colmap_bin", "") or "",
        "vocab_tree_path":  getattr(settings, "colmap_vocab_tree", "") or "",
        "abs_rots":         [r.tolist() for r in abs_rots],
        "anchor_idx":       anchor_idx,
    })

    # Windows CreateProcess limit ~32 KB; 325 poses easily exceeds it.
    # Write payload to a temp file and pass the path instead.
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(payload)
        payload_path = tf.name

    proc = subprocess.Popen(
        [_PYTHON_314, _WORKER, f"@{payload_path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )

    last_json_line = ""
    _last_t        = time.time()
    current_pct    = 37

    while True:
        raw = proc.stdout.readline()
        if not raw and proc.poll() is not None:
            break
        if not raw:
            continue
        line = raw.rstrip()
        if not line:
            continue
        print(f"  [rigsfm_worker] {line}", flush=True)

        if line.startswith("WORKER_PROGRESS:"):
            parts = line.split(":", 2)
            if len(parts) == 3:
                try:
                    w_pct = int(parts[1])
                    current_pct = 37 + int(w_pct * 0.53)
                except ValueError:
                    pass
                msg = parts[2]
                if time.time() - _last_t >= 2.0:
                    report(stage, current_pct, f"RigSfM: {msg}")
                    _last_t = time.time()
        elif line.startswith("{"):
            last_json_line = line

        if cancel_event.is_set():
            proc.terminate()
            return

    proc.wait()

    result: dict = {}
    try:
        result = json.loads(last_json_line)
    except Exception:
        pass

    if proc.returncode != 0 and not result.get("success"):
        err = result.get("error", "worker process failed — check log")
        raise RuntimeError(f"RigSfM worker: {err}")

    n_imgs = result.get("images", 0)
    n_pts  = result.get("points3D", 0)
    report(stage, 91, f"RigSfM: {n_imgs} images, {n_pts:,} 3D points triangulated")

    if cancel_event.is_set():
        return

    # ── 5b. Generate cameras.html visualizer ──────────────────────────────────
    _viz_html = rigsfm_dir / "cameras.html"
    if Path(_VISUALIZER).exists() and sparse_txt.exists():
        anchor_sensor_name = f"pano_camera{anchor_idx}"
        subprocess.run(
            [_PYTHON_314, _VISUALIZER, str(sparse_txt), str(_viz_html),
             "0", "0", anchor_sensor_name, str(image_dir)],
            check=False,
        )
        if _viz_html.exists():
            import webbrowser
            webbrowser.open(_viz_html.as_uri())
            report(stage, 92, f"RigSfM: opened camera visualizer ({_viz_html.name})")

    # ── 6. Copy images + sample point colors → brush_input/ ──────────────────
    report(stage, 93, "RigSfM: copying reconstruction → brush_input/…")
    if brush_input_dir.exists():
        shutil.rmtree(str(brush_input_dir))
    brush_input_dir.mkdir(parents=True, exist_ok=True)

    images_dst = brush_input_dir / "images"
    if not images_dst.exists() and image_dir.exists():
        shutil.copytree(str(image_dir), str(images_dst))

    report(stage, 95, "RigSfM: sampling point colors from images…")
    _sample_and_write_colored_recon(sparse_txt, brush_input_dir, report, stage)

    n_files = (
        len(list(brush_input_dir.glob("*.txt")))
        + len(list(brush_input_dir.glob("*.bin")))
    )
    report(stage, 100,
           f"RigSfM alignment complete — {n_files} reconstruction files in brush_input/")
