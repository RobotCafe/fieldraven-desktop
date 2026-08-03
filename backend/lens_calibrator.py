"""
Lens Calibrator — ChArUco detection + fisheye calibration, embedded from the
standalone lens-calibrator tool (C:\\Users\\DenmanNic\\Projects\\lens-calibrator).
Feeds directly into COLMAP's OPENCV_FISHEYE camera model via the
colmap_fisheye pipeline mode.

Mounted into the main FastAPI app under /api/calibrator — see server.py.
"""
import os
import glob
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import cv2
from fastapi import APIRouter, Form, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/calibrator")

CALIBRATION_DIR = Path("C:/FieldRaven/Calibration")
STORAGE_DIR     = CALIBRATION_DIR / "scratch"
PROFILES_DIR    = CALIBRATION_DIR / "profiles"
LIVE_DIR        = CALIBRATION_DIR / "live_sessions"
for _d in (STORAGE_DIR, PROFILES_DIR, LIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Simple in-memory session state — this is a local single-user tool,
# no need for a database. Mirrors the standalone lens-calibrator tool exactly.
STATE = {
    "board": None,
    "image_folder": None,
    "images": [],       # [{path, filename}]
    "detections": {},   # filename -> detection result
    "calibration": None,
    "_K": None,
    "_D": None,
    "live_session": None,
}


class BoardProfile(BaseModel):
    name: str = "default"
    squares_x: int = 10
    squares_y: int = 7
    square_size_mm: float = 30.0
    marker_size_mm: float = 22.0
    dictionary: str = "DICT_5X5_100"


def build_board(board_cfg: dict):
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, board_cfg["dictionary"]))
    board = cv2.aruco.CharucoBoard(
        (board_cfg["squares_x"], board_cfg["squares_y"]),
        board_cfg["square_size_mm"] / 1000.0,
        board_cfg["marker_size_mm"] / 1000.0,
        dictionary,
    )
    return board, dictionary


@router.post("/board")
def set_board(profile: BoardProfile):
    STATE["board"] = profile.dict()
    return {"ok": True, "board": STATE["board"]}


@router.get("/board")
def get_board():
    return STATE["board"] or {}


@router.post("/images/scan")
def scan_images(folder: str = Form(...)):
    exts = ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG")
    files = []
    for e in exts:
        files.extend(glob.glob(os.path.join(folder, e)))
    files = sorted(set(files))

    STATE["image_folder"] = folder
    STATE["images"] = [{"path": f, "filename": os.path.basename(f)} for f in files]
    STATE["detections"] = {}
    STATE["calibration"] = None
    STATE["_K"] = None
    STATE["_D"] = None

    if not files:
        return {"count": 0, "images": [], "error": "No images found in that folder"}
    return {"count": len(files), "images": STATE["images"]}


@router.post("/detect")
def run_detection():
    if not STATE["board"]:
        return {"error": "Set a board profile first"}
    if not STATE["images"]:
        return {"error": "Scan an image folder first"}

    board, dictionary = build_board(STATE["board"])
    detector_params = cv2.aruco.DetectorParameters()
    charuco_params = cv2.aruco.CharucoParameters()
    charuco_detector = cv2.aruco.CharucoDetector(board, charuco_params, detector_params)

    results = []
    for img_info in STATE["images"]:
        img = cv2.imread(img_info["path"])
        if img is None:
            results.append({"filename": img_info["filename"], "success": False, "num_corners": 0})
            continue

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        charuco_corners, charuco_ids, marker_corners, marker_ids = charuco_detector.detectBoard(gray)
        num_corners = 0 if charuco_corners is None else len(charuco_corners)
        success = num_corners >= 8

        overlay = img.copy()
        if marker_corners is not None and len(marker_corners) > 0:
            cv2.aruco.drawDetectedMarkers(overlay, marker_corners, marker_ids)
        if charuco_corners is not None and num_corners > 0:
            cv2.aruco.drawDetectedCornersCharuco(overlay, charuco_corners, charuco_ids, (0, 232, 130))
        overlay_path = STORAGE_DIR / f"overlay_{img_info['filename']}.jpg"
        cv2.imwrite(str(overlay_path), overlay)

        STATE["detections"][img_info["filename"]] = {
            "charuco_corners": charuco_corners.tolist() if charuco_corners is not None else None,
            "charuco_ids": charuco_ids.tolist() if charuco_ids is not None else None,
            "success": success,
            "num_corners": num_corners,
            "excluded": not success,
        }
        results.append({
            "filename": img_info["filename"],
            "success": success,
            "num_corners": num_corners,
            "excluded": not success,
            "overlay_url": f"/api/calibrator/overlay/{img_info['filename']}",
        })

    return {"results": results}


@router.get("/overlay/{filename}")
def get_overlay(filename: str):
    path = STORAGE_DIR / f"overlay_{filename}.jpg"
    if path.exists():
        return FileResponse(str(path))
    return {"error": "not found"}


@router.post("/images/toggle")
def toggle_image(filename: str = Form(...), excluded: bool = Form(...)):
    if filename in STATE["detections"]:
        STATE["detections"][filename]["excluded"] = excluded
        return {"ok": True}
    return {"error": "unknown filename"}


@router.post("/calibrate")
def run_calibration():
    if not STATE["detections"]:
        return {"error": "Run detection first"}

    board, dictionary = build_board(STATE["board"])

    used_files, obj_points_list, img_points_list = [], [], []
    img_size = None

    for fname, det in STATE["detections"].items():
        if det["excluded"] or not det["success"]:
            continue
        corners = np.array(det["charuco_corners"], dtype=np.float32)
        ids = np.array(det["charuco_ids"], dtype=np.int32)
        obj_pts, img_pts = board.matchImagePoints(corners, ids)
        if obj_pts is None or len(obj_pts) < 4:
            continue

        obj_points_list.append(obj_pts.reshape(-1, 1, 3).astype(np.float32))
        img_points_list.append(img_pts.reshape(-1, 1, 2).astype(np.float32))
        used_files.append(fname)

        if img_size is None:
            img_info = next(i for i in STATE["images"] if i["filename"] == fname)
            img = cv2.imread(img_info["path"])
            img_size = (img.shape[1], img.shape[0])

    if len(used_files) < 5:
        return {"error": f"Only {len(used_files)} valid images — need at least 5 to calibrate"}

    K = np.zeros((3, 3))
    D = np.zeros((4, 1))
    flags = (
        cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC
        | cv2.fisheye.CALIB_CHECK_COND
        | cv2.fisheye.CALIB_FIX_SKEW
    )

    try:
        rms, K, D, rvecs, tvecs = cv2.fisheye.calibrate(
            obj_points_list,
            img_points_list,
            img_size,
            K,
            D,
            flags=flags,
            criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-6),
        )
    except cv2.error as e:
        return {"error": f"Calibration failed — likely a bad detection (CALIB_CHECK_COND tripped). Try excluding outlier images. Details: {str(e)}"}

    per_image_errors = []
    for i, fname in enumerate(used_files):
        projected, _ = cv2.fisheye.projectPoints(obj_points_list[i], rvecs[i], tvecs[i], K, D)
        err = float(cv2.norm(img_points_list[i], projected, cv2.NORM_L2) / len(projected))
        per_image_errors.append({"filename": fname, "error": err})

    per_image_errors.sort(key=lambda x: -x["error"])

    result = {
        "fx": float(K[0, 0]), "fy": float(K[1, 1]),
        "cx": float(K[0, 2]), "cy": float(K[1, 2]),
        "k1": float(D[0, 0]), "k2": float(D[1, 0]),
        "k3": float(D[2, 0]), "k4": float(D[3, 0]),
        "image_width": img_size[0], "image_height": img_size[1],
        "overall_rms_error": float(rms),
        "num_images_used": len(used_files),
        "per_image_errors": per_image_errors,
    }
    STATE["calibration"] = result
    STATE["_K"] = K
    STATE["_D"] = D
    return result


@router.post("/undistort")
def undistort_preview(filename: str = Form(...)):
    if STATE["_K"] is None:
        return {"error": "Run calibration first"}
    img_info = next((i for i in STATE["images"] if i["filename"] == filename), None)
    if not img_info:
        return {"error": "Image not found"}

    img = cv2.imread(img_info["path"])
    K, D = STATE["_K"], STATE["_D"]
    h, w = img.shape[:2]

    new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(K, D, (w, h), np.eye(3), balance=0.5)
    map1, map2 = cv2.fisheye.initUndistortRectifyMap(K, D, np.eye(3), new_K, (w, h), cv2.CV_16SC2)
    undistorted = cv2.remap(img, map1, map2, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

    cv2.imwrite(str(STORAGE_DIR / "preview_before.jpg"), img)
    cv2.imwrite(str(STORAGE_DIR / "preview_after.jpg"), undistorted)
    return {"before_url": "/api/calibrator/preview/before", "after_url": "/api/calibrator/preview/after"}


@router.get("/preview/before")
def preview_before():
    return FileResponse(str(STORAGE_DIR / "preview_before.jpg"))


@router.get("/preview/after")
def preview_after():
    return FileResponse(str(STORAGE_DIR / "preview_after.jpg"))


@router.get("/export")
def export_calibration():
    c = STATE["calibration"]
    if not c:
        return {"error": "No calibration run yet"}

    params = f"{c['fx']},{c['fy']},{c['cx']},{c['cy']},{c['k1']},{c['k2']},{c['k3']},{c['k4']}"
    return {
        "camera_model": "OPENCV_FISHEYE",
        "colmap_params_string": params,
        "colmap_feature_extractor_snippet": (
            f'colmap feature_extractor --ImageReader.camera_model OPENCV_FISHEYE '
            f'--ImageReader.camera_params "{params}"'
        ),
        "colmap_mapper_lock_snippet": (
            "colmap mapper --Mapper.ba_refine_focal_length 0 "
            "--Mapper.ba_refine_principal_point 0 --Mapper.ba_refine_extra_params 0"
        ),
        "pycolmap_params_list": [c["fx"], c["fy"], c["cx"], c["cy"], c["k1"], c["k2"], c["k3"], c["k4"]],
        "full_calibration": c,
        "board": STATE["board"],
    }


# ── Named calibration profiles ──────────────────────────────────
# Not part of the standalone tool — lets a saved calibration be referenced
# by name (e.g. "x4_front", "x4_back") from a pipeline job's settings.

class ProfileSave(BaseModel):
    name: str


def _profile_path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()
    if not safe:
        raise ValueError("Invalid profile name")
    return PROFILES_DIR / f"{safe}.json"


@router.post("/profiles/save")
def save_profile(req: ProfileSave):
    if not STATE["calibration"]:
        return {"error": "No calibration run yet"}
    try:
        path = _profile_path(req.name)
    except ValueError as e:
        return {"error": str(e)}

    profile = {
        "name": req.name,
        "created_at": datetime.now().isoformat(),
        "camera_model": "OPENCV_FISHEYE",
        **{k: STATE["calibration"][k] for k in
           ("fx", "fy", "cx", "cy", "k1", "k2", "k3", "k4",
            "image_width", "image_height", "overall_rms_error", "num_images_used")},
        "board": STATE["board"],
    }
    path.write_text(json.dumps(profile, indent=2))
    return {"ok": True, "profile": profile}


@router.get("/profiles")
def list_profiles():
    profiles = []
    for path in sorted(PROFILES_DIR.glob("*.json")):
        try:
            profiles.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return {"profiles": profiles}


@router.get("/profiles/{name}")
def get_profile(name: str):
    try:
        path = _profile_path(name)
    except ValueError as e:
        return {"error": str(e)}
    if not path.exists():
        return {"error": "Profile not found"}
    return json.loads(path.read_text())


@router.delete("/profiles/{name}")
def delete_profile(name: str):
    try:
        path = _profile_path(name)
    except ValueError as e:
        return {"error": str(e)}
    if path.exists():
        path.unlink()
        return {"ok": True}
    return {"error": "Profile not found"}


@router.post("/board/generate")
def generate_board(dpi: int = Form(300)):
    """Renders the current board profile as a print-ready image, sized in real
    physical mm so that printing at 100% scale (no 'fit to page') produces the
    exact square_size_mm you specified."""
    if not STATE["board"]:
        return {"error": "Save a board profile first"}

    board_cfg = STATE["board"]
    board, dictionary = build_board(board_cfg)

    px_per_mm = dpi / 25.4
    width_mm = board_cfg["squares_x"] * board_cfg["square_size_mm"]
    height_mm = board_cfg["squares_y"] * board_cfg["square_size_mm"]
    img_w = int(round(width_mm * px_per_mm))
    img_h = int(round(height_mm * px_per_mm))

    board_gray = board.generateImage((img_w, img_h), marginSize=0, borderBits=1)
    board_bgr = cv2.cvtColor(board_gray, cv2.COLOR_GRAY2BGR)

    margin = int(0.6 * dpi)          # ~0.6in white border around the board
    footer_h = int(1.3 * dpi)        # space for label + verification ruler
    canvas_w = img_w + margin * 2
    canvas_h = img_h + margin * 2 + footer_h
    canvas = np.full((canvas_h, canvas_w, 3), 255, dtype=np.uint8)
    canvas[margin:margin + img_h, margin:margin + img_w] = board_bgr

    label = (
        f"{board_cfg['squares_x']}x{board_cfg['squares_y']} squares  |  "
        f"square {board_cfg['square_size_mm']}mm  |  marker {board_cfg['marker_size_mm']}mm  |  "
        f"{board_cfg['dictionary']}  |  {int(width_mm)}x{int(height_mm)}mm  |  "
        f"PRINT AT 100% / ACTUAL SIZE — DO NOT SCALE TO FIT PAGE"
    )
    cv2.putText(canvas, label, (margin, int(margin * 0.5)),
                cv2.FONT_HERSHEY_SIMPLEX, dpi / 500, (0, 0, 0), max(1, dpi // 200))

    ruler_mm = 50
    ruler_px = int(ruler_mm * px_per_mm)
    ry = margin + img_h + int(footer_h * 0.35)
    rx0 = margin
    tick = int(0.08 * dpi)
    cv2.line(canvas, (rx0, ry), (rx0 + ruler_px, ry), (0, 0, 0), max(2, dpi // 150))
    cv2.line(canvas, (rx0, ry - tick), (rx0, ry + tick), (0, 0, 0), max(2, dpi // 150))
    cv2.line(canvas, (rx0 + ruler_px, ry - tick), (rx0 + ruler_px, ry + tick), (0, 0, 0), max(2, dpi // 150))
    cv2.putText(canvas, f"{ruler_mm}mm reference — measure this line after printing",
                (rx0, ry + int(footer_h * 0.28)),
                cv2.FONT_HERSHEY_SIMPLEX, dpi / 600, (0, 0, 0), max(1, dpi // 250))

    out_path = STORAGE_DIR / "board_print.png"
    cv2.imwrite(str(out_path), canvas)

    return {
        "url": "/api/calibrator/board/download",
        "width_mm": round(width_mm, 1),
        "height_mm": round(height_mm, 1),
        "dpi": dpi,
        "pixel_width": canvas_w,
        "pixel_height": canvas_h,
    }


@router.get("/board/download")
def download_board():
    path = STORAGE_DIR / "board_print.png"
    if not path.exists():
        return {"error": "Generate a board image first"}
    return FileResponse(str(path), media_type="image/png", filename="charuco_board_print.png")


@router.post("/live/session/start")
def start_live_session(lens: str = Form(...)):
    """Begins a live calibration capture session for one lens. Resets zone
    coverage tracking and creates a fresh folder for keeper frames."""
    if not STATE["board"]:
        return {"error": "Save a board profile first"}
    session_dir = LIVE_DIR / lens
    session_dir.mkdir(parents=True, exist_ok=True)
    STATE["live_session"] = {
        "lens": lens,
        "dir": str(session_dir),
        "zones": {f"{r}_{c}": 0 for r in range(3) for c in range(3)},
        "distance": {"near": 0, "medium": 0, "far": 0},
        "tilt": {"flat": 0, "left": 0, "right": 0, "up": 0, "down": 0},
        "keeper_count": 0,
    }
    return {"ok": True, "session_dir": str(session_dir)}


def _decode_upload(file_bytes: bytes):
    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _zone_for_point(x_norm: float, y_norm: float):
    col = min(2, int(x_norm * 3))
    row = min(2, int(y_norm * 3))
    return f"{row}_{col}"


def _distance_tilt_proxy(charuco_corners, frame_w, frame_h):
    """Rough, non-metric proxies for distance and tilt — no camera matrix
    exists yet, so these are directional signals only, not measurements.
    Distance: apparent board size relative to frame diagonal.
    Tilt: keystoning — spacing ratio between opposite edges of the board.
    """
    pts = charuco_corners[:, 0, :]
    x_min, x_max = pts[:, 0].min(), pts[:, 0].max()
    y_min, y_max = pts[:, 1].min(), pts[:, 1].max()
    board_diag = np.hypot(x_max - x_min, y_max - y_min)
    frame_diag = np.hypot(frame_w, frame_h)
    fill_ratio = board_diag / frame_diag

    if fill_ratio > 0.55:
        distance = "near"
    elif fill_ratio > 0.28:
        distance = "medium"
    else:
        distance = "far"

    x_span = x_max - x_min if x_max > x_min else 1.0
    left_mask = pts[:, 0] < (x_min + x_span / 3)
    right_mask = pts[:, 0] > (x_max - x_span / 3)
    top_mask = pts[:, 1] < (y_min + (y_max - y_min) / 3)
    bottom_mask = pts[:, 1] > (y_max - (y_max - y_min) / 3)

    def _span(mask, axis):
        sel = pts[mask, axis]
        return (sel.max() - sel.min()) if sel.size >= 2 else None

    left_h, right_h = _span(left_mask, 1), _span(right_mask, 1)
    top_w, bottom_w = _span(top_mask, 0), _span(bottom_mask, 0)

    tilt = "flat"
    threshold = 0.18  # 18% asymmetry before calling it a tilt, avoids noise
    if left_h and right_h:
        lr_ratio = (right_h - left_h) / max(left_h, right_h)
        if lr_ratio > threshold:
            tilt = "right"
        elif lr_ratio < -threshold:
            tilt = "left"
    if tilt == "flat" and top_w and bottom_w:
        tb_ratio = (bottom_w - top_w) / max(top_w, bottom_w)
        if tb_ratio > threshold:
            tilt = "down"
        elif tb_ratio < -threshold:
            tilt = "up"

    return distance, tilt


@router.post("/live/detect")
async def live_detect(file: UploadFile = File(...)):
    """Fast, low-res detection check for real-time feedback. Not saved."""
    if not STATE["board"]:
        return {"error": "No board profile set"}
    sess = STATE.get("live_session")
    if not sess:
        return {"error": "No live session started"}

    img = _decode_upload(await file.read())
    if img is None:
        return {"success": False, "num_corners": 0}

    board, dictionary = build_board(STATE["board"])
    detector_params = cv2.aruco.DetectorParameters()
    charuco_params = cv2.aruco.CharucoParameters()
    charuco_detector = cv2.aruco.CharucoDetector(board, charuco_params, detector_params)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    charuco_corners, charuco_ids, _, _ = charuco_detector.detectBoard(gray)
    num_corners = 0 if charuco_corners is None else len(charuco_corners)
    success = num_corners >= 8

    zone, distance, tilt = None, None, None
    if success:
        h, w = gray.shape[:2]
        cx = float(np.mean(charuco_corners[:, 0, 0])) / w
        cy = float(np.mean(charuco_corners[:, 0, 1])) / h
        zone = _zone_for_point(cx, cy)
        distance, tilt = _distance_tilt_proxy(charuco_corners, w, h)

    return {
        "success": success,
        "num_corners": num_corners,
        "zone": zone,
        "distance": distance,
        "tilt": tilt,
        "zones": sess["zones"],
        "distance_coverage": sess["distance"],
        "tilt_coverage": sess["tilt"],
        "keeper_count": sess["keeper_count"],
    }


@router.post("/live/capture")
async def live_capture(file: UploadFile = File(...)):
    """Saves a full-res keeper frame into the session folder and updates
    zone/distance/tilt coverage. This is what actually builds the dataset."""
    sess = STATE.get("live_session")
    if not sess:
        return {"error": "No live session started"}

    raw = await file.read()
    img = _decode_upload(raw)
    if img is None:
        return {"error": "Could not decode image"}

    board, dictionary = build_board(STATE["board"])
    detector_params = cv2.aruco.DetectorParameters()
    charuco_params = cv2.aruco.CharucoParameters()
    charuco_detector = cv2.aruco.CharucoDetector(board, charuco_params, detector_params)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    charuco_corners, charuco_ids, _, _ = charuco_detector.detectBoard(gray)
    num_corners = 0 if charuco_corners is None else len(charuco_corners)
    if num_corners < 8:
        return {"saved": False, "reason": "Detection failed on full-res frame", "num_corners": num_corners}

    idx = sess["keeper_count"] + 1
    fname = f"{sess['lens']}_{idx:03d}.jpg"
    path = Path(sess["dir"]) / fname
    path.write_bytes(raw)

    h, w = gray.shape[:2]
    cx = float(np.mean(charuco_corners[:, 0, 0])) / w
    cy = float(np.mean(charuco_corners[:, 0, 1])) / h
    zone = _zone_for_point(cx, cy)
    distance, tilt = _distance_tilt_proxy(charuco_corners, w, h)
    sess["zones"][zone] = sess["zones"].get(zone, 0) + 1
    sess["distance"][distance] = sess["distance"].get(distance, 0) + 1
    sess["tilt"][tilt] = sess["tilt"].get(tilt, 0) + 1
    sess["keeper_count"] = idx

    return {
        "saved": True,
        "filename": fname,
        "zone": zone,
        "distance": distance,
        "tilt": tilt,
        "zones": sess["zones"],
        "distance_coverage": sess["distance"],
        "tilt_coverage": sess["tilt"],
        "keeper_count": sess["keeper_count"],
    }


@router.get("/live/session")
def get_live_session():
    sess = STATE.get("live_session")
    if not sess:
        return {"error": "No live session started"}
    return {
        "lens": sess["lens"],
        "zones": sess["zones"],
        "distance_coverage": sess["distance"],
        "tilt_coverage": sess["tilt"],
        "keeper_count": sess["keeper_count"],
    }


@router.get("/state")
def get_state():
    """Lets the frontend rehydrate on refresh."""
    return {
        "board": STATE["board"],
        "images": STATE["images"],
        "detections": {
            k: {kk: vv for kk, vv in v.items() if kk not in ("charuco_corners", "charuco_ids")}
            for k, v in STATE["detections"].items()
        },
        "calibration": STATE["calibration"],
    }
