# RigGlueMap — End-to-End Implementation

## What it is

RigGlueMap is a rig-aware SfM (Structure-from-Motion) pipeline that reconstructs camera poses for a panoramic multi-sensor rig by running global SfM (Pi3/GlueMap) on a single anchor sensor and then mathematically expanding those poses to all other sensors using the known rig geometry baked into the view extraction.

The key insight: Pi3 is slow because it must reason about hundreds of images at once. The rig cameras at a given frame share a physical position — only their orientations differ, and those orientations are exactly known from the extraction angles. So: run Pi3 on just one sensor's ~25 images, get 25 rig-body positions, then compute all other sensors' poses by matrix multiplication. This is ~13× faster than running Pi3 on the full image set.

---

## Directory Layout

```
<project>/
  02_views/
    <frame_name>/                    # one folder per source frame
      <frame_name>_view_00_p+00_y00.jpg   # horizon ref (if enabled)
      <frame_name>_view_01_p-30_y00.jpg   # pitch -30°, yaw index 0
      <frame_name>_view_02_p-30_y01.jpg   # pitch -30°, yaw index 1
      ...
      <frame_name>_view_06_p-30_y05.jpg   # pitch -30°, yaw index 5

  03_alignment/
    colmap/
      images/
        pano_camera0/    # horizon ref sensor — one JPG per frame
          <frame_name>.jpg
        pano_camera1/    # pitch -30°, yaw index 0
          <frame_name>.jpg
        ...
        pano_camera6/    # pitch -30°, yaw index 5
    rigsfm/
      anchors/
        pano_anchor/     # anchor sensor images (copy of pano_camera{N})
      pi3_output/        # GlueMap/Pi3 COLMAP binary output
      colmap.db          # SIFT feature database
      colmap.matched     # marker: feature extraction+matching done
      colmap.guided      # marker: guided re-matching ran or was attempted
      sparse_txt/        # cameras.txt + images.txt + points3D.txt
      cameras.html       # Three.js visualizer (auto-opened after run)

  04_training/
    brush_input/         # final output: images/ + COLMAP text files
```

---

## Sensor Naming Convention

The view extractor (`panorama_processing.py`) produces files named:

```
<frame_name>_view_{i:02d}_p{sign}{pitch}_y{yaw_idx}.jpg
```

where `i` is a sequential integer starting at 0. `_reorganize_views()` strips everything but the frame name and puts the file into `pano_camera{i}/`. So:

| Filename suffix | pano_camera index | Meaning |
|---|---|---|
| `_view_00_p+00_y00` | `pano_camera0` | Horizon ref (pitch 0°, yaw 0°) |
| `_view_01_p-30_y00` | `pano_camera1` | Pitch −30°, yaw index 0 = 0° |
| `_view_02_p-30_y01` | `pano_camera2` | Pitch −30°, yaw index 1 = 60° |
| `_view_03_p-30_y02` | `pano_camera3` | Pitch −30°, yaw index 2 = 120° |
| `_view_04_p-30_y03` | `pano_camera4` | Pitch −30°, yaw index 3 = 180° |
| `_view_05_p-30_y04` | `pano_camera5` | Pitch −30°, yaw index 4 = 240° |
| `_view_06_p-30_y05` | `pano_camera6` | Pitch −30°, yaw index 5 = 300° |

The yaw angle in degrees = `yaw_index × (360 / yaw_steps)`. With `yaw_steps=6` and negative pitch angles, the offset is 0° (offset only applies to positive-pitch rings). With `yaw_steps=6` and a positive-pitch ring, offset = 30°, so yaw index 0 = 30°, index 1 = 90°, etc.

**The UI sensor numbering matches this directly.** `buildRigSensorOptions()` increments the same sequential index starting at 0, with the same horizon-ref conditional. UI sensor `#N` = `pano_camera{N}` always.

---

## Step-by-Step Pipeline

### Step 0 — UI: Anchor Sensor Selection

`buildRigSensorOptions(pitchAngles, yawSteps, horizonRef)` in `App.jsx` generates a dropdown with entries like `#1 — pitch -30°, yaw 0°`. The `value` of each option is `String(idx)` where `idx` is the sequential pano_camera index. Selecting option #1 sets `settings.rigsfmAnchorSensor = 1`.

This value maps via the settings bridge to `rigsfm_anchor_sensor: 1` in `PipelineSettings`. In the pipeline: `anchor_idx = int(getattr(settings, "rigsfm_anchor_sensor", 0))`.

**Anchor thumbnail preview:** The `AlignmentTab` `useEffect` crops a region from the raw source equirectangular images (the ones in `jobFiles`) to give a visual preview of what the selected anchor will look like. The crop uses:

```javascript
const cx = (((yaw + 180) % 360) / 360) * IW;   // +180° because the equirectangular seam is at the back
const cy = IH / 2 - (pitch / 90) * (IH / 2);   // pitch -30° → lower third of image
```

The +180° offset exists because the equirectangular seam (pixel x=0) is at yaw=180° (rear of camera); the front-facing direction is at pixel x=IW/2.

---

### Step 1 — View Reorganisation (`_reorganize_views`)

File: `splatpipe_core/colmap_runner.py:83`

Reads `02_views/<frame_dir>/<frame>_view_{i:02d}_p...jpg` and copies them to:
```
colmap/images/pano_camera{i}/<frame_name>.jpg
```

The regex `r'^(.+)_view_(\d+)_p'` extracts `frame_name` (group 1) and `view_idx` (group 2). The sensor directory name is literally `f"pano_camera{view_idx}"`. Skipped if directories already exist and contain files.

---

### Step 2 — Anchor Staging

File: `splatpipe_core/rigsfm_runner.py:302`

Two modes controlled by `rigsfm_quad_anchors`:

**Single-anchor mode (`rigsfm_quad_anchors=False`, default):**
Copies all images from `colmap/images/pano_camera{anchor_idx}/` into `rigsfm/anchors/pano_anchor/`. Pi3 runs on these N single-sensor images and returns one pose per station.

**Quad-anchor mode (`rigsfm_quad_anchors=True`):**
Runs `_stage_quad_anchors()` which extracts 4 perspective crops per equirectangular source frame (from `01_frames/`) at yaw 0°/90°/180°/270°, pitch 0°, and writes them as `{orig_stem}_h{0-3}.jpg`. Images are named station-first so Pi3's sequential ordering sees a full 360° rotation at each station before advancing to the next, giving strong within-station loop closure.

This produces 4× more images for Pi3 (N stations × 4 crops = 4N images) and requires a subsequent aggregation step (Step 4b) to collapse the 4 crop poses per station into a single rig body pose.

**Why quad anchors exist:** A single perspective crop gives Pi3 one view direction per station. Four crops give four independent estimates of R_rig (all should agree after un-rotating by the known extraction yaw), providing redundancy and a 360° constraint on the rig orientation at each station.

In both modes, `rigsfm/anchors/` contains exactly one subdirectory: `pano_anchor/`. This is the `anchor_parent_dir` passed to Pi3 (`--images_path anchors/`, `--intrinsics_mode PER_FOLDER`).

> **Bug fixed (2026-07-13):** `rigsfm_quad_anchors` was never read by `_build_settings()` — it was present in `PipelineSettings` and in `fieldraven.json` but not mapped in either the INI or UI settings blocks. All runs prior to this fix used single-anchor mode even when `rigsfm_quad_anchors: "true"` was set. Fix: `_build_settings()` now loads `fieldraven.json["settings"]` into `cfg` and `rigsfm_quad_anchors` is read from both `cfg` and `_ui_settings`.

---

### Step 3 — Pi3 / GlueMap on Anchor Images

File: `splatpipe_core/rigsfm_runner.py:167` (`_run_pi3_anchors`)

Calls GlueMap inside WSL2:

```bash
wsl -d Ubuntu-22.04 -- micromamba run -n gluemap gluemap-demo \
  --images_path /mnt/c/.../rigsfm/anchors \
  --write_path  /mnt/c/.../rigsfm/pi3_output \
  --intrinsics_mode PER_FOLDER \
  --chosen_model pi3 \
  --is_sequential \
  [--skip_doppelgangers]
```

Pi3 runs its full pipeline (SALAD retrieval → two-view covisibility → multi-view inference → global BA) on the 25 anchor images only. Output is a standard COLMAP binary reconstruction (`cameras.bin`, `images.bin`, `points3D.bin`) inside `pi3_output/gluemap_aba/`.

If `pi3_output/` already contains a valid COLMAP reconstruction the step is skipped (resume support).

---

### Step 3b — Pi3 Caching and Resume

If `pi3_output/` already contains a valid COLMAP reconstruction the entire anchor staging + Pi3 step is skipped. **Consequence:** if you change `rigsfm_quad_anchors` from False to True, you must delete `pi3_output/` to force re-staging and a fresh Pi3 run, otherwise the cached single-anchor poses will be reused with the quad-anchor expansion path and give wrong results.

---

### Step 4 — Parse Pi3 Poses

File: `splatpipe_core/rigsfm_runner.py:78` (`_parse_pi3_poses`)

Reads the Pi3 COLMAP binary output using `pycolmap.Reconstruction`. For each registered image:

```python
cfw = img.cam_from_world          # Rigid3d in pycolmap 4.x
R   = np.array(cfw.rotation.matrix(), dtype=np.float64)
t   = np.array(cfw.translation, dtype=np.float64).flatten()
```

Returns `{image_name: (R_3×3, t_3)}` where `image_name` is something like `pano_anchor/IMG_20260708_181826_00_254.jpg`.

The convention is COLMAP standard: `X_cam = R @ X_world + t`. The translation `t` is the negative of the camera centre in world space: `t = -R @ C_rig`.

---

### Step 4b — Quad Pose Aggregation (`_aggregate_quad_poses`) *(quad mode only)*

File: `splatpipe_core/rigsfm_runner.py:280`

Takes the 4 Pi3 poses per station (one per horizon crop h0–h3) and collapses them into a single rig-body pose `(R_rig, t_rig)`:

```python
R_rig_estimate = quad_abs_rots[h_idx].T @ R_crop   # undo the baked-in yaw
t_rig_estimate = quad_abs_rots[h_idx].T @ t_crop

R_rig = SVD_orthogonalise(mean(R_rig_estimates))    # proper SO(3) average
t_rig = mean(t_rig_estimates)
```

The rotation average is re-orthogonalised via SVD to stay on SO(3). The translation average uses a plain mean of the 4 estimates.

**Why the within-station translation average is safe:** The 4 crops at a single station have NO parallax between them — they are perspective extractions of the same equirectangular image and therefore share the same physical optical center. Pi3 has no parallax signal to drive within-station translation differences; any inter-crop translation it estimates is noise. The plain average suppresses this noise. The actual station positions (C_rig trajectory) are determined by cross-station same-direction pairs (N_stn1 ↔ N_stn2 etc.) which DO have real parallax from the physical camera movement.

Also writes `rigsfm/pi3_quad_poses.json` — a sidecar used by `visualize_cameras.py` to display the 4 crop direction rays at each station in the Three.js viewer.

---

### Step 5 — Rig Expansion (`_expand_rig_poses`)

File: `splatpipe_core/rigsfm_runner.py:119`

This is the core math. Pi3 gives `(R_a, t_a)` — the cam_from_world pose for the anchor sensor at each frame. Each sensor's extraction rotation `abs_rots[j]` is the known `cam_from_pano` rotation that was used to crop its view from the equirectangular panorama.

**Formula:**

```
R_j = abs_rots[j] @ abs_rots[anchor_idx].T @ R_a
t_j = -(R_j @ C_rig)    where C_rig = -R_a.T @ t_a
```

**Derivation:**

- `R_a` = `abs_rots[anchor_idx] @ R_rig_world` — the anchor sensor's cam_from_world pose has the anchor's extraction rotation baked in, applied to the raw rig-body orientation.
- `abs_rots[anchor_idx].T @ R_a` = `R_rig_world` — undoes the anchor's extraction rotation, recovering the rig-body's raw world orientation.
- `abs_rots[j] @ R_rig_world` = `R_j` — re-applies sensor j's own extraction rotation.
- `C_rig = -R_a.T @ t_a` — the rig's physical optical centre, shared by all sensors at the same frame (all sensors are co-located at a single point).
- `t_j = -R_j @ C_rig` — reconstructs sensor j's translation in COLMAP convention.

All sensors per frame share the same `C_rig`. The expansion produces `n_frames × n_sensors` pose entries, keyed as `"pano_camera{j}/{frame_filename}"`.

---

### Nodal Point Alignment — Mathematical Guarantee

**Why all sensors share one optical center by construction:**

An equirectangular image is a single-point projection. Every pixel encodes a direction from ONE physical point — the camera nodal point. There is no parallax within a single equirectangular frame. All perspective crops (quad anchors, and all 13 pano_camera sensors) are virtual pinhole cameras located at that same single point; they differ only in orientation.

In COLMAP convention: `C = -R.T @ t`. After expansion:

```
t_j = -(R_j @ C_rig)
C_j = -R_j.T @ t_j = -R_j.T @ (-(R_j @ C_rig)) = C_rig   ✓
```

This is an algebraic identity that holds for ALL `j` regardless of rotation. The nodal co-location cannot be broken by the expansion step itself.

**Confirmed by measurement (CPR Trail run, 2026-07-13):**
After rig-constrained BA, spread per station = `max=0.000418, mean=0.000126` scene units, **identical at every station**. This is floating-point noise from 9-decimal quaternion text serialisation — not real deviation. All 13 sensors at each frame genuinely share the same rig optical center.

**What the spread chart shows:** The spread bar chart in `cameras.html` uses an absolute reference scale (`absRef = max(1.2 × maxVal, 1e-3)`). When rig BA is working correctly, all bars should be near-zero and green. The max value is printed in the top-left corner of the chart.

**What visual misalignment in cameras.html actually is:**
Frustum clusters that look "tilted" or "fanned out" are rotation errors in `R_rig`, not translation/nodal-point errors. If Pi3's orientation estimate for a station is off by even 0.1°, all 13 sensors at that station point in slightly wrong directions. The optical centers are still co-located; the directions are wrong. This cannot be fixed by BA on the rig (which refines positions) — it requires either better Pi3 convergence, better `abs_rots` calibration, or a rotation-only refinement pass on `R_rig`.

**What should NOT be done:** Running standard (non-rig) BA on the sensor images to "fix" misalignment. This breaks the co-location guarantee by letting BA move sensors independently. The correct approach is: Pi3 estimates `R_rig` and `C_rig` for each station; the rig is expanded rigidly; rig-constrained BA refines only `rig_from_world` per frame (one 6-DoF pose for all 13 sensors together).

**Previous (broken) formula:** Used `_local_rotations()` which returned relative rotations (`R_j @ R_0.T`) and the expansion formula tried to undo and redo the reference-sensor factor but re-introduced `R_0` incorrectly for any `anchor_idx != 0`. This caused sibling cameras to drift away from the rig centre when using a non-horizon-ref anchor.

---

### Rig Rotation Mathematics (`_cam_from_pano` / `_virtual_rotations`)

File: `splatpipe_core/colmap_runner.py:121` and `147`

`_cam_from_pano(yaw_deg, pitch_deg)` builds the cam_from_pano rotation that matches exactly what `panorama_processing.py`'s `look_at_rotation()` uses to extract each view:

```python
direction = [sin(yaw)*cos(pitch), sin(pitch), cos(yaw)*cos(pitch)]
right      = normalize(cross(up, direction))
true_up    = cross(direction, right)
R_world_from_cam = stack([right, true_up, direction], axis=1)
return R_world_from_cam.T   # cam_from_world convention
```

Note: the pitch sign is **inverted** when calling `_cam_from_pano` from `_virtual_rotations`:

```python
rots.append(_cam_from_pano(y, -p))  # p = extraction pitch (e.g. -30.0)
```

Because `panorama_processing.py`'s pitch convention is the extraction direction (negative = look down), while COLMAP's world-frame DIAG pitch is the opposite sign. Passing `-p` makes the rotation match the visual result confirmed in the COLMAP GUI.

`_virtual_rotations(yaw_steps, pitch_angles, horizon_ref)` returns one rotation matrix per sensor in `pano_camera` order:

- If `horizon_ref=True`: first entry is `_cam_from_pano(0, 0)` = identity (sensor 0)
- For each `p` in `pitch_angles`: iterate `yaw_steps` angles starting at 0° (offset = 0 for negative pitch, 360/yaw_steps/2 for positive pitch)

The result matches the sequential `view_{i:02d}` numbering produced by the view extractor.

---

### Step 6 — SIFT Extraction, Matching, Rig Wiring, Triangulation, BA

Payload is serialised to a temp JSON file (to avoid Windows 32 KB CreateProcess limit) and passed to `rigsfm_worker.py` running under Python 3.14 / pycolmap 4.0.4.

File: `splatpipe_core/rigsfm_worker.py`

#### 6a. SIFT Feature Extraction

One camera model per folder (`PER_FOLDER` / `CameraMode.PER_FOLDER`), `SIMPLE_PINHOLE` model. Focal length computed from FOV and image size:

```python
focal = image_size / (2.0 * tan(radians(fov) / 2.0))
```

GPU path: delegates to `colmap.exe feature_extractor` CLI (requires `colmap_bin` setting). CPU path: uses `pycolmap.extract_features()`.

#### 6b. Apply Rig Config to DB *(new)*

Before matching, `_build_rig_config(abs_rots_list, anchor_idx)` constructs a `pycolmap.RigConfig`:

- Anchor sensor: `ref_sensor=True`, no `cam_from_rig` offset
- Non-anchor sensor j: `cam_from_rig = Rigid3d(abs_rots[j] @ abs_rots[anchor].T, zero_t)`

`pycolmap.apply_rig_config([rig_config], db)` is then called on the open database. This wires each image into a frame based on its `pano_camera{j}/` prefix, enabling the matcher to know which images share a physical location.

#### 6c. Feature Matching

Always **exhaustive** — sequential matching misses cross-sensor correspondences between sensors at different timestamps. Same-frame zero-baseline pairs are suppressed via `FeatureMatchingOptions.skip_image_pairs_in_same_frame = True`, which works because the DB has frame membership from step 6b.

**GPU path (vocab tree set):** runs `colmap.exe vocab_tree_matcher` with a frame-windowed pair list (`--VocabTreeMatching.match_list_path pairs.txt`). The pair list bypasses vocab-tree retrieval — only the ~18,590 specified pairs are matched (all sensor combinations within a 5-frame window). Requires `colmap_vocab_tree` setting pointing to a SIFT vocab tree binary (e.g. `vocab_tree_flickr100K_words32K.bin` from COLMAP releases). Roughly 3× fewer pairs than full exhaustive.

**GPU path (no vocab tree):** runs `colmap.exe exhaustive_matcher` (~52K pairs for 325 images).

Both GPU paths pass `--FeatureMatching.guided_matching 1` — a second SIFT pass guided by the estimated E/F matrix, recovering correspondences that passed RANSAC but failed the ratio test. Both GPU paths also touch `colmap.guided` immediately after matching, so step 6e is permanently bypassed for new runs (guided matching already completed in this step).

**CPU path:** runs `pycolmap.match_exhaustive` with `guided_matching=True` and `skip_image_pairs_in_same_frame=True`, then touches `colmap.guided`.

> **`--FeatureMatching.rig_verification` removed:** this flag (COLMAP 3.13+) constrains RANSAC using known `sensor_from_rig` relative poses. In a sequential rig setup it would only apply correctly to same-frame pairs (zero-baseline, degenerate E-matrix). The flag was added and then removed after a run with it set produced 0 triangulated points when the guided step below failed and cleared `two_view_geometries`.

> **Previous approach (removed):** `_purge_same_frame_pairs()` read all two-view geometry pairs from the DB after matching and deleted any pair sharing the same frame stem. This was replaced by the structural `skip_image_pairs_in_same_frame` approach above.

#### 6d. Seed Reconstruction with Known Poses

`pycolmap 4.x` makes `cam_from_world` completely read-only — neither post-construction assignment nor constructor kwargs work. Workaround: write the expanded poses as COLMAP text format (`cameras.txt` / `images.txt` / `points3D.txt`) to a temp directory, then read them back with `pycolmap.Reconstruction.read_text()`. This causes pycolmap to construct the `Reconstruction` correctly with all poses set.

Image entries use the format `pano_camera{j}/{frame_name}.jpg` to match the SIFT database image names.

#### 6d-ii. Layer Rig/Frame Structure onto Reconstruction *(new)*

After text-format seeding, `pycolmap.apply_rig_config([rig_config], db, recon)` layers the frame/rig structure onto the reconstruction. This groups images into frames and registers sensors. The anchor's `cam_from_world` is then copied into `frame.rig_from_world` and `recon.register_frame()` is called for each frame:

```python
frame.rig_from_world = pycolmap.Rigid3d(Rotation3d(R_anchor), t_anchor)
recon.register_frame(frame_id)
```

This enables `triangulate_points` to resolve poses through `sensor_from_rig @ rig_from_world` instead of independent per-image poses. Log line: `Rig structure applied — 25/25 frames posed`.

#### 6e. Guided Re-Matching (old cached DBs only)

**For new runs this block is never reached** — step 6c sets `colmap.guided` immediately after matching (both GPU paths now run `guided_matching=1` in step 6c itself).

This block only fires for old cached DBs where `colmap.matched` exists but `colmap.guided` does not. Strategy:

1. Clear only `two_view_geometries` from the DB (raw SIFT matches kept — COLMAP skips SIFT re-extraction for pairs that already have raw matches)
2. Re-run geometric verification via `_run_guided_cli()` with guided matching

**`_run_guided_cli(colmap_bin, db_path)`** calls `colmap.exe exhaustive_matcher --FeatureMatching.guided_matching 1`. Using the COLMAP CLI binary avoids the FAISS C++ `std::terminate` that crashed the previous `_run_guided_subprocess()` approach on Windows. `_run_guided_subprocess()` is kept as a fallback for the CPU path.

**Failure handling:** if the CLI fails, the worker now raises immediately and **resets both markers** (`colmap.matched` and `colmap.guided` are deleted). The next run re-does matching from scratch. Previously, the worker silently continued with empty `two_view_geometries`, which caused 0 triangulated points — the bug that forced this redesign.

> **Root cause of 0-point failure (2026-07-12):** the guided step cleared `two_view_geometries` before invoking the CLI, but the CLI was called with `--ExhaustiveMatching.guided_matching` (wrong option group — should be `--FeatureMatching.guided_matching`). COLMAP rejected it; the worker caught the exception and continued with empty `two_view_geometries`. Triangulation produced 0 points. Fixed by: (1) correct flag name in `_run_guided_cli`, (2) step 6c now sets `guided_marker` immediately so this block is never reached for GPU paths, (3) failure now raises instead of silently continuing.

#### 6f. Triangulation

```python
recon = pycolmap.triangulate_points(reconstruction=recon, database_path=..., image_path=..., output_path=...)
```

Uses known poses + SIFT correspondences to compute 3D point positions. With the rig/frame structure active (step 6d-ii), triangulation uses the full sensor array per frame. Writes intermediate result to `sparse_txt/`. Post-triangulation rig state is logged for diagnostics.

#### 6g. ABA (Bundle Adjustment)

Two-pass: BA → re-triangulate → BA. Fixed intrinsics. Re-triangulation after the first BA recovers points initially rejected by stricter angular thresholds.

**GPU path:** uses `colmap.exe bundle_adjuster` with GPU Ceres backend (COLMAP 4.1+):

```
colmap bundle_adjuster
  --input_path / --output_path sparse_txt/   (reads and writes reconstruction in-place)
  --BundleAdjustment.refine_focal_length 0
  --BundleAdjustment.refine_principal_point 0
  --BundleAdjustment.refine_extra_params 0
  --BundleAdjustment.refine_sensor_from_rig 0   (keeps calibrated rig geometry fixed)
  --BundleAdjustmentCeres.use_gpu 1              (Caspar GPU Ceres, ~10-100× faster)
```

`refine_rig_from_world=1` (default) optimises one 6-DoF `rig_from_world` pose per frame using tracks from all 13 sensors simultaneously — rig-constrained BA without the pycolmap C++ CHECK crashes.

**Why the rig structure survives the binary round-trip:** `pycolmap.Reconstruction.write_binary()` writes `rigs.bin` and `frames.bin` alongside the standard `cameras.bin`, `images.bin`, `points3D.bin`. COLMAP CLI reads all five files. The rig constraints are therefore active in the CLI BA.

**Confirmed working (CPR Trail, 2026-07-13):** BA1 parameter count = 154,901. Breakdown: 25 frames × 6 DOF = 150 rig pose params + 51,583 points × 3 = 154,749 point params → total 154,899 ≈ 154,901 ✓. If cameras were free: 325 × 6 = 1,950 camera params → total would be ~156,708. The observed count matches the rig model, not the free-camera model. Spread after BA = 0.000418 scene units (floating-point noise).

**CPU path:** falls back to `pycolmap.bundle_adjustment()` (Ceres CPU, fixed intrinsics).

> **Previous approach:** `create_default_bundle_adjuster` with `set_constant_sensor_from_rig_pose` was attempted via pycolmap but caused C++ CHECK aborts on Windows in 3 attempts. The CLI approach achieves equivalent rig-constrained BA without the crash.

> **Previous approach (removed):** `_enforce_rig_poses()` re-derived sibling poses from the anchor after each BA pass to counteract drift. Deleted once the structural rig seeding (step 6d-ii) was confirmed working.

#### 6h. Write sparse_txt

Final `recon.write_text(sparse_txt)` produces `cameras.txt`, `images.txt`, `points3D.txt` in standard COLMAP text format.

Progress lines are prefixed `WORKER_PROGRESS:<pct>:<message>` on stdout, consumed by the parent process and mapped to pipeline progress (37–90%).

---

### Step 7 — Camera Visualizer

File: `splatpipe_core/rigsfm_runner.py:478`; `tools/visualize_cameras.py`

After the worker completes, `visualize_cameras.py` is called under Python 3.14:

```python
subprocess.run([_PYTHON_314, _VISUALIZER,
    str(sparse_txt), str(_viz_html),
    "0", "0", f"pano_camera{anchor_idx}"])
```

The visualizer reads `images.txt` and `cameras.txt`, derives camera centres (`C = -R.T @ t`), converts to Three.js coordinate space (COLMAP Y-down/Z-fwd → Three.js Y-up/Z-toward-viewer), embeds base64 JPEG thumbnails for each camera, and writes a standalone `cameras.html`.

The HTML has:
- **Anchor sensor selector:** dropdown listing all `pano_cameraX` names; chosen sensor shown in orange, siblings in blue-to-green gradient
- **Reveal slider:** steps through non-anchor sensors one at a time across all frames (not per-frame groups), enabling progressive inspection
- **Image size slider:** default 0.15×, range 0.05–0.6×
- **Fade slider:** defaults to 100%
- **Photo sphere:** click any frustum to see its full image in a sphere viewer

The HTML is auto-opened in the system browser on completion.

---

### Step 8 — brush_input Population

File: `splatpipe_core/rigsfm_runner.py:492`

Copies `colmap/images/` (all sensor subdirectories) to `brush_input/images/`, then calls `_sample_and_write_colored_recon()` (from `gluemap_runner.py`) which reads the triangulated points, samples colour from the images for each observed point, and writes the coloured `cameras.txt`, `images.txt`, `points3D.txt` into `brush_input/`. This is the format consumed by Brush for Gaussian Splatting training.

---

## Python Environment Split

| Process | Python | Purpose |
|---|---|---|
| Backend server | 3.13 | Runs pipeline orchestration, manages state, spawns subprocesses |
| `rigsfm_worker.py` | 3.14 (pycolmap 4.0.4) | SIFT extraction, matching, reconstruction, triangulation, BA |
| `visualize_cameras.py` | 3.14 | PIL thumbnail generation, HTML output |
| GlueMap / Pi3 | WSL2 (Ubuntu-22.04, micromamba `gluemap` env) | Neural pose estimation |

The split exists because `pycolmap 3.12.4` (Python 3.13) is missing `FeatureMatchingOptions` and has broken `camera_params` bindings. `pycolmap 4.0.4` (Python 3.14) fixes these but makes `cam_from_world` read-only, requiring the text-format workaround in the worker.

---

## Settings (`PipelineSettings`)

| Field | Default | Description |
|---|---|---|
| `run_rigsfm` | `False` | Enable this pipeline mode |
| `rigsfm_anchor_sensor` | `0` | pano_camera index used as Pi3 anchor (single-anchor mode only) |
| `rigsfm_quad_anchors` | `False` | Select 4 evenly-spaced rendered sensor views per station as Pi3 anchors instead of 1; uses existing `pano_camera{i}/` images — no equirectangular source files required |
| `rigsfm_matcher` | `"sequential"` | Overridden to exhaustive in worker; this setting is accepted but not used |
| `pitch_angles` | `[-30.0]` | Extraction pitch angles (same as COLMAP/GlueMap) |
| `yaw_steps` | `6` | Views per pitch ring |
| `horizon_ref` | `True` | Prepend pitch-0° sensor as pano_camera0 |
| `fov` | `94.6` | Sensor field of view in degrees |
| `gluemap_backbone` | `"pi3"` | Pi3, Pi3x, VGGT, or map_anything |
| `gluemap_skip_doppelgangers` | `True` | Skip two-view covisibility (faster) |
| `gluemap_num_neighbors` | `100` | SALAD retrieval neighbours |
| `gluemap_batch_size` | `60` | Inference batch size (reduce for <16 GB VRAM) |
| `gluemap_wsl_home` | `"/home/decosson"` | WSL2 home for checkpoint paths |
| `gluemap_wsl_distro` | `"Ubuntu-22.04"` | WSL2 distribution |

---

## What Was Modified vs Original Design

| Area | Change | Reason |
|---|---|---|
| `_local_rotations` → `_abs_rotations` | Replaced relative-to-reference rotations with absolute cam_from_pano rotations | Previous relative rotations introduced a spurious `R_0` factor in the expansion formula for any `anchor_idx != 0`, causing sibling cameras to drift from the rig centre |
| `_expand_rig_poses` formula | Changed from `R_j = (R_j_rel @ R_0.T) @ (R_a @ R_0 @ R_anchor.T)` to `R_j = abs_rots[j] @ abs_rots[anchor_idx].T @ R_a` | Correct derivation using absolute rotations; no spurious reference-sensor conjugate |
| `pycolmap.Image.cam_from_world` assignment | Replaced direct assignment with write-COLMAP-text / read-back workaround | pycolmap 4.0.4 makes `cam_from_world` fully read-only (constructor kwarg also silently ignored) |
| Feature matching | Always exhaustive, regardless of `rigsfm_matcher` setting | Sequential matching misses inter-sensor correspondences between temporally separated frames of different sensors |
| `_purge_same_frame_pairs` → `skip_image_pairs_in_same_frame` | Deleted post-hoc DB pair deletion; replaced with `FeatureMatchingOptions.skip_image_pairs_in_same_frame = True` | Structural prevention is cleaner and requires no DB scan; works because `apply_rig_config` is now called before matching so the DB knows frame membership |
| Rig config applied to DB before matching | Added `_build_rig_config()` + `pycolmap.apply_rig_config([rig_config], db)` in worker step 2 | Wires frame/sensor structure into DB so the matcher can identify same-frame pairs and skip them; also groups cameras for future rig-constrained operations |
| Rig config applied to reconstruction after seeding | Added `pycolmap.apply_rig_config([rig_config], db, recon)` + `frame.rig_from_world` assignment in worker step 4b | Layers frame/rig structure onto the seeded poses so `triangulate_points` operates in rig-frame mode (`sensor_from_rig @ rig_from_world`) rather than independently per image |
| `_enforce_rig_poses` | Deleted | No longer needed; rig rigidity is maintained structurally by the frame/sensor seeding rather than enforced post-hoc after each BA pass |
| Guided matching — CPU path | Added `guided_matching=True` to `FeatureMatchingOptions` in step 3 | Second SIFT pass guided by E/F matrix recovers correspondences that passed RANSAC but failed the ratio test; increases inlier count without full re-extraction |
| Guided re-matching — GPU path (step 6e) | Originally a post-seeding guided pass; now bypassed for GPU paths | Step 6c now passes `--FeatureMatching.guided_matching 1` to the primary matcher and immediately touches `colmap.guided`; step 6e is only reached for old cached DBs that predate this change |
| `_run_guided_subprocess()` | New helper; kept as CPU fallback | pycolmap's FAISS backend triggers C++ `std::terminate` on Windows during guided matching on large GPU-built DBs; subprocess isolation prevents this from killing the main worker |
| `_run_guided_cli()` | Helper for step 6e fallback path | Uses `colmap.exe exhaustive_matcher --FeatureMatching.guided_matching 1`; CLI binary avoids FAISS crash. Step 6e failure now raises and resets both markers instead of silently continuing with empty `two_view_geometries` |
| `--FeatureMatching.rig_verification` | Added then removed | Added to constrain RANSAC using rig geometry; removed after a run produced 0 points — the flag is designed for same-frame multi-camera rigs (simultaneous capture), not sequential rig traversal where cross-frame pairs dominate |
| `--FeatureMatching.guided_matching 1` | Added to both GPU primary matching steps | Replaces the step 6e two-pass approach; guided matching now runs in the primary step and `colmap.guided` is set immediately, eliminating the fragile clear-then-maybe-fail pattern |
| `_run_ba_cli()` | New helper; runs GPU Ceres BA via CLI | `colmap bundle_adjuster --BundleAdjustmentCeres.use_gpu 1 --BundleAdjustment.refine_sensor_from_rig 0`; rig-constrained GPU BA without pycolmap C++ CHECK crashes |
| Bundle adjustment (step 6g) | Switched GPU path from pycolmap CPU BA to CLI GPU BA | GPU Ceres (Caspar, COLMAP 4.1+) is ~10–100× faster; `refine_sensor_from_rig=0` keeps calibrated rig fixed; `refine_rig_from_world=1` (default) optimises frame poses using all sensor tracks |
| Vocab tree matching support | Added `vocab_tree_path` payload field; step 3 uses `vocab_tree_matcher --match_list_path` when set | Reduces matching from ~52K pairs to ~18.5K pairs (frame window=5, all sensor combos); bypasses retrieval, uses pair list only; requires `colmap_vocab_tree` setting |
| `_generate_pair_list()` | New helper; generates frame-windowed pair list | Scans `pano_camera{i}/` folders, generates all si×sj pairs within frame_window=5 steps; saves to `colmap.pairs.txt` |
| stdout UTF-8 reconfigure | Added `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at worker startup | Prevents `UnicodeEncodeError` when emoji in `_prog()` messages hit cp1252 Windows console (`⚠️` previously crashed the worker) |
| `colmap.guided` marker | New resume marker alongside `colmap.matched` | Tracks whether guided re-matching completed or was attempted; prevents step 4c from retrying on re-run after a crash (crash before `guided_marker.touch()` was causing infinite failure loops) |
| `pipeline_runner.start()` cancel grace period | When `cancel_event` is already set but thread is alive, `join(timeout=15)` before proceeding | Allows user to cancel and immediately re-run without getting a 409 "already running" error while the previous thread is still cleaning up |
| Payload delivery | Temp file `@path` instead of inline JSON arg | Windows `CreateProcess` argument limit (~32 KB) exceeded by 325-pose payload |
| Anchor thumbnail preview in UI | +180° offset on equirectangular crop x-coordinate | Equirectangular seam is at pixel x=0 = yaw 180° (rear); yaw=0° is at x=IW/2 |
| `rigsfm_quad_anchors` settings loading | Added to `_build_settings` cfg + ui blocks; `fieldraven.json["settings"]` merged into cfg | Setting existed in dataclass and in fieldraven.json but was never read — all runs prior to 2026-07-13 were single-anchor mode regardless of the setting value |
| `_build_settings` fieldraven.json merge | `fieldraven.json["settings"]` is loaded and merged into `cfg` at the start of `_build_settings` | Project-level config (set via UI config panel and written to fieldraven.json) was not being read by the pipeline runner; settings like `rigsfm_quad_anchors`, `colmap_vocab_tree`, and `colmap_bin` were silently ignored |
| Spread chart in cameras.html | Absolute reference scale (`max(1.2 × maxVal, 1e-3)`) instead of relative (val/max) | All-same-value bars normalised to 1.0 = all red at full height, misleading when spread is uniform near-zero; absolute scale shows true deviation magnitude |
| Quad anchor aggregation (`_aggregate_quad_poses`) | New function; SVD rotation average + translation mean | Collapses 4 Pi3 crop poses per station into one rig-body pose; writes `pi3_quad_poses.json` sidecar for visualizer |
| Pi3 quad crop visualisation | `_write_quad_poses_json` + `quadCropGroup` in cameras.html | Direction rays (R/G/B/M for N/E/S/W) at each station show which directions the 4 anchor crops were pointing |
| HTML reveal slider | Changed from frame-grouped reveal to sensor-by-sensor reveal | Makes it easy to inspect one sibling sensor's placement across all frames at once |
| HTML image size default | 0.15×, range 0.05–0.6× | Previous 0.5× default was too large for dense captures |
| HTML fade default | 100% | Was initialising at partial opacity |
| Visualizer auto-open | Added `webbrowser.open()` after HTML generation | Convenience — otherwise user had to navigate to the file manually |

---

## Known Fundamental Limitations

### Multi-Lens Stitching Parallax

**The core issue:** The equirectangular images produced by cameras like the Insta360 Pro 2 are NOT single-point projections. The camera body has multiple physical lenses (e.g. 6 on the Pro 2) each located a few centimetres apart from one another. The stitching software composites these into a single equirectangular image by assuming all rays originate from a single virtual nodal point — but they do not.

The stitching algorithm typically optimises for a "parallax-free distance" (commonly ~2–3 m). At that depth, the inter-lens offset appears to cancel and the stitch is seamless. At closer or farther distances, actual parallax remains:

- Features at typical outdoor scene depth (> 5 m): stitching error is small relative to feature localisation error, probably negligible in practice.
- Features at close range (< 2 m): the per-lens offset (~2 cm) can shift feature positions by pixels or more depending on depth and which lens zone the feature falls in.
- Features that straddle a stitch boundary: may appear at slightly different geometric positions on either side, introducing feature matching bias across the boundary zone.

**Impact on the pipeline:**

The expansion formula (`C_j = C_rig` for all sensors) is algebraically correct — it holds given that the equirectangular is a true single-point projection. If the equirectangular violates this, then:
1. The "nodal point" of pano_camera0 (horizon ref) and the "nodal point" of pano_camera3 (pitch −30°, yaw 120°) may correspond to slightly different physical lens positions in the camera body.
2. Feature matches between sensors that cross lens zones in the equirectangular carry a small systematic position error.
3. The visual frustum misalignment seen in cameras.html may be partly real: the different sensor views are genuinely extracted from slightly different physical vantage points even within a single equirectangular frame.

**Why the algebraic guarantee still mostly holds:** For outdoor scenes with far-field features the effect is sub-pixel. The main risk is interior/close-range captures or sequences with objects very near the camera.

**Known workaround (not implemented):** Use raw fisheye images directly with a proper multi-camera rig model (one camera model per physical lens, correct extrinsic offsets between lenses). This requires: (a) access to pre-stitch fisheye frames, (b) accurate per-lens intrinsics, and (c) a SfM pipeline that supports this rig model. Insta360 Pro 2 does export individual fisheye images but this path is not currently part of the FieldRaven workflow.

**Current stance:** Accept the stitching approximation. For the typical CPR trail / outdoor capture use case the effect is small. Log this as a source of unexplained residual error if reconstruction quality is worse than expected indoors or on close-range subjects.

---

### Alternative Prior Estimators (SphereSfM / COLMAP Native Equirectangular)

The Pi3/GlueMap step (Step 3) provides initial per-station poses. Two alternatives that operate natively on equirectangular images were identified (2026-07-14):

**SphereSfM** — [github.com/json87/SphereSfM](https://github.com/json87/SphereSfM)
- A COLMAP fork that adds a `SPHERE` camera model for native ERP (equirectangular) input.
- Paper: *"3D Reconstruction of Spherical Images based on Incremental Structure from Motion"*, Int. Journal of Remote Sensing 2024 ([arXiv 2306.12770](https://arxiv.org/abs/2306.12770)).
- Runs directly on the equirectangular source frames — no view extraction or quad-anchor staging required.
- Supports multi-camera rig mode with fixed extrinsic constraints.
- Output is standard COLMAP binary format — poses could be injected as priors for the rig expansion step, bypassing Pi3 entirely.
- Limitation: incremental SfM drift on long sequences; feature rectification is CPU-intensive.

**COLMAP native equirectangular** — available in recent COLMAP releases (4.x+).
- COLMAP itself now supports a `SPHERICAL` camera model for equirectangular images, similar to SphereSfM.
- Would slot into the existing pipeline (already using COLMAP for SIFT and BA) with minimal new dependencies.
- Same single-point projection assumption — same multi-lens stitching limitation as above.

**How either would replace Pi3:**
1. Run SphereSfM / COLMAP `SPHERICAL` on the raw equirectangular frames (`01_frames/`, `imported photos/`, or `import from camera/`) to get per-frame poses.
2. Use those poses instead of Pi3's anchor output as the starting point for rig expansion (Step 5).
3. Skip Steps 2–4 (anchor staging, Pi3 run, quad aggregation) entirely.

**Trade-offs vs Pi3:**
- SphereSfM/COLMAP are geometry-based (incremental SfM): more reliable scale, metric output, no GPU memory limit.
- Pi3 is learning-based (feed-forward): faster for short sequences, handles low-texture scenes better, robust to repeated structures.
- SphereSfM needs more overlap / good sequential ordering to close loops; Pi3 handles retrieval internally.

**Status:** Not yet tried on a real capture. Evaluate if Pi3 pose quality proves insufficient after the rig-aware BA improvements.

---

## Pending / Not Yet Implemented

| Area | Status | Notes |
|---|---|---|
| Rig-aware BA (`create_default_bundle_adjuster`) | Superseded | Plan in `vivid-kindling-metcalfe.md`. Pycolmap approach crashed with C++ CHECK abort on Windows in 3 attempts. Superseded by CLI `bundle_adjuster --BundleAdjustmentCeres.use_gpu 1 --BundleAdjustment.refine_sensor_from_rig 0` which achieves equivalent rig-constrained result via the CLI binary without the crash. |
| Vocab tree matching | Implemented but untested | `vocab_tree_matcher --match_list_path` path is wired up; requires downloading `vocab_tree_flickr100K_words32K.bin` from COLMAP 3.11.1 release and setting `colmap_vocab_tree` in settings. Falls back to `exhaustive_matcher` if not set. |
| Quad anchor mode end-to-end test | Not yet run | `rigsfm_quad_anchors` was silently disabled until the 2026-07-13 `_build_settings` fix. First real quad-anchor run still pending. Must delete `pi3_output/` before running to clear the cached single-anchor Pi3 result. |
| ALIKED + LightGlue | Not implemented | Would replace SIFT extraction + matching. Potentially lower priority if GluMap (MASt3R) quality on anchor images already dominates reconstruction accuracy. |
| SphereSfM as prior estimator | Not tried | Runs native ERP → COLMAP binary poses; could replace Pi3 anchor step entirely. See Known Limitations section. |
| COLMAP SPHERICAL camera model as prior | Not tried | Recent COLMAP 4.x supports native equirectangular via `SPHERICAL` model — same idea as SphereSfM but built into the existing toolchain dependency. |
| Raw fisheye rig mode | Not implemented | Use pre-stitch Insta360 fisheye images with per-lens intrinsics/extrinsics to eliminate stitching parallax error. Requires fisheye camera models and access to individual lens frames from the camera. |
