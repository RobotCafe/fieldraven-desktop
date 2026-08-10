"""
Derives colmap_fisheye_raw_dir automatically from a job's own raw, un-stitched
Insta360 .insv/.insp file -- see colmap_fisheye_runner.py's module docstring for why
01_frames/ and 02_views/ (both already-stitched-equirectangular-derived) can't be used
for this alignment mode.

Two structurally different raw source kinds (confirmed by direct inspection, not
assumed):
  - ".insp" (still photo): a single ordinary JPEG containing BOTH fisheye lenses
    side by side (confirmed: PIL opens it directly, matching backend/server.py's
    existing EXIF-reading code path for .insp). Split down the middle.
  - ".insv" (video): TWO separate, already single-lens HEVC video streams inside one
    container (confirmed via ffprobe on real recordings: two identical-resolution
    hevc streams, same handler_name tag, no lens-identifying metadata). No split
    needed -- only the FOV crop -- but each lens needs its own `-map 0:N` ffmpeg
    decode. Deliberately NOT routed through video_extraction.extract_frames_for_video()
    (a shared function in a different repo, 3DGS Pipe V13 with VGGT) -- this module
    has its own small, self-contained decode wrapper instead, so nothing outside this
    repo is touched.

Neither source kind's raw file carries metadata saying which physical lens (front vs
back) is which -- settings.colmap_fisheye_raw_swap_lenses exists because of this.

Runs in the main Python 3.13 server process (imported by backend/pipeline_runner.py),
not the Python 3.14 pycolmap worker subprocess.
"""
import json
import shutil
import subprocess
from math import radians
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
from PIL import Image

_IMG_EXTS = {".jpg", ".jpeg", ".png"}
_GEOMETRY_FILENAME = "fisheye_crop_geometry.json"
_NO_WIN = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


# ── Circle detection + crop geometry ──────────────────────────────────────────

def detect_lens_circle(frame_bgr: np.ndarray) -> dict:
    """
    Find the visible fisheye circle in a single-lens tile by thresholding the
    near-black background around it. Runs once per job on one representative
    frame per lens -- camera geometry is fixed for a given recording, no need to
    repeat this per frame.
    """
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY) if frame_bgr.ndim == 3 else frame_bgr
    _, mask = cv2.threshold(gray, 12, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise RuntimeError("No lens circle detected — frame may be entirely black or thresholding failed")
    largest = max(contours, key=cv2.contourArea)
    (cx, cy), radius = cv2.minEnclosingCircle(largest)
    return {"cx": float(cx), "cy": float(cy), "radius_px": float(radius)}


def compute_crop_geometry(circle: dict, tile_w: int, tile_h: int, fov_deg: float,
                           raw_fov_deg: float, profile: Optional[dict]) -> dict:
    """
    Compute the target crop radius + center for one lens, centered on the
    best-known optical center. Returns {"reference", "cx", "cy", "target_radius"}
    -- NOT a finalized crop box yet, since front/back crop dimensions must end up
    identical (both lenses feed one shared image_width/height into
    colmap_fisheye_worker.py's rig) and that reconciliation needs both lenses'
    results first -- see _finalize_crop_box, called by the caller after comparing.

    With a real calibration profile: target radius comes from the profile's own
    fisheye polynomial (theta_d = theta*(1+k1*theta^2+k2*theta^4+k3*theta^6+
    k4*theta^8), radius = fx*theta_d -- the same equidistant/Kannala-Brandt-style
    model cv2.fisheye.calibrate itself uses), centered on the profile's own
    (cx, cy) -- the calibrated optical center is authoritative, not the
    independently detected circle center. Falls back to the detected-circle path
    (with a warning) if the profile's own image_width/image_height don't match
    this tile's actual size, since cx/cy/fx would not be valid in a mismatched
    coordinate frame.

    Without a profile (self-calibrate path): scales the detected circle's own
    radius by fov_deg/raw_fov_deg, centered on the detected circle center.
    """
    if profile is not None:
        prof_w, prof_h = profile.get("image_width"), profile.get("image_height")
        size_matches = not (prof_w and prof_h) or (int(prof_w) == tile_w and int(prof_h) == tile_h)
        if not size_matches:
            print(f"⚠️  Calibration profile image size ({prof_w}x{prof_h}) does not match "
                  f"raw tile size ({tile_w}x{tile_h}) — falling back to detected-circle crop "
                  f"for this lens instead of the (now coordinate-mismatched) profile cx/cy.")
        else:
            theta = radians(fov_deg / 2.0)
            k1, k2 = float(profile.get("k1", 0.0)), float(profile.get("k2", 0.0))
            k3, k4 = float(profile.get("k3", 0.0)), float(profile.get("k4", 0.0))
            theta_d = theta * (1 + k1 * theta**2 + k2 * theta**4 + k3 * theta**6 + k4 * theta**8)
            target_radius = abs(float(profile["fx"])) * theta_d
            return {"reference": "calibration", "cx": float(profile["cx"]), "cy": float(profile["cy"]),
                    "target_radius": target_radius}

    target_radius = circle["radius_px"] * (fov_deg / raw_fov_deg)
    return {"reference": "detected", "cx": circle["cx"], "cy": circle["cy"], "target_radius": target_radius}


def _finalize_crop_box(cx: float, cy: float, radius: float, tile_w: int, tile_h: int) -> dict:
    size = int(2 * radius)
    size = max(2, min(size, tile_w, tile_h))
    x0 = max(0, min(int(round(cx - size / 2.0)), tile_w - size))
    y0 = max(0, min(int(round(cy - size / 2.0)), tile_h - size))
    return {"crop_x0": x0, "crop_y0": y0, "crop_w": size, "crop_h": size}


def _first_frame(dir_: Path) -> Path:
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        matches = sorted(dir_.glob(ext))
        if matches:
            return matches[0]
    raise RuntimeError(f"No decoded frames found in {dir_}")


def _plan_lens_crop(src_dir: Path, fov_deg: float, raw_fov_deg: float,
                     profile: Optional[dict]) -> tuple:
    sample_path = _first_frame(src_dir)
    frame = cv2.imread(str(sample_path))
    if frame is None:
        raise RuntimeError(f"Could not read sample frame {sample_path}")
    tile_h, tile_w = frame.shape[:2]
    circle = detect_lens_circle(frame)
    geom = compute_crop_geometry(circle, tile_w, tile_h, fov_deg, raw_fov_deg, profile)
    return geom, tile_w, tile_h


def _apply_crop_to_all_frames(src_dir: Path, dst_dir: Path, crop: dict,
                               cancel_event, progress_cb: Callable[[int, str], None], label: str) -> None:
    files = sorted(p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in _IMG_EXTS)
    total = len(files)
    if total == 0:
        raise RuntimeError(f"No decoded {label} frames found in {src_dir}")
    box = (crop["crop_x0"], crop["crop_y0"],
           crop["crop_x0"] + crop["crop_w"], crop["crop_y0"] + crop["crop_h"])
    for i, f in enumerate(files):
        if cancel_event.is_set():
            return
        with Image.open(f) as img:
            img.convert("RGB").crop(box).save(dst_dir / f.name, quality=95)
        if i % 20 == 0 or i == total - 1:
            progress_cb(int((i + 1) / total * 100), f"Cropping {label} frames ({i + 1}/{total})")


# ── .insp (single side-by-side JPEG) ──────────────────────────────────────────

def _split_insp_files(insp_paths: list, left_dir: Path, right_dir: Path,
                       cancel_event, progress_cb: Callable[[int, str], None]) -> None:
    total = len(insp_paths)
    if total == 0:
        raise RuntimeError("No .insp files given to split")
    for i, insp_path in enumerate(insp_paths):
        if cancel_event.is_set():
            return
        with Image.open(insp_path) as img:
            img = img.convert("RGB")
            w, h = img.size
            half = w // 2
            img.crop((0, 0, half, h)).save(left_dir / f"{insp_path.stem}.jpg", quality=95)
            img.crop((half, 0, w, h)).save(right_dir / f"{insp_path.stem}.jpg", quality=95)
        progress_cb(int((i + 1) / total * 100), f"Split {insp_path.name} ({i + 1}/{total})")


# ── .insv (two independent single-lens HEVC streams) ──────────────────────────

def _resolve_ffprobe(ffmpeg_path: str) -> str:
    if ffmpeg_path and ffmpeg_path != "ffmpeg":
        p = Path(ffmpeg_path)
        candidate = p.parent / p.name.lower().replace("ffmpeg", "ffprobe")
        if candidate.exists():
            return str(candidate)
    return "ffprobe"


def _probe_duration(path: str, ffmpeg_path: str) -> float:
    ffprobe = _resolve_ffprobe(ffmpeg_path)
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, creationflags=_NO_WIN,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        raise RuntimeError(f"Could not determine duration of {path}: {result.stderr}")


def _build_ffmpeg_filter_args(extraction_settings: dict, duration: float) -> list:
    """Mirrors video_extraction.extract_frames_for_video()'s sampling-rate math at a
    much smaller scope (no GPU-decoder probing) -- kept deliberately self-contained
    in this repo rather than calling into that shared, cross-repo function."""
    method = extraction_settings.get("extraction_method", "interval")
    if method == "count":
        count = max(1, int(extraction_settings.get("frame_count", 30) or 30))
        safe_duration = max(0.1, duration - 0.2)
        rate = (1.0 / safe_duration) if count == 1 else ((count - 1) / safe_duration)
        return ["-vf", f"fps={rate:.6f}"]
    interval_unit = extraction_settings.get("interval_unit", "seconds")
    if interval_unit == "frames":
        step = max(1, int(extraction_settings.get("interval_value", 1) or 1))
        return ["-vf", f"select='not(mod(n\\,{step}))'", "-vsync", "vfr"]
    interval_value = max(0.001, float(extraction_settings.get("interval_value", 1.0) or 1.0))
    return ["-vf", f"fps={1.0 / interval_value:.6f}"]


def _decode_insv_stream(insv_path: Path, stream_index: int, out_dir: Path,
                         extraction_settings: dict, cancel_event,
                         progress_cb: Callable[[int, str], None], label: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_path = extraction_settings.get("ffmpeg_path") or "ffmpeg"
    duration = _probe_duration(str(insv_path), ffmpeg_path)
    filter_args = _build_ffmpeg_filter_args(extraction_settings, duration)
    output_pattern = str(out_dir / "frame_%06d.jpg")
    cmd = ([ffmpeg_path, "-y", "-i", str(insv_path), "-map", f"0:{stream_index}"]
           + filter_args + ["-q:v", "2", "-loglevel", "error", output_pattern])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, creationflags=_NO_WIN)
    for _ in proc.stdout:
        if cancel_event.is_set():
            proc.terminate()
            return
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed decoding raw .insv stream {stream_index} ({label})")
    progress_cb(100, f"Decoded raw .insv stream {stream_index} ({label})")


# ── Public entry points ────────────────────────────────────────────────────────

def ensure_fisheye_raw_frames(
    raw_sources: list, source_kind: str, out_dir: Path,
    fov_deg: float, raw_fov_deg: float, swap_lenses: bool,
    front_profile: Optional[dict], back_profile: Optional[dict],
    extraction_settings: dict, cancel_event, progress_cb: Callable[[int, str], None],
) -> Path:
    """
    Idempotent: skips entirely if front//back/ are already populated AND a
    fisheye_crop_geometry.json sidecar's recorded settings match the current
    settings (mirrors colmap_fisheye_runner.py's _copy_raw_fisheye_frames pattern,
    plus a settings-match check so changing colmap_fisheye_fov_deg between runs
    doesn't silently serve a stale crop).
    """
    out_dir = Path(out_dir)
    front_dir, back_dir = out_dir / "front", out_dir / "back"
    geometry_path = out_dir / _GEOMETRY_FILENAME
    current_key = {"source_kind": source_kind, "fov_deg": fov_deg,
                   "raw_fov_deg": raw_fov_deg, "swap_lenses": swap_lenses}

    if (front_dir.exists() and back_dir.exists() and geometry_path.exists()
            and any(front_dir.glob("*.jpg")) and any(back_dir.glob("*.jpg"))):
        try:
            existing = json.loads(geometry_path.read_text(encoding="utf-8"))
            if existing.get("settings") == current_key:
                progress_cb(100, "Raw fisheye frames already extracted — skipping")
                return out_dir
        except Exception:
            pass

    out_dir.mkdir(parents=True, exist_ok=True)
    for d in (front_dir, back_dir):
        if d.exists():
            shutil.rmtree(str(d))
        d.mkdir(parents=True, exist_ok=True)

    scratch_dir = out_dir / "_raw_decoded"
    if scratch_dir.exists():
        shutil.rmtree(str(scratch_dir))
    scratch_dir.mkdir(parents=True, exist_ok=True)

    try:
        if source_kind == "insv":
            insv_path = Path(raw_sources[0])
            stream0_dir, stream1_dir = scratch_dir / "stream0", scratch_dir / "stream1"
            progress_cb(5, "Decoding raw .insv lens stream 0…")
            _decode_insv_stream(insv_path, 0, stream0_dir, extraction_settings, cancel_event, progress_cb, "stream 0")
            if cancel_event.is_set(): return out_dir
            progress_cb(35, "Decoding raw .insv lens stream 1…")
            _decode_insv_stream(insv_path, 1, stream1_dir, extraction_settings, cancel_event, progress_cb, "stream 1")
            left_dir, right_dir = stream0_dir, stream1_dir
        elif source_kind == "insp":
            left_dir, right_dir = scratch_dir / "left", scratch_dir / "right"
            left_dir.mkdir(parents=True, exist_ok=True)
            right_dir.mkdir(parents=True, exist_ok=True)
            progress_cb(5, f"Splitting {len(raw_sources)} raw .insp file(s)…")
            _split_insp_files([Path(p) for p in raw_sources], left_dir, right_dir, cancel_event, progress_cb)
        else:
            raise ValueError(f"Unknown fisheye raw source kind: {source_kind!r}")

        if cancel_event.is_set():
            return out_dir

        front_src, back_src = (right_dir, left_dir) if swap_lenses else (left_dir, right_dir)

        progress_cb(65, "Detecting lens geometry…")
        front_geom, tile_w, tile_h = _plan_lens_crop(front_src, fov_deg, raw_fov_deg, front_profile)
        back_geom, tile_w2, tile_h2 = _plan_lens_crop(back_src, fov_deg, raw_fov_deg, back_profile)
        if (tile_w, tile_h) != (tile_w2, tile_h2):
            raise RuntimeError(
                f"Front/back raw tile sizes differ ({tile_w}x{tile_h} vs {tile_w2}x{tile_h2}) — "
                f"cannot build a shared-size rig from mismatched sensor decodes."
            )

        # Front and back crop dimensions must end up identical -- both lenses feed
        # one shared image_width/height into colmap_fisheye_worker.py's rig.
        shared_radius = min(front_geom["target_radius"], back_geom["target_radius"])
        front_crop = _finalize_crop_box(front_geom["cx"], front_geom["cy"], shared_radius, tile_w, tile_h)
        back_crop = _finalize_crop_box(back_geom["cx"], back_geom["cy"], shared_radius, tile_w, tile_h)
        front_crop["reference"] = front_geom["reference"]
        back_crop["reference"] = back_geom["reference"]

        progress_cb(75, "Cropping front-lens frames…")
        _apply_crop_to_all_frames(front_src, front_dir, front_crop, cancel_event, progress_cb, "front")
        if cancel_event.is_set(): return out_dir
        progress_cb(90, "Cropping back-lens frames…")
        _apply_crop_to_all_frames(back_src, back_dir, back_crop, cancel_event, progress_cb, "back")
        if cancel_event.is_set(): return out_dir

        geometry_path.write_text(json.dumps({
            "settings": current_key, "tile_w": tile_w, "tile_h": tile_h,
            "front": front_crop, "back": back_crop,
        }, indent=2), encoding="utf-8")

        progress_cb(100, "Raw fisheye frame extraction complete")
        return out_dir
    finally:
        shutil.rmtree(str(scratch_dir), ignore_errors=True)


def load_crop_geometry(raw_dir) -> Optional[dict]:
    """Returns None for any folder not produced by ensure_fisheye_raw_frames()
    (manually-pointed folders, mobile live-capture output) -- this is the mechanism
    that keeps full backward compatibility with the pre-existing manual-folder path."""
    p = Path(raw_dir) / _GEOMETRY_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
