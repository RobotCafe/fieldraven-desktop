# splatpipe_core/colmap_fisheye_worker.py
"""
Isolated PyCOLMAP worker for the "COLMAP Fisheye" alignment mode — cloned
from colmap_worker.py (the verified SIMPLE_PINHOLE rig path) with two
substantive differences:

  1. Camera model is OPENCV_FISHEYE (fx,fy,cx,cy,k1,k2,k3,k4) with REAL
     calibrated intrinsics per lens (front/back), sourced from a saved
     lens_calibrator.py profile — not a derived single-focal guess. Front
     and back lenses get their own distinct pycolmap.Camera object/id
     instead of one shared object reused across every rig sensor.
  2. The Caspar GPU BA tier is removed entirely. Caspar's adapter only
     understands SIMPLE_RADIAL/PINHOLE (see colmap_worker.py's
     _convert_db_cameras_to_pinhole docstring) and has no conversion path
     for OPENCV_FISHEYE — reaching it unconverted silently produces
     "No residuals to optimize" (a wrong answer, not a crash). This worker
     goes straight to Ceres CLI, then in-process CPU as fallback — a
     2-tier chain instead of the 3-tier Caspar/Ceres/CPU chain.

Receives a JSON payload via sys.argv[1] and runs:
  1. Feature extraction  (OPENCV_FISHEYE, PER_FOLDER)
       → CLI (colmap.exe) if colmap_bin is set, else pycolmap (CPU only)
  2. Rig config build + apply_rig_config  (always pycolmap) — front/back
     each get their own calibrated intrinsics, never refined by BA.
  3. Feature matching
       → CLI (colmap.exe) if colmap_bin is set, else pycolmap (CPU only)
  4. Incremental/global mapping, intrinsics locked, Caspar tier removed
  5. Write sparse_txt output

Progress lines are printed as  WORKER_PROGRESS:<pct>:<message>
and parsed by the parent process.  The final stdout line is always JSON.
"""
import sys
import json
import subprocess
import traceback
from pathlib import Path

import numpy as np


# ── progress helper ───────────────────────────────────────────────────────────

def _prog(pct: int, msg: str) -> None:
    print(f"WORKER_PROGRESS:{pct}:{msg}", flush=True)


# ── matcher dispatcher (pycolmap fallback) ────────────────────────────────────

def _match(matcher: str, db_str: str, options) -> None:
    import pycolmap
    if matcher == "sequential":
        pycolmap.match_sequential(db_str, matching_options=options)
    elif matcher == "exhaustive":
        pycolmap.match_exhaustive(db_str, matching_options=options)
    elif matcher == "vocabtree":
        pycolmap.match_vocabtree(db_str, matching_options=options)
    else:
        raise ValueError(f"Unknown COLMAP matcher: {matcher!r}")


def _purge_same_frame_pairs(db_path: Path) -> int:
    """
    Delete verified two-view geometries (and their raw matches) between the
    front/back images of the same captured instant -- e.g. front/IMG_167.jpg
    <-> back/IMG_167.jpg. The X4's two lenses sit only a few cm apart at the
    camera body center relative to typical scene depth, close enough to the
    same pano-rig zero-baseline degeneracy that colmap_worker.py's version of
    this function exists for: COLMAP's geometric verifier accepts these pairs
    as legitimate matches, but triangulating them is poorly conditioned.
    Returns count of pairs purged. Assumes front/back frames share a filename
    stem for the same capture instant -- confirm this against the actual
    frame-extraction naming convention before relying on it.
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


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    try:
        payload     = json.loads(sys.argv[1])
        db_path     = Path(payload["database_path"])
        image_path  = Path(payload["image_path"])
        sparse_path = Path(payload["sparse_path"])
        sparse_txt  = Path(payload["sparse_txt_path"])
        matcher         = payload.get("colmap_matcher", "sequential")
        colmap_bin      = payload.get("colmap_bin", "")      # empty → pycolmap CPU fallback
        colmap_mapper   = payload.get("colmap_mapper", "incremental")  # "incremental" | "global"
        vocab_tree_path    = payload.get("vocab_tree_path", "")
        vocab_tree_enabled = payload.get("vocab_tree_enabled", True)

        rig          = payload["rig"]
        image_width  = int(rig["image_width"])
        image_height = int(rig["image_height"])
        # 8-value OPENCV_FISHEYE params per lens: fx,fy,cx,cy,k1,k2,k3,k4
        front_params = [float(v) for v in rig["front_params"]]
        back_params  = [float(v) for v in rig["back_params"]]
        # cam_from_rig rotation matrices (sensor 0 = front = identity = reference)
        rotations    = [np.array(r, dtype=np.float64) for r in rig["rotations"]]
        prefixes     = rig["image_prefixes"]   # ["front/", "back/"]

        def _pitch_yaw(R: np.ndarray) -> tuple:
            fwd = R[2]
            pitch = float(np.degrees(np.arcsin(np.clip(fwd[1], -1, 1))))
            yaw = float(np.degrees(np.arctan2(fwd[0], fwd[2])))
            return pitch, yaw

        rig_config_dump = {
            "camera_model": "OPENCV_FISHEYE",
            "image_width": image_width,
            "image_height": image_height,
            "front_params": front_params,
            "back_params": back_params,
            "n_sensors": len(rotations),
            "sensors": [
                {
                    "index": i,
                    "image_prefix": prefix,
                    "is_ref_sensor": i == 0,
                    "pitch_deg": _pitch_yaw(R)[0],
                    "yaw_deg": _pitch_yaw(R)[1],
                    "cam_from_rig": R.tolist(),
                }
                for i, (R, prefix) in enumerate(zip(rotations, prefixes))
            ],
        }
        (db_path.parent / "rig_config.json").write_text(
            json.dumps(rig_config_dump, indent=2), encoding="utf-8"
        )

        import pycolmap

        # Placeholder used for the initial extraction pass only -- ImageReaderOptions
        # is global for the whole extract_features()/project.ini call and can't vary
        # per folder, so every folder is seeded with the front lens's params here.
        # The rig-building step below overwrites each sensor with its OWN real
        # per-lens intrinsics via a distinct pycolmap.Camera object per sensor --
        # same mechanism colmap_worker.py uses to apply its one shared camera
        # object, just parameterized per-lens instead of shared.
        placeholder_params_str = ",".join(str(v) for v in front_params)
        reader_opts = pycolmap.ImageReaderOptions(
            camera_model="OPENCV_FISHEYE",
            camera_params=placeholder_params_str,
        )

        # ── CLI helper ─────────────────────────────────────────────────────────
        def _run_cli(args: list, pct: int) -> None:
            proc = subprocess.Popen(
                [colmap_bin] + args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    print(f"WORKER_PROGRESS:{pct}:{line}", flush=True)
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"COLMAP CLI '{args[0]}' exited with code {proc.returncode}"
                )

        # ── 1. Feature extraction ──────────────────────────────────────────────
        _prog(20, f"Extracting SIFT features ({len(prefixes)} sensors, OPENCV_FISHEYE)…")
        if colmap_bin:
            if db_path.exists():
                db_path.unlink()
            proj_ini = db_path.parent / "_colmap_extract.ini"
            proj_ini.write_text(
                f"database_path={db_path}\n"
                f"image_path={image_path}\n"
                f"ImageReader.camera_model=OPENCV_FISHEYE\n"
                f"ImageReader.camera_params={placeholder_params_str}\n"
                f"ImageReader.single_camera_per_folder=1\n"
                f"SiftExtraction.max_num_features=16384\n",
                encoding="utf-8",
            )
            _run_cli(["feature_extractor", "--project_path", str(proj_ini)], pct=20)
            proj_ini.unlink(missing_ok=True)
        else:
            pycolmap.extract_features(
                database_path=db_path,
                image_path=image_path,
                reader_options=reader_opts,
                camera_mode=pycolmap.CameraMode.PER_FOLDER,
            )
        _prog(28, "Feature extraction complete.")

        # ── 2. Build rig config — one real camera per lens ─────────────────────
        def _make_camera(camera_id: int, params: list):
            try:
                cam = pycolmap.Camera.create_from_model_id(
                    camera_id=camera_id, model=pycolmap.CameraModelId.OPENCV_FISHEYE,
                    focal_length=params[0], width=image_width, height=image_height,
                )
            except TypeError:
                cam = pycolmap.Camera.create_from_model_id(
                    camera_id=camera_id, model_id=pycolmap.CameraModelId.OPENCV_FISHEYE,
                    focal_length=params[0], width=image_width, height=image_height,
                )
            except AttributeError:
                cam = pycolmap.Camera.create(
                    camera_id=camera_id, model=pycolmap.CameraModelId.OPENCV_FISHEYE,
                    focal_length=params[0], width=image_width, height=image_height,
                )
            cam.params = np.array(params, dtype=np.float64)
            cam.has_prior_focal_length = True
            return cam

        front_camera = _make_camera(0, front_params)
        back_camera  = _make_camera(1, back_params)

        zero_t = np.zeros((3, 1), dtype=np.float64)
        rig_cameras = []
        for i, (R, prefix) in enumerate(zip(rotations, prefixes)):
            cam_from_rig = (
                None if i == 0
                else pycolmap.Rigid3d(pycolmap.Rotation3d(R), zero_t)
            )
            rc = pycolmap.RigConfigCamera(
                ref_sensor=(i == 0),
                image_prefix=prefix,
                cam_from_rig=cam_from_rig,
            )
            rc.camera = front_camera if i == 0 else back_camera
            rig_cameras.append(rc)

        rig_config = pycolmap.RigConfig(cameras=rig_cameras)

        # ── 3. Apply rig config ────────────────────────────────────────────────
        _prog(30, "Applying rig configuration (calibrated fisheye intrinsics, locked)…")
        with pycolmap.Database.open(str(db_path)) as db:
            pycolmap.apply_rig_config([rig_config], db)

        # ── 4. Feature matching ────────────────────────────────────────────────
        _n_imgs = sum(1 for p in image_path.rglob("*") if p.is_file())

        _prog(40, f"Matching features ({matcher}, {_n_imgs} images)…")
        if colmap_bin:
            cli_cmd = {
                "sequential": "sequential_matcher",
                "exhaustive": "exhaustive_matcher",
                "vocabtree":  "vocab_tree_matcher",
            }.get(matcher, "sequential_matcher")
            cli_args = [
                cli_cmd,
                "--database_path", str(db_path),
                "--FeatureMatching.rig_verification", "1",
                "--FeatureMatching.skip_image_pairs_in_same_frame", "1",
            ]
            if matcher == "sequential":
                if _n_imgs > 300:
                    _overlap, _quad = 10, 0
                elif _n_imgs > 150:
                    _overlap, _quad = 15, 1
                else:
                    _overlap, _quad = 20, 1
                cli_args += [
                    "--SequentialMatching.overlap",           str(_overlap),
                    "--SequentialMatching.quadratic_overlap", str(_quad),
                ]
            _run_cli(cli_args, pct=40)
        else:
            mo = pycolmap.FeatureMatchingOptions()
            mo.rig_verification = True
            mo.skip_image_pairs_in_same_frame = True
            _match(matcher, str(db_path), mo)
        _prog(58, "Feature matching complete.")

        # ── 4b. Vocab tree loop closure pass (optional second pass) ────────────
        if vocab_tree_enabled and vocab_tree_path and colmap_bin and matcher == "sequential":
            _vt_k = 5 if _n_imgs > 200 else (10 if _n_imgs > 100 else 20)
            _prog(58, f"Vocab tree loop closure pass — {vocab_tree_path.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}… ({_n_imgs} images, k={_vt_k})")
            try:
                _run_cli([
                    "vocab_tree_matcher",
                    "--database_path",                   str(db_path),
                    "--VocabTreeMatching.vocab_tree_path", vocab_tree_path,
                    "--VocabTreeMatching.num_images_after_verification", str(_vt_k),
                    "--FeatureMatching.use_gpu",  "1",
                    "--FeatureMatching.gpu_index", "-1",
                    "--FeatureMatching.skip_image_pairs_in_same_frame", "1",
                ], pct=58)
                _prog(60, "Vocab tree loop closure pass complete.")
            except RuntimeError as _vt_err:
                print(f"WORKER_PROGRESS:58:⚠️ Vocab tree pass skipped (non-fatal): {_vt_err}", flush=True)

        # ── 4c. Purge same-instant front/back pairs (safety net) ───────────────
        n_purged = _purge_same_frame_pairs(db_path)
        if n_purged:
            _prog(58, f"Purged {n_purged} same-instant front/back pairs (near-zero-baseline, non-triangulable).")

        # ── 5. Mapping ─────────────────────────────────────────────────────────
        import shutil, sqlite3

        sparse_path.mkdir(parents=True, exist_ok=True)
        sparse_txt.mkdir(parents=True, exist_ok=True)

        def _run_mapper(label: str) -> dict:
            """Run the selected mapper, return recs dict. No Caspar tier here —
            OPENCV_FISHEYE has no Caspar adapter path (see module docstring):
            Ceres CLI first, then in-process CPU pycolmap as the only fallback."""
            if colmap_mapper == "global":
                def _try_cli_global_mapper() -> dict:
                    cli_out = sparse_path.parent / "global_ceres_out"
                    if cli_out.exists():
                        shutil.rmtree(str(cli_out))
                    cli_out.mkdir(parents=True, exist_ok=True)

                    _run_cli([
                        "global_mapper",
                        "--database_path", str(db_path),
                        "--image_path", str(image_path),
                        "--output_path", str(cli_out),
                        "--GlobalMapper.ba_backend", "CERES",
                        "--GlobalMapper.refine_sensor_from_rig", "0",
                        "--GlobalMapper.ba_refine_focal_length", "0",
                        "--GlobalMapper.ba_refine_principal_point", "0",
                        "--GlobalMapper.ba_refine_extra_params", "0",
                        "--GlobalMapper.ba_gpu_index", "-1",
                    ], pct=60)

                    model_dirs = sorted(p for p in cli_out.iterdir() if p.is_dir())
                    if not model_dirs:
                        raise RuntimeError("Ceres global_mapper produced no output models")
                    cli_recs = {}
                    for i, model_dir in enumerate(model_dirs):
                        r = pycolmap.Reconstruction()
                        r.read(str(model_dir))
                        cli_recs[i] = r
                    return cli_recs

                recs = None
                if colmap_bin:
                    try:
                        _prog(60, f"Running GLOMAP global SfM via Ceres CLI, rig-locked ({label})…")
                        recs = _try_cli_global_mapper()
                    except Exception as e:
                        _prog(60, f"Ceres global_mapper failed/rejected ({e}) — falling back to in-process CPU")

                if recs is None:
                    _prog(60, f"Running GLOMAP global SfM in-process, rig-locked, BA CPU ({label})…")
                    gm_opts = pycolmap.GlobalPipelineOptions()
                    gm_opts.mapper.refine_sensor_from_rig = False
                    gm_opts.mapper.bundle_adjustment.refine_focal_length = False
                    gm_opts.mapper.bundle_adjustment.refine_principal_point = False
                    gm_opts.mapper.bundle_adjustment.refine_extra_params = False
                    recs = pycolmap.global_mapping(
                        str(db_path), str(image_path), str(sparse_path), gm_opts
                    )
                    if not recs:
                        raise RuntimeError("GLOMAP global mapping produced no reconstructions.")
            else:
                def _try_cli_mapper() -> dict:
                    cli_out = sparse_path.parent / "mapper_ceres_out"
                    if cli_out.exists():
                        shutil.rmtree(str(cli_out))
                    cli_out.mkdir(parents=True, exist_ok=True)

                    _run_cli([
                        "mapper",
                        "--database_path", str(db_path),
                        "--image_path", str(image_path),
                        "--output_path", str(cli_out),
                        "--Mapper.ba_local_backend", "CERES",
                        "--Mapper.ba_global_backend", "CERES",
                        "--Mapper.ba_refine_sensor_from_rig", "0",
                        "--Mapper.ba_refine_focal_length", "0",
                        "--Mapper.ba_refine_principal_point", "0",
                        "--Mapper.ba_refine_extra_params", "0",
                        "--Mapper.ba_use_gpu", "1",
                        "--Mapper.ba_gpu_index", "-1",
                    ], pct=60)

                    model_dirs = sorted(p for p in cli_out.iterdir() if p.is_dir())
                    if not model_dirs:
                        raise RuntimeError("Ceres mapper produced no output models")
                    cli_recs = {}
                    for i, model_dir in enumerate(model_dirs):
                        r = pycolmap.Reconstruction()
                        r.read(str(model_dir))
                        cli_recs[i] = r
                    return cli_recs

                recs = None
                if colmap_bin:
                    try:
                        _prog(60, f"Running incremental mapping via Ceres CLI ({label})…")
                        recs = _try_cli_mapper()
                    except Exception as e:
                        _prog(60, f"Ceres CLI mapper failed/rejected ({e}) — falling back to in-process CPU")

                if recs is None:
                    _prog(60, f"Running incremental mapping, fixed rig, BA CPU in-process ({label})…")
                    map_opts = pycolmap.IncrementalPipelineOptions(
                        ba_refine_sensor_from_rig=False,
                        ba_refine_focal_length=False,
                        ba_refine_principal_point=False,
                        ba_refine_extra_params=False,
                        ba_use_gpu=True,
                        ba_gpu_index="-1",
                    )
                    recs = pycolmap.incremental_mapping(
                        str(db_path), str(image_path), str(sparse_path), map_opts
                    )
                    if not recs:
                        raise RuntimeError("COLMAP incremental mapping produced no reconstructions.")
            return recs

        recs = _run_mapper("pass 1")

        # ── Split-reconstruction detection + exhaustive retry ──────────────────
        if len(recs) > 1:
            frag_sizes  = sorted((len(r.images) for r in recs.values()), reverse=True)
            total_imgs  = sum(frag_sizes)
            frag_report = ", ".join(str(s) for s in frag_sizes)
            _prog(86, f"WARNING: split reconstruction — {len(recs)} fragments "
                      f"({frag_report} images). Largest keeps {frag_sizes[0]}/{total_imgs}.")

            if matcher == "sequential" and total_imgs <= 400:
                _prog(60, f"Auto-retry: clearing matches → exhaustive re-match "
                          f"({total_imgs} images)…")

                with sqlite3.connect(str(db_path)) as _conn:
                    _conn.execute("DELETE FROM matches")
                    _conn.execute("DELETE FROM two_view_geometries")

                if colmap_bin:
                    _run_cli(["exhaustive_matcher",
                              "--database_path", str(db_path),
                              "--FeatureMatching.rig_verification", "1",
                              "--FeatureMatching.skip_image_pairs_in_same_frame", "1",
                              ], pct=65)
                else:
                    _mo2 = pycolmap.FeatureMatchingOptions()
                    _mo2.rig_verification            = True
                    _mo2.skip_image_pairs_in_same_frame = True
                    _match("exhaustive", str(db_path), _mo2)

                n2 = _purge_same_frame_pairs(db_path)
                if n2:
                    _prog(70, f"Re-purged {n2} same-instant front/back pairs.")

                shutil.rmtree(str(sparse_path))
                sparse_path.mkdir()

                recs2 = _run_mapper("pass 2 — exhaustive retry")
                if len(recs2) < len(recs):
                    _prog(86, f"Retry unified reconstruction to {len(recs2)} fragment(s).")
                else:
                    frag_sizes2 = sorted((len(r.images) for r in recs2.values()), reverse=True)
                    _prog(86, f"Retry: still {len(recs2)} fragments "
                              f"({', '.join(str(s) for s in frag_sizes2)} images). "
                              f"Proceeding with largest.")
                recs = recs2
            else:
                _prog(86, f"Scene has {total_imgs} images — exhaustive retry skipped "
                          f"(threshold 400). Use exhaustive or vocabtree matcher in settings.")

        best   = max(recs.values(), key=lambda r: len(r.images))
        n_imgs = len(best.images)
        n_pts  = len(best.points3D)
        _prog(86, f"Mapping complete — {n_imgs} images, {n_pts} 3D points.")

        # ── 6. Write sparse_txt ────────────────────────────────────────────────
        _prog(88, "Writing sparse_txt output…")
        best.write_text(str(sparse_txt))

        print(json.dumps({
            "success":  True,
            "images":   n_imgs,
            "points3D": n_pts,
        }), flush=True)

    except Exception as e:
        print(json.dumps({
            "success":   False,
            "error":     str(e),
            "traceback": traceback.format_exc(),
        }), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
