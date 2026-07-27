# splatpipe_core/colmap_worker.py
"""
Isolated PyCOLMAP v4 worker — runs under Python 3.14 / pycolmap 4.0.4.

Receives a JSON payload via sys.argv[1] and runs the full COLMAP rig pipeline:
  1. Feature extraction  (SIMPLE_PINHOLE, PER_FOLDER)
       → CLI (colmap.exe) if colmap_bin is set, else pycolmap (CPU only)
  2. Rig config build + apply_rig_config  (always pycolmap)
  3. Feature matching
       → CLI (colmap.exe) if colmap_bin is set, else pycolmap (CPU only)
  4. Incremental mapping (ba_refine_sensor_from_rig=False)  (always pycolmap)
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
    Delete verified two-view geometries (and their raw matches) between
    sibling sensors of the same rig frame -- e.g. pano_camera0/IMG_..._167.jpg
    <-> pano_camera1/IMG_..._167.jpg. All sensors in a frame are rendered from
    the same panorama at the same position (zero baseline), so these pairs
    have no triangulation information; COLMAP's geometric verifier accepts
    them anyway (it's a legitimate "panoramic"/pure-rotation match), and
    triangulating one collapses to the shared camera center. Returns count
    of pairs purged.
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
        vocab_tree_path = payload.get("vocab_tree_path", "")  # optional second-pass loop closure

        rig         = payload["rig"]
        focal       = float(rig["focal"])
        image_size  = int(rig["image_size"])
        # cam_from_rig rotation matrices (sensor 0 = identity = reference sensor)
        rotations   = [np.array(r, dtype=np.float64) for r in rig["rotations"]]
        prefixes    = rig["image_prefixes"]   # ["pano_camera0/", "pano_camera1/", …]

        # Dump the exact rig config we're about to apply to COLMAP, straight from
        # the data used to build it below — a faithful record, not a re-derivation.
        def _pitch_yaw(R: np.ndarray) -> tuple:
            fwd = R[2]
            pitch = float(np.degrees(np.arcsin(np.clip(fwd[1], -1, 1))))
            yaw = float(np.degrees(np.arctan2(fwd[0], fwd[2])))
            return pitch, yaw

        rig_config_dump = {
            "focal": focal,
            "image_size": image_size,
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

        # Shared params_str used by both CLI and pycolmap paths
        params_str  = f"{focal},{image_size // 2},{image_size // 2}"
        reader_opts = pycolmap.ImageReaderOptions(
            camera_model="SIMPLE_PINHOLE",
            camera_params=params_str,
        )

        # ── CLI helper ─────────────────────────────────────────────────────────
        def _run_cli(args: list, pct: int) -> None:
            """Run a colmap.exe subcommand, streaming output as WORKER_PROGRESS lines."""
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
        _prog(20, f"Extracting SIFT features ({len(prefixes)} sensors, SIMPLE_PINHOLE)…")
        if colmap_bin:
            # Remove any stale database from a previous pycolmap run.
            if db_path.exists():
                db_path.unlink()
            # Write a project file so all ImageReader options come from ONE source.
            # Passing them both via --project_path and on the command line causes
            # "cannot be specified more than once" in newer COLMAP builds.
            proj_ini = db_path.parent / "_colmap_extract.ini"
            proj_ini.write_text(
                f"database_path={db_path}\n"
                f"image_path={image_path}\n"
                f"ImageReader.camera_model=SIMPLE_PINHOLE\n"
                f"ImageReader.camera_params={params_str}\n"
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

        # ── 2. Build rig config ────────────────────────────────────────────────
        # pycolmap 4.x renamed the kwarg from model_id= to model=.
        # Try the new signature first, fall back to old for older installs.
        try:
            camera = pycolmap.Camera.create_from_model_id(
                camera_id=0,
                model=pycolmap.CameraModelId.SIMPLE_PINHOLE,
                focal_length=focal,
                width=image_size,
                height=image_size,
            )
        except TypeError:
            camera = pycolmap.Camera.create_from_model_id(
                camera_id=0,
                model_id=pycolmap.CameraModelId.SIMPLE_PINHOLE,
                focal_length=focal,
                width=image_size,
                height=image_size,
            )
        except AttributeError:
            camera = pycolmap.Camera.create(
                camera_id=0,
                model=pycolmap.CameraModelId.SIMPLE_PINHOLE,
                focal_length=focal,
                width=image_size,
                height=image_size,
            )
        camera.has_prior_focal_length = True

        zero_t = np.zeros((3, 1), dtype=np.float64)
        rig_cameras = []
        for i, (R, prefix) in enumerate(zip(rotations, prefixes)):
            # rotations[0] is identity (ref @ ref.T = I) — reference sensor
            cam_from_rig = (
                None if i == 0
                else pycolmap.Rigid3d(pycolmap.Rotation3d(R), zero_t)
            )
            rc = pycolmap.RigConfigCamera(
                ref_sensor=(i == 0),
                image_prefix=prefix,
                cam_from_rig=cam_from_rig,
            )
            rc.camera = camera
            rig_cameras.append(rc)

        rig_config = pycolmap.RigConfig(cameras=rig_cameras)

        # ── 3. Apply rig config ────────────────────────────────────────────────
        _prog(30, "Applying rig configuration…")
        with pycolmap.Database.open(str(db_path)) as db:
            pycolmap.apply_rig_config([rig_config], db)

        # ── 4. Feature matching ────────────────────────────────────────────────
        # Image count for matcher scaling below. image_path's direct children are
        # per-sensor folders (pano_camera0/, pano_camera1/, …), not image files —
        # iterdir()+is_file() sees zero files here; must recurse.
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
            ]
            if matcher == "sequential":
                # Wider-than-default overlap for denser matches/point cloud (see
                # COLMAP_POSE_CORRECTION_BRIEF.md Problem 5 / Problem 6 follow-up —
                # the earlier stall was Firebase backpressure, not quadratic_overlap).
                # But neither overlap nor quadratic_overlap scale down on their own,
                # and this pipeline's virtual multi-sensor rig frames mean many
                # nearby images share near-identical content — the same shape of
                # risk that made the vocab-tree pass balloon to hours on a large
                # job. Scale back toward COLMAP's own default as N grows instead of
                # applying the generous small-job setting unconditionally.
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
        # Runs AFTER sequential matching and adds non-sequential image pairs that
        # share visual content — essential for walks that loop back or cross
        # themselves. Sequential matching misses these; vocab tree retrieval finds
        # them by global visual similarity. Only fires when a vocab tree .bin is
        # configured AND colmap_bin is set (no pycolmap vocab tree path API).
        if vocab_tree_path and colmap_bin and matcher == "sequential":
            # Scale retrieval window inversely with image count (_n_imgs computed
            # above, before sequential matching). With N=351 and k=50 the matcher
            # generates ~8,775 candidate pairs — some take 200+ seconds on
            # non-trivial feature sets, making the pass take hours. Loop-closure
            # only needs a handful of true revisits; small k is sufficient.
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
                ], pct=58)
                _prog(60, "Vocab tree loop closure pass complete.")
            except RuntimeError as _vt_err:
                # Non-fatal — the sequential matches already exist; vocab tree is a bonus
                # loop-closure pass. Common failure: FAISS .bin file with a non-FAISS
                # COLMAP build. Pipeline continues without it.
                print(f"WORKER_PROGRESS:58:⚠️ Vocab tree pass skipped (non-fatal): {_vt_err}", flush=True)

        # ── 4c. Purge same-rig-frame sibling pairs ──────────────────────────────
        # pycolmap's own matcher excludes these via skip_image_pairs_in_same_frame
        # (set above), but the GPU CLI matcher has no equivalent flag. Sibling
        # sensors within one rig frame share a single optical center (zero
        # baseline) -- COLMAP's geometric verification happily accepts these as
        # real "panoramic" matches (the genuine overlap seam between adjacent
        # virtual sensors), but triangulating them is degenerate: it collapses
        # to the shared camera center. Confirmed empirically: 252 such verified
        # pairs with 800-7000+ inliers each, producing points with reprojection
        # error in the thousands of px. See COLMAP_POSE_CORRECTION_BRIEF.md
        # Problem 12. Cheap no-op when the matcher already excluded them.
        n_purged = _purge_same_frame_pairs(db_path)
        if n_purged:
            _prog(58, f"Purged {n_purged} same-rig-frame sibling pairs (zero-baseline, non-triangulable).")

        # ── 5. Mapping ─────────────────────────────────────────────────────────
        import shutil, sqlite3

        sparse_path.mkdir(parents=True, exist_ok=True)
        sparse_txt.mkdir(parents=True, exist_ok=True)

        def _run_mapper(label: str) -> dict:
            """Run the selected mapper, return recs dict."""
            if colmap_mapper == "global":
                # pycolmap.global_mapping() is available directly in pycolmap 4.0.4
                # (no CLI required). Solves all cameras simultaneously — drift-free
                # global rotations + positions. Does NOT enforce rig zero-baseline
                # constraints (each sensor reconstructed independently), which is a
                # valid tradeoff for large captures where global consistency matters.
                _prog(60, f"Running GLOMAP global SfM via pycolmap ({label})…")
                recs = pycolmap.global_mapping(
                    str(db_path), str(image_path), str(sparse_path)
                )
                if not recs:
                    raise RuntimeError("GLOMAP global mapping produced no reconstructions.")
            else:
                _prog(60, f"Running incremental mapping, fixed rig, GPU BA ({label})…")
                # ba_use_gpu/ba_gpu_index: bundle adjustment was previously assumed to
                # require a CLI subprocess (colmap.exe bundle_adjuster) to get GPU Ceres,
                # since pycolmap.incremental_mapping() looked like a black box with no
                # BA-backend hook. The installed pycolmap (4.1.0, newer than the 4.0.4
                # this codebase was originally built against) exposes these fields
                # directly on IncrementalPipelineOptions — same call, no rewrite needed.
                map_opts = pycolmap.IncrementalPipelineOptions(
                    ba_refine_sensor_from_rig=False,
                    ba_refine_focal_length=False,
                    ba_refine_principal_point=False,
                    ba_refine_extra_params=False,
                    ba_use_gpu=True,
                    ba_gpu_index="-1",  # string field, unlike the CLI's int gpu_index flags
                )
                recs = pycolmap.incremental_mapping(
                    str(db_path), str(image_path), str(sparse_path), map_opts
                )
                if not recs:
                    raise RuntimeError("COLMAP incremental mapping produced no reconstructions.")
            return recs

        recs = _run_mapper("pass 1")

        # ── Split-reconstruction detection + exhaustive retry ──────────────────
        # COLMAP / GLOMAP both produce one dict entry per connected component.
        # More than one means the match graph had a gap and the scene split into
        # disconnected fragments — the largest fragment is used, the rest dropped.
        # Auto-retry with exhaustive matching when the image count is manageable.
        if len(recs) > 1:
            frag_sizes  = sorted((len(r.images) for r in recs.values()), reverse=True)
            total_imgs  = sum(frag_sizes)
            frag_report = ", ".join(str(s) for s in frag_sizes)
            _prog(86, f"WARNING: split reconstruction — {len(recs)} fragments "
                      f"({frag_report} images). Largest keeps {frag_sizes[0]}/{total_imgs}.")

            if matcher == "sequential" and total_imgs <= 400:
                _prog(60, f"Auto-retry: clearing matches → exhaustive re-match "
                          f"({total_imgs} images)…")

                # Clear matches but keep feature extraction (expensive to redo).
                with sqlite3.connect(str(db_path)) as _conn:
                    _conn.execute("DELETE FROM matches")
                    _conn.execute("DELETE FROM two_view_geometries")

                # Re-run exhaustive matching
                if colmap_bin:
                    _run_cli(["exhaustive_matcher",
                              "--database_path", str(db_path)], pct=65)
                else:
                    _mo2 = pycolmap.FeatureMatchingOptions()
                    _mo2.rig_verification            = True
                    _mo2.skip_image_pairs_in_same_frame = True
                    _match("exhaustive", str(db_path), _mo2)

                n2 = _purge_same_frame_pairs(db_path)
                if n2:
                    _prog(70, f"Re-purged {n2} same-rig-frame pairs.")

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
