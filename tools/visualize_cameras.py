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
        if not line or line.startswith("#"):
            continue
        if not data_line:
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


def extract(rec_dir: Path):
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

    images_root = rec_dir / "images"
    if not images_root.exists():
        images_root = rec_dir.parent / "images"
    if not images_root.exists():
        images_root = rec_dir.parent.parent / "images"

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

    return cameras_data, pts_data


def build_html(cameras: list, points: list, pitch_deg: float = -10.0, correction_deg: float = 0.0) -> str:
    depth      = _scene_scale(cameras)
    n          = len(cameras)
    cams_json  = json.dumps(cameras)
    pts_json   = json.dumps(points)

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
  width: 320px;
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
    <div class="row">
      <label><input type="checkbox" id="chkSphere" checked> Ref sphere</label>
      <span class="val" id="sphere-pitch" style="font-size:10px">…</span>
    </div>
    <div class="row"><label><input type="checkbox" id="chkAngles"> Pitch / yaw</label></div>
    {'<div class="row"><label><input type="checkbox" id="chkPosthoc" checked> Corrected (+' + f'{correction_deg:.1f}' + chr(176) + ')</label></div>' if correction_deg else ''}
    <div class="sep"></div>
    <div class="row">
      <span style="flex:0 0 auto">Image size</span>
      <input type="range" id="depth-slider" min="0.15" max="4" step="0.05" value="1">
      <span class="val" id="depth-label">1.00x</span>
    </div>
    <div class="sep"></div>
    <div class="dim" id="stats">cameras: {n} &nbsp;·&nbsp; points: {len(points)}</div>
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
let currentDepth = DEPTH_BASE;
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
const frameData      = new Map(); // frameKey → {{ centroid, avgUp, sR }}

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
// Sphere is rebuilt whenever a camera is selected (see selectCamera below)

const galleryPanel   = document.getElementById('gallery');
const galleryGrid    = document.getElementById('gallery-grid');
const galleryFrameId = document.getElementById('gallery-frame-id');

function updateGallery(frameKey, activeCamIdx) {{
  const indices = frameIndex.get(frameKey) || [];
  if (indices.length < 2) {{ galleryPanel.style.display = 'none'; return; }}
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

  // Rebuild sphere for this frame
  _buildSphere(c.frame_key);

  // Update rig gallery
  updateGallery(c.frame_key, idx);
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
document.getElementById('chkPhotos')  .addEventListener('change', e => photoGroup     .visible = e.target.checked);
document.getElementById('chkFrustums').addEventListener('change', e => frustumGroup   .visible = e.target.checked);
document.getElementById('chkPoints')  .addEventListener('change', e => ptGroup        .visible = e.target.checked);
document.getElementById('chkRays')    .addEventListener('change', e => raysGroup      .visible = e.target.checked);
document.getElementById('chkSphere')  .addEventListener('change', e => {{
  rigSphereGroup.visible = e.target.checked;
  if (selectedIdx >= 0) _buildSphere(CAMERAS[selectedIdx].frame_key);
}});
document.getElementById('chkAngles')  .addEventListener('change', e => {{
  const el = document.getElementById('cam-angles');
  el.style.display = e.target.checked && selectedIdx >= 0 ? 'block' : 'none';
}});

// Pre/post correction toggle — rotates all scene groups by R_X(-CORRECTION_DEG) to undo alignment
const _sceneGroups = [frustumGroup, photoGroup, ptGroup, raysGroup, rigSphereGroup];
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

// Keyboard: ← → to step
window.addEventListener('keydown', e => {{
  if (e.key === 'ArrowLeft')  selectCamera((selectedIdx - 1 + CAMERAS.length) % CAMERAS.length);
  if (e.key === 'ArrowRight') selectCamera((selectedIdx + 1) % CAMERAS.length);
}});

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

    print(f"Reading reconstruction from {rec_dir} ...")
    cameras, points = extract(rec_dir)
    print(f"  {len(cameras)} cameras, {len(points)} 3D points")

    html = build_html(cameras, points, pitch_deg=pitch_deg, correction_deg=correction_deg)
    out.write_text(html, encoding="utf-8")
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
