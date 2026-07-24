# VGGT Pipeline — Progress & Status

Companion to `GLUEMAP_INTEGRATION.md` (GlueMap) and `COLMAP_POSE_CORRECTION_BRIEF.md` (COLMAP rig).
This document tracks the VGGT anchor+rig pipeline specifically.

---

## What VGGT Does in This Pipeline

VGGT (Visual Geometry Grounded Transformer) is a neural multi-view pose estimator. In FieldRaven:

1. Anchor images (`pano_camera0`) are extracted from 360° equirectangular panoramas at a configured pitch (e.g. −30°) using `panorama_processing.get_virtual_rotations`.
2. VGGT receives those tilted anchor images and predicts per-image camera poses + depth maps. The anchor pitch is baked into VGGT's predicted world frame — VGGT has no concept of gravity or "up", so its coordinate frame inherits the camera orientation.
3. `expand_anchor_to_rig` derives sibling camera poses (pano_camera1–6) from each anchor pose using the known rig geometry (yaw steps + pitch angles).
4. `write_colmap_files` exports cameras.txt / images.txt / points3D.txt for Brush or PostShot training.

---

## Files Involved

| File | Role |
|---|---|
| `3DGS Pipe V13 with VGGT/App/vggt_training.py` | Core VGGT pipeline: `run_full_pipeline`, `expand_anchor_to_rig`, `write_colmap_files`, `collect_all_rig_images`, `triangulate_rig_points` |
| `3DGS Pipe V13 with VGGT/App/app_callbacks.py` | Orchestrator: flat-copies all rig images to `images/` before VGGT runs, calls triangulation after `write_colmap_files` |
| `3DGS Pipe V13 with VGGT/App/triangulate_worker.py` | Python 3.14 subprocess: SIFT extraction + sequential matching + `pycolmap.triangulate_points` with fixed poses |
| `FieldRaven_desktop/splatpipe_core/pipeline.py` | `run_pipeline`: VGGT stage, routes output to `brush_input/` or `postshot_input/`, now calls training runners after COLMAP export |

---

## Coordinate System — `expand_anchor_to_rig`

The key formula:

```python
global_pitch = -original_pitch   # e.g. pitch=-30° → global_pitch=+30°
```

- **Step 1:** Counter-tilt the rig coordinate system by `+original_pitch` (undoing the anchor's baked-in extraction pitch)
- **Step 2:** Yaw-rotate each sibling camera around the counter-tilted up-axis
- **Step 3:** Re-apply `individual_pitch` (the per-sensor pitch angle) to each sibling

**Why `global_pitch = -original_pitch` is correct:** VGGT's anchor poses have the extraction pitch baked in from VGGT's inference (e.g. the anchor image was extracted at −30°, so VGGT's world frame is already tilted −30°). The double-negation is intentional and required.

**False alarm (2026-07-02):** A sign difference was found between `expand_anchor_to_rig`'s yaw rotation and `panorama_processing.get_virtual_rotations`. This analysis was incorrect — the two functions operate in different coordinate frames (`get_virtual_rotations` uses Y-up world convention; `expand_anchor_to_rig` operates in VGGT's world frame). No code change was made.

This math was arrived at through ~1 month of iteration and is confirmed working. Do not change `expand_anchor_to_rig` without an end-to-end empirical test.

---

## FOV / 518px Handling — Confirmed Correct

VGGT handles focal length through a resize → estimate → scale-back loop:

1. `load_and_preprocess_images()` resizes each anchor image to **518px** (VGGT's required input)
2. VGGT estimates `vggt_fx` / `vggt_fy` at 518px resolution from image content — no pre-calibration
3. `write_colmap_files` scales back:
   ```python
   resize_ratio = colmap_image_width / 518   # e.g. 1920 / 518 ≈ 3.71
   fx = vggt_fx * resize_ratio
   ```

`cameras.txt` intrinsics are consistent with the camera poses (both derived from the 518px VGGT estimate). The triangulation worker reads directly from `cameras.txt` — correct by design.

**Do NOT override with extraction FOV.** VGGT's focal estimate is self-consistent with its own poses. Substituting the panorama extraction FOV (computed from `fov_deg`) would misalign points with poses.

A `fov` parameter was added to `write_colmap_files` as an escape hatch but should remain `None` unless VGGT's estimate is confirmed significantly wrong on a specific dataset.

---

## Image Pre-Copying

`app_callbacks.py` flat-copies ALL rig images (anchor + all siblings from every frame subdirectory) from `views_dir` to `vggt_output_dir/images/` **before** `run_full_pipeline` is called.

The triangulation worker (`triangulate_worker.py`) relies on this — it expects images at `sparse_dir/../images/` and does no copying of its own.

---

## The Point Cloud Gap — Anchor-Only VGGT

**Problem:** VGGT only processes anchor images (`pano_camera0`). Depth maps only exist for the anchor FOV. Sibling cameras (`pano_camera1–6`) get correct poses via `expand_anchor_to_rig` but have no 3D points in their exclusive view cone. This caused black/failed Brush training in sibling view directions.

**Solution:** Triangulation with fixed poses.
1. VGGT on anchors → N poses + depth-based point cloud (anchor FOV only)
2. `expand_anchor_to_rig` → correct sibling poses (already correct, existing code)
3. `triangulate_rig_points()` → SIFT extraction from ALL images + sequential matching + triangulate with fixed poses → fills in sibling view cones
4. Write COLMAP format → Brush gets full multi-view point cloud

---

## Triangulation Implementation Status

### `triangulate_rig_points()` in `vggt_training.py`
- Spawns `triangulate_worker.py` as Python 3.14 subprocess (same pattern as `colmap_worker.py`)
- Passes `sparse_dir` as JSON payload
- Reads result JSON for success/point count

### `triangulate_worker.py` — Python 3.14 / pycolmap 4.0.4

**Status: Written and API-verified. Not yet end-to-end tested.**

Workflow:
1. Parse `cameras.txt` → recover model, width, height, params
2. Extract SIFT features from `sparse_dir/../images/` via `pycolmap.extract_features` (SINGLE camera mode — same intrinsics for all images)
3. Sequential matching via `pycolmap.match_sequential`
4. Build `pycolmap.Reconstruction` from poses in `images.txt` (with quaternion convention fix: images.txt stores `[qw,qx,qy,qz]` but `Rotation3d` expects `[qx,qy,qz,qw]`)
5. Triangulate via `pycolmap.triangulate_points(recon, database_path, images_dir, sparse_dir, refine_intrinsics=False)`
6. Output written to `sparse_dir` by `triangulate_points` (overwrites `points3D.txt`)

**pycolmap 4.0.4 API verified:**
- `pycolmap.Camera(model=..., width=..., height=..., params=[...], camera_id=1)` ✅
- `pycolmap.IncrementalPipelineOptions` with `fix_existing_frames=True`, `multiple_models=False` ✅
- `pycolmap.triangulate_points(recon, db, image_path, output_path, ...)` ✅
- `pycolmap.CorrespondenceGraph.from_database` — **does NOT exist in 4.0.4** (old approach, removed)
- `pycolmap.IncrementalTriangulator` — exists but not needed (use `triangulate_points` instead)

**Known caveat from COLMAP_POSE_CORRECTION_BRIEF.md (Problem 9, item 1):**
> `pycolmap.triangulate_points()` silently perturbs poses even with `fix_existing_frames=True`.

This was confirmed empirically in the COLMAP rig pipeline. The fix used there was to drive `IncrementalTriangulator` directly. For VGGT, the risk is lower because we write `points3D.txt` only (not cameras.txt/images.txt), and the return value of `triangulate_points` is a new reconstruction object — the original `cameras.txt`/`images.txt` files on disk are not overwritten since `triangulate_points` writes to the same `output_path`. **Needs empirical verification:** check whether VGGT's `cameras.txt`/`images.txt` are perturbed on disk after `triangulate_points` runs.

---

## Dead Code in `vggt_training.py`

These functions exist but are never called at runtime. Artifacts from earlier development approaches:

| Function | Status |
|---|---|
| `align_rig_to_anchor_pose` + `calculate_rigid_body_transform` | Replaced by `expand_anchor_to_rig` |
| `look_at_rotation_opencv` | Standalone utility, never wired |
| `debug_rig_coordinate_system` | Debug helper, never called |
| `validate_rig_z_y_axis_angles` | Commented out inside `expand_anchor_to_rig` |
| `extract_yaw_pitch_roll_from_c2w` | Utility, never called |

Safe to leave in place — harmless.

---

## Runtime Call Graph

```
app_callbacks.py
├── [flat-copy all rig images → vggt_output_dir/images/]
├── run_full_pipeline  (vggt_training.py)
│   ├── VGGTProcessor.initialize / process_vggt_inference
│   ├── unproject_depth_map_to_point_map
│   ├── [anchor+rig mode] expand_anchor_to_rig → convert_w2c_to_c2w
│   ├── [count only] get_virtual_rotations  (len() only, not for poses)
│   ├── apply_quality_filters
│   ├── apply_rig_optimization → optimize_points_for_rig_coverage
│   ├── [if enabled] apply_sparse_filter → create_sparse_point_cloud_for_3dgs
│   └── create_glb_scene
├── write_colmap_files → _project_points_for_colmap → _project_points_vectorized
├── [anchor+rig mode] triangulate_rig_points → [spawns triangulate_worker.py]
├── save_ply
└── create_simple_glb_viewer
```

---

## Pipeline Integration Status

### In `pipeline.py` (FieldRaven desktop)

| State | Detail |
|---|---|
| VGGT alignment stage | ✅ Runs `run_full_pipeline` + `write_colmap_files` |
| Routes output to `brush_input/` or `postshot_input/` | ✅ Based on `run_brush` / `run_postshot` |
| Calls Brush training after COLMAP export | ✅ Fixed 2026-07-06 (was silently missing) |
| Calls PostShot training after COLMAP export | ✅ Fixed 2026-07-06 |
| Triangulation called from `app_callbacks.py` | ✅ After `write_colmap_files` in anchor+rig mode |

---

## Open Items

1. **End-to-end triangulation test** — run a full VGGT anchor+rig job through the desktop app and verify:
   - `triangulate_worker.py` completes without error
   - `points3D.txt` count is substantially higher than anchor-only (3D points in sibling FOVs filled in)
   - Brush training succeeds in sibling view directions (no black blobs)
   - `cameras.txt`/`images.txt` not perturbed on disk after `triangulate_points` runs

2. **`triangulate_points` pose perturbation risk** — if poses ARE perturbed on disk, switch to driving `IncrementalTriangulator` directly (same fix as COLMAP_POSE_CORRECTION_BRIEF.md Problem 9). Only trigger this if empirical testing shows a problem.

3. **Sequential matching order** — `triangulate_worker.py` uses sequential matching on flat-copied images. The flat copy order matters: anchor frames first (frame 0..N), then sibling frames. If the copy order is different, sequential matching may not connect temporal neighbors. Verify the copy order in `app_callbacks.py` against what `match_sequential` expects.

4. **VGGT anchor-only GlueMap hybrid** (future) — for very large captures (500+ images), run GlueMap only on anchor images (7× fewer), derive sibling poses via rig geometry, triangulate all sensors. This would give GlueMap quality at anchor-only inference cost.

5. **Training pipeline wiring** — `pipeline.py` VGGT path now calls Brush/PostShot training. Needs end-to-end test to confirm the output directory structure (`brush_input/` vs `postshot_input/`) is correct for each trainer.
