"""
Read-only access to saved lens calibration profiles for pipeline workers.

Profiles are written by backend/lens_calibrator.py's /api/calibrator/profiles/save
endpoint into C:/FieldRaven/Calibration/profiles/<name>.json. This module reads
that same on-disk location directly rather than importing from backend/ --
splatpipe_core is pipeline logic and shouldn't depend on FastAPI/server internals,
and colmap_fisheye_worker.py runs under a different Python interpreter entirely.
"""
import json
from pathlib import Path
from typing import Optional

PROFILES_DIR = Path("C:/FieldRaven/Calibration/profiles")


def load_profile(name: str) -> Optional[dict]:
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()
    if not safe:
        return None
    path = PROFILES_DIR / f"{safe}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
