"""
Re-extract perspective views from source panoramas using the FIXED
panorama_processing.py (focal-length bug fix -- see RIG_PIPELINE_PROCESS.md
"Open issue" section). Writes to a fresh 02_views_fixed/ directory, leaving
the original (buggy) 02_views/ untouched for comparison.

Runs under whatever Python has cv2/numpy/PIL available for the
3DGS Pipe V13 with VGGT App.
"""
import sys
import threading
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, r"C:/Users/DenmanNic/Projects/3DGS Pipe V13 with VGGT/App")

from panorama_processing import render_views

SRC_DIR = Path(r"C:/Users/DenmanNic/Desktop/nile creek/import from camera")
OUT_DIR = Path(r"C:/Users/DenmanNic/Desktop/nile creek/02_views_fixed")
OUT_DIR.mkdir(parents=True, exist_ok=True)

FOV_DEG    = 94.6
YAW_STEPS  = 6
PITCH_ANGLES = [-10.0]
HORIZON_REF  = True

cancel_event = threading.Event()
panos = sorted(SRC_DIR.glob("*.jpg"))
print(f"Found {len(panos)} source panoramas.")

for i, pano_path in enumerate(panos):
    print(f"[{i+1}/{len(panos)}] {pano_path.name}")
    render_views(
        str(pano_path), str(OUT_DIR),
        fov_deg=FOV_DEG, yaw_steps=YAW_STEPS, pitch_angles=PITCH_ANGLES,
        export_xmp=False, save_images=True, cancel_event=cancel_event,
        horizon_ref=HORIZON_REF,
    )

print("DONE.")
