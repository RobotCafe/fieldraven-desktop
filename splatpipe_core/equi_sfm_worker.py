# splatpipe_core/equi_sfm_worker.py
"""
EquiSfM worker — runs in Python 3.14 / pycolmap 4.1.0.

Receives a JSON payload via sys.argv[1] and runs COLMAP EQUIRECTANGULAR SfM:
  1. Feature extraction  (EQUIRECTANGULAR camera model, SINGLE camera mode)
  2. Sequential (or exhaustive) feature matching
  3. Incremental mapping
  4. Write pano-level sparse_txt

Progress lines: WORKER_PROGRESS:<pct>:<message>
Final stdout line: JSON result dict
"""
import sys
import json
import traceback
from pathlib import Path


def _prog(pct: int, msg: str) -> None:
    print(f"WORKER_PROGRESS:{pct}:{msg}", flush=True)


def main():
    import pycolmap

    # Verify EQUIRECTANGULAR is available (pycolmap ≥ 4.1.0)
    available = set(pycolmap.CameraModelId.__members__)
    if "EQUIRECTANGULAR" not in available:
        print(json.dumps({
            "success": False,
            "error": (
                f"EQUIRECTANGULAR camera model not available in this pycolmap build "
                f"(version {pycolmap.__version__}, available: {sorted(available)}). "
                "Upgrade to pycolmap ≥ 4.1.0."
            ),
        }), flush=True)
        sys.exit(1)

    payload  = json.loads(sys.argv[1])
    db_path  = Path(payload["database_path"])
    pano_dir = Path(payload["pano_dir"])
    out_dir  = Path(payload["output_dir"])
    matcher  = payload.get("matcher", "sequential")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Feature extraction (EQUIRECTANGULAR) ───────────────────────────────
    _prog(5, "EquiSfM: extracting SIFT features (EQUIRECTANGULAR)…")
    pycolmap.extract_features(
        str(db_path),
        str(pano_dir),
        camera_mode=pycolmap.CameraMode.SINGLE,
        reader_options=pycolmap.ImageReaderOptions(camera_model="EQUIRECTANGULAR"),
    )

    # ── 2. Feature matching ────────────────────────────────────────────────────
    _prog(30, f"EquiSfM: {matcher} feature matching…")
    if matcher == "exhaustive":
        pycolmap.match_exhaustive(str(db_path))
    else:
        # Sequential: appropriate for ordered captures (video / walk-through)
        pycolmap.match_sequential(str(db_path))

    # ── 3. Incremental mapping ─────────────────────────────────────────────────
    _prog(55, "EquiSfM: incremental mapping (EQUIRECTANGULAR SfM)…")
    recs = pycolmap.incremental_mapping(
        database_path=str(db_path),
        image_path=str(pano_dir),
        output_path=str(out_dir),
    )

    if not recs:
        print(json.dumps({
            "success": False,
            "error": (
                "COLMAP incremental mapping produced no reconstructions. "
                "Check that pano images have sufficient sequential overlap "
                "and that the source directory contains valid equirectangular JPEGs."
            ),
        }), flush=True)
        sys.exit(1)

    _prog(85, f"EquiSfM: {len(recs)} reconstruction(s) — selecting best by image count…")
    best = max(recs.values(), key=lambda r: len(r.images))

    # ── 4. Write pano-level sparse_txt ────────────────────────────────────────
    sparse_txt = out_dir / "sparse_txt"
    sparse_txt.mkdir(parents=True, exist_ok=True)
    best.write_text(str(sparse_txt))
    n_pts = len(best.points3D)
    _prog(92, f"EquiSfM: sparse_txt written ({len(best.images)} panos, {n_pts:,} points)")

    # Export per-pano cam_from_world poses for the runner (plain serialisable dicts)
    poses: dict = {}
    for img in best.images.values():
        cfw = img.cam_from_world
        R = cfw.rotation.matrix()
        t = cfw.translation
        poses[img.name] = {
            "R": R.tolist() if hasattr(R, "tolist") else list(R),
            "t": t.tolist() if hasattr(t, "tolist") else list(t),
        }

    print(json.dumps({
        "success":    True,
        "images":     len(best.images),
        "points3D":   n_pts,
        "poses":      poses,
        "sparse_txt": str(sparse_txt),
    }), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({
            "success":   False,
            "error":     str(exc),
            "traceback": traceback.format_exc(),
        }), flush=True)
        sys.exit(1)
