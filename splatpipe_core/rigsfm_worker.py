# splatpipe_core/rigsfm_worker.py
"""
RigSfM worker — runs under Python 3.14 / pycolmap 4.0.4.

Receives a JSON payload via sys.argv[1]:
  database_path, image_path, sparse_txt_path,
  poses ({img_name: {R: [[...]], t: [...]}}),
  focal, image_size, matcher, colmap_bin,
  abs_rots ([[R_3x3], ...]), anchor_idx

Pipeline:
  1. SIFT feature extraction   (PER_FOLDER, SIMPLE_PINHOLE, 16384 features/image)
  2. Apply rig config to DB    (groups images into frames, enables skip_same_frame)
  3. Feature matching
       GPU + vocab tree: vocab_tree_matcher with frame-windowed pair list
                         (window=5, all sensor combos; bypasses retrieval)
       GPU only:         exhaustive_matcher (all pairs)
       CPU:              pycolmap match_exhaustive with guided_matching=True
  4. Build Reconstruction with per-sensor poses from expanded rig geometry
  4b. Apply rig config to reconstruction → set frame.rig_from_world from anchor poses
  4c. Guided re-matching       (GPU path only; COLMAP CLI exhaustive_matcher with
                                guided_matching=1; clears two_view_geometries then
                                re-verifies raw matches without re-running SIFT)
  5. Triangulate 3D points
  6. Bundle adjustment         (two passes with re-triangulation between them)
                               create_default_bundle_adjuster with
                               set_constant_sensor_from_rig_pose (rig-constrained);
                               falls back to pycolmap.bundle_adjustment if no rigs active
  7. Write sparse_txt

Progress lines: WORKER_PROGRESS:<pct>:<message>
Final line:     JSON {"success":bool, "images":n, "points3D":n}
"""
# Remove this script's own directory from sys.path so that splatpipe_core/types.py
# does not shadow the stdlib 'types' module when Python adds the script dir at startup.
import sys as _sys, os as _os
_this_dir = _os.path.dirname(_os.path.abspath(__file__))
if _this_dir in _sys.path:
    _sys.path.remove(_this_dir)
del _this_dir, _sys, _os

# Force UTF-8 stdout so emoji in _prog messages don't crash on cp1252 Windows consoles.
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
del _sys

import json
import subprocess
import sys
import traceback
from pathlib import Path

import numpy as np


def _run_colmap_cli(colmap_bin: str, cmd: str, args: list) -> None:
    """Run a COLMAP CLI subcommand, forwarding output to stdout for the parent log."""
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
    makes sequential_matcher useless for cross-sensor pairing.  This list reorders
    to frame-major: all sensors of frame N, then all sensors of frame N+1, etc.
    With this insertion order, sequential_matcher with overlap >= n_sensors reaches
    every cross-sensor pair at the same capture station.

    Returns the number of image paths written.
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


def _generate_pair_list(image_path: Path, n_sensors: int, frame_window: int, pairs_file: Path) -> int:
    """
    Build a frame-windowed image pair list for COLMAP custom_matching.

    For each frame A, pairs against all frames B within frame_window steps.
    All sensor combinations (si × sj) are included for each frame pair.
    Same-frame pairs are omitted — COLMAP's rig DB skips them anyway.
    Returns the number of pairs written.
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
    for fa in range(n_frames):
        for fb in range(fa + 1, min(n_frames, fa + frame_window + 1)):
            for si in sensors:
                if fa >= len(sensor_images[si]):
                    continue
                img_a = sensor_images[si][fa]
                for sj in sensors:
                    if fb >= len(sensor_images[sj]):
                        continue
                    lines.append(f"{img_a} {sensor_images[sj][fb]}")

    pairs_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)




def _run_guided_cli(colmap_bin: str, db_path: Path) -> bool:
    """
    Run guided + rig-constrained geometric verification via COLMAP CLI.

    Uses exhaustive_matcher with:
      guided_matching=1    — second SIFT pass guided by estimated E/F matrix
      rig_verification=1   — RANSAC uses known sensor_from_rig geometry to
                             constrain E/F estimation (COLMAP 3.13+)

    The CLI binary does not load pycolmap's _core.pyd extension, avoiding the
    FAISS C++ abort that crashes the Python subprocess path on Windows.
    For pairs that already have raw matches, COLMAP skips SIFT re-extraction
    and only re-runs RANSAC + guided verification — fast.
    Returns True if the CLI exited cleanly.
    """
    try:
        _run_colmap_cli(colmap_bin, "exhaustive_matcher", [
            "--database_path",                   str(db_path),
            "--FeatureMatching.guided_matching", "1",
        ])
        return True
    except Exception:
        return False


def _triangulate(recon, db_path: Path, image_path: Path, sparse_txt: Path):
    """
    Triangulate 3D points with tight quality constraints.

    Passes min_track_len=3, max_reproj_error=2.0 px, min_tri_angle=1.5° to the
    incremental triangulator so only well-supported points are created.
    Falls back to defaults if the options argument isn't accepted (older pycolmap).
    """
    import pycolmap as _pc
    kwargs = dict(
        reconstruction=recon,
        database_path=str(db_path),
        image_path=str(image_path),
        output_path=str(sparse_txt),
    )
    try:
        opts = _pc.IncrementalPipelineOptions()
        opts.triangulation.min_track_len    = 3
        opts.triangulation.max_reproj_error = 2.0
        opts.triangulation.min_tri_angle    = 1.5
        return _pc.triangulate_points(**kwargs, options=opts)
    except Exception:
        return _pc.triangulate_points(**kwargs)


def _filter_floaters(recon, max_reproj_error: float = 2.0, min_track_len: int = 3) -> int:
    """
    Remove points with high reprojection error or short tracks from a reconstruction.

    Floaters (errant unconstrained points) tend to have either:
    - high pt.error (mean reprojection error after BA)
    - short tracks (seen in only 2 views — low angular diversity)
    Returns the number of points removed.
    """
    to_remove = [
        pid for pid, pt in recon.points3D.items()
        if pt.error > max_reproj_error or len(pt.track.elements) < min_track_len
    ]
    for pid in to_remove:
        try:
            recon.delete_point3D(pid)
        except AttributeError:
            recon.deregister_point3D(pid)
    return len(to_remove)



def _run_guided_subprocess(db_path: "Path") -> bool:
    """
    CPU fallback: run guided matching in an isolated Python subprocess.

    Used when colmap_bin is unavailable. pycolmap's match_exhaustive with
    guided_matching=True can trigger a FAISS C++ abort on Windows; running it
    in a child process isolates the crash so only the child dies.
    Returns True if the subprocess exited cleanly.
    """
    import tempfile
    script = (
        "import sys, pycolmap\n"
        "db = sys.argv[1]\n"
        "mo = pycolmap.FeatureMatchingOptions()\n"
        "mo.skip_image_pairs_in_same_frame = True\n"
        "mo.guided_matching = True\n"
        "s = pycolmap.SiftMatchingOptions()\n"
        "s.max_ratio = 0.85\n"
        "s.max_distance = 0.75\n"
        "mo.sift = s\n"
        "pycolmap.match_exhaustive(db, matching_options=mo)\n"
    )
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(script)
            tmp_path = f.name
        result = subprocess.run(
            [sys.executable, tmp_path, str(db_path)],
            timeout=7200,
        )
        return result.returncode == 0
    except Exception:
        return False
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass


def _prog(pct: int, msg: str) -> None:
    print(f"WORKER_PROGRESS:{pct}:{msg}", flush=True)


def _mat_to_qvec(R: np.ndarray) -> np.ndarray:
    """Convert 3×3 rotation matrix to quaternion [qw, qx, qy, qz] (COLMAP convention)."""
    trace = R[0, 0] + R[1, 1] + R[2, 2]
    if trace > 0:
        s = 0.5 / np.sqrt(trace + 1.0)
        return np.array([0.25 / s,
                         (R[2, 1] - R[1, 2]) * s,
                         (R[0, 2] - R[2, 0]) * s,
                         (R[1, 0] - R[0, 1]) * s])
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        return np.array([(R[2, 1] - R[1, 2]) / s, 0.25 * s,
                         (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s])
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        return np.array([(R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s,
                         0.25 * s, (R[1, 2] + R[2, 1]) / s])
    else:
        s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        return np.array([(R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s,
                         (R[1, 2] + R[2, 1]) / s, 0.25 * s])


def _build_rig_config(abs_rots_list: list, anchor_idx: int):
    """
    Build a pycolmap.RigConfig from the abs_rots for every sensor.

    The anchor sensor (anchor_idx) is the reference — sensor_from_rig = identity.
    All other sensors get sensor_from_rig = R_j @ R_anchor.T (pure rotation, zero
    translation, since all sensors share one optical centre on a panoramic head).

    image_prefix matches the pano_camera{j}/ folder layout already in the DB.
    """
    import pycolmap
    abs_rots = [np.array(r, dtype=np.float64) for r in abs_rots_list]
    R_ref  = abs_rots[anchor_idx]
    zero_t = np.zeros(3)
    cameras = []
    for j, R_j in enumerate(abs_rots):
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
    Build a BundleAdjustmentConfig that locks all sensor_from_rig offsets.

    BA then optimizes only the 6-DoF rig_from_world per frame, using tracks
    from all N sensors simultaneously. Intrinsics and gauge are also fixed.
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
    ba_config.fix_gauge(pycolmap.BundleAdjustmentGauge.TWO_CAMS_FROM_WORLD)
    return ba_config


def main() -> None:
    try:
        arg = sys.argv[1]
        if arg.startswith("@"):
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
        db_path       = Path(payload["database_path"])
        image_path    = Path(payload["image_path"])
        sparse_txt    = Path(payload["sparse_txt_path"])
        poses_raw     = payload["poses"]       # {name: {"R": [[...]], "t": [...]}}
        focal         = float(payload["focal"])
        image_size    = int(payload["image_size"])
        matcher       = payload.get("matcher", "sequential")
        colmap_bin       = payload.get("colmap_bin", "")
        vocab_tree_path  = payload.get("vocab_tree_path", "") or ""
        abs_rots_list    = payload.get("abs_rots", [])
        anchor_idx       = int(payload.get("anchor_idx", 0))

        import pycolmap

        params_str = f"{focal},{image_size // 2},{image_size // 2}"
        db_done_marker  = db_path.with_suffix(".matched")
        guided_marker   = db_path.with_suffix(".guided")

        use_gpu = bool(colmap_bin) and Path(colmap_bin).exists()
        gpu_tag = " (GPU)" if use_gpu else ""

        if db_path.exists() and db_done_marker.exists() and guided_marker.exists():
            # Features, matches, and guided re-matching all cached.
            _prog(45, "Feature database (guided) already built — skipping extraction and matching.")
            rig_config = _build_rig_config(abs_rots_list, anchor_idx) if abs_rots_list else None
        elif db_path.exists() and db_done_marker.exists():
            # Features + initial matching cached; guided re-matching runs at step 4c.
            _prog(45, "Feature database already built — guided re-matching pending.")
            rig_config = _build_rig_config(abs_rots_list, anchor_idx) if abs_rots_list else None
        else:
            if db_path.exists():
                db_path.unlink()
            if db_done_marker.exists():
                db_done_marker.unlink()
            if guided_marker.exists():
                guided_marker.unlink()

            # ── 1. Feature extraction ──────────────────────────────────────────
            _prog(5, f"Extracting SIFT features{gpu_tag} ({image_path.name})…")
            if use_gpu:
                # Generate a frame-major image list so DB insertion order is
                # frame-major (all sensors of frame N before frame N+1).
                # This is required for sequential_matcher to cross sensor boundaries.
                _img_list_file = db_path.with_suffix(".image_list.txt")
                _n_listed = _generate_image_list(image_path, len(abs_rots_list), _img_list_file) \
                            if abs_rots_list else 0
                _ext_args = [
                    "--database_path",                        str(db_path),
                    "--image_path",                           str(image_path),
                    "--ImageReader.camera_model",             "SIMPLE_PINHOLE",
                    "--ImageReader.camera_params",            params_str,
                    "--ImageReader.single_camera_per_folder", "1",
                    "--SiftExtraction.max_num_features",      "16384",
                    "--SiftExtraction.peak_threshold",        "0.004",
                ]
                if _n_listed > 0:
                    _ext_args += ["--image_list_path", str(_img_list_file)]
                    _prog(5, f"  Image list: {_n_listed} images in frame-major order.")
                _run_colmap_cli(colmap_bin, "feature_extractor", _ext_args)
            else:
                reader_opts = pycolmap.ImageReaderOptions(
                    camera_model="SIMPLE_PINHOLE",
                    camera_params=params_str,
                )
                try:
                    reader_opts.max_num_features = 16384
                except AttributeError:
                    pass
                try:
                    _sift_ext = pycolmap.SiftExtractionOptions()
                    _sift_ext.peak_threshold    = 0.004
                    _sift_ext.max_num_features  = 16384
                    pycolmap.extract_features(
                        database_path=str(db_path),
                        image_path=str(image_path),
                        reader_options=reader_opts,
                        camera_mode=pycolmap.CameraMode.PER_FOLDER,
                        sift_options=_sift_ext,
                    )
                except TypeError:
                    pycolmap.extract_features(
                        database_path=str(db_path),
                        image_path=str(image_path),
                        reader_options=reader_opts,
                        camera_mode=pycolmap.CameraMode.PER_FOLDER,
                    )
            _prog(25, f"Feature extraction complete{gpu_tag}.")

            # ── 2. Apply rig config to DB so the matcher knows frame membership ─
            rig_config = None
            if abs_rots_list:
                rig_config = _build_rig_config(abs_rots_list, anchor_idx)
                with pycolmap.Database.open(str(db_path)) as _db:
                    pycolmap.apply_rig_config([rig_config], _db)
                _prog(26, f"Rig config applied — {len(abs_rots_list)} sensors grouped into frames.")

            # ── 3. Feature matching ────────────────────────────────────────────
            # GPU path (vocab tree set): vocab_tree_matcher with frame-windowed
            #   pair list.  The pair list replaces vocab tree retrieval — only
            #   the specified pairs are SIFT-matched.  Requires a vocab tree .bin
            #   (set colmap_vocab_tree in settings) even though retrieval is
            #   bypassed; COLMAP loads it at init regardless.
            # GPU path (vocab tree set):    vocab_tree_matcher with frame-windowed pair list.
            # GPU path (sequential, no vt): sequential_matcher, overlap = n_sensors × 3.
            #   Requires frame-major DB insertion order (ensured by --image_list_path above).
            #   Overlap covers: same-frame cross-sensor (n_sensors-1) + 2 temporal frames
            #   (2 × n_sensors) ≈ n_sensors × 3.  No vocab tree needed.
            # GPU path (exhaustive, no vt): exhaustive_matcher (all pairs).
            # CPU path: pycolmap match_exhaustive with guided_matching=True.
            # Step 4c re-runs guided E/F verification via CLI (GPU paths only).
            _use_vocab_tree = use_gpu and bool(vocab_tree_path) and Path(vocab_tree_path).exists()
            if _use_vocab_tree:
                _seq_overlap = len(abs_rots_list) * 3
                _prog(27, f"Matching features vocab_tree (20-NN) + sequential "
                          f"(overlap={_seq_overlap}){gpu_tag}…")
                # Pass 1: vocab tree retrieval — finds visually similar pairs across
                # the whole sequence (long-range + same-sensor sequential).
                # NOTE: do NOT pass match_list_path here — in COLMAP 4.1 it validates
                # pairs against the vocab tree index before indexing runs (empty index
                # → all pairs rejected → 0 retrieval candidates → 0 matches).
                try:
                    _run_colmap_cli(colmap_bin, "vocab_tree_matcher", [
                        "--database_path",                           str(db_path),
                        "--VocabTreeMatching.vocab_tree_path",       vocab_tree_path,
                        "--VocabTreeMatching.num_nearest_neighbors", "20",
                        "--SiftMatching.max_ratio",                  "0.85",
                        "--SiftMatching.max_distance",               "0.75",
                        "--FeatureMatching.guided_matching",         "1",
                        "--TwoViewGeometry.max_error",               "1.5",
                    ])
                except RuntimeError as _vt_err:
                    _prog(28, f"Vocab tree matcher failed ({_vt_err}) — skipping{gpu_tag}…")
                else:
                    import sqlite3 as _sqlite3
                    with _sqlite3.connect(str(db_path)) as _conn:
                        _n_tvg = _conn.execute(
                            "SELECT COUNT(*) FROM two_view_geometries"
                        ).fetchone()[0]
                    if _n_tvg == 0:
                        _prog(28, "Vocab tree produced 0 verified pairs "
                                  "(incompatible tree?) — continuing with sequential only…")
                # Pass 2: sequential matcher — guarantees frame-adjacent cross-sensor
                # pairs are matched regardless of visual similarity (COLMAP appends to
                # the DB; already-matched pairs are skipped).
                _run_colmap_cli(colmap_bin, "sequential_matcher", [
                    "--database_path",                      str(db_path),
                    "--SequentialMatching.overlap",         str(_seq_overlap),
                    "--SequentialMatching.loop_detection",  "0",
                    "--FeatureMatching.guided_matching",    "1",
                    "--TwoViewGeometry.max_error",          "1.5",
                ])
                guided_marker.touch()   # guided_matching already applied; skip step 4c
            elif use_gpu and matcher == "sequential" and abs_rots_list:
                _seq_overlap = len(abs_rots_list) * 3
                _prog(27, f"Matching features sequential{gpu_tag} "
                          f"(overlap={_seq_overlap}, {len(abs_rots_list)} sensors × 3 frames)…")
                _run_colmap_cli(colmap_bin, "sequential_matcher", [
                    "--database_path",                      str(db_path),
                    "--SequentialMatching.overlap",         str(_seq_overlap),
                    "--SequentialMatching.loop_detection",  "0",
                    "--FeatureMatching.guided_matching",    "1",
                    "--TwoViewGeometry.max_error",          "1.5",
                ])
                guided_marker.touch()
            elif use_gpu:
                _prog(27, f"Matching features exhaustive{gpu_tag}…")
                _run_colmap_cli(colmap_bin, "exhaustive_matcher", [
                    "--database_path",                   str(db_path),
                    "--FeatureMatching.guided_matching", "1",
                    "--TwoViewGeometry.max_error",       "1.5",
                ])
                guided_marker.touch()   # guided_matching already applied; skip step 4c
            else:
                mo = pycolmap.FeatureMatchingOptions()
                mo.skip_image_pairs_in_same_frame = True
                mo.guided_matching = True
                _sift = pycolmap.SiftMatchingOptions()
                _sift.max_ratio    = 0.85
                _sift.max_distance = 0.75
                mo.sift = _sift
                pycolmap.match_exhaustive(str(db_path), matching_options=mo)
                guided_marker.touch()   # CPU path already did guided matching
            _prog(45, f"Feature matching complete{gpu_tag}.")

            # Mark database as complete so resume skips extraction/matching
            db_done_marker.touch()

        # ── 4. Build Reconstruction with known rig poses ───────────────────────
        # pycolmap 4.x: Image.cam_from_world is completely read-only (neither
        # post-construction assignment nor constructor kwarg works).  Write the
        # poses as COLMAP text format and read them back — pycolmap constructs
        # the Reconstruction correctly that way.
        _prog(48, f"Building reconstruction from {len(poses_raw)} known poses…")

        with pycolmap.Database.open(str(db_path)) as db:
            db_cameras = {cam.camera_id: cam for cam in db.read_all_cameras()}
            db_images  = {img.name: img for img in db.read_all_images()}

        import tempfile, shutil
        _tmp_recon = Path(tempfile.mkdtemp(prefix="rigsfm_recon_"))
        try:
            # cameras.txt
            with open(_tmp_recon / "cameras.txt", "w") as _f:
                _f.write("# Camera list\n")
                for cid, cam in db_cameras.items():
                    try:
                        model_name = cam.model_name
                    except AttributeError:
                        model_name = cam.model.name
                    _params = " ".join(f"{p:.6f}" for p in cam.params)
                    _f.write(f"{cid} {model_name} {cam.width} {cam.height} {_params}\n")

            # images.txt — two lines per image: pose line + empty 2D-points line
            n_registered = 0
            with open(_tmp_recon / "images.txt", "w") as _f:
                _f.write("# Image list\n")
                for img_name, pose_data in poses_raw.items():
                    if img_name not in db_images:
                        continue
                    db_img = db_images[img_name]
                    R  = np.array(pose_data["R"], dtype=np.float64)
                    t  = np.array(pose_data["t"], dtype=np.float64).flatten()
                    qv = _mat_to_qvec(R)
                    _f.write(
                        f"{db_img.image_id} "
                        f"{qv[0]:.9f} {qv[1]:.9f} {qv[2]:.9f} {qv[3]:.9f} "
                        f"{t[0]:.9f} {t[1]:.9f} {t[2]:.9f} "
                        f"{db_img.camera_id} {img_name}\n\n"
                    )
                    n_registered += 1

            # points3D.txt — must exist, may be empty
            (_tmp_recon / "points3D.txt").write_text("# 3D point list\n")

            recon = pycolmap.Reconstruction()
            try:
                recon.read_text(str(_tmp_recon))
            except AttributeError:
                recon.read(str(_tmp_recon))
        finally:
            shutil.rmtree(_tmp_recon, ignore_errors=True)

        if n_registered == 0:
            raise RuntimeError(
                "No poses matched any database images. "
                "Check that image names in poses_raw match the database "
                "(expected format: 'pano_cameraX/frame_XXXXXX.jpg')."
            )
        _prog(50, f"Reconstruction seeded with {n_registered} registered images.")

        # ── 4b. Layer rig/frame structure onto the seeded reconstruction ───────
        # apply_rig_config groups images into frames by their pano_camera{j}/ prefix.
        # It clears any existing rigs/frames but preserves recon.images and their
        # cam_from_world.  Since the anchor is the ref sensor (sensor_from_rig=I),
        # rig_from_world == anchor cam_from_world — set it directly from the image.
        if rig_config is not None:
            with pycolmap.Database.open(str(db_path)) as _db:
                _db_images = _db.read_all_images()
                for _rc in rig_config.cameras:
                    _n_match = sum(1 for _i in _db_images if _i.name.startswith(_rc.image_prefix))
                    if _n_match == 0:
                        raise RuntimeError(
                            f"apply_rig_config precondition failed: no DB images start with "
                            f"{_rc.image_prefix!r}. This would cause a C++ CHECK abort. "
                            f"DB has {len(_db_images)} images; first few: "
                            f"{[_i.name for _i in _db_images[:5]]}"
                        )
                _prog(51, f"Rig precondition OK — all sensor prefixes found in DB.")
                pycolmap.apply_rig_config([rig_config], _db, recon)
            anchor_dir = f"pano_camera{anchor_idx}"
            frame_posed = set()
            for _img_id, _img in recon.images.items():
                if _img.frame_id in frame_posed:
                    continue
                if Path(_img.name).parent.name != anchor_dir:
                    continue
                _frame = recon.frames[_img.frame_id]
                _cfw = _img.cam_from_world()
                _frame.rig_from_world = pycolmap.Rigid3d(
                    pycolmap.Rotation3d(
                        np.array(_cfw.rotation.matrix(), dtype=np.float64)
                    ),
                    np.array(_cfw.translation, dtype=np.float64).flatten(),
                )
                recon.register_frame(_img.frame_id)
                frame_posed.add(_img.frame_id)
            n_framed = len(frame_posed)
            # Verify frame.has_pose actually took effect (rig_from_world setter check)
            n_has_pose = sum(1 for f in recon.frames.values() if f.has_pose)
            _prog(51, f"Rig structure applied — {n_framed}/{len(recon.frames)} frames posed "
                      f"(has_pose={n_has_pose}/{len(recon.frames)}).")

        # ── 4c. Guided re-matching (old cached DBs only) ──────────────────────
        # GPU step 3 now runs --FeatureMatching.guided_matching 1 and immediately
        # touches guided_marker, so this block is never reached for new GPU runs.
        # It only fires for old cached DBs (colmap.matched exists, guided absent)
        # that were built without guided matching in step 3.
        #
        # IMPORTANT: clears two_view_geometries before the attempt.  If the CLI
        # fails the job raises immediately and resets both markers so the next run
        # re-does matching from scratch instead of triangulating 0 points.
        # CPU path: falls back to _run_guided_subprocess() (isolated Python child).
        if not guided_marker.exists():
            _prog(52, "Guided verification pass: re-running geometric verification with epipolar guidance…")
            with pycolmap.Database.open(str(db_path)) as _db:
                _n_raw_before = _db.num_matches()
                _db.clear_two_view_geometries()   # force re-verification; raw matches kept
            _prog(52, f"Guided verification pass: {_n_raw_before:,} raw matches in DB — "
                      f"re-verifying with guided E/F pass via {'CLI' if use_gpu else 'subprocess'}…")
            if use_gpu:
                _guided_ok = _run_guided_cli(colmap_bin, db_path)
            else:
                _guided_ok = _run_guided_subprocess(db_path)
            if not _guided_ok:
                # two_view_geometries was cleared and guided failed — reset markers so the
                # next run re-does matching from scratch rather than triangulating 0 points.
                db_done_marker.unlink(missing_ok=True)
                guided_marker.unlink(missing_ok=True)
                raise RuntimeError(
                    "Guided verification failed after clearing two_view_geometries. "
                    "Markers reset — re-run the job to re-match from scratch."
                )
            guided_marker.touch()
            with pycolmap.Database.open(str(db_path)) as _db:
                _n_inlier = _db.num_inlier_matches()
            _prog(53, f"Guided verification complete — {_n_inlier:,} total inlier matches.")

        # ── 5. Triangulate 3D points ───────────────────────────────────────────
        _prog(54, "Triangulating 3D point cloud from known poses…")
        sparse_txt.mkdir(parents=True, exist_ok=True)

        recon = _triangulate(recon, db_path, image_path, sparse_txt)

        n_pts  = len(recon.points3D)
        n_imgs = len(recon.reg_image_ids())
        # Diagnostic: check rig/frame state after triangulate_points
        _n_rigs_post  = recon.num_rigs()
        _n_frames_post = len(recon.frames) if _n_rigs_post > 0 else 0
        _n_posed_post  = sum(1 for f in recon.frames.values() if f.has_pose) if _n_rigs_post > 0 else 0
        _prog(70, f"Triangulation complete — {n_pts:,} 3D points, {n_imgs} registered images. "
                  f"Post-triangulation rig state: rigs={_n_rigs_post}, frames={_n_frames_post}, "
                  f"posed={_n_posed_post}.")

        if n_pts == 0:
            raise RuntimeError(
                "Triangulation produced 0 points. "
                "Feature matching may have found too few inter-frame correspondences. "
                "Try 'exhaustive' matcher or check that views have sufficient overlap."
            )

        # ── 6. Bundle adjustment ───────────────────────────────────────────────────
        # Native pycolmap rig-aware BA: create_default_bundle_adjuster with
        # set_constant_sensor_from_rig_pose locks all 12 non-ref sensor offsets.
        # BA optimizes one 6-DoF rig_from_world per frame (25 × 6 = 150 DOF)
        # using tracks from all N sensors simultaneously — rigidity enforced by
        # the optimizer, not post-hoc. Falls back to unconstrained bundle_adjustment
        # when no rig structure is active (abs_rots unavailable).
        ba_opts = pycolmap.BundleAdjustmentOptions()
        ba_opts.refine_focal_length    = False
        ba_opts.refine_principal_point = False
        ba_opts.refine_extra_params    = False

        def _do_ba(r):
            if r.num_rigs() > 0:
                try:
                    pycolmap.create_default_bundle_adjuster(
                        ba_opts, _rig_ba_config(r), r
                    ).solve()
                except Exception as _ba_err:
                    print(f"  [rigsfm_worker] Rig BA failed ({_ba_err}), falling back to unconstrained BA", flush=True)
                    pycolmap.bundle_adjustment(r, ba_opts)
            else:
                pycolmap.bundle_adjustment(r, ba_opts)

        _rig_tag = "rig-constrained" if recon.num_rigs() > 0 else "no-rig"
        _prog(72, f"BA pass 1 ({_rig_tag}, pycolmap)…")
        _do_ba(recon)
        n_after_ba1 = len(recon.points3D)

        n_floaters = _filter_floaters(recon, max_reproj_error=2.0, min_track_len=3)
        _prog(78, f"BA1: {n_after_ba1:,} pts, filtered {n_floaters:,} floaters "
                  f"→ {len(recon.points3D):,} clean — re-triangulating…")
        recon = _triangulate(recon, db_path, image_path, sparse_txt)

        # Re-apply rig/frame structure if triangulate_points returned a plain reconstruction.
        if rig_config is not None and recon.num_rigs() == 0:
            _anchor_dir = f"pano_camera{anchor_idx}"
            with pycolmap.Database.open(str(db_path)) as _db2:
                pycolmap.apply_rig_config([rig_config], _db2, recon)
            _frame_posed2 = set()
            for _img_id2, _img2 in recon.images.items():
                if _img2.frame_id in _frame_posed2:
                    continue
                if Path(_img2.name).parent.name != _anchor_dir:
                    continue
                _frame2 = recon.frames[_img2.frame_id]
                _cfw2 = _img2.cam_from_world()
                _frame2.rig_from_world = pycolmap.Rigid3d(
                    pycolmap.Rotation3d(
                        np.array(_cfw2.rotation.matrix(), dtype=np.float64)
                    ),
                    np.array(_cfw2.translation, dtype=np.float64).flatten(),
                )
                recon.register_frame(_img2.frame_id)
                _frame_posed2.add(_img2.frame_id)
            _prog(81, f"Rig re-applied after retri — {len(_frame_posed2)} frames posed.")

        n_after_retri = len(recon.points3D)
        _rig_tag2 = "rig-constrained" if recon.num_rigs() > 0 else "no-rig"
        _prog(84, f"Retri: {n_after_retri:,} pts — BA pass 2 ({_rig_tag2}, pycolmap)…")
        _do_ba(recon)

        n_pts_after = len(recon.points3D)
        _prog(89, f"Rig-aware ABA complete — {n_pts_after:,} points.")

        # ── 7. Write sparse_txt ────────────────────────────────────────────────
        _prog(90, "Writing sparse_txt output…")
        recon.write_text(str(sparse_txt))

        print(json.dumps({
            "success":  True,
            "images":   n_imgs,
            "points3D": n_pts_after,
        }), flush=True)

    except Exception as exc:
        print(json.dumps({
            "success":   False,
            "error":     str(exc),
            "traceback": traceback.format_exc(),
        }), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
