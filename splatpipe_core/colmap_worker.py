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


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    try:
        payload     = json.loads(sys.argv[1])
        db_path     = Path(payload["database_path"])
        image_path  = Path(payload["image_path"])
        sparse_path = Path(payload["sparse_path"])
        sparse_txt  = Path(payload["sparse_txt_path"])
        matcher     = payload.get("colmap_matcher", "sequential")
        colmap_bin  = payload.get("colmap_bin", "")   # empty → pycolmap CPU fallback

        rig         = payload["rig"]
        focal       = float(rig["focal"])
        image_size  = int(rig["image_size"])
        # cam_from_rig rotation matrices (sensor 0 = identity = reference sensor)
        rotations   = [np.array(r, dtype=np.float64) for r in rig["rotations"]]
        prefixes    = rig["image_prefixes"]   # ["pano_camera0/", "pano_camera1/", …]

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
                f"ImageReader.single_camera_per_folder=1\n",
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
        _prog(40, f"Matching features ({matcher})…")
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
                # Disable quadratic overlap — avoids degenerate within-rig pairs
                # that cause RANSAC to stall (near-zero baseline, no epipolar constraint).
                # overlap=7 = one full rig-frame worth of sequential neighbors.
                cli_args += [
                    "--SequentialMatching.overlap",           "7",
                    "--SequentialMatching.quadratic_overlap", "0",
                ]
            _run_cli(cli_args, pct=40)
        else:
            mo = pycolmap.FeatureMatchingOptions()
            mo.rig_verification = True
            mo.skip_image_pairs_in_same_frame = True
            _match(matcher, str(db_path), mo)
        _prog(58, "Feature matching complete.")

        # ── 5. Incremental mapping ─────────────────────────────────────────────
        # Seed world frame from anchor (pano_camera0) image pair so COLMAP
        # initialises with a horizontal baseline. Without this, the 6 siblings
        # at -10° dominate and COLMAP tilts the whole world +10° to compensate.
        _prog(60, "Running incremental mapping (fixed rig)…")
        sparse_path.mkdir(parents=True, exist_ok=True)
        map_opts = pycolmap.IncrementalPipelineOptions(
            ba_refine_sensor_from_rig=False,
            ba_refine_focal_length=False,
            ba_refine_principal_point=False,
            ba_refine_extra_params=False,
        )
        recs = pycolmap.incremental_mapping(
            str(db_path), str(image_path), str(sparse_path), map_opts
        )

        if not recs:
            raise RuntimeError("COLMAP incremental mapping produced no reconstructions.")

        best   = max(recs.values(), key=lambda r: len(r.images))
        n_imgs = len(best.images)
        n_pts  = len(best.points3D)
        _prog(86, f"Mapping complete — {n_imgs} images, {n_pts} 3D points.")

        # ── 6. Write sparse_txt ────────────────────────────────────────────────
        _prog(88, "Writing sparse_txt output…")
        sparse_txt.mkdir(parents=True, exist_ok=True)
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
