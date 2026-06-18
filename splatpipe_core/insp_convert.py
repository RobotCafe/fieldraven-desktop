"""
INSP → equirectangular JPEG converter.

Insta360 .insp files are dual-fisheye JPEGs: two square lens images stored
side-by-side in a single JPEG. This module:
  1. Splits each .insp into a left and right fisheye image (via Pillow)
  2. Stitches them into an equirectangular panorama via the fusion2sphere binary

fusion2sphere: https://github.com/dorthrithil/fusion2sphere  (Windows build available)
Lens size (px per side): 3040 for ONE X / ONE X2 / ONE R 360-mod
"""
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Optional

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

DEFAULT_FUSION2SPHERE = r"C:\Users\DenmanNic\Projects\3DGS Pipe V13 with VGGT\insp_fusion2sphere\fusion2sphere.exe"

# Insta360 ONE X / ONE X2 / ONE R 360 per-lens resolution
DEFAULT_LENS_SIZE = 3040

# Standard equirectangular output width (2:1 aspect → 5760×2880)
DEFAULT_OUTPUT_WIDTH = 5760


# ── Public API ─────────────────────────────────────────────────

def is_available(fusion_binary: str = DEFAULT_FUSION2SPHERE) -> bool:
    """Return True if the fusion2sphere binary can be executed."""
    try:
        subprocess.run(
            [fusion_binary], capture_output=True, timeout=5
        )
        return True
    except (FileNotFoundError, PermissionError):
        return False
    except subprocess.TimeoutExpired:
        return True  # Ran but hung waiting for args — binary exists


def find_insp_files(directory: str) -> List[Path]:
    """Return sorted list of .insp/.INSP files in directory."""
    d = Path(directory)
    files = sorted(list(d.glob("*.insp")) + list(d.glob("*.INSP")))
    return files


def convert_folder(
    input_dir: str,
    output_dir: str,
    fusion_binary: str = DEFAULT_FUSION2SPHERE,
    lens_size: int = DEFAULT_LENS_SIZE,
    output_width: int = DEFAULT_OUTPUT_WIDTH,
    blend_radius: int = 10,
    cancel_event: Optional[threading.Event] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> List[str]:
    """
    Convert all .insp files in input_dir to equirectangular JPEGs in output_dir.

    Args:
        input_dir:         Source directory containing .insp files.
        output_dir:        Destination directory for equirectangular JPEGs.
        fusion_binary:     Full path to the fusion2sphere executable.
        lens_size:         Width/height of each fisheye lens in the INSP file.
        output_width:      Width of the equirectangular output (height = width/2).
        blend_radius:      Seam-blend radius passed to fusion2sphere.
        cancel_event:      Set to abort processing mid-batch.
        progress_callback: Called as (done, total, message) for each file.

    Returns:
        List of successfully created output file paths.
    """
    if not PIL_AVAILABLE:
        raise ImportError(
            "Pillow is required for INSP conversion. "
            "Run: pip install Pillow"
        )

    insp_files = find_insp_files(input_dir)
    if not insp_files:
        return []

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    outputs: List[str] = []

    with tempfile.TemporaryDirectory(prefix="fieldraven_insp_") as tmp:
        for i, insp_path in enumerate(insp_files):
            if cancel_event and cancel_event.is_set():
                break

            base = insp_path.stem
            left_tmp  = os.path.join(tmp, f"{base}_L.jpg")
            right_tmp = os.path.join(tmp, f"{base}_R.jpg")
            out_path  = str(Path(output_dir) / f"{base}.jpg")

            if progress_callback:
                progress_callback(i, len(insp_files), f"Converting {insp_path.name}…")

            try:
                _split_insp(str(insp_path), left_tmp, right_tmp, lens_size)
                ok = _stitch(
                    left_tmp, right_tmp, out_path,
                    fusion_binary, output_width, blend_radius,
                )
                if ok:
                    outputs.append(out_path)
                    print(f"  ✅ {insp_path.name} → {base}.jpg")
                else:
                    print(f"  ⚠️ fusion2sphere failed for {insp_path.name}")
            except Exception as exc:
                print(f"  ⚠️ INSP error for {insp_path.name}: {exc}")

    if progress_callback and insp_files:
        progress_callback(len(insp_files), len(insp_files), "INSP conversion complete")

    return outputs


# ── Internal helpers ───────────────────────────────────────────

def _split_insp(insp_path: str, left_out: str, right_out: str, lens_size: int):
    """
    Open an .insp and crop it into two fisheye lens images.

    Insta360 stores lenses as [LEFT | RIGHT] in a single JPEG.
    The left lens is rotated 90° CW and the right 90° CCW to correct orientation.
    """
    with Image.open(insp_path) as img:
        left  = img.crop((0,          0, lens_size,     lens_size)).rotate(-90, expand=True)
        right = img.crop((lens_size,  0, lens_size * 2, lens_size)).rotate( 90, expand=True)
        left.save(left_out,  "JPEG", quality=95)
        right.save(right_out, "JPEG", quality=95)


def _stitch(
    left_path: str,
    right_path: str,
    output_path: str,
    fusion_binary: str,
    output_width: int,
    blend_radius: int,
) -> bool:
    """Run fusion2sphere to produce an equirectangular JPEG. Returns True on success."""
    cmd = [
        fusion_binary,
        "-b", str(blend_radius),
        "-w", str(output_width),
        left_path,
        right_path,
        "-o", output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"    fusion2sphere stderr: {result.stderr.strip()[:200]}")
        return result.returncode == 0 and Path(output_path).exists()
    except subprocess.TimeoutExpired:
        print("    fusion2sphere timed out")
        return False
