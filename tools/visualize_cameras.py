#!/usr/bin/env python3
"""
Generate a standalone Three.js HTML visualising COLMAP camera frustums with
photo textures — similar to Spark viewer's "show photos" mode.

Usage:
    python tools/visualize_cameras.py <sparse_or_brush_input_dir> [output.html]
"""

import sys
import json
import base64
import io
from collections import defaultdict
from pathlib import Path

import numpy as np


def _c(v):
    """COLMAP world (Y-down, Z-fwd) → Three.js world (Y-up, Z-toward-viewer)."""
    return [float(v[0]), float(-v[1]), float(-v[2])]


def _scene_scale(cameras_data):
    if len(cameras_data) < 2:
        return 0.1
    centers = np.array([c["center"] for c in cameras_data])
    span = np.max(centers, axis=0) - np.min(centers, axis=0)
    return float(np.linalg.norm(span)) * 0.06


def _thumb_b64(path: Path, max_px: int = 384):
    if not path.exists():
        return None
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=78)
            return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _quat_to_mat(qw, qx, qy, qz):
    """Quaternion (w,x,y,z) → 3×3 rotation matrix (Hamilton convention, matches COLMAP text format)."""
    return np.array([
        [1-2*(qy*qy+qz*qz), 2*(qx*qy-qw*qz),   2*(qx*qz+qw*qy)],
        [2*(qx*qy+qw*qz),   1-2*(qx*qx+qz*qz), 2*(qy*qz-qw*qx)],
        [2*(qx*qz-qw*qy),   2*(qy*qz+qw*qx),   1-2*(qx*qx+qy*qy)],
    ], dtype=np.float64)


def _parse_images_txt(rec_dir: Path) -> dict:
    """Parse images.txt directly → {image_id: (R_3x3, t_3, camera_id, name)}.

    Reads quaternions using our own convention (Hamilton, matching _quat_to_mat)
    to avoid any pycolmap cam_from_world() convention differences.
    """
    result = {}
    images_txt = rec_dir / "images.txt"
    if not images_txt.exists():
        return result
    data_line = False
    for raw in images_txt.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        # Comment lines never count toward the pose/points2D pair alternation,
        # but a genuinely BLANK points2D line still must -- some writers
        # (e.g. equi_sfm_runner.py's _write_sensor_sparse_txt, which has no
        # per-sensor track data) emit an empty second line for every image.
        # Treating "blank" the same as "comment" here desyncs the toggle
        # permanently after the first such image, silently dropping every
        # other image from then on (confirmed: exactly half of a 312-image
        # sensor_sparse_txt was lost this way, in a clean alternating pattern).
        if line.startswith("#"):
            continue
        if not data_line:
            if line:
                parts = line.split()
                if len(parts) >= 10:
                    img_id = int(parts[0])
                    qw, qx, qy, qz = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                    tx, ty, tz     = float(parts[5]), float(parts[6]), float(parts[7])
                    cam_id = int(parts[8])
                    name   = parts[9]
                    R = _quat_to_mat(qw, qx, qy, qz)
                    t = np.array([tx, ty, tz], dtype=np.float64)
                    result[img_id] = (R, t, cam_id, name)
            data_line = True
        else:
            data_line = False
    return result


def extract(rec_dir: Path, images_path: Path = None):
    import pycolmap

    rec = pycolmap.Reconstruction()
    if (rec_dir / "cameras.txt").exists():
        rec.read_text(str(rec_dir))
    else:
        rec.read(str(rec_dir))

    # Parse image poses directly from images.txt to avoid pycolmap quaternion
    # convention differences with cam_from_world().rotation.matrix().
    img_poses = _parse_images_txt(rec_dir)

    # Diagnostic: anchor pitch via direct text parse (should be ~0° after correction)
    for img_id, (R, t, cam_id, name) in sorted(img_poses.items()):
        if "pano_camera0" in name:
            fwd   = R.T[:, 2]
            pitch = np.degrees(np.arcsin(np.clip(fwd[1], -1, 1)))
            print(f"DIAG anchor cam pitch (direct parse): {pitch:.3f}°", flush=True)
            break

    if images_path and Path(images_path).exists():
        images_root = Path(images_path)
    else:
        images_root = rec_dir / "images"
        if not images_root.exists():
            images_root = rec_dir.parent / "images"
        if not images_root.exists():
            images_root = rec_dir.parent.parent / "images"
    print(f"  [visualizer] images_root: {images_root} (exists={images_root.exists()})", flush=True)

    cameras_data = []
    for img_id, (R, t, cam_id, name) in sorted(img_poses.items()):
        if cam_id not in rec.cameras:
            continue
        cam = rec.cameras[cam_id]

        right   =  R.T[:, 0]
        down    =  R.T[:, 1]
        forward =  R.T[:, 2]
        center  = -R.T @ t

        frame_key = Path(name).stem

        cameras_data.append({
            "id":        img_id,
            "name":      name,
            "frame_key": frame_key,
            "sensor":    Path(name).parent.name,   # e.g. "pano_camera2"
            "center":    _c(center),
            "right":     _c(right),
            "up":        _c(-down),
            "forward":   _c(forward),
            "focal":     float(cam.focal_length),
            "width":     int(cam.width),
            "height":    int(cam.height),
            "image":     _thumb_b64(images_root / name),
        })

    pts_data = []
    for pt in rec.points3D.values():
        pts_data.append({
            "xyz": _c(pt.xyz),
            "rgb": [int(c) for c in pt.color],
        })

    # Per-frame rig optical-center spread
    _frame_groups = defaultdict(list)
    for c in cameras_data:
        _frame_groups[c["frame_key"]].append(c)

    spread_data = {}
    for fk, cams in _frame_groups.items():
        _ctrs = np.array([c["center"] for c in cams])
        _cen  = _ctrs.mean(axis=0)
        _dsts = np.linalg.norm(_ctrs - _cen, axis=1)
        spread_data[fk] = {
            "centroid": _cen.tolist(),
            "sensors":  {c["sensor"]: float(d) for c, d in zip(cams, _dsts)},
            "mean":     float(_dsts.mean()),
            "max":      float(_dsts.max()),
        }

    # Load Pi3 quad crop poses if present (quad_anchors mode)
    quad_poses = []
    quad_json = rec_dir.parent / "pi3_quad_poses.json"
    if quad_json.exists():
        raw_qp = json.loads(quad_json.read_text(encoding="utf-8"))
        for r in raw_qp:
            quad_poses.append({
                "station":  r["station"],
                "h_idx":    r["h_idx"],
                "yaw_deg":  r["yaw_deg"],
                "center":   _c(r["center"]),
                "forward":  _c(r["forward"]),
            })
        print(f"  [visualizer] Loaded {len(quad_poses)} Pi3 quad crop poses", flush=True)

    return cameras_data, pts_data, spread_data, quad_poses


def build_html(cameras: list, points: list, pitch_deg: float = -10.0, correction_deg: float = 0.0, anchor_sensor: str = "pano_camera7", spread: dict = None, quad_poses: list = None) -> str:
    depth          = _scene_scale(cameras)
    n              = len(cameras)
    cams_json      = json.dumps(cameras)
    pts_json       = json.dumps(points)
    quad_poses_json = json.dumps(quad_poses or [])

    # Anchor mode — sensor picker options
    sensors = sorted(
        set(c["sensor"] for c in cameras),
        key=lambda s: int(''.join(ch for ch in s if ch.isdigit()) or 0),
    )
    anchor_opts = "\n".join(
        f'<option value="{s}"{" selected" if s == anchor_sensor else ""}>{s}</option>'
        for s in sensors
    )
    # Number of non-anchor sibling sensors (reveal slider max)
    n_non_anchor_sensors = len(set(c["sensor"] for c in cameras if c["sensor"] != anchor_sensor)) or 1

    # Spread stats
    spread_json = json.dumps(spread or {})
    if spread:
        _all_maxes = [v["max"] for v in spread.values()]
        _all_means = [v["mean"] for v in spread.values()]
        _omax  = max(_all_maxes)
        _omean = sum(_all_means) / len(_all_means)
        spread_stats_html = (
            f'<div class="dim" style="margin-top:3px" title="optical-center spread from rig centroid (scene units)">'
            f'spread &nbsp;μ {_omean:.5f} &nbsp;·&nbsp; max {_omax:.5f}</div>'
        )
    else:
        spread_stats_html = ''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Camera Alignment</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ width: 100%; height: 100%; overflow: hidden; background: #0d0d0f; }}

/* ── panels ─────────────────────────────────────────────────────────────── */
#ui-wrap {{
  position: fixed; top: 12px; left: 12px;
  display: flex; gap: 8px; align-items: flex-start;
  pointer-events: none;
}}
#controls, #viewer {{
  pointer-events: all;
  background: rgba(10,10,14,.88);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  border: 1px solid rgba(255,255,255,.09);
  border-radius: 10px;
  font: 12px/1.5 'SF Mono', 'Fira Code', ui-monospace, monospace;
  color: #c4c4cc;
}}
#controls {{ padding: 12px 14px; min-width: 190px; }}
#viewer   {{ padding: 10px; width: 230px; display: none; flex-direction: column; gap: 8px; }}

/* ── control rows ────────────────────────────────────────────────────────── */
.row  {{ display: flex; align-items: center; gap: 6px; margin: 3px 0; }}
label {{ display: flex; align-items: center; gap: 6px; cursor: pointer; }}
input[type=checkbox] {{ accent-color: #6e82ff; width:13px; height:13px; flex-shrink:0; }}
input[type=range]    {{ flex: 1; accent-color: #6e82ff; cursor: pointer; height: 4px; }}
.val  {{ color: #6e82ff; font-size: 11px; min-width: 32px; text-align: right; }}
.sep  {{ border-top: 1px solid rgba(255,255,255,.07); margin: 8px 0; }}
.dim  {{ color: #484858; font-size: 10px; }}

/* ── image viewer ────────────────────────────────────────────────────────── */
#preview-wrap {{
  position: relative; width: 100%; aspect-ratio: 1;
  background: #111; border-radius: 6px; overflow: hidden;
}}
#preview-img {{
  width: 100%; height: 100%; object-fit: cover; display: block;
  border-radius: 6px;
}}
#no-img {{
  position: absolute; inset: 0; display: flex; align-items: center;
  justify-content: center; color: #333; font-size: 11px;
}}
.cam-meta {{ font-size: 11px; color: #888; }}
.cam-name {{ color: #c4c4cc; font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.nav-row  {{ display: flex; align-items: center; gap: 6px; }}
.nav-row button {{
  background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.1);
  color: #aaa; border-radius: 5px; padding: 3px 8px;
  font-size: 13px; cursor: pointer; flex-shrink: 0;
  transition: background .15s;
}}
.nav-row button:hover {{ background: rgba(255,255,255,.14); }}
#cam-slider {{ flex:1; }}

/* ── rig gallery ─────────────────────────────────────────────────────────── */
#gallery {{
  pointer-events: all;
  background: rgba(10,10,14,.88);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  border: 1px solid rgba(255,255,255,.09);
  border-radius: 10px;
  font: 12px/1.5 'SF Mono', 'Fira Code', ui-monospace, monospace;
  color: #c4c4cc;
  padding: 12px;
  width: 340px;
  display: none;
  flex-direction: column;
  gap: 10px;
}}
.gallery-header {{ font-size: 10px; color: #666; letter-spacing: .05em; text-transform: uppercase; }}
.gallery-frame-id {{ font-size: 11px; color: #888; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.gallery-grid {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 7px;
}}
.gallery-thumb {{
  position: relative; cursor: pointer;
  border-radius: 4px; overflow: hidden;
  border: 2px solid transparent;
  transition: border-color .12s;
  aspect-ratio: 1;
}}
.gallery-thumb:hover {{ border-color: rgba(255,255,255,.3); }}
.gallery-thumb.active {{ border-color: #ffaa22; }}
.gallery-thumb img {{ width:100%; height:100%; object-fit:cover; display:block; }}
.thumb-label {{
  position: absolute; bottom:0; left:0; right:0;
  background: rgba(0,0,0,.65); color:#ccc; font-size:9px;
  padding: 1px 3px; text-align:center;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}}
.thumb-no-img {{
  width:100%; height:100%; background:#1a1a22;
  display:flex; align-items:center; justify-content:center;
  color:#333; font-size:9px;
}}

/* ── target map ──────────────────────────────────────────────────────────── */
#target-section {{ margin-top: 0; }}
#target-canvas  {{ display:block; border-radius:6px; border:1px solid rgba(255,255,255,.07);
                   margin:0 auto; cursor:default; }}
#target-legend  {{ display:flex; flex-wrap:wrap; gap:1px; margin-top:6px; justify-content:center; }}
</style>
</head>
<body>

<div id="ui-wrap">

  <!-- controls ────────────────────────────────────────────────────────────── -->
  <div id="controls">
    <div class="row"><label><input type="checkbox" id="chkPhotos"   checked> Photos</label></div>
    <div class="row"><label><input type="checkbox" id="chkFrustums" checked> Frustums</label></div>
    <div class="row"><label><input type="checkbox" id="chkPoints"   checked> Point cloud</label></div>
    <div class="row"><label><input type="checkbox" id="chkRays"> Fwd rays</label></div>
    <div class="row"><label><input type="checkbox" id="chkPath" checked> Pi3 path</label></div>
    <div class="row"><label><input type="checkbox" id="chkCrops" checked> Pi3 crops</label></div>
    <div class="row">
      <label><input type="checkbox" id="chkSphere" checked> Ref sphere</label>
      <span class="val" id="sphere-pitch" style="font-size:10px">…</span>
    </div>
    <div class="row"><label><input type="checkbox" id="chkAngles"> Pitch / yaw</label></div>
    {'<div class="row"><label><input type="checkbox" id="chkPosthoc" checked> Corrected (+' + f'{correction_deg:.1f}' + chr(176) + ')</label></div>' if correction_deg else ''}
    <div class="sep"></div>
    <div class="row">
      <span style="flex:0 0 auto">Image size</span>
      <input type="range" id="depth-slider" min="0.05" max="0.6" step="0.01" value="0.15">
      <span class="val" id="depth-label">0.15x</span>
    </div>
    <div class="sep"></div>
    <div class="row" style="gap:6px">
      <label style="flex:0 0 auto"><input type="checkbox" id="chkAnchor"> Anchor</label>
      <select id="anchor-select" style="flex:1;background:#1a1a22;border:1px solid #2a2a3a;color:#aaa;border-radius:4px;font-size:10px;padding:2px 4px">{anchor_opts}</select>
    </div>
    <div id="anchor-panel" style="display:none;margin-top:4px">
      <div class="row">
        <span style="flex:0 0 auto;font-size:10px">Fade</span>
        <input type="range" id="fade-slider" min="0" max="1" step="0.05" value="1">
        <span class="val" id="fade-val">100%</span>
      </div>
      <div class="row">
        <span style="flex:0 0 auto;font-size:10px">Reveal</span>
        <input type="range" id="reveal-slider" min="0" max="{n_non_anchor_sensors}" step="1" value="0">
        <span class="val" id="reveal-val">0/{n_non_anchor_sensors}</span>
      </div>
    </div>
    <div class="sep"></div>
    <div class="dim" id="stats">cameras: {n} &nbsp;·&nbsp; points: {len(points)}</div>
    {spread_stats_html}
    {'<canvas id="spread-chart" width="162" height="56" style="width:100%;height:56px;border-radius:3px;margin-top:5px;cursor:crosshair;display:block"></canvas><div id="spread-tooltip" style="font-size:9px;color:#5a5a6a;margin-top:2px;min-height:1.4em;line-height:1.4"></div>' if spread else ''}
    <div class="dim" style="margin-top:3px">drag·scroll·shift+drag &nbsp;·&nbsp; ← →</div>
  </div>

  <!-- viewer ─────────────────────────────────────────────────────────────── -->
  <div id="viewer">
    <div id="preview-wrap">
      <img id="preview-img" alt="">
      <div id="no-img">no image</div>
    </div>
    <div class="row" style="justify-content:space-between">
      <span class="cam-meta" id="cam-counter">1 / {n}</span>
    </div>
    <div class="cam-name" id="cam-name">&nbsp;</div>
    <div class="cam-meta" id="cam-angles" style="display:none;color:#6e82ff">&nbsp;</div>
    <div class="cam-meta" id="spread-info" style="display:none;color:#666;font-size:10px;line-height:1.6">&nbsp;</div>
    <div class="nav-row">
      <button id="prev-btn">&#9664;</button>
      <input type="range" id="cam-slider" min="0" max="{n-1}" step="1" value="0">
      <button id="next-btn">&#9654;</button>
    </div>
  </div>

  <!-- gallery ─────────────────────────────────────────────────────────────── -->
  <div id="gallery">
    <div class="gallery-header">Rig sensors</div>
    <div class="gallery-frame-id" id="gallery-frame-id">&nbsp;</div>
    <div class="gallery-grid" id="gallery-grid"></div>
    <div class="sep" id="target-sep" style="display:none;margin-top:8px"></div>
    <div id="target-section" style="display:none">
      <div class="gallery-header" style="margin-bottom:5px" title="Rings are scaled against a fixed 1e-3 scene-unit floor, not the noise itself — dots near the crosshair mean spread is at or below sub-mm rounding noise (rig locked), regardless of how small the true value is.">Nodal spread</div>
      <canvas id="target-canvas" width="260" height="200"></canvas>
      <div id="target-legend"></div>
    </div>
  </div>

</div>

<script type="importmap">
{{"imports":{{
  "three":          "https://cdn.jsdelivr.net/npm/three@0.164.1/build/three.module.js",
  "three/addons/":  "https://cdn.jsdelivr.net/npm/three@0.164.1/examples/jsm/"
}}}}
</script>

<script type="module">
import * as THREE from 'three';
import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';

const CAMERAS        = {cams_json};
const POINTS         = {pts_json};
const DEPTH_BASE     = {depth:.6f};
const KNOWN_PITCH_DEG = {pitch_deg:.1f}; // rig pitch from pipeline settings
const CORRECTION_DEG  = {correction_deg:.1f}; // post-hoc gravity correction applied to sparse_txt
const SPREAD          = {spread_json};
const SPREAD_SCALE    = Object.values(SPREAD).reduce((m, f) => Math.max(m, f.max), 1e-9);
// Absolute reference floor (same constant the bar chart uses below): 1e-3 scene
// units ≈ sub-mm. Below this, deviation is COLMAP text-format rounding noise,
// not real rig drift. Charts scale against this floor, not their own noise
// ceiling, so noise collapses toward zero instead of always filling the plot.
const SPREAD_NOISE_FLOOR = 1e-3;
const ANCHOR_SENSOR   = '{anchor_sensor}';
const QUAD_POSES      = {quad_poses_json};

// ── renderer ─────────────────────────────────────────────────────────────────
const renderer = new THREE.WebGLRenderer({{ antialias: true }});
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0d0d0f);

const view = new THREE.PerspectiveCamera(55, innerWidth / innerHeight, 0.001, 2000);
view.position.set(0, 0, 5);

const controls = new OrbitControls(view, renderer.domElement);
controls.enableDamping  = true;
controls.dampingFactor  = 0.06;

// ── point cloud ───────────────────────────────────────────────────────────────
const ptGroup = new THREE.Group();
if (POINTS.length) {{
  const geo = new THREE.BufferGeometry();
  const pos = new Float32Array(POINTS.length * 3);
  const col = new Float32Array(POINTS.length * 3);
  POINTS.forEach((p, i) => {{
    pos[i*3]   = p.xyz[0]; pos[i*3+1] = p.xyz[1]; pos[i*3+2] = p.xyz[2];
    col[i*3]   = p.rgb[0]/255; col[i*3+1] = p.rgb[1]/255; col[i*3+2] = p.rgb[2]/255;
  }});
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('color',    new THREE.BufferAttribute(col, 3));
  ptGroup.add(new THREE.Points(geo, new THREE.PointsMaterial({{
    size: DEPTH_BASE * 0.04, sizeAttenuation: true, vertexColors: true
  }})));
}}
scene.add(ptGroup);

// ── camera visual groups ─────────────────────────────────────────────────────
const frustumGroup = new THREE.Group();
const photoGroup   = new THREE.Group();
scene.add(frustumGroup);
scene.add(photoGroup);

const loader = new THREE.TextureLoader();

// Per-camera objects; built once, geometry updated on depth change
const camObjects = CAMERAS.map((c, i) => {{
  // Frustum lines — unique material per camera for per-camera colour control
  const lineMat = new THREE.LineBasicMaterial({{ color:0xffffff, transparent:true, opacity:0.35 }});
  const lines   = new THREE.LineSegments(new THREE.BufferGeometry(), lineMat);
  lines.userData.camIdx = i;
  frustumGroup.add(lines);

  // Photo plane
  let photo = null;
  if (c.image) {{
    const tex = loader.load('data:image/jpeg;base64,' + c.image);
    tex.colorSpace = THREE.SRGBColorSpace;
    photo = new THREE.Mesh(
      new THREE.PlaneGeometry(1, 1),
      new THREE.MeshBasicMaterial({{ map: tex, side: THREE.DoubleSide }})
    );
    photo.userData.camIdx = i;
    photoGroup.add(photo);
  }}

  // Selection border (child of photo, lives in photo local-space)
  const borderMat = new THREE.LineBasicMaterial({{ color:0xffaa22, transparent:true, opacity:0 }});
  const border    = new THREE.LineSegments(new THREE.BufferGeometry(), borderMat);
  if (photo) {{
    border.scale.set(1.015, 1.015, 1);  // slightly larger than photo
    border.position.z = 0.002;
    photo.add(border);
  }}

  return {{ c, i, lines, lineMat, photo, border, borderMat }};
}});

// ── geometry builder ─────────────────────────────────────────────────────────
function computeCorners(c, depth) {{
  const O  = new THREE.Vector3(...c.center);
  const Rt = new THREE.Vector3(...c.right);
  const Up = new THREE.Vector3(...c.up);
  const Fw = new THREE.Vector3(...c.forward);
  const hw = (c.width  / 2 / c.focal) * depth;
  const hh = (c.height / 2 / c.focal) * depth;
  const base = O.clone().addScaledVector(Fw, depth);
  return {{
    O, Rt, Up, Fw, hw, hh,
    tl: base.clone().addScaledVector(Rt,-hw).addScaledVector(Up, hh),
    tr: base.clone().addScaledVector(Rt, hw).addScaledVector(Up, hh),
    br: base.clone().addScaledVector(Rt, hw).addScaledVector(Up,-hh),
    bl: base.clone().addScaledVector(Rt,-hw).addScaledVector(Up,-hh),
  }};
}}

function updateGeometries(depth) {{
  camObjects.forEach(obj => {{
    const {{ O, Rt, Up, Fw, hw, hh, tl, tr, br, bl }} = computeCorners(obj.c, depth);

    // Frustum lines (apex → 4 corners + rectangle)
    const fv = [O,tl,O,tr,O,br,O,bl, tl,tr,tr,br,br,bl,bl,tl]
      .flatMap(v => [v.x, v.y, v.z]);
    obj.lines.geometry.dispose();
    obj.lines.geometry = new THREE.BufferGeometry();
    obj.lines.geometry.setAttribute('position',
      new THREE.BufferAttribute(new Float32Array(fv), 3));

    // Photo plane
    if (obj.photo) {{
      const pgeo = new THREE.PlaneGeometry(hw*2, hh*2);
      obj.photo.geometry.dispose();
      obj.photo.geometry = pgeo;

      const center = tl.clone().add(tr).add(br).add(bl).multiplyScalar(0.25);
      obj.photo.position.copy(center);
      obj.photo.setRotationFromMatrix(
        new THREE.Matrix4().makeBasis(Rt, Up, Fw.clone().negate()));

      // Border edges match new plane size
      obj.border.geometry.dispose();
      obj.border.geometry = new THREE.EdgesGeometry(pgeo);
    }}
  }});
}}

// Initial build
let currentDepth = DEPTH_BASE * 0.15;
updateGeometries(currentDepth);

// ── auto-fit ──────────────────────────────────────────────────────────────────
{{
  const box = new THREE.Box3();
  CAMERAS.forEach(c => box.expandByPoint(new THREE.Vector3(...c.center)));
  if (!box.isEmpty()) {{
    const centre = box.getCenter(new THREE.Vector3());
    const size   = box.getSize(new THREE.Vector3()).length();
    controls.target.copy(centre);
    view.position.copy(centre).addScaledVector(
      new THREE.Vector3(0.4, 0.6, 1).normalize(), size * 2.2);
    controls.update();
  }}
}}

// ── rig gallery ───────────────────────────────────────────────────────────────
// Build frame_key → sorted list of camera indices
const frameIndex = new Map();
CAMERAS.forEach((c, i) => {{
  if (!frameIndex.has(c.frame_key)) frameIndex.set(c.frame_key, []);
  frameIndex.get(c.frame_key).push(i);
}});

// ── per-rig reference geometry ────────────────────────────────────────────────
// NOTE: all cameras in a rig share zero_t (no translation offset), so their
// centers are co-located.  The sphere is drawn only for the selected frame so
// it is never occluded by the 360° photo planes surrounding the camera cluster.
const raysGroup      = new THREE.Group(); raysGroup.visible      = false; scene.add(raysGroup);
const rigSphereGroup = new THREE.Group(); rigSphereGroup.visible = true;  scene.add(rigSphereGroup);
const anchorPathGroup = new THREE.Group(); scene.add(anchorPathGroup);
const frameData      = new Map(); // frameKey → {{ centroid, avgUp, sR }}

// ── per-sensor colour palette (13 distinct vivid colours on dark bg) ──────────
const _PALETTE = [
  0xff6b6b, 0xffa94d, 0xffe066, 0xa9e34b, 0x40c057,
  0x20c997, 0x15aabf, 0x4dabf7, 0x748ffc, 0xda77f2,
  0xf783ac, 0xe87c1e, 0xb5cf6b,
];
const _sensorList = [...new Set(CAMERAS.map(c => c.sensor))]
  .sort((a,b) => (parseInt(a.replace(/\\D+/g,''))||0) - (parseInt(b.replace(/\\D+/g,''))||0));
const sensorHex = Object.fromEntries(_sensorList.map((s,i) => [s, _PALETTE[i % _PALETTE.length]]));
const sensorCSS = Object.fromEntries(Object.entries(sensorHex).map(([s,h]) =>
  [s, '#' + h.toString(16).padStart(6,'0')]));

// Colour frustum lines by sensor immediately (anchor mode overrides later)
camObjects.forEach(obj => {{
  obj.lineMat.color.setHex(sensorHex[obj.c.sensor] || 0xffffff);
  obj.lineMat.opacity = 0.5;
}});

// ── Pi3 anchor traversal path ─────────────────────────────────────────────────
// Sorted by frame_key so the line follows station order, not DB insertion order
{{
  const _acs = CAMERAS
    .map((c, i) => ({{ ...c, _i: i }}))
    .filter(c => c.sensor === ANCHOR_SENSOR)
    .sort((a, b) => (a.frame_key < b.frame_key ? -1 : 1));

  if (_acs.length > 1) {{
    const _pts = _acs.map(c => new THREE.Vector3(...c.center));

    // Traversal line — orange to match anchor frustum colour
    anchorPathGroup.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(_pts),
      new THREE.LineBasicMaterial({{ color: 0xffaa22, transparent: true, opacity: 0.80 }})));

    // Station dot at each anchor position
    const _pa = new Float32Array(_pts.flatMap(p => [p.x, p.y, p.z]));
    const _pg = new THREE.BufferGeometry();
    _pg.setAttribute('position', new THREE.BufferAttribute(_pa, 3));
    anchorPathGroup.add(new THREE.Points(_pg,
      new THREE.PointsMaterial({{ color: 0xffcc44, size: DEPTH_BASE * 0.20, sizeAttenuation: true }})));
  }}
}}

// ── Pi3 quad-anchor crop direction rays ──────────────────────────────────────
const quadCropGroup = new THREE.Group(); scene.add(quadCropGroup);
if (QUAD_POSES.length) {{
  const _yawColors = [0xff4444, 0x44cc44, 0x4488ff, 0xff44ff]; // N/E/S/W
  const _rayLen = DEPTH_BASE * 2.5;
  const _qsort = [...QUAD_POSES].sort((a, b) =>
    a.station < b.station ? -1 : a.station > b.station ? 1 : a.h_idx - b.h_idx);
  // Per-crop direction ray
  _qsort.forEach(p => {{
    const ori = new THREE.Vector3(...p.center);
    const fwd = new THREE.Vector3(...p.forward).normalize();
    const tip = ori.clone().addScaledVector(fwd, _rayLen);
    quadCropGroup.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([ori, tip]),
      new THREE.LineBasicMaterial({{ color: _yawColors[p.h_idx] ?? 0xffffff, transparent: true, opacity: 0.75 }})));
  }});
  // Traversal path: h0→h1→h2→h3→next_station_h0→...
  const _tpts = _qsort.map(p => new THREE.Vector3(...p.center));
  if (_tpts.length > 1) {{
    quadCropGroup.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(_tpts),
      new THREE.LineBasicMaterial({{ color: 0xffd700, transparent: true, opacity: 0.30 }})));
  }}
}}

frameIndex.forEach((indices, frameKey) => {{
  const centroid = new THREE.Vector3();
  indices.forEach(i => centroid.add(new THREE.Vector3(...CAMERAS[i].center)));
  centroid.divideScalar(indices.length);

  // avgUp: average camera-up vectors; horizontal components cancel for even yaw spacing
  const avgUp = new THREE.Vector3();
  indices.forEach(i => avgUp.add(new THREE.Vector3(...CAMERAS[i].up)));
  avgUp.normalize();

  const sR = DEPTH_BASE * 2.5;
  frameData.set(frameKey, {{ centroid, avgUp, sR }});

  // Forward rays (always built for all frames; shown when chkRays is on)
  const rayMat = new THREE.LineBasicMaterial({{ color: 0x44aaff, transparent: true, opacity: 0.60 }});
  indices.forEach(i => {{
    const O  = new THREE.Vector3(...CAMERAS[i].center);
    const fw = new THREE.Vector3(...CAMERAS[i].forward).normalize();
    raysGroup.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([O, O.clone().addScaledVector(fw, sR)]),
      rayMat));
  }});
}});

// Build a horizontal basis perpendicular to `up` using world X/Z as reference
function _horizontalBasis(up) {{
  // Always use world X as the first reference — gives a stable horizontal right axis
  // regardless of how close `up` is to world Y.
  const worldX = new THREE.Vector3(1, 0, 0);
  const right  = new THREE.Vector3().crossVectors(worldX, up).normalize();
  // If up is nearly parallel to X, fall back to world Z
  if (right.lengthSq() < 0.01) {{
    right.crossVectors(new THREE.Vector3(0, 0, 1), up).normalize();
  }}
  const fwd = new THREE.Vector3().crossVectors(up, right).normalize();
  return {{ right, fwd }};
}}

function _buildSphere(frameKey) {{
  // Clear previous sphere
  while (rigSphereGroup.children.length) {{
    const c = rigSphereGroup.children[0];
    if (c.geometry) c.geometry.dispose();
    if (c.material) c.material.dispose();
    rigSphereGroup.remove(c);
  }}
  if (!rigSphereGroup.visible) return;
  const fd = frameData.get(frameKey);
  if (!fd) return;

  const {{ centroid, avgUp, sR }} = fd;
  const {{ right: lr, fwd: lf }} = _horizontalBasis(avgUp);

  function _latRing(latDeg, color, opacity) {{
    const lat = THREE.MathUtils.degToRad(latDeg);
    const rH  = sR * Math.cos(lat);
    const rV  = sR * Math.sin(lat);
    const pts = [];
    for (let i = 0; i <= 72; i++) {{
      const a = (i / 72) * Math.PI * 2;
      pts.push(centroid.clone()
        .addScaledVector(avgUp, rV)
        .addScaledVector(lr,    rH * Math.cos(a))
        .addScaledVector(lf,    rH * Math.sin(a)));
    }}
    return new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({{ color, transparent: true, opacity, depthTest: false }}));
  }}
  rigSphereGroup.add(_latRing(0,             0x4466bb, 0.70)); // equator — blue
  rigSphereGroup.add(_latRing(KNOWN_PITCH_DEG, 0xffaa22, 1.00)); // pitch ring — orange
}}

document.getElementById('sphere-pitch').textContent = KNOWN_PITCH_DEG.toFixed(1) + '°';

// ── target map ────────────────────────────────────────────────────────────────
const _tCanvas = document.getElementById('target-canvas');
const _tCtx    = _tCanvas ? _tCanvas.getContext('2d') : null;

function _drawTarget(frameKey) {{
  if (!_tCtx || !_tCanvas) return;
  const W = _tCanvas.width, H = _tCanvas.height;
  const ctx = _tCtx;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#0d0d0f';
  ctx.fillRect(0, 0, W, H);

  const fd  = frameData.get(frameKey);
  const fs  = SPREAD[frameKey];
  if (!fd || !fs) return;

  const idxs = frameIndex.get(frameKey) || [];
  if (!idxs.length) return;

  // Project 3D optical-centre offsets onto the plane perpendicular to avgUp
  const {{ centroid, avgUp }} = fd;
  const {{ right: lr, fwd: lf }} = _horizontalBasis(avgUp);
  const pts = idxs.map(i => {{
    const c  = CAMERAS[i];
    const ox = c.center[0] - centroid.x;
    const oy = c.center[1] - centroid.y;
    const oz = c.center[2] - centroid.z;
    return {{ sensor: c.sensor, u: ox*lr.x+oy*lr.y+oz*lr.z, v: ox*lf.x+oy*lf.y+oz*lf.z }};
  }});

  // Scale against the absolute noise floor, not SPREAD_SCALE itself — otherwise
  // a dataset with only nanometer-scale rounding noise (rig genuinely locked)
  // gets that noise stretched to fill the whole disc. Real spread above the
  // floor still grows the scale normally; noise below it collapses to center.
  const pad  = 26;
  const maxR = Math.max(SPREAD_SCALE * 1.3, SPREAD_NOISE_FLOOR);
  const R0   = Math.min(W * 0.82, H) / 2 - pad;
  const scl  = R0 / maxR;
  const cx   = W * 0.42, cy = H / 2;  // shift left to leave room for labels

  // Target rings
  [0.25, 0.5, 0.75, 1.0].forEach(f => {{
    ctx.beginPath();
    ctx.arc(cx, cy, f * R0, 0, Math.PI * 2);
    ctx.strokeStyle = f === 1.0 ? 'rgba(100,100,130,.95)' : 'rgba(55,55,80,.75)';
    ctx.lineWidth   = f === 1.0 ? 1.5 : 0.8;
    ctx.stroke();
    // Scale label inside each ring (at the right edge)
    ctx.fillStyle = 'rgba(90,90,115,.9)';
    ctx.font      = '7px monospace';
    const label   = (maxR * f).toExponential(1);
    ctx.fillText(label, cx + f * R0 + 2, cy - 2);
  }});

  // Crosshair at centre
  ctx.strokeStyle = 'rgba(90,90,120,.8)';
  ctx.lineWidth   = 0.8;
  ctx.beginPath(); ctx.moveTo(cx - 8, cy); ctx.lineTo(cx + 8, cy); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(cx, cy - 8); ctx.lineTo(cx, cy + 8); ctx.stroke();

  // Sensor dots + number labels
  pts.forEach(p => {{
    const col = sensorCSS[p.sensor] || '#fff';
    const px  = cx + p.u * scl;
    const py  = cy - p.v * scl;
    ctx.beginPath();
    ctx.arc(px, py, 5.5, 0, Math.PI * 2);
    ctx.fillStyle   = col;
    ctx.fill();
    ctx.strokeStyle = 'rgba(0,0,0,.6)';
    ctx.lineWidth   = 1;
    ctx.stroke();
    // Tiny number offset from dot
    ctx.fillStyle = 'rgba(255,255,255,.9)';
    ctx.font      = 'bold 8px monospace';
    ctx.fillText(p.sensor.replace(/\\D+/g, ''), px + 7, py + 3);
  }});

  // Show section
  const sec = document.getElementById('target-section');
  const sep = document.getElementById('target-sep');
  if (sec) sec.style.display = 'block';
  if (sep) sep.style.display = 'block';
}}

function _buildTargetLegend(frameKey) {{
  const el   = document.getElementById('target-legend');
  if (!el) return;
  const idxs = frameIndex.get(frameKey) || [];
  const sens = [...new Set(idxs.map(i => CAMERAS[i].sensor))]
    .sort((a,b) => (parseInt(a.replace(/\\D+/g,''))||0) - (parseInt(b.replace(/\\D+/g,''))||0));
  el.innerHTML = sens.map(s => {{
    const col = sensorCSS[s] || '#fff';
    const n   = s.replace(/\\D+/g, '');
    return `<span style="display:inline-flex;align-items:center;gap:2px;margin:1px 3px">` +
      `<span style="width:8px;height:8px;border-radius:50%;background:${{col}};` +
      `display:inline-block;flex-shrink:0;border:1px solid rgba(0,0,0,.4)"></span>` +
      `<span style="font-size:8px;color:#888">c${{n}}</span></span>`;
  }}).join('');
}}
// Sphere is rebuilt whenever a camera is selected (see selectCamera below)

const galleryPanel   = document.getElementById('gallery');
const galleryGrid    = document.getElementById('gallery-grid');
const galleryFrameId = document.getElementById('gallery-frame-id');

function updateGallery(frameKey, activeCamIdx) {{
  const rawIndices = frameIndex.get(frameKey) || [];
  if (rawIndices.length < 2) {{ galleryPanel.style.display = 'none'; return; }}
  // Sort numerically by sensor name so pano_camera2 comes before pano_camera10
  const sensorNum = ci => parseInt((CAMERAS[ci].sensor || '').replace(/\\D/g, '')) || 0;
  const indices = [...rawIndices].sort((a, b) => sensorNum(a) - sensorNum(b));
  galleryPanel.style.display = 'flex';
  galleryFrameId.textContent = frameKey;
  galleryGrid.innerHTML = '';
  indices.forEach(ci => {{
    const c = CAMERAS[ci];
    const thumb = document.createElement('div');
    thumb.className = 'gallery-thumb' + (ci === activeCamIdx ? ' active' : '');

    if (c.image) {{
      const img = document.createElement('img');
      img.src = 'data:image/jpeg;base64,' + c.image;
      img.alt = c.sensor || c.name;
      thumb.appendChild(img);
    }} else {{
      const noImg = document.createElement('div');
      noImg.className = 'thumb-no-img';
      noImg.textContent = 'no img';
      thumb.appendChild(noImg);
    }}

    const label = document.createElement('div');
    label.className = 'thumb-label';
    label.textContent = c.sensor || c.name;
    thumb.appendChild(label);

    // Pi3 anchor badge (top-left corner)
    if (c.sensor === ANCHOR_SENSOR) {{
      const _ab = document.createElement('div');
      _ab.style.cssText = 'position:absolute;top:3px;left:3px;background:#ff8822;color:#000;' +
        'font-size:6px;font-weight:bold;padding:1px 3px;border-radius:2px;' +
        'letter-spacing:.04em;pointer-events:none;line-height:1.2';
      _ab.textContent = 'Pi3';
      thumb.appendChild(_ab);
    }}

    // Spread dot — color encodes how far this sensor's optical center is from rig centroid
    const _gfs = SPREAD[frameKey];
    if (_gfs && _gfs.sensors[c.sensor] !== undefined) {{
      const _d = _gfs.sensors[c.sensor];
      const _norm = Math.min(1, _d / SPREAD_SCALE);
      const _r = Math.round(_norm * 220);
      const _g = Math.round((1 - _norm) * 160);
      const _dot = document.createElement('div');
      _dot.title = 'Δ ' + _d.toExponential(3);
      _dot.style.cssText = `position:absolute;top:3px;right:3px;width:7px;height:7px;border-radius:50%;background:rgb(${{_r}},${{_g}},30);border:1px solid rgba(255,255,255,.25)`;
      thumb.appendChild(_dot);
    }}

    thumb.addEventListener('click', () => selectCamera(ci));
    galleryGrid.appendChild(thumb);
  }});
}}

// ── selection ─────────────────────────────────────────────────────────────────
let selectedIdx = -1;

const previewImg    = document.getElementById('preview-img');
const noImg         = document.getElementById('no-img');
const viewerPanel   = document.getElementById('viewer');
const camCounter    = document.getElementById('cam-counter');
const camName       = document.getElementById('cam-name');
const camSlider     = document.getElementById('cam-slider');

function selectCamera(idx) {{
  // Clear previous highlight
  if (selectedIdx >= 0) {{
    const prev = camObjects[selectedIdx];
    prev.lineMat.color.setHex(0xffffff);
    prev.lineMat.opacity = 0.35;
    prev.borderMat.opacity = 0;
  }}

  selectedIdx = idx;
  const obj = camObjects[idx];
  const c   = obj.c;

  // Apply highlight
  obj.lineMat.color.setHex(0xffaa22);
  obj.lineMat.opacity = 1.0;
  obj.borderMat.opacity = 1.0;

  // Update inspector panel
  viewerPanel.style.display = 'flex';
  if (c.image) {{
    previewImg.src     = 'data:image/jpeg;base64,' + c.image;
    previewImg.style.display = 'block';
    noImg.style.display = 'none';
  }} else {{
    previewImg.style.display = 'none';
    noImg.style.display = 'flex';
  }}
  camCounter.textContent = `${{idx + 1}} / ${{CAMERAS.length}}`;
  camName.textContent    = c.name;
  camSlider.value        = idx;

  // Pitch / yaw — relative to the rig's local coordinate system, not COLMAP world Y
  const angEl  = document.getElementById('cam-angles');
  const _fdata = frameData.get(c.frame_key);
  if (_fdata) {{
    const _fw = new THREE.Vector3(...c.forward).normalize();
    const {{ right: _r, fwd: _f }} = _horizontalBasis(_fdata.avgUp);
    const _fwH = _fw.clone().sub(
      _fdata.avgUp.clone().multiplyScalar(_fw.dot(_fdata.avgUp)));
    if (_fwH.length() > 1e-6) _fwH.normalize();
    const _yawDeg   = THREE.MathUtils.radToDeg(Math.atan2(_fwH.dot(_r), _fwH.dot(_f)));
    const _pitchDeg = THREE.MathUtils.radToDeg(
      Math.asin(THREE.MathUtils.clamp(_fw.dot(_fdata.avgUp), -1, 1)));
    angEl.textContent = `pitch ${{_pitchDeg.toFixed(1)}}°  yaw ${{_yawDeg.toFixed(1)}}°`;
  }} else {{
    angEl.textContent = '';
  }}
  angEl.style.display = document.getElementById('chkAngles').checked ? 'block' : 'none';

  // Spread info
  const _spreadEl = document.getElementById('spread-info');
  const _fs = SPREAD[c.frame_key];
  if (_spreadEl) {{
    if (_fs) {{
      const _sn = s => parseInt((s || '').replace(/\\D+/g, '')) || 0;
      const _parts = Object.entries(_fs.sensors)
        .sort(([a], [b]) => _sn(a) - _sn(b))
        .map(([s, d]) => `${{s.replace('pano_camera', 'c')}}:${{d.toExponential(2)}}`);
      _spreadEl.textContent = 'Δ ' + _parts.join(' · ');
      _spreadEl.style.display = 'block';
    }} else {{
      _spreadEl.style.display = 'none';
    }}
  }}

  // Rebuild sphere for this frame
  _buildSphere(c.frame_key);

  // Update rig gallery
  updateGallery(c.frame_key, idx);

  // Target map
  _drawTarget(c.frame_key);
  _buildTargetLegend(c.frame_key);

  // Re-apply anchor mode coloring (if active)
  if (window._applyAnchorMode) window._applyAnchorMode();
}}

// ── click detection ───────────────────────────────────────────────────────────
const raycaster = new THREE.Raycaster();
const mouse2    = new THREE.Vector2();
let downPos     = null;

renderer.domElement.addEventListener('mousedown', e => {{
  downPos = {{ x: e.clientX, y: e.clientY }};
}});
renderer.domElement.addEventListener('mouseup', e => {{
  if (!downPos) return;
  const dx = e.clientX - downPos.x, dy = e.clientY - downPos.y;
  const wasDrag = Math.sqrt(dx*dx + dy*dy) > 5;
  downPos = null;
  if (wasDrag) return;

  const rect = renderer.domElement.getBoundingClientRect();
  mouse2.x =  ((e.clientX - rect.left) / rect.width ) * 2 - 1;
  mouse2.y = -((e.clientY - rect.top ) / rect.height) * 2 + 1;
  raycaster.setFromCamera(mouse2, view);

  const hits = raycaster.intersectObjects(photoGroup.children, false);
  if (hits.length > 0) {{
    const idx = hits[0].object.userData.camIdx;
    if (idx !== undefined) selectCamera(idx);
  }}
}});

// ── UI controls ───────────────────────────────────────────────────────────────
// Layer toggles
document.getElementById('chkPhotos')  .addEventListener('change', e => {{ photoGroup  .visible = e.target.checked; if (window._applyAnchorMode) window._applyAnchorMode(); }});
document.getElementById('chkFrustums').addEventListener('change', e => {{ frustumGroup.visible = e.target.checked; if (window._applyAnchorMode) window._applyAnchorMode(); }});
document.getElementById('chkPoints')  .addEventListener('change', e => ptGroup        .visible = e.target.checked);
document.getElementById('chkRays')    .addEventListener('change', e => raysGroup      .visible = e.target.checked);
document.getElementById('chkPath')    .addEventListener('change', e => anchorPathGroup.visible = e.target.checked);
document.getElementById('chkCrops')   .addEventListener('change', e => quadCropGroup.visible = e.target.checked);
document.getElementById('chkSphere')  .addEventListener('change', e => {{
  rigSphereGroup.visible = e.target.checked;
  if (selectedIdx >= 0) _buildSphere(CAMERAS[selectedIdx].frame_key);
}});
document.getElementById('chkAngles')  .addEventListener('change', e => {{
  const el = document.getElementById('cam-angles');
  el.style.display = e.target.checked && selectedIdx >= 0 ? 'block' : 'none';
}});

// Pre/post correction toggle — rotates all scene groups by R_X(-CORRECTION_DEG) to undo alignment
const _sceneGroups = [frustumGroup, photoGroup, ptGroup, raysGroup, rigSphereGroup, anchorPathGroup, quadCropGroup];
function _setPosthocMode(showCorrected) {{
  const theta = showCorrected ? 0 : -THREE.MathUtils.degToRad(CORRECTION_DEG);
  const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), theta);
  _sceneGroups.forEach(g => g.setRotationFromQuaternion(q));
}}
if (CORRECTION_DEG !== 0) {{
  const el = document.getElementById('chkPosthoc');
  if (el) el.addEventListener('change', e => _setPosthocMode(e.target.checked));
}}

// Image size slider
const depthSlider = document.getElementById('depth-slider');
const depthLabel  = document.getElementById('depth-label');
depthSlider.addEventListener('input', e => {{
  const scale    = parseFloat(e.target.value);
  currentDepth   = DEPTH_BASE * scale;
  depthLabel.textContent = scale.toFixed(2) + 'x';
  updateGeometries(currentDepth);
  // Restore highlight if a camera is selected
  if (selectedIdx >= 0) {{
    const obj = camObjects[selectedIdx];
    obj.lineMat.color.setHex(0xffaa22);
    obj.lineMat.opacity = 1.0;
    obj.borderMat.opacity = 1.0;
  }}
}});

// Camera browser
camSlider.addEventListener('input',  e => selectCamera(parseInt(e.target.value)));
document.getElementById('prev-btn').addEventListener('click', () =>
  selectCamera((selectedIdx - 1 + CAMERAS.length) % CAMERAS.length));
document.getElementById('next-btn').addEventListener('click', () =>
  selectCamera((selectedIdx + 1) % CAMERAS.length));

// ── Anchor mode ──────────────────────────────────────────────────────────────
(function () {{
  const chkAnchor    = document.getElementById('chkAnchor');
  const anchorSelect = document.getElementById('anchor-select');
  const anchorPanel  = document.getElementById('anchor-panel');
  const fadeSlider   = document.getElementById('fade-slider');
  const revealSlider = document.getElementById('reveal-slider');
  const fadeValEl    = document.getElementById('fade-val');
  const revealValEl  = document.getElementById('reveal-val');

  // Non-anchor sibling sensors in sorted order — reveal one sensor at a time across all frames
  let orderedSensors = [];   // ['pano_camera0', 'pano_camera1', ...]

  function rebuildIndex() {{
    const sensor = anchorSelect.value;
    orderedSensors = [...new Set(CAMERAS.filter(c => c.sensor !== sensor).map(c => c.sensor))]
      .sort((a, b) => (parseInt(a.replace(/\\D+/g,'')) || 0) - (parseInt(b.replace(/\\D+/g,'')) || 0));
    revealSlider.max = orderedSensors.length;
    syncRevealLabel();
  }}

  function syncRevealLabel() {{
    const r = parseInt(revealSlider.value);
    const name = r > 0 ? (orderedSensors[r - 1] || '') : '';
    revealValEl.textContent = r + '/' + orderedSensors.length + (name ? ' — ' + name : '');
  }}

  function apply() {{
    const enabled = chkAnchor.checked;
    const sensor  = anchorSelect.value;
    const reveal  = parseInt(revealSlider.value);
    const fade    = parseFloat(fadeSlider.value);

    if (!enabled) {{
      // Restore every camera to sensor-coloured default
      camObjects.forEach((obj, i) => {{
        obj.lines.visible = true;
        if (obj.photo) {{ obj.photo.visible = true; obj.photo.material.opacity = 1; obj.photo.material.transparent = false; }}
        if (i !== selectedIdx) {{
          obj.lineMat.color.setHex(sensorHex[obj.c.sensor] || 0xffffff);
          obj.lineMat.opacity = 0.5;
        }}
      }});
      return;
    }}

    // Build set of revealed camera indices — one sibling sensor at a time, all frames
    const revealedSet = new Set();
    for (let s = 0; s < reveal; s++) {{
      const sName = orderedSensors[s];
      if (sName) CAMERAS.forEach((c, i) => {{ if (c.sensor === sName) revealedSet.add(i); }});
    }}

    camObjects.forEach((obj, i) => {{
      const isAnchor   = CAMERAS[i].sensor === sensor;
      const isRevealed = revealedSet.has(i);
      const isSel      = i === selectedIdx;

      if (isAnchor) {{
        // Always visible — orange tint
        obj.lines.visible = true;
        if (obj.photo) {{ obj.photo.visible = true; obj.photo.material.opacity = 1; obj.photo.material.transparent = false; }}
        obj.lineMat.color.setHex(isSel ? 0xffcc33 : 0xff8822);
        obj.lineMat.opacity = isSel ? 1.0 : 0.9;
      }} else if (isRevealed) {{
        // Revealed — blue, faded by slider
        obj.lines.visible = true;
        if (obj.photo) {{ obj.photo.visible = true; obj.photo.material.opacity = fade; obj.photo.material.transparent = fade < 0.99; }}
        obj.lineMat.color.setHex(isSel ? 0x88ddff : 0x44aaff);
        obj.lineMat.opacity = isSel ? 1.0 : Math.max(0.06, fade * 0.7);
      }} else {{
        // Not yet revealed — hidden
        obj.lines.visible = false;
        if (obj.photo) obj.photo.visible = false;
      }}
    }});
  }}

  chkAnchor.addEventListener('change', () => {{
    anchorPanel.style.display = chkAnchor.checked ? 'block' : 'none';
    if (chkAnchor.checked) rebuildIndex();
    apply();
  }});
  anchorSelect.addEventListener('change', () => {{ rebuildIndex(); apply(); }});
  fadeSlider.addEventListener('input', () => {{
    fadeValEl.textContent = Math.round(parseFloat(fadeSlider.value) * 100) + '%';
    apply();
  }});
  revealSlider.addEventListener('input', () => {{ syncRevealLabel(); apply(); }});

  rebuildIndex();
  window._applyAnchorMode = apply;
}})();

// ── deselect ──────────────────────────────────────────────────────────────────
function deselectCamera() {{
  if (selectedIdx >= 0) {{
    const prev = camObjects[selectedIdx];
    prev.lineMat.color.setHex(sensorHex[prev.c.sensor] || 0xffffff);
    prev.lineMat.opacity = 0.5;
    prev.borderMat.opacity = 0;
  }}
  selectedIdx = -1;
  viewerPanel.style.display  = 'none';
  galleryPanel.style.display = 'none';
  const sec = document.getElementById('target-section');
  const sep = document.getElementById('target-sep');
  if (sec) sec.style.display = 'none';
  if (sep) sep.style.display = 'none';
  if (window._applyAnchorMode) window._applyAnchorMode();
}}

// Keyboard: ← → to step, Escape to deselect
window.addEventListener('keydown', e => {{
  if (e.key === 'Escape')     deselectCamera();
  if (e.key === 'ArrowLeft')  selectCamera((selectedIdx - 1 + CAMERAS.length) % CAMERAS.length);
  if (e.key === 'ArrowRight') selectCamera((selectedIdx + 1) % CAMERAS.length);
}});

// ── Spread per-station bar chart ──────────────────────────────────────────────
(function () {{
  const canvas = document.getElementById('spread-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;

  const stations = Object.keys(SPREAD).sort();
  if (!stations.length) return;
  const maxVal = stations.reduce((m, k) => Math.max(m, SPREAD[k].max), 1e-12);
  const barW = W / stations.length;

  // Use an absolute reference scale for the y-axis so the chart reflects
  // true deviation magnitude, not just relative differences (see
  // SPREAD_NOISE_FLOOR definition above for why).
  const absRef = Math.max(maxVal * 1.2, SPREAD_NOISE_FLOOR);

  function draw(hoverIdx) {{
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#0d0d12';
    ctx.fillRect(0, 0, W, H);
    stations.forEach((k, i) => {{
      const norm = Math.min(1, SPREAD[k].max / absRef);
      const bh = Math.max(1, Math.round(norm * (H - 12)));
      const x = Math.round(i * barW);
      const bw = Math.max(1, Math.round((i + 1) * barW) - x - 1);
      if (i === hoverIdx) {{
        ctx.fillStyle = 'rgba(255,255,255,0.85)';
      }} else {{
        const r = Math.round(Math.min(255, norm * 2 * 255));
        const g = Math.round(Math.min(255, (1 - norm) * 2 * 255));
        ctx.fillStyle = `rgb(${{r}},${{g}},40)`;
      }}
      ctx.fillRect(x, H - bh - 1, bw, bh);
    }});
    // baseline and max-value label
    ctx.fillStyle = 'rgba(255,255,255,0.07)';
    ctx.fillRect(0, H - 1, W, 1);
    ctx.fillStyle = '#3a3a4a';
    ctx.font = '8px monospace';
    ctx.fillText(`max ${{maxVal.toExponential(2)}}`, 2, 9);
  }}

  draw(-1);

  const tip = document.getElementById('spread-tooltip');
  canvas.addEventListener('mousemove', e => {{
    const i = Math.floor((e.offsetX / canvas.offsetWidth) * stations.length);
    if (i < 0 || i >= stations.length) return;
    draw(i);
    const f = SPREAD[stations[i]];
    if (tip) tip.textContent = `${{stations[i]}}  max ${{f.max.toExponential(2)}}  μ ${{f.mean.toExponential(2)}}`;
  }});
  canvas.addEventListener('mouseleave', () => {{ draw(-1); if (tip) tip.textContent = ''; }});
  canvas.addEventListener('click', e => {{
    const i = Math.floor((e.offsetX / canvas.offsetWidth) * stations.length);
    if (i < 0 || i >= stations.length) return;
    const idxs = frameIndex.get(stations[i]);
    if (idxs && idxs.length) selectCamera(idxs[0]);
  }});
}})();

// ── render loop ───────────────────────────────────────────────────────────────
window.addEventListener('resize', () => {{
  renderer.setSize(innerWidth, innerHeight);
  view.aspect = innerWidth / innerHeight;
  view.updateProjectionMatrix();
}});

(function loop() {{
  requestAnimationFrame(loop);
  controls.update();
  renderer.render(scene, view);
}})();
</script>
</body>
</html>"""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    rec_dir        = Path(sys.argv[1])
    out            = Path(sys.argv[2]) if len(sys.argv) > 2 else rec_dir.parent / "cameras.html"
    pitch_deg      = float(sys.argv[3]) if len(sys.argv) > 3 else -10.0
    correction_deg = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
    anchor_sensor  = sys.argv[5]        if len(sys.argv) > 5 else "pano_camera7"
    images_path    = Path(sys.argv[6])  if len(sys.argv) > 6 else None

    print(f"Reading reconstruction from {rec_dir} ...")
    cameras, points, spread, quad_poses = extract(rec_dir, images_path=images_path)
    print(f"  {len(cameras)} cameras, {len(points)} 3D points")

    html = build_html(cameras, points, pitch_deg=pitch_deg, correction_deg=correction_deg, anchor_sensor=anchor_sensor, spread=spread, quad_poses=quad_poses)
    out.write_text(html, encoding="utf-8")
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
