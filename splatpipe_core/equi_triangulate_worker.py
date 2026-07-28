# splatpipe_core/equi_triangulate_worker.py
"""
EquiSfM "rig glue" worker — runs in Python 3.14 / pycolmap 4.1.0.

Takes the 312 already-solved, already-trusted per-sensor poses (EquiSfM pano
pose solve + analytic rig expansion, computed by equi_sfm_runner.py) and runs
real SIFT matching + triangulation across the actual per-sensor perspective
images, to replace the sparse pano-level point cloud with a properly dense,
properly tracked one.

Hard invariant, enforced by construction: **the rig itself is never adjusted.**
The 13 per-sensor offsets within one capture position (sensor_from_rig) are
locked exactly the same way colmap_worker.py's main rig pipeline locks them
during bundle adjustment -- via pycolmap.BundleAdjustmentConfig.
set_constant_sensor_from_rig_pose(). What CAN move now is each FRAME's overall
pose (rig_from_world) -- i.e. where the whole 13-sensor rig sits in space at
each capture position -- via a real, GPU-enabled, rig-constrained bundle
adjustment pass. This is a deliberate change from this file's original design
(which kept every pose 100% fixed, no BA at all): comparing this job's EquiSfM
reconstruction against a plain-COLMAP reconstruction of the same data showed
the plain-COLMAP cloud is visibly tighter, because its BA can correct small
frame-pose errors inherited from EquiSfM's pano-level solve + analytic rig
expansion, and EquiSfM's fixed poses had no such correction (confirmed earlier
via a Procrustes comparison showing real per-frame pose disagreement).

Triangulation itself still uses `pycolmap.IncrementalTriangulator` (no BA
inside triangulation), matching this file's original approach and
docs/COLMAP_POSE_CORRECTION_BRIEF.md "Problem 9" (`pycolmap.
triangulate_points()` is still avoided -- its internal global-BA pass has no
rig-locking option at all). Bundle adjustment is a separate, explicit,
rig-constrained step afterward.

Mandatory verification, still a hard gate (see `_rig_snapshot`/`_rig_fixed`):
every non-reference sensor's `sensor_from_rig` transform is snapshotted before
BA and compared byte-for-byte after -- if the rig itself moved by even a
rounding error, the worker reports failure and the runner discards the output,
falling back to whatever sparse cloud was there before. Frame-pose movement
(the thing BA is now allowed to do) is reported as a diagnostic, not a
failure condition.

Receives a JSON payload via sys.argv[1], OR "@<path>" pointing at a temp file
containing it (required in practice -- 312 poses inline as a literal
command-line argument overruns Windows' CreateProcess length limit; same
convention as rigsfm_worker.py):
  database_path, image_path, sparse_txt_path, poses ({name: {"R","t"}}),
  abs_rots (list of 3x3 sensor_from_rig rotations, index 0 == anchor/ref sensor),
  focal, image_w, image_h, matcher ("sequential"|"exhaustive")

Progress lines: WORKER_PROGRESS:<pct>:<message>
Final stdout line: JSON result dict, including the mandatory verification
metrics (rig-fixed check, frame-pose movement, reprojection error) -- the
runner must treat a failing rig_fixed as a hard failure, not a log line.
"""
from __future__ import annotations

# Remove this script's own directory from sys.path so that splatpipe_core/types.py
# does not shadow the stdlib 'types' module when Python adds the script dir at startup.
import sys as _sys, os as _os
_this_dir = _os.path.dirname(_os.path.abspath(__file__))
if _this_dir in _sys.path:
    _sys.path.remove(_this_dir)
del _this_dir, _sys, _os

# Force UTF-8 stdout so non-ASCII in _prog messages don't crash on cp1252 Windows consoles.
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
del _sys

import sys
import json
import traceback
from pathlib import Path

import numpy as np


def _prog(pct: int, msg: str) -> None:
    print(f"WORKER_PROGRESS:{pct}:{msg}", flush=True)


def _pose_snapshot(recon) -> dict:
    """{image_id: (quat_wxyz_copy, translation_copy)} for every image."""
    snap = {}
    for img_id, img in recon.images.items():
        cfw = img.cam_from_world()
        snap[img_id] = (np.array(cfw.rotation.quat, copy=True),
                         np.array(cfw.translation, copy=True))
    return snap


def _max_pose_drift(recon, before: dict) -> tuple[float, float]:
    max_rot = max_t = 0.0
    for img_id, img in recon.images.items():
        cfw = img.cam_from_world()
        q0, t0 = before[img_id]
        max_rot = max(max_rot, float(np.linalg.norm(np.array(cfw.rotation.quat) - q0)))
        max_t = max(max_t, float(np.linalg.norm(np.array(cfw.translation) - t0)))
    return max_rot, max_t


def _build_rig_config(abs_rots: list, anchor_idx: int = 0):
    """
    Build a pycolmap.RigConfig from the per-sensor rotations already used to
    expand one solved anchor pose into 13 sensor poses (equi_sfm_runner.py's
    _abs_rotations/_expand_poses). Inlined from rigsfm_worker.py's identical
    helper (_build_rig_config): anchor sensor is the rig reference
    (sensor_from_rig = identity), every other sensor gets sensor_from_rig =
    R_j @ R_anchor.T (pure rotation, zero translation -- all 13 sensors share
    one optical centre on the panoramic head).
    """
    import pycolmap
    rots = [np.array(r, dtype=np.float64) for r in abs_rots]
    R_ref = rots[anchor_idx]
    zero_t = np.zeros(3)
    cameras = []
    for j, R_j in enumerate(rots):
        rc = pycolmap.RigConfigCamera()
        rc.image_prefix = f"pano_camera{j}/"
        if j == anchor_idx:
            rc.ref_sensor = True
        else:
            rc.ref_sensor = False
            rc.cam_from_rig = pycolmap.Rigid3d(
                pycolmap.Rotation3d(R_j @ R_ref.T), zero_t
            )
        cameras.append(rc)
    return pycolmap.RigConfig(cameras=cameras)


def _rig_ba_config(recon):
    """
    Build a BundleAdjustmentConfig that locks every non-reference sensor's
    sensor_from_rig offset and every camera's intrinsics, leaving only each
    frame's 6-DoF rig_from_world free. Inlined from rigsfm_worker.py's
    identical helper (_rig_ba_config) -- this IS the actual mechanism that
    makes "rig-constrained BA" true: not a convention, a structural
    constant-parameter marking in the optimizer itself.
    """
    import pycolmap
    ba_config = pycolmap.BundleAdjustmentConfig()
    for img_id in recon.reg_image_ids():
        ba_config.add_image(img_id)
    for rig_id in recon.rigs:
        rig = recon.rig(rig_id)
        try:
            non_ref = rig.non_ref_sensors
        except AttributeError:
            non_ref = [s for s in rig.sensor_ids if not rig.is_ref_sensor(s)]
        for sensor_id in non_ref:
            ba_config.set_constant_sensor_from_rig_pose(sensor_id)
    for cam_id in recon.cameras:
        ba_config.set_constant_cam_intrinsics(cam_id)
    return ba_config


def _rig_snapshot(recon) -> dict:
    """{(rig_id, sensor_id): (quat_wxyz, translation)} for every non-ref
    sensor's sensor_from_rig transform. This is the actual hard safety
    boundary -- if any of these change, the rig itself moved, which must
    never happen regardless of what bundle adjustment does to frame poses."""
    snap = {}
    for rig_id in recon.rigs:
        rig = recon.rig(rig_id)
        try:
            non_ref = rig.non_ref_sensors
        except AttributeError:
            non_ref = [s for s in rig.sensor_ids if not rig.is_ref_sensor(s)]
        for sensor_id in non_ref:
            sfr = rig.sensor_from_rig(sensor_id)
            if sfr is None:
                continue  # shouldn't happen for a non-ref sensor, but don't crash if it does
            snap[(rig_id, sensor_id.id if hasattr(sensor_id, "id") else sensor_id)] = (
                np.array(sfr.rotation.quat, copy=True),
                np.array(sfr.translation, copy=True),
            )
    return snap


def _rig_fixed(recon, before: dict) -> tuple[bool, float, float]:
    """Compare current sensor_from_rig transforms against a prior snapshot.
    Returns (fixed, max_rot_delta, max_t_delta)."""
    max_rot = max_t = 0.0
    for rig_id in recon.rigs:
        rig = recon.rig(rig_id)
        try:
            non_ref = rig.non_ref_sensors
        except AttributeError:
            non_ref = [s for s in rig.sensor_ids if not rig.is_ref_sensor(s)]
        for sensor_id in non_ref:
            key = (rig_id, sensor_id.id if hasattr(sensor_id, "id") else sensor_id)
            if key not in before:
                continue
            sfr = rig.sensor_from_rig(sensor_id)
            if sfr is None:
                continue
            q0, t0 = before[key]
            max_rot = max(max_rot, float(np.linalg.norm(np.array(sfr.rotation.quat) - q0)))
            max_t = max(max_t, float(np.linalg.norm(np.array(sfr.translation) - t0)))
    return (max_rot < 1e-9 and max_t < 1e-9), max_rot, max_t


def _frame_pose_snapshot(recon) -> dict:
    """{frame_id: (quat_wxyz, translation)} for every posed frame's
    rig_from_world. Frame poses ARE allowed to move now (that's the whole
    point of adding BA) -- this is a diagnostic, not a safety gate."""
    snap = {}
    for frame_id, frame in recon.frames.items():
        if not frame.has_pose:
            continue
        rfw = frame.rig_from_world
        snap[frame_id] = (np.array(rfw.rotation.quat, copy=True),
                           np.array(rfw.translation, copy=True))
    return snap


def _max_frame_pose_delta(recon, before: dict) -> tuple[float, float]:
    max_rot = max_t = 0.0
    for frame_id, frame in recon.frames.items():
        if frame_id not in before or not frame.has_pose:
            continue
        rfw = frame.rig_from_world
        q0, t0 = before[frame_id]
        max_rot = max(max_rot, float(np.linalg.norm(np.array(rfw.rotation.quat) - q0)))
        max_t = max(max_t, float(np.linalg.norm(np.array(rfw.translation) - t0)))
    return max_rot, max_t


def _reprojection_stats(recon) -> dict:
    errors = []
    for pt in recon.points3D.values():
        for el in pt.track.elements:
            if el.image_id not in recon.images:
                continue
            img = recon.images[el.image_id]
            kp = img.points2D[el.point2D_idx]
            proj = img.project_point(pt.xyz)
            if proj is None:
                continue
            errors.append(float(np.linalg.norm(np.array(proj) - kp.xy)))
    if not errors:
        return {"n_observations": 0, "mean_reproj_px": None, "median_reproj_px": None, "pct_over_4px": None}
    arr = np.array(errors)
    return {
        "n_observations": len(arr),
        "mean_reproj_px": float(arr.mean()),
        "median_reproj_px": float(np.median(arr)),
        "pct_over_4px": float(100.0 * (arr > 4.0).mean()),
    }


def _filter_floaters(recon, max_error: float = 2.0, min_track_len: int = 3) -> int:
    """Drop points3D with reprojection error too high or too few observations
    to be trustworthy. Only removes points; never touches any pose.

    Deliberately does NOT use pt.error: that field is only ever populated by
    bundle adjustment, which this worker never runs (that's the whole safety
    property -- poses stay fixed). Confirmed empirically on a real job: every
    point's written ERROR field was -1 (COLMAP's "never computed" sentinel),
    meaning a pt.error > max_error check is a silent no-op -- it never drops
    a single point no matter how bad its actual fit is. Recomputes each
    point's own mean reprojection error directly instead, using the same
    projection method _reprojection_stats() uses for its report.
    """
    bad_ids = []
    for pt_id, pt in recon.points3D.items():
        if len(pt.track.elements) < min_track_len:
            bad_ids.append(pt_id)
            continue
        errs = []
        for el in pt.track.elements:
            if el.image_id not in recon.images:
                continue
            img = recon.images[el.image_id]
            kp = img.points2D[el.point2D_idx]
            proj = img.project_point(pt.xyz)
            if proj is None:
                continue
            errs.append(float(np.linalg.norm(np.array(proj) - kp.xy)))
        if not errs or (sum(errs) / len(errs)) > max_error:
            bad_ids.append(pt_id)
    for pt_id in bad_ids:
        recon.delete_point3D(pt_id)
    return len(bad_ids)


def _run_colmap_cli(colmap_bin: str, cmd: str, args: list) -> None:
    """Run a COLMAP CLI subcommand, forwarding output to stdout for the parent log.
    Inlined from rigsfm_worker.py -- worker subprocesses in this codebase don't
    import each other (see the sys.path/types.py shadowing fix above)."""
    import subprocess
    proc = subprocess.run(
        [colmap_bin, cmd] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.stdout:
        for line in proc.stdout.splitlines():
            print(f"  [colmap_cli] {line}", flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f"COLMAP {cmd} failed (exit {proc.returncode})")


def _generate_image_list(image_path: Path, n_sensors: int, list_file: Path) -> int:
    """
    Write a frame-major image list for COLMAP feature_extractor --image_list_path.

    Default extraction scans sensor folders alphabetically (sensor-major), which
    makes sequential_matcher useless for cross-sensor pairing. This list reorders
    to frame-major: all sensors of frame N, then all sensors of frame N+1, etc.
    With this insertion order, sequential_matcher with overlap >= n_sensors reaches
    every cross-sensor pair at the same capture station. Inlined from
    rigsfm_worker.py's identical helper -- same pano_cameraN/ layout.
    """
    sensor_images: dict[int, list[str]] = {}
    for i in range(n_sensors):
        folder = image_path / f"pano_camera{i}"
        if not folder.exists():
            continue
        imgs = sorted(
            f"pano_camera{i}/{p.name}"
            for p in folder.iterdir()
            if p.suffix.lower() in (".jpg", ".jpeg", ".png")
        )
        if imgs:
            sensor_images[i] = imgs

    if not sensor_images:
        return 0

    sensors  = sorted(sensor_images.keys())
    n_frames = max(len(v) for v in sensor_images.values())

    lines: list[str] = []
    for fi in range(n_frames):
        for si in sensors:
            if fi < len(sensor_images[si]):
                lines.append(sensor_images[si][fi])

    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def _purge_same_frame_pairs(db_path: Path) -> int:
    """
    Delete verified two-view geometries (and their raw matches) between
    sibling sensors of the same rig frame -- e.g. pano_camera0/IMG_..._167.jpg
    <-> pano_camera1/IMG_..._167.jpg. All sensors in a frame are rendered from
    the same panorama at the same position (zero baseline), so these pairs
    have no triangulation information; COLMAP's geometric verifier accepts
    them anyway, and triangulating one collapses to the shared camera center.
    Inlined from colmap_worker.py's identical helper -- worker subprocesses in
    this codebase don't import each other (see the sys.path/types.py shadowing
    fix above; each worker is a standalone script). Returns count purged.
    """
    import pycolmap
    n_purged = 0
    with pycolmap.Database.open(str(db_path)) as db:
        images = db.read_all_images()
        id_to_name = {img.image_id: img.name for img in images}
        pair_ids, _ = db.read_two_view_geometry_num_inliers()
        for pid in pair_ids:
            id1, id2 = pycolmap.pair_id_to_image_pair(pid)
            n1, n2 = id_to_name.get(id1), id_to_name.get(id2)
            if n1 is None or n2 is None:
                continue
            if Path(n1).stem == Path(n2).stem and Path(n1).parent.name != Path(n2).parent.name:
                db.delete_two_view_geometry(id1, id2)
                db.delete_matches(id1, id2)
                n_purged += 1
    return n_purged


def main() -> None:
    import pycolmap

    arg = sys.argv[1]
    if arg.startswith("@"):
        # 312 poses (R 3x3 + t) as inline JSON blows past Windows' CreateProcess
        # command-line length limit -- same fix already used by rigsfm_worker.py.
        import os
        path = arg[1:]
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        try:
            os.unlink(path)
        except OSError:
            pass
    else:
        payload = json.loads(arg)
    db_path = Path(payload["database_path"])
    image_path = Path(payload["image_path"])
    sparse_txt = Path(payload["sparse_txt_path"])
    poses = payload["poses"]  # {name: {"R": [[...]], "t": [...]}}
    abs_rots = payload["abs_rots"]  # list of 3x3 sensor_from_rig rotations, index 0 == anchor
    focal = float(payload["focal"])
    image_w = int(payload["image_w"])
    image_h = int(payload["image_h"])
    matcher = payload.get("matcher", "sequential")
    colmap_bin = payload.get("colmap_bin", "") or ""
    vocab_tree_path = payload.get("vocab_tree_path", "") or ""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    sparse_txt.mkdir(parents=True, exist_ok=True)

    cx, cy = image_w / 2.0, image_h / 2.0
    params_str = f"{focal},{cx},{cy}"

    n_sensors = len({name.split("/")[0] for name in poses if "/" in name})
    use_gpu = bool(colmap_bin) and Path(colmap_bin).exists()
    gpu_tag = " (GPU)" if use_gpu else ""

    # Resume support: extraction + matching across ~300 high-feature-count
    # images can take well over an hour, and everything downstream of it is
    # cheap by comparison. If a database from a prior (possibly crashed) run
    # already has verified two-view geometries, skip straight to seeding --
    # same resume convention equi_sfm_runner.py already uses for its own
    # recon step.
    already_matched = False
    if db_path.exists():
        with pycolmap.Database.open(str(db_path)) as _probe:
            pair_ids, _ = _probe.read_two_view_geometry_num_inliers()
            already_matched = len(pair_ids) > 0

    if already_matched:
        _prog(45, "EquiSfM glue: reusing existing database (prior extraction + matching found)")
    elif use_gpu:
        # ── 1. Feature extraction via CLI (CUDA-enabled colmap.exe) ────────
        # In-process pycolmap's SIFT is CPU-only in this environment (confirmed
        # empirically -- "Creating SIFT CPU feature extractor" even with device
        # left at its default). The standalone CLI binary has real CUDA SIFT,
        # same mechanism rigsfm_worker.py already relies on for speed.
        _prog(5, f"EquiSfM glue: extracting SIFT features{gpu_tag} on {len(poses)} sensor images…")
        if db_path.exists():
            db_path.unlink()
        img_list_file = db_path.with_suffix(".image_list.txt")
        n_listed = _generate_image_list(image_path, n_sensors, img_list_file)
        ext_args = [
            "--database_path", str(db_path),
            "--image_path", str(image_path),
            "--ImageReader.camera_model", "SIMPLE_PINHOLE",
            "--ImageReader.camera_params", params_str,
            "--ImageReader.single_camera_per_folder", "1",
            "--SiftExtraction.max_num_features", "16384",
        ]
        if n_listed > 0:
            ext_args += ["--image_list_path", str(img_list_file)]
            _prog(5, f"EquiSfM glue: image list: {n_listed} images in frame-major order")
        _run_colmap_cli(colmap_bin, "feature_extractor", ext_args)
        _prog(25, f"EquiSfM glue: feature extraction complete{gpu_tag}")

        # ── 2. Matching via CLI ─────────────────────────────────────────────
        # Frame-major DB insertion order (above) means "sequential" adjacency
        # actually spans sensors at nearby capture positions, not just frames
        # within one sensor folder -- overlap=n_sensors*3 covers same-frame
        # cross-sensor pairs plus ~2 neighboring frames. A vocab-tree pass first
        # (if configured) adds long-range retrieval on top; sequential still runs
        # after it to guarantee the physically-meaningful pairs aren't missed.
        _use_vocab_tree = bool(vocab_tree_path) and Path(vocab_tree_path).exists()
        seq_overlap = n_sensors * 3
        if _use_vocab_tree:
            _prog(30, f"EquiSfM glue: vocab_tree (20-NN) + sequential (overlap={seq_overlap}){gpu_tag}…")
            try:
                _run_colmap_cli(colmap_bin, "vocab_tree_matcher", [
                    "--database_path",                           str(db_path),
                    "--VocabTreeMatching.vocab_tree_path",       vocab_tree_path,
                    "--VocabTreeMatching.num_nearest_neighbors", "20",
                    "--SiftMatching.max_ratio",                  "0.85",
                    "--SiftMatching.max_distance",                "0.75",
                    "--FeatureMatching.guided_matching",         "1",
                    "--TwoViewGeometry.max_error",                "1.5",
                ])
            except RuntimeError as vt_err:
                _prog(31, f"EquiSfM glue: vocab tree matcher failed ({vt_err}) — continuing with sequential…")
            _run_colmap_cli(colmap_bin, "sequential_matcher", [
                "--database_path",                     str(db_path),
                "--SequentialMatching.overlap",        str(seq_overlap),
                "--SequentialMatching.loop_detection",  "0",
                "--FeatureMatching.guided_matching",   "1",
                "--TwoViewGeometry.max_error",          "1.5",
            ])
        elif matcher == "sequential":
            _prog(30, f"EquiSfM glue: sequential{gpu_tag} (overlap={seq_overlap}, {n_sensors} sensors × 3 frames)…")
            _run_colmap_cli(colmap_bin, "sequential_matcher", [
                "--database_path",                     str(db_path),
                "--SequentialMatching.overlap",        str(seq_overlap),
                "--SequentialMatching.loop_detection",  "0",
                "--FeatureMatching.guided_matching",   "1",
                "--TwoViewGeometry.max_error",          "1.5",
            ])
        else:
            _prog(30, f"EquiSfM glue: exhaustive{gpu_tag}…")
            _run_colmap_cli(colmap_bin, "exhaustive_matcher", [
                "--database_path",                   str(db_path),
                "--FeatureMatching.guided_matching", "1",
                "--TwoViewGeometry.max_error",        "1.5",
            ])
        _prog(45, f"EquiSfM glue: matching complete{gpu_tag}")
    else:
        # ── CPU fallback: in-process pycolmap (no colmap_bin configured) ────
        _prog(5, f"EquiSfM glue: extracting SIFT features on {len(poses)} sensor images…")
        if db_path.exists():
            db_path.unlink()
        pycolmap.extract_features(
            database_path=str(db_path),
            image_path=str(image_path),
            camera_mode=pycolmap.CameraMode.PER_FOLDER,
            reader_options=pycolmap.ImageReaderOptions(
                camera_model="SIMPLE_PINHOLE",
                camera_params=params_str,
            ),
            extraction_options=pycolmap.FeatureExtractionOptions(
                sift=pycolmap.SiftExtractionOptions(max_num_features=16384),
            ),
        )
        _prog(25, "EquiSfM glue: feature extraction complete")

        _prog(30, f"EquiSfM glue: {matcher} matching…")
        mo = pycolmap.FeatureMatchingOptions()
        mo.guided_matching = True
        if matcher == "exhaustive":
            pycolmap.match_exhaustive(str(db_path), matching_options=mo)
        else:
            pycolmap.match_sequential(str(db_path), matching_options=mo)
        _prog(45, "EquiSfM glue: matching complete")

    # ── 3. Purge same-rig-frame sibling pairs (idempotent -- always run, even
    #        on a resumed database, in case a prior crash happened before this
    #        step completed) ────────────────────────────────────────────────
    n_purged = _purge_same_frame_pairs(db_path)
    if n_purged:
        _prog(46, f"EquiSfM glue: purged {n_purged} zero-baseline same-frame pairs")

    # ── 4. Seed a PLAIN (no rig files at all) Reconstruction with the fixed,
    #        already-trusted poses. IMAGE_ID must equal the database's own
    #        image_id -- confirmed via spike that a mismatch here crashes
    #        IncrementalTriangulator's constructor outright. ─────────────────
    _prog(48, "EquiSfM glue: seeding reconstruction with fixed poses…")
    db = pycolmap.Database.open(str(db_path))
    db_images = {im.name: im for im in db.read_all_images()}
    db_cameras = {c.camera_id: c for c in db.read_all_cameras()}

    missing = [n for n in poses if n not in db_images]
    if missing:
        raise RuntimeError(
            f"{len(missing)} pose(s) have no matching extracted image "
            f"(first few: {missing[:5]}) -- image_path/poses name mismatch"
        )

    seed_dir = sparse_txt.parent / "_seed_txt"
    seed_dir.mkdir(parents=True, exist_ok=True)

    cam_lines = ["# CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n"]
    for cam_id, cam in db_cameras.items():
        p = cam.params
        cam_lines.append(f"{cam_id} SIMPLE_PINHOLE {cam.width} {cam.height} "
                          f"{p[0]:.6f} {p[1]:.6f} {p[2]:.6f}\n")
    (seed_dir / "cameras.txt").write_text("".join(cam_lines), encoding="utf-8")

    img_lines = ["# IMAGE_ID, QW,QX,QY,QZ, TX,TY,TZ, CAMERA_ID, NAME\n#   POINTS2D[] (left empty; populated in-memory below)\n"]
    for name, pose in poses.items():
        dbimg = db_images[name]
        R = np.array(pose["R"], dtype=np.float64)
        t = np.array(pose["t"], dtype=np.float64)
        qw, qx, qy, qz = _mat_to_quat(R)
        img_lines.append(
            f"{dbimg.image_id} {qw:.10f} {qx:.10f} {qy:.10f} {qz:.10f} "
            f"{t[0]:.10f} {t[1]:.10f} {t[2]:.10f} {dbimg.camera_id} {name}\n"
        )
        img_lines.append("\n")
    (seed_dir / "images.txt").write_text("".join(img_lines), encoding="utf-8")
    (seed_dir / "points3D.txt").write_text("", encoding="utf-8")

    recon = pycolmap.Reconstruction()
    recon.read_text(str(seed_dir))
    if recon.num_reg_images() != len(poses):
        raise RuntimeError(
            f"Expected {len(poses)} registered images after seeding, got {recon.num_reg_images()}"
        )

    pose_before = _pose_snapshot(recon)

    # ── 4b. Layer real rig/frame structure onto the seeded reconstruction ──
    # Needed so BA (step 8) has an actual rig to constrain -- the flat seed
    # above has no rig concept at all. apply_rig_config groups images into
    # frames by their pano_cameraN/ prefix; anchor sensor (index 0) is the
    # rig reference, so rig_from_world == the anchor image's own cam_from_world.
    # Inlined from rigsfm_worker.py's identical step 4b.
    rig_config = _build_rig_config(abs_rots, anchor_idx=0)
    for rc in rig_config.cameras:
        if not any(name.startswith(rc.image_prefix) for name in db_images):
            raise RuntimeError(
                f"apply_rig_config precondition failed: no image starts with "
                f"{rc.image_prefix!r} -- this would abort inside pycolmap's C++ layer."
            )
    pycolmap.apply_rig_config([rig_config], db, recon)

    anchor_dir = "pano_camera0"
    frame_posed = set()
    for img_id, img in recon.images.items():
        if img.frame_id in frame_posed:
            continue
        if Path(img.name).parent.name != anchor_dir:
            continue
        frame = recon.frames[img.frame_id]
        cfw = img.cam_from_world()
        frame.rig_from_world = pycolmap.Rigid3d(
            pycolmap.Rotation3d(np.array(cfw.rotation.matrix(), dtype=np.float64)),
            np.array(cfw.translation, dtype=np.float64).flatten(),
        )
        recon.register_frame(img.frame_id)
        frame_posed.add(img.frame_id)
    n_has_pose = sum(1 for f in recon.frames.values() if f.has_pose)
    _prog(51, f"EquiSfM glue: rig structure applied — {len(frame_posed)}/{len(recon.frames)} "
              f"frames posed (has_pose={n_has_pose}/{len(recon.frames)})")

    rig_before = _rig_snapshot(recon)
    frame_pose_before = _frame_pose_snapshot(recon)

    # ── 5. Populate Image.points2D from the database's own keypoints ───────
    # Required: IncrementalTriangulator needs each image's keypoints in memory,
    # index-aligned with the database's keypoint row order (matches reference
    # these rows positionally) -- an empty-POINTS2D seed has none.
    name_to_reconid = {img.name: img_id for img_id, img in recon.images.items()}
    for name, dbimg in db_images.items():
        if name not in name_to_reconid:
            continue  # image extracted but has no known pose -- shouldn't happen, skip defensively
        kps = db.read_keypoints(dbimg.image_id)
        pts2d = pycolmap.Point2DList()
        for kp in kps:
            pts2d.append(pycolmap.Point2D(xy=np.array([kp[0], kp[1]], dtype=np.float64)))
        recon.images[name_to_reconid[name]].points2D = pts2d

    # ── 6. Build the CorrespondenceGraph ────────────────────────────────────
    _prog(52, "EquiSfM glue: building correspondence graph…")
    graph = pycolmap.CorrespondenceGraph()
    for name, dbimg in db_images.items():
        if name not in name_to_reconid:
            continue
        n_pts = len(recon.images[name_to_reconid[name]].points2D)
        graph.add_image(dbimg.image_id, n_pts)

    posed_db_ids = {db_images[n].image_id for n in name_to_reconid}
    pair_ids, geoms = db.read_two_view_geometries()
    n_pairs = 0
    for pid, geom in zip(pair_ids, geoms):
        id1, id2 = pycolmap.pair_id_to_image_pair(pid)
        if id1 not in posed_db_ids or id2 not in posed_db_ids:
            continue  # one side has no known pose (e.g. an image with no matching entry in `poses`)
        graph.add_two_view_geometry(id1, id2, geom)
        n_pairs += 1
    graph.finalize()
    _prog(55, f"EquiSfM glue: {n_pairs} verified pairs in graph")

    # ── 7. Triangulate -- fixed poses at this point, no BA yet ──────────────
    _prog(58, "EquiSfM glue: triangulating…")
    tri_opts = pycolmap.IncrementalTriangulatorOptions()
    tri_opts.min_angle = 1.5
    tri_opts.create_max_angle_error = 2.0
    tri_opts.complete_max_reproj_error = 4.0
    # Default (True) matches COLMAP's own incremental pipeline default, favoring
    # quality over density -- with the real per-job image count (many sensors x
    # many frames), enough true multi-view overlap should exist for this not to
    # starve density the way it did in a narrow single-sensor spike test. If a
    # real job comes back too sparse, this is the first knob to reconsider --
    # not a settings toggle yet, deliberately, until real jobs justify one.
    tri_opts.ignore_two_view_tracks = True

    triangulator = pycolmap.IncrementalTriangulator(graph, recon)
    for name in name_to_reconid:
        triangulator.triangulate_image(tri_opts, name_to_reconid[name])
    triangulator.complete_all_tracks(tri_opts)
    triangulator.merge_all_tracks(tri_opts)
    _prog(66, f"EquiSfM glue: {len(recon.points3D)} points3D before floater filtering")

    n_dropped = _filter_floaters(recon)
    _prog(70, f"EquiSfM glue: dropped {n_dropped} floater points, {len(recon.points3D)} remain")

    # ── 8. Rig-constrained GPU bundle adjustment ────────────────────────────
    # This is the actual hard safety boundary: set_constant_sensor_from_rig_pose
    # (inside _rig_ba_config) marks every non-ref sensor's offset as a constant
    # parameter in the optimizer itself -- structurally, not by convention --
    # so the rig cannot move no matter what BA does to frame poses or points.
    # Frame poses (rig_from_world) and points3D ARE refined; that's the point.
    ba_ok = False
    try:
        ba_opts = pycolmap.BundleAdjustmentOptions()
        ba_opts.refine_focal_length = False
        ba_opts.refine_principal_point = False
        ba_opts.refine_extra_params = False
        ba_opts.ceres.use_gpu = True
        _prog(72, "EquiSfM glue: rig-constrained bundle adjustment (GPU)…")
        pycolmap.create_default_bundle_adjuster(
            ba_opts, _rig_ba_config(recon), recon
        ).solve()
        ba_ok = True
        _prog(80, f"EquiSfM glue: BA complete — {len(recon.points3D)} points3D")
    except Exception as ba_err:
        _prog(80, f"EquiSfM glue: rig-constrained BA failed ({ba_err}) — "
                  f"keeping pre-BA (fixed-pose) result instead")

    if ba_ok:
        n_dropped_post_ba = _filter_floaters(recon)
        _prog(83, f"EquiSfM glue: post-BA filter dropped {n_dropped_post_ba} more points, "
                  f"{len(recon.points3D)} remain")
        n_dropped += n_dropped_post_ba

    # ── 9. Mandatory verification: the RIG must not have moved, regardless of
    #        what BA did to frame poses. This is the hard gate; frame-pose
    #        movement is reported as a diagnostic only. ────────────────────
    rig_fixed, rig_max_rot, rig_max_t = _rig_fixed(recon, rig_before)
    _prog(85, f"EquiSfM glue: rig-fixed check — rot={rig_max_rot:.2e} t={rig_max_t:.2e} "
              f"(fixed={rig_fixed})")

    frame_max_rot, frame_max_t = _max_frame_pose_delta(recon, frame_pose_before)
    _prog(86, f"EquiSfM glue: frame-pose movement from BA — "
              f"rot={frame_max_rot:.2e} t={frame_max_t:.2e} (expected/allowed)")

    reproj = _reprojection_stats(recon)
    _prog(88, f"EquiSfM glue: reprojection — mean={reproj['mean_reproj_px']} "
              f"median={reproj['median_reproj_px']} pct_over_4px={reproj['pct_over_4px']}")

    # ── 10. Write output, then verify the round trip didn't move the rig ───
    recon.write_text(str(sparse_txt))
    recon_check = pycolmap.Reconstruction()
    recon_check.read_text(str(sparse_txt))
    rig_fixed_rt, rig_rt_rot, rig_rt_t = _rig_fixed(recon_check, rig_before)
    frame_rt_rot, frame_rt_t = _max_frame_pose_delta(recon_check, frame_pose_before)
    _prog(95, f"EquiSfM glue: round-trip — rig rot={rig_rt_rot:.2e} t={rig_rt_t:.2e} "
              f"(fixed={rig_fixed_rt}), frame rot={frame_rt_rot:.2e} t={frame_rt_t:.2e}")

    overall_rig_fixed = rig_fixed and rig_fixed_rt

    result = {
        "success": True,
        "rig_fixed": overall_rig_fixed,
        "ba_ran": ba_ok,
        "max_rig_rot_drift": rig_max_rot,
        "max_rig_t_drift": rig_max_t,
        "max_rig_roundtrip_rot_drift": rig_rt_rot,
        "max_rig_roundtrip_t_drift": rig_rt_t,
        "max_frame_pose_rot_delta": frame_max_rot,
        "max_frame_pose_t_delta": frame_max_t,
        "images": len(recon.images),
        "points3D": len(recon.points3D),
        "purged_same_frame_pairs": n_purged,
        "dropped_floaters": n_dropped,
        **{f"reproj_{k}": v for k, v in reproj.items()},
    }
    _prog(100, f"EquiSfM glue complete — {len(recon.points3D)} points, "
               f"rig_fixed={overall_rig_fixed}, ba_ran={ba_ok}")
    print(json.dumps(result), flush=True)


def _mat_to_quat(R: np.ndarray) -> tuple:
    """3x3 rotation matrix -> quaternion (w, x, y, z). Shepperd's method."""
    trace = R[0, 0] + R[1, 1] + R[2, 2]
    if trace > 0:
        s = 0.5 / np.sqrt(trace + 1.0)
        qw = 0.25 / s
        qx = (R[2, 1] - R[1, 2]) * s
        qy = (R[0, 2] - R[2, 0]) * s
        qz = (R[1, 0] - R[0, 1]) * s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        qw = (R[2, 1] - R[1, 2]) / s
        qx = 0.25 * s
        qy = (R[0, 1] + R[1, 0]) / s
        qz = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        qw = (R[0, 2] - R[2, 0]) / s
        qx = (R[0, 1] + R[1, 0]) / s
        qy = 0.25 * s
        qz = (R[1, 2] + R[2, 1]) / s
    else:
        s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        qw = (R[1, 0] - R[0, 1]) / s
        qx = (R[0, 2] + R[2, 0]) / s
        qy = (R[1, 2] + R[2, 1]) / s
        qz = 0.25 * s
    return float(qw), float(qx), float(qy), float(qz)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }), flush=True)
        sys.exit(1)
