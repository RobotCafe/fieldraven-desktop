"""
End-to-end test driver: runs the REAL production colmap_runner._run_perspective_rig
against a fresh copy of already-extracted view images, exactly as the live
pipeline would, for visual inspection in COLMAP GUI afterward.

Runs under Python 3.13 (matches production server process — colmap_runner.py
itself spawns the Python 3.14 / pycolmap worker as a subprocess, same as live).
"""
import sys
import threading
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent))

from splatpipe_core.colmap_runner import _run_perspective_rig
from splatpipe_core.settings import PipelineSettings

TEST_ROOT = Path(r"C:/Users/DenmanNic/Desktop/nile_creek_focal_fix_run")
colmap_dir      = TEST_ROOT / "colmap_dir"
brush_input_dir = TEST_ROOT / "brush_input"
views_dir       = Path(r"C:/Users/DenmanNic/Desktop/nile creek/02_views_fixed")  # focal-fix re-extraction

settings = PipelineSettings()
settings.yaw_steps   = 6
settings.pitch_angles = [-10.0]
settings.fov          = 94.6
settings.horizon_ref  = True
settings.colmap_matcher = "sequential"
settings.colmap_bin = r"C:\Users\DenmanNic\Projects\windows colmap CUDA\bin\colmap.exe"
settings.colmap_correct_pitch       = True  # now applies the single-global-rotation leveling fix

def report(stage, pct, msg):
    print(f"[{pct:>3}] {msg}", flush=True)

cancel_event = threading.Event()

print(f"Running _run_perspective_rig against {colmap_dir} ...", flush=True)
_run_perspective_rig(
    views_dir=views_dir,
    colmap_dir=colmap_dir,
    brush_input_dir=brush_input_dir,
    settings=settings,
    report=report,
    cancel_event=cancel_event,
    project_dir=None,
)
print("DONE.", flush=True)
print(f"Raw (pre-correction) binary reconstruction: {colmap_dir / 'sparse' / '0'}")
print(f"Corrected text reconstruction:               {colmap_dir / 'sparse_txt'}")
print(f"Database:                                    {colmap_dir / 'database.db'}")
print(f"Images:                                       {colmap_dir / 'images'}")
