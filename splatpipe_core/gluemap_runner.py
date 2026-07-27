"""
GlueMap alignment engine for the SplatPipe pipeline.

Calls gluemap-demo inside WSL2 to produce a COLMAP-format sparse
reconstruction from the per-sensor image directories built by the view
extraction step. GlueMap's PER_FOLDER intrinsics mode assigns one camera
model per subfolder (pano_camera0/, pano_camera1/, …), matching our rig.

Five-stage pipeline:
  1. Retrieval   — SALAD builds the image neighbour graph (replaces vocab tree)
  2. Two-view    — Doppelgangers++ covisibility (skip_doppelgangers=True for speed)
  3. Inference   — Pi3 / VGGT / etc. multi-view pose estimation in star configs
  4. Global BA   — rotation/intrinsics averaging + bundle adjustment
  5. Refinement  — SIFT track snapping + augmented BA (skip with coarse_only=True)

Output (COLMAP cameras/images/points3D) is copied to brush_input/ for Brush.
"""
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .types import PipelineStage
from .settings import PipelineSettings


def _win_to_wsl(path: Path) -> str:
    """Convert a Windows absolute path to a WSL2 /mnt/<drive>/... path."""
    s = str(path.resolve()).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        return f"/mnt/{s[0].lower()}{s[2:]}"
    return s


def _find_colmap_output(write_path: Path) -> Optional[Path]:
    """
    Locate the COLMAP-format reconstruction inside write_path.
    Tries common GlueMap output layouts before falling back to a recursive search.
    Returns the directory containing cameras.bin or cameras.txt, or None.
    """
    for candidate in [
        write_path / "gluemap_aba",  # final output: after augmented BA (confirmed)
        write_path / "refined",      # alternative name (may vary by version)
        write_path / "coarse",       # intermediate: after global BA, before refinement
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


# Stage keyword → (pct, short label).  First match wins (order matters).
_STAGE_HINTS: list[tuple[list[str], int, str]] = [
    (["salad", "retrieval", "building graph", "image graph"],          15, "Retrieval: building image neighbour graph"),
    (["doppelganger", "two-view", "covisibility"],                     30, "Two-view: covisibility estimation"),
    (["star config", "multi-view", "feedforward", "pi3", "vggt",
      "map_anything", "backbone"],                                      45, "Multi-view inference"),
    (["rotation averaging", "global mapping", "bundle adjust",
      "global ba", "gravity align"],                                    70, "Global BA: rotation averaging + bundle adjustment"),
    (["refinement", "track snap", "augmented ba"],                     85, "Refinement: SIFT track snapping"),
    (["writing", "saving colmap", "export"],                           92, "Writing COLMAP output"),
]

_ANSI = re.compile(r"\x1b\[[0-9;]*[mA-Za-z]")


def _parse_stage(line: str) -> Optional[tuple[int, str]]:
    """Return (pct, message) if the line signals a pipeline stage, else None."""
    low = line.lower()
    for keywords, pct, label in _STAGE_HINTS:
        if any(kw in low for kw in keywords):
            return pct, f"{label} — {line.strip()[:120]}"
    return None


_VISUALIZER = str(Path(__file__).parent.parent / "tools" / "visualize_cameras.py")


def _generate_viewer(brush_input_dir: Path, project_dir: Path, pitch_deg: float = 0.0) -> None:
    """Run visualize_cameras.py on brush_input/ to produce cameras.html in the gluemap folder.

    Passes brush_input_dir/"images" (populated by _copy_to_brush_input in
    colmap_runner.py) as the visualizer's images_path so it embeds base64
    thumbnails in the HTML — this call site was previously missing that arg,
    producing a cameras.html with zero embedded images."""
    import sys
    out_html  = project_dir / "03_alignment" / "gluemap" / "cameras.html"
    image_dir = brush_input_dir / "images"
    if not Path(_VISUALIZER).exists():
        print("  [gluemap] visualize_cameras.py not found — skipping viewer", flush=True)
        return
    try:
        args = [sys.executable, _VISUALIZER, str(brush_input_dir), str(out_html), str(pitch_deg), "0.0"]
        if image_dir.exists():
            args += ["pano_camera0", str(image_dir)]
        subprocess.run(
            args,
            check=False,
            timeout=120,
        )
        if out_html.exists():
            print(f"  [gluemap] Camera viewer written: {out_html}", flush=True)
    except Exception as e:
        print(f"  [gluemap] Viewer generation failed (non-fatal): {e}", flush=True)


def _sample_and_write_colored_recon(recon_dir, brush_input_dir, report, stage):
    """Read a COLMAP binary reconstruction, sample RGB from images, write .txt files.

    GlueMap writes all point colors as 0,0,0. Brush initializes Gaussian colors
    from the point cloud, so we project each 3D point back to its first visible
    image and sample the pixel color before handing off to Brush training.
    images/ subfolder is expected at brush_input_dir/images/.
    """
    from pathlib import Path as _P
    import shutil as _shutil
    try:
        import numpy as np
        from PIL import Image as PILImage
        import pycolmap

        recon_dir      = _P(recon_dir)
        brush_input_dir = _P(brush_input_dir)
        images_dir     = brush_input_dir / "images"

        recon = pycolmap.Reconstruction()
        recon.read(str(recon_dir))

        img_cache: dict = {}

        def _get_img(name: str):
            if name not in img_cache:
                p = images_dir / name
                img_cache[name] = np.array(PILImage.open(p).convert("RGB")) if p.exists() else None
            return img_cache[name]

        colored = 0
        for pt in recon.points3D.values():
            for el in pt.track.elements:
                if el.image_id not in recon.images:
                    continue
                img_meta = recon.images[el.image_id]
                arr = _get_img(img_meta.name)
                if arr is None:
                    continue
                kp = img_meta.points2D[el.point2D_idx]
                x = int(np.clip(kp.xy[0], 0, arr.shape[1] - 1))
                y = int(np.clip(kp.xy[1], 0, arr.shape[0] - 1))
                pt.color = arr[y, x]
                colored += 1
                break

        recon.write_text(str(brush_input_dir))
        print(f"  [gluemap] Colored {colored}/{len(recon.points3D)} points, wrote text COLMAP files", flush=True)
        report(stage, 98, f"GlueMap: colored {colored:,} points, wrote text COLMAP files")

    except Exception as e:
        print(f"  [gluemap] Color sampling failed ({e}), copying binary files as fallback", flush=True)
        for f in _P(recon_dir).iterdir():
            _shutil.copy2(str(f), str(_P(brush_input_dir) / f.name))


def run_gluemap_pipeline(
    views_dir: Path,
    colmap_dir: Path,
    brush_input_dir: Path,
    settings: PipelineSettings,
    report: Callable[[PipelineStage, int, str], None],
    cancel_event: threading.Event,
    project_dir: Optional[Path] = None,
) -> None:
    """
    Run GlueMap alignment via WSL2 and populate brush_input_dir.

    Args:
        views_dir:       02_views/ — source for image reorganisation if needed
        colmap_dir:      03_alignment/colmap/ — per-sensor images (built or reused here)
        brush_input_dir: 04_training/brush_input/ — output destination
        settings:        PipelineSettings (gluemap_* fields consumed here)
        report:          progress callback (stage, pct, message)
        cancel_event:    set to abort
    """
    from .colmap_runner import _reorganize_views

    stage = PipelineStage.GLUEMAP_ALIGNMENT
    report(stage, 0, "GlueMap: preparing image directories…")

    # ── 1. Per-sensor image dirs ──────────────────────────────────────────────
    image_dir = colmap_dir / "images"
    sensors_exist = (
        image_dir.exists() and
        any(d.is_dir() and d.name.startswith("pano_camera")
            for d in image_dir.iterdir())
    ) if image_dir.exists() else False

    if sensors_exist:
        n_sensors = sum(
            1 for d in image_dir.iterdir()
            if d.is_dir() and d.name.startswith("pano_camera")
        )
        report(stage, 5, f"GlueMap: {n_sensors} sensor dirs already exist — skipping reorganisation")
    else:
        report(stage, 3, "GlueMap: reorganising views into per-sensor directories…")
        n_sensors = _reorganize_views(views_dir, image_dir)
        if n_sensors == 0:
            raise RuntimeError("No view images found in 02_views/ for GlueMap")
        report(stage, 5, f"GlueMap: reorganised into {n_sensors} sensor dirs")

    if cancel_event.is_set():
        return

    # ── 2. Settings + paths ───────────────────────────────────────────────────
    backbone         = getattr(settings, "gluemap_backbone",             "pi3")
    skip_dg          = getattr(settings, "gluemap_skip_doppelgangers",   True)
    coarse_only      = getattr(settings, "gluemap_coarse_only",          False)
    sequential       = getattr(settings, "gluemap_is_sequential",        True)
    n_neighbors      = getattr(settings, "gluemap_num_neighbors",        100)
    batch_size       = getattr(settings, "gluemap_batch_size",           30)
    num_track        = getattr(settings, "gluemap_num_track_per_img",    1024)
    wsl_home         = getattr(settings, "gluemap_wsl_home",             "/home/decosson")
    wsl_distro       = getattr(settings, "gluemap_wsl_distro",           "Ubuntu-22.04")

    gluemap_home = f"{wsl_home}/gluemap"
    micromamba   = f"{wsl_home}/.local/bin/micromamba"
    checkpoints  = f"{gluemap_home}/checkpoints"

    backbone_ckpt = {
        "pi3":          f"{checkpoints}/pi3.safetensors",
        "pi3x":         f"{checkpoints}/pi3.safetensors",
        "vggt":         f"{checkpoints}/vggt.safetensors",
        "map_anything": f"{checkpoints}/map_anything.safetensors",
    }.get(backbone, f"{checkpoints}/pi3.safetensors")

    gluemap_dir = colmap_dir.parent / "gluemap"
    write_path  = gluemap_dir / "output"
    write_path.mkdir(parents=True, exist_ok=True)

    wsl_images = _win_to_wsl(image_dir)
    wsl_write  = _win_to_wsl(write_path)

    # ── 3. Build WSL command ──────────────────────────────────────────────────
    inner = [
        micromamba, "run", "-n", "gluemap", "gluemap-demo",
        "--images_path",      wsl_images,
        "--write_path",       wsl_write,
        "--intrinsics_mode",  "PER_FOLDER",
        "--chosen_model",     backbone,
        "--path_feedforward", backbone_ckpt,
        "--path_retrieval",   f"{checkpoints}/dino_salad.ckpt",
        "--path_tracker",     f"{checkpoints}/vggsfm_v2_0_0_track_predictor.bin",
        "--path_dg",          f"{checkpoints}/checkpoint-dg+visym.pth",
        "--num_neighbors",      str(n_neighbors),
        "--batch_size",         str(batch_size),
        "--num_track_per_img",  str(num_track),
    ]
    if skip_dg:
        inner.append("--skip_doppelgangers")
    if coarse_only:
        inner.append("--coarse_only")
    if sequential:
        inner.append("--is_sequential")

    wsl_cmd = ["wsl", "-d", wsl_distro, "--"] + inner

    report(stage, 8,
           f"GlueMap: launching {backbone} backbone "
           f"({'sequential' if sequential else 'unordered'}, "
           f"{n_sensors} sensor dirs, skip_dg={skip_dg}, coarse_only={coarse_only})…")
    print(f"  [gluemap] CMD: {' '.join(wsl_cmd)}", flush=True)

    if cancel_event.is_set():
        return

    # ── 4. Run gluemap-demo and stream output ─────────────────────────────────
    process = subprocess.Popen(
        wsl_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )

    current_pct    = 8
    _last_report_t = time.time()
    _FB_INTERVAL   = 3.0

    while True:
        raw = process.stdout.readline()
        if not raw and process.poll() is not None:
            break
        if not raw:
            continue
        line = _ANSI.sub("", raw).rstrip()
        if not line:
            continue

        print(f"  [gluemap] {line}", flush=True)

        parsed = _parse_stage(line)
        if parsed:
            new_pct, stage_msg = parsed
            if new_pct > current_pct:
                current_pct = new_pct
            display_msg = stage_msg
        else:
            display_msg = f"[gluemap] {line[:200]}"

        now = time.time()
        if (now - _last_report_t) >= _FB_INTERVAL:
            report(stage, current_pct, display_msg)
            _last_report_t = now

        if cancel_event.is_set():
            process.terminate()
            return

    process.wait()

    if process.returncode != 0:
        raise RuntimeError(
            f"gluemap-demo exited with code {process.returncode}. "
            "Check the server log for details."
        )

    if cancel_event.is_set():
        return

    # ── 5. Find COLMAP output and copy to brush_input/ ────────────────────────
    report(stage, 93, "GlueMap: locating COLMAP output…")
    recon_dir = _find_colmap_output(write_path)
    if recon_dir is None:
        raise RuntimeError(
            f"gluemap-demo finished (exit 0) but no COLMAP files found under {write_path}. "
            "Check the server log for output errors."
        )

    report(stage, 95, f"GlueMap: copying reconstruction → brush_input/…")
    if brush_input_dir.exists():
        shutil.rmtree(str(brush_input_dir))
    brush_input_dir.mkdir(parents=True, exist_ok=True)

    images_dst = brush_input_dir / "images"
    if not images_dst.exists() and image_dir.exists():
        shutil.copytree(str(image_dir), str(images_dst))

    # ── 6. Sample point colors and write text-format COLMAP files ─────────────
    report(stage, 97, "GlueMap: sampling point colors from images…")
    _sample_and_write_colored_recon(recon_dir, brush_input_dir, report, stage)

    # ── 7. Generate Three.js camera viewer HTML ───────────────────────────────
    report(stage, 99, "GlueMap: generating camera viewer…")
    pitch = settings.pitch_angles[0] if getattr(settings, "pitch_angles", None) else 0.0
    _generate_viewer(brush_input_dir, project_dir, pitch_deg=pitch)

    n_files = len(list(brush_input_dir.glob("*.txt"))) + len(list(brush_input_dir.glob("*.bin")))
    report(stage, 100,
           f"GlueMap alignment complete — {n_files} reconstruction files in brush_input/")
