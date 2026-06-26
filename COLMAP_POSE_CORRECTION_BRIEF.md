# COLMAP Camera Pose Correction — Technical Brief

**Project:** FieldRaven_desktop (SplatPipe)  
**File under review:** `splatpipe_core/colmap_runner.py`  
**Problem status:** Active — correction code exists but result is unverified / may still be wrong

---

## What We Are Trying to Do

We extract perspective images from 360° equirectangular frames at **known pitch angles**:
- **1 anchor sensor** (`pano_camera0`): extracted at **pitch = 0°** (pointing at the horizon)
- **6 sibling sensors** (`pano_camera1`–`pano_camera6`): extracted at **pitch = −10°** (pointing 10° below the horizon)

Each group of 7 images from a single equirec frame forms a **rigid rig** in COLMAP. There are 28 frames → 196 images total. The rig constraints are locked (`ba_refine_sensor_from_rig=False`), so the shape of each rig is fixed during bundle adjustment.

**The goal:** After COLMAP runs, the camera poses in `sparse_txt/images.txt` should reflect the true extraction angles. The anchor should point at the horizon (0° pitch) and the siblings should point 10° below it (−10° pitch). These poses feed directly into 3DGS training (Brush), so incorrect poses = degraded training.

---

## The Problem: COLMAP World-Frame Bias

COLMAP has no concept of "up" or gravity. Its world frame is initialized from the first image pair and then refined by bundle adjustment. Because 6 of the 7 sensors are at −10° pitch, the world frame tilts — COLMAP "flattens" the majority by tilting the world upward.

**Observed result** (from `cameras.html` visualizer):
- Anchor (`pano_camera0`) appears **tilted upward** — not horizontal
- Siblings appear to point **toward the horizon** — not downward

This is the opposite of the true extraction angles. The rig shape is correct (siblings are all angled the same way relative to the anchor), but the entire rig's absolute orientation in the world frame is wrong.

**Key point:** This is a **camera pose accuracy problem**, not a scene orientation / gravity alignment problem. The optical axes of the cameras do not match the angles at which they were extracted from the sphere.

---

## Coordinate System Conventions

COLMAP uses **Y-down, Z-forward** convention.

In `images.txt`, each image has a quaternion `(QW, QX, QY, QZ)` representing the **cam-from-world** rotation `R`. This maps world points into camera space: `p_cam = R @ p_world + t`.

The **camera's forward axis (optical axis) in world coordinates** is:
```
forward_world = R.T[:, 2]   # third column of R-transpose
```

**DIAG pitch** (used throughout this codebase) is defined as:
```python
diag_pitch = arcsin(forward_world[1])   # Y component, Y-down convention
```

- `diag_pitch > 0` → camera is pointing **downward** (positive Y = downward)
- `diag_pitch < 0` → camera is pointing **upward**
- `diag_pitch = 0` → camera is pointing **horizontally**

**True extraction pitches in DIAG convention:**
| Sensor | Extraction pitch | DIAG pitch |
|--------|-----------------|------------|
| pano_camera0 (anchor) | 0° | 0° |
| pano_camera1–6 (siblings) | −10° | +10° |

---

## What Was Observed in the Last Run

**Pipeline log:**
```
[colmap_alignment] 88% — Writing sparse_txt output
[colmap_alignment] 91% — Applying scene-tilt correction R_X(8.57°) (sibling_pitch=-10.0°)
[colmap_alignment] 93% — COLMAP: 196 images, 7830 points — finalising brush input...
[colmap_alignment] 99% — Generating camera visualizer...
Reading reconstruction from .../03_alignment/colmap/sparse_txt ...
DIAG anchor cam pitch in world: -11.961°
```

**Interpretation:**  
After applying `R_X(+8.57°)` the anchor is at **−11.961° DIAG** — pointing upward by almost 12°. This is wrong; it should be 0°.

Working backward: the `R_X(+correction_deg)` correction shifts DIAG pitch by approximately `+correction_deg`. So before correction:
```
anchor_pitch_before ≈ -11.961° - 8.57° ≈ -20.53°
```

COLMAP had placed the anchor at −20.53° DIAG (pointing 20.5° above horizontal), not −8.57° as the formula predicted. **The deterministic formula was wrong by about 12°.**

---

## The Failed Deterministic Formula

The original approach computed the correction analytically:

```python
# Old code — INCORRECT
n_siblings = n_total - 1   # 6
return -(n_siblings * sibling_pitch) / n_total
# = -(6 * -10) / 7 = +8.57°
```

This assumed COLMAP biases the world by exactly `−mean_pitch`. In practice, COLMAP's incremental reconstruction depends on which image pair initializes the reconstruction, the BA convergence path, and the specific scene geometry. The actual bias is NOT the analytical mean — it can be significantly larger.

---

## The Current Fix (Needs Verification)

The fix replaces the formula with a **measurement** of the actual anchor pitch from `images.txt`, then applies the exact correction to bring it to 0°.

**`_measure_anchor_pitch` in `colmap_runner.py`:**
```python
def _measure_anchor_pitch(sparse_txt_dir: Path) -> float:
    images_txt = sparse_txt_dir / "images.txt"
    if not images_txt.exists():
        return 0.0
    pitches: list[float] = []
    data_line = False
    for raw in images_txt.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not data_line:
            parts = line.split()
            if len(parts) >= 10 and parts[9].startswith("pano_camera0/"):
                qw, qx, qy, qz = (float(parts[i]) for i in (1, 2, 3, 4))
                R   = _quat_to_mat(qw, qx, qy, qz)   # cam_from_world
                fwd = R.T[:, 2]                        # forward axis in COLMAP world
                pitches.append(float(np.degrees(np.arcsin(np.clip(fwd[1], -1.0, 1.0)))))
            data_line = True
        else:
            data_line = False
    return float(np.mean(pitches)) if pitches else 0.0
```

**Call site in `_run_perspective_rig`:**
```python
anchor_pitch = _measure_anchor_pitch(sparse_txt)
correction_deg = -anchor_pitch
if abs(correction_deg) > 0.5:
    _apply_gravity_alignment(sparse_txt, correction_deg)
```

**The correction function:**
```python
def _apply_gravity_alignment(sparse_txt_dir: Path, correction_deg: float) -> None:
    theta = np.radians(correction_deg)
    Rg = np.array([
        [1, 0,              0             ],
        [0, np.cos(theta), -np.sin(theta) ],
        [0, np.sin(theta),  np.cos(theta) ],
    ], dtype=np.float64)
    _fix_images(sparse_txt_dir / "images.txt", Rg)
    _fix_points3D(sparse_txt_dir / "points3D.txt", Rg)
```

**`_fix_images` applies:** `R_new = R_old @ Rg`

---

## The Math We Believe Is Correct (Please Verify)

Given `R_new = R_old @ R_X(θ)`:

```
R_new.T[:, 2] = (R_old @ R_X(θ)).T[:, 2]
              = R_X(θ).T @ R_old.T[:, 2]
              = R_X(-θ) @ fwd_old
```

Effect on the Y component (DIAG pitch), using `R_X(-θ)`:
```
y_new = y_old * cos(θ) + z_old * sin(θ)
```

For a near-horizontal camera (`z_old ≈ 1`, `y_old = sin(P)` where P is DIAG pitch):
```
y_new ≈ sin(P) * cos(θ) + cos(P) * sin(θ) = sin(P + θ)
```

So: **DIAG pitch after ≈ DIAG pitch before + θ**

To bring anchor from DIAG pitch `P` to 0°: need `θ = -P`, i.e., `correction_deg = -anchor_pitch`. ✓

**Sanity check with last run's numbers:**
- anchor_pitch_before ≈ −20.53°
- correction_deg = +20.53°
- DIAG pitch after ≈ −20.53° + 20.53° = 0° ✓

**After correct correction, siblings should be at:**
- Each sibling was at `sibling_pitch_before ≈ −20.53° + 10° = −10.53°` (same world tilt, but they start 10° below anchor)
- After correction: `−10.53° + 20.53° = +10°` DIAG = camera pointing 10° below horizontal ✓ (matches extraction angle of −10°)

---

## What Still Needs Verification

The code was just updated but has NOT been re-run yet. The next pipeline run should produce:

```
Applying scene-tilt correction R_X(~20.5°) (anchor measured at ~-20.5° DIAG, sibling_pitch=-10.0°)
...
DIAG anchor cam pitch in world: ~0.0°
```

If the DIAG line shows ~0.0°, the correction is working and `cameras.html` should show the anchor pointing at the horizon and siblings angled 10° downward.

**If the result is still wrong, the questions to investigate are:**

1. **Is `_fix_images` actually writing back to disk?**  
   It calls `path.write_text(...)` at the end. Does the path point to the correct file? Could there be a permissions issue?

2. **Is the visualizer reading the corrected file?**  
   `_generate_visualizer` passes `colmap_dir / "sparse_txt"` as the input directory. The correction is applied to the same `colmap_dir / "sparse_txt"` path. The visualizer should be reading the corrected files.

3. **Is the quaternion round-trip (`_quat_to_mat` → matrix multiply → `_mat_to_quat`) introducing errors?**  
   These are pure Python/numpy implementations. Shepperd's method is used for `_mat_to_quat`. Could there be a degenerate case for any quaternion in the dataset?

4. **Is `images.txt` being parsed correctly?**  
   COLMAP's text format alternates between image header lines and keypoint lines. The `data_line` flag in both `_measure_anchor_pitch` and `_fix_images` tracks which line type is being processed. Verify that the column index for NAME (index 9) is correct: `IMAGE_ID(0) QW(1) QX(2) QY(3) QZ(4) TX(5) TY(6) TZ(7) CAMERA_ID(8) NAME(9)`.

5. **Are ALL anchor cameras at the same pitch, or just one?**  
   The measurement averages all pano_camera0 images. If the rig constraint is working correctly, all anchors should have identical DIAG pitch (only differing in translation). If they differ significantly, the rig constraint may not be holding.

6. **Is the `R_X` rotation the right axis?**  
   The bias is a tilt in the vertical plane. If the camera rig has a non-trivial yaw distribution (yaw steps spread around 360°), the pitch bias from COLMAP should still be a pure X-axis rotation. But if the reconstruction has any roll component, the correction axis might be wrong.

---

## File Locations

| File | Role |
|------|------|
| `splatpipe_core/colmap_runner.py` | Contains `_measure_anchor_pitch`, `_apply_gravity_alignment`, `_fix_images`, `_fix_points3D`, `_generate_visualizer`, `_run_perspective_rig` |
| `splatpipe_core/colmap_worker.py` | Python 3.14 subprocess: runs pycolmap feature extraction, rig config, matching, incremental mapping, writes `sparse_txt` |
| `tools/visualize_cameras.py` | Reads corrected `sparse_txt` via pycolmap, generates `cameras.html`. Contains a diagnostic that prints `DIAG anchor cam pitch in world: X.XXX°` |
| `03_alignment/colmap/sparse_txt/` | COLMAP text output — corrected in-place before being copied to brush_input |
| `04_training/brush_input/` | Copy of corrected sparse_txt + images — input to 3DGS training |

---

## Pipeline Execution Environment

- **Python 3.13** runs the server and `colmap_runner.py`
- **Python 3.14 + pycolmap 4.0.4** runs `colmap_worker.py` (spawned as subprocess) and `visualize_cameras.py` (spawned as subprocess)
- `pycolmap.Reconstruction.read_text()` is the pycolmap 4.x API used in the visualizer
- `image.cam_from_world()` is a **method call** in pycolmap 4.0.4 (requires `()`)
- Bundle adjustment is CPU-only (Ceres solver); GPU used only for SIFT extraction/matching

---

## What Success Looks Like

After a correct run:
- Log shows `R_X(~20°)` applied (or whatever the measured anchor pitch is)
- Diagnostic prints `DIAG anchor cam pitch in world: ~0.0°`
- `cameras.html` shows the anchor camera frustums pointing at the horizon (horizontal blue equator ring matches the anchor)
- Sibling camera frustums point 10° below the equator ring (orange pitch ring at −10°)
- These poses feed into Brush for 3DGS training with accurate camera parameters

---

## What We Actually Did to Fix It (Resolution Log)

### Problem 1: Global R_X rotation approach failed

The first approach applied a global X-axis rotation to all cameras and points (via `_fix_images` and `_fix_points3D`). Two bugs were discovered:

1. **Formula was wrong.** The analytical formula `-(n_siblings * sibling_pitch) / n_total` to predict COLMAP's world-frame tilt was incorrect. COLMAP's actual bias depends on its initialization and BA convergence path — it cannot be predicted analytically.

2. **The global rotation applied to cameras with different yaws produced different pitch corrections.** `R_new = R_old @ R_X(θ)` causes `fwd_new = R_X(-θ) @ fwd_old`. For cameras pointing roughly along Z this corrects pitch correctly, but for cameras pointing at other yaws the correction is inconsistent. With 28 rig frames at varying headings, a single global R_X cannot bring all anchors to 0° pitch simultaneously.

3. **Sign issue.** `_fix_images` used `R_new = R_old @ Rg`, which applies `Rg.T` to the forward vector, not `Rg`. The sign of the correction was backward.

**Conclusion:** Global R_X rotation is the wrong tool for this problem.

---

### Problem 2: Per-camera correction scattered the rig

The replacement approach (`_correct_camera_pitches_from_extraction`) individually set each camera's rotation to the known extraction pitch (0° for anchor, 10° DIAG for siblings) while keeping COLMAP's estimated yaw. This correctly set pitches but **the camera translations (`TX TY TZ`) were not updated**.

In COLMAP, `cam_center = -R.T @ t`. When R changes but t stays the same, the camera centre moves. With 7 sensors per frame all getting different new rotations, the 7 formerly co-located cameras ended up at 7 different world positions — the rig appeared completely scattered in `cameras.html`.

**Fix:** After computing `R_new`, recompute `t_new` to preserve the original camera centre:
```python
t_old  = np.array([float(parts[5]), float(parts[6]), float(parts[7])])
center = -R_cur.T @ t_old          # original camera position in world
t_new  = -R_new @ center           # new translation for same position, new rotation
```

We added a diagnostic that printed the camera centres for all 7 sensors in the first rig frame, both before and after correction:
```
[RIG DIAG] Frame 'IMG_...' camera centres (BEFORE correction):
    pano_camera0: [1.9188, -1.1703, 1.2838]
    pano_camera1: [1.9188, -1.1703, 1.2838]
    ...
    → max spread BEFORE: 0.000000
[RIG DIAG] Frame 'IMG_...' camera centres (AFTER correction):
    ...
    → max spread AFTER: 0.000000
```

This confirmed COLMAP's rig constraint was working perfectly (all 7 sensors co-located, spread = 0.000000), and that the t-fix preserved this correctly.

---

### Problem 3: cameras.html looked "all over the place" even after the data was correct

With the rig structure proven correct (spread = 0.000000, anchor DIAG = 0.000°), the visualizer still looked chaotic. The cause: **196 photo planes at 28 positions, 7 overlapping at each position, each pointing in a different direction**. This is visually overwhelming even when mathematically correct.

**Fix:** None needed in the data. The user unchecked "Photos" in the `cameras.html` controls to show only the white frustum wireframes. With photos hidden, the 28 compact rig clusters along the camera path were clearly visible and correctly oriented.

---

### Problem 4: pycolmap CameraMap API

`rec.cameras` in pycolmap 4.x is a `CameraMap`, not a Python dict. It does not support `.get()`. Calling `rec.cameras.get(cam_id)` raised `AttributeError`.

**Fix:** Replace with bracket access and membership test:
```python
if cam_id not in rec.cameras:
    continue
cam = rec.cameras[cam_id]
```

---

### Problem 5: Firebase backpressure stalling COLMAP

`report()` (a Firebase write) was called synchronously inside the stdout-reading loop for the COLMAP worker subprocess. COLMAP logs hundreds of lines during matching and mapping, each triggering a Firebase write. When any Firebase call was slow, the stdout pipe buffer filled, causing the worker to block on `print()`, causing the COLMAP CLI to block on its own stdout — appearing as a stall.

**Fix:** Deduplicate Firebase writes in `_make_reporter` — only push to Firebase when `(stage, pct)` changes:
```python
_last: list = [None]
def report(stage, pct, message, detail=None):
    print(f"  [{stage.value}] {pct}% — {message}")
    if on_progress:
        key = (stage, pct)
        if key != _last[0]:
            _last[0] = key
            on_progress(StageProgress(...))
```

---

### Problem 6: CLI sequential matcher stalling at image [5/196]

The COLMAP CLI sequential matcher was stalling with default settings (`quadratic_overlap=1`, `overlap=10`). With quadratic overlap enabled, image 5 would be matched against up to ~20 neighbours including within-rig-frame pairs (same position, different sensor). These near-zero-baseline pairs cause RANSAC to loop on degenerate geometry.

**Fix:** Disable quadratic overlap and use a linear window of 7 (one full rig frame worth of neighbours):
```python
cli_args += [
    "--SequentialMatching.overlap",           "7",
    "--SequentialMatching.quadratic_overlap", "0",
]
```

---

### Final working state

- `_correct_camera_pitches_from_extraction` in `colmap_runner.py`: sets each camera's R to known extraction pitch + COLMAP yaw + zero roll, updates t to preserve camera centre.
- `_measure_anchor_pitch`: reads actual anchor DIAG pitch from images.txt (not formula-based).
- `visualize_cameras.py`: parses images.txt directly (bypassing pycolmap's cam_from_world convention), uses `cam_id not in rec.cameras` guard.
- Pitch/yaw display in cameras.html: computed from actual forward vector, not hardcoded.

---

### Problem 7: Pitch readout sign was inverted after switching from hardcoded to computed

When the hover readout was changed from hardcoded `KNOWN_PITCH_DEG` to a per-camera computed value, the formula used was:
```javascript
Math.asin(THREE.MathUtils.clamp(-_fw.dot(_fdata.avgUp), -1, 1))
```
This gives the **DIAG pitch convention** (positive = pointing down), so siblings displayed as **+10.0°**. But the readout is meant to mirror the known **extraction pitch convention** (negative = pointing below horizon, matching `pitch_angles` in settings, e.g. `-10°`). Because the old hardcoded value happened to be `-10.0°`, the sign flip wasn't noticed until the anchor (which should read `0.0°`) was fixed and the siblings' sign became visibly wrong by comparison.

**Fix:** Drop the negation so the readout matches extraction-pitch sign convention:
```javascript
const _pitchDeg = THREE.MathUtils.radToDeg(
  Math.asin(THREE.MathUtils.clamp(_fw.dot(_fdata.avgUp), -1, 1)));
```
Anchor now reads `0.0°`, siblings read `-10.0°` — matching `pitch_angles` exactly. This was purely a hover-text display bug; it never affected the actual camera poses in `images.txt` or the 3D frustum orientations.

---

### Problem 8: Removed the points3D global-rotation "gravity align" step; exposed translation correction as its own toggle

Even after the per-camera pitch fix made the frustums point the correct direction, Brush training quality was still suspect. Two structural issues were identified as candidates:

1. **The points3D correction was never upgraded to the per-camera approach.** `_run_perspective_rig` was still rotating the entire point cloud by a *single global* `R_X(-anchor_pitch_before)` rotation (via `_fix_points3D`), gated by the same `colmap_gravity_align` setting that also gated the (correct, per-camera) camera fix. This is exactly the "global R_X is the wrong tool" problem from Problem 1 — just applied to points instead of cameras. Since the real per-camera pitch correction is *not* a single global rotation (each camera gets a different correction depending on its yaw), rotating the points by one global angle could leave them inconsistent with the corrected camera frusta, especially for frames whose yaw was far from whatever heading dominated the anchor-pitch measurement.

2. **The camera-center-preserving translation recompute (`t_new = -R_new @ center`) was bundled into the pitch correction with no way to disable it**, making it impossible to test in isolation whether *that* specific step — not the rotation — was responsible for a downstream Brush quality regression.

**Fix:**
- Deleted the points3D global-rotation step entirely (`_fix_points3D`, `_fix_images`, and the now-fully-dead `_apply_gravity_alignment` were removed from `colmap_runner.py`). `points3D.txt` is no longer touched — it stays in COLMAP's raw output frame.
- Replaced the single `colmap_gravity_align` setting with two independent, UI-exposed settings:
  - `colmap_correct_pitch` (default `True`) — whether to run the per-camera pitch/yaw/roll correction at all.
  - `colmap_correct_translation` (default `True`) — whether, while correcting pitch, to also recompute `TX TY TZ` to keep the camera's world position fixed. Set to `False` to leave translation exactly as COLMAP wrote it (only `QW QX QY QZ` change), to A/B test whether the translation recompute itself is contributing to any Brush training regression.
- `_correct_camera_pitches_from_extraction` now takes a `preserve_center: bool` parameter controlling this.

**Still open:** whether the point cloud now being left in COLMAP's raw (biased) frame, while cameras are corrected, introduces its own inconsistency for Brush's initialization — this trade-off (global-rotation approximation vs. no correction at all) has not yet been resolved, only made independently testable.
