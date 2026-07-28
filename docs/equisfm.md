# EquiSfM — Cheap Panorama Posing + Proposed Rig Glue

**Files:** `splatpipe_core/equi_sfm_runner.py` (Python 3.13, orchestrator), `splatpipe_core/equi_sfm_worker.py` (Python 3.14 / pycolmap 4.1.0 subprocess, does the actual COLMAP work).

---

## Purpose

EquiSfM is an alignment mode built around one idea: solve camera poses **once per real capture position**, on the raw equirectangular panorama, instead of running full SfM across every per-sensor perspective crop.

A single capture position in this pipeline's rig produces 13 virtual perspective "sensor" images (1 anchor + rings at configured pitch/yaw offsets — see `_compute_rig_params` in `colmap_runner.py`). The standard COLMAP-rig pipeline runs SIFT matching, triangulation, and bundle adjustment across **all** of those per-sensor images directly (e.g. 24 panoramas × 13 sensors = 312 images). EquiSfM instead:

1. Runs COLMAP's native `EQUIRECTANGULAR` camera model SfM directly on the **24 raw panoramas** — no per-sensor crops involved at this stage at all.
2. Takes the 24 solved poses and **analytically expands** each one into its 13 virtual sensor poses, using the exact same known rig geometry (`pitch_angles` / `yaw_steps`) the rest of the pipeline already relies on — no additional matching or estimation, just applying a fixed rotation per sensor to a real solved anchor pose.

The appeal: solving 24 poses via classical SIFT-based SfM is fast and doesn't touch the 312 per-sensor images at all. The cost: nothing about those 312 per-sensor images — their actual visual content, real feature correspondences, or real 3D structure — is ever used. The point cloud handed to Brush is just whatever the 24-panorama reconstruction produced (COLMAP's own points3D, in pano-image feature-space, unmodified), and the "poses" for all 312 sensor images are pure analytic derivations, never refined against anything.

---

## Current pipeline (as of 2026-07-27)

`run_equisfm_pipeline()` in `equi_sfm_runner.py`:

1. Ensure per-sensor view directories exist (reuses `02_views/` renders — same crops the rig pipeline uses).
2. Stage the 24 raw panoramas into a flat directory for COLMAP.
3. Spawn the Python 3.14 worker (`equi_sfm_worker.py`):
   - `pycolmap.extract_features()` — `EQUIRECTANGULAR` camera model, `CameraMode.SINGLE`.
   - `pycolmap.match_sequential()` or `match_exhaustive()` (user-selectable, `equisfm_matcher` setting).
   - `pycolmap.incremental_mapping()`.
   - Write pano-level `sparse_txt`, export each registered pano's `cam_from_world()` pose.
4. Parse the 24 pano poses (`_parse_pano_sparse_txt`).
5. **Rig expansion** (`_expand_poses`): 24 pano poses × 13 sensor offsets → 312 sensor poses. Purely analytic — same math as the rig pipeline's `_compute_rig_params`, just applied post-hoc to already-solved anchor poses instead of being baked into a live COLMAP rig config.
6. Write a hand-rolled "sensor-level" COLMAP text reconstruction (`_write_sensor_sparse_txt`): one `SIMPLE_PINHOLE` camera, 312 image poses, and the points3D **copied unchanged** from the pano-level reconstruction. No per-sensor 2D track data is generated — the comment in the code is explicit about this: *"points3D.txt: 3D points from pano recon, no per-sensor tracks."*
7. Copy to `brush_input/`, attempt a color-sampling re-projection pass (reused from GlueMap's pipeline — see "Known issues" below for why this is currently a no-op here).
8. Generate `cameras.html` (`tools/visualize_cameras.py`) and open it.
9. Hand off to Brush training.

**Practical consequence:** point cloud density is bounded entirely by whatever the 24-panorama reconstruction produced — on a real test job (Kings Peak Summit, 24 panoramas), this was **1,388 points**, versus tens of thousands for the same job size through the standard COLMAP-rig pipeline. This is the direct cause of the "brush reconstruction isn't very good" result that prompted this investigation.

---

## Known issues found and fixed (2026-07-27)

All four were found by actually running the pipeline end to end against a real job, not by inspection alone.

1. **`equi_sfm_worker.py`: `img.cam_from_world` used as a property, not a method.** pycolmap 4.1.0 exposes this as a method (`img.cam_from_world()`); calling it without `()` returns the bound method object itself, and the subsequent `.rotation` access threw `AttributeError`. This crashed the worker *after* `sparse_txt` had already been written, right before the pose-export JSON was printed — so the pano-level reconstruction always succeeded, but the runner always saw the job as failed. Fixed by adding `()`.

2. **`_write_sensor_sparse_txt` wrote camera model `PINHOLE` instead of `SIMPLE_PINHOLE`.** Every other exporter in this codebase uses `SIMPLE_PINHOLE` (one shared focal length). `PINHOLE` has *two* focal-length parameter slots (fx, fy). `tools/visualize_cameras.py` reads `cam.focal_length`, a convenience property that's only defined when there's exactly one focal-length slot — with `PINHOLE` it hard-asserts inside COLMAP's C++ layer (`Check failed: idxs.size() == 1 (2 vs. 1)`). Because the visualizer runs via a bare `subprocess.run(..., check=False)` with no captured/logged output, this crash was completely invisible in the pipeline log — the visualizer step just silently produced nothing, every time. Fixed by switching to `SIMPLE_PINHOLE` (single shared focal length, matching the rest of the codebase).

3. **`tools/visualize_cameras.py`'s `_parse_images_txt()` desyncs on blank POINTS2D lines.** The pose/points2D line-pair alternation used `if not line or line.startswith("#"): continue` — treating a genuinely blank points2D line (which `_write_sensor_sparse_txt` always emits, since it writes no track data) identically to a comment line, i.e. *invisible to the toggle*. Once one blank line gets skipped without flipping the toggle, every subsequent image is misread as the other type, forever — a clean 50% loss, alternating. Reproduced in isolation (a 4-image test kept `A, C`, dropped `B, D`) and confirmed against the real 312-image reconstruction: exactly 156 cameras loaded instead of 312. Because 312 sensor images are written sorted by sensor prefix (all 24 `pano_camera0` entries together, then all 24 `pano_camera1`, etc.) and 24 is even, the *same* 12 chronological frames survived in every sensor block — presenting as "12 whole capture positions visible, 12 completely missing" rather than a scattered loss. Fixed by only skipping `#`-comment lines unconditionally; a blank line still counts as (and correctly toggles past) the points2D line.

   **This is a systemic pattern, not unique to this file** — the same `if not line or line.startswith("#"): continue` construct appears in 6 other places (`equi_sfm_runner.py` ×2, `colmap_runner.py`, `pipeline_runner.py`, `tools/verify_rs_rig.py` ×2). It only misbehaves when a file has a genuinely blank second line, which in practice only happens for hand-rolled output like `_write_sensor_sparse_txt`'s — real pycolmap-written reconstructions essentially always have non-empty POINTS2D lines for registered images. Checked `_parse_pano_sparse_txt` (reads the *real* pano-level reconstruction) directly — not currently triggered. The other 5 occurrences are unreviewed; treat as a latent risk if any of them is ever pointed at a hand-rolled/track-free COLMAP text export.

4. **Default `sequential` matcher produced almost no verified matches on the panoramas.** 12,000–14,000 SIFT features per image, but only 89 total verified matches across 24 images, and COLMAP could not find any usable initial image pair even after repeatedly relaxing constraints. Switching to `exhaustive` (cheap for only 24 images — 276 pairs) fixed it: 23–24/24 images registered, real reconstructions produced. **Root cause not resolved** — the working theory is that COLMAP's two-view geometry verification assumes a planar/perspective essential-matrix model, which may not be correctly generalized for `EQUIRECTANGULAR` images (a dedicated fork, SphereSfM, exists specifically to add a native spherical camera model for this — see `project-spheresfm-and-parallax` memory). `exhaustive` trying more pairs happened to be enough to find *some* valid geometry, but doesn't explain why `sequential`'s default overlap (already generous for 24 images) verified almost nothing. Worth investigating before relying on this for larger panorama counts, where `exhaustive`'s O(n²) cost stops being cheap.

---

## Proposed extension: reuse RigSfM's "glue" step

`splatpipe_core/rigsfm_runner.py` already implements a structurally near-identical pipeline, just with a different cheap-pose source:

1. Pi3 (neural) inference on anchor images only → cheap real poses.
2. Rig expansion (anchors → all sensor poses) — analytic, same math.
3. **SIFT extraction + matching + triangulation + bundle adjustment on the real per-sensor images.**
4. Color sampling → `brush_input/`.

Critically, `rigsfm_runner.py`'s `_expand_rig_poses()` (fed by Pi3) and `equi_sfm_runner.py`'s `_expand_poses()` (fed by classical panorama SfM) produce the **identical output shape**: `{image_name: (R, t)}` for all 312 sensor images. EquiSfM already does the equivalent of RigSfM's steps 1–2 — arguably even cheaper, since it's classical SIFT-based SfM with no neural inference at all — it just currently stops there, short-circuiting straight to the sparse pano-level point cloud instead of running step 3 against the real per-sensor images.

**Proposal:** extract RigSfM's step 3 (SIFT matching + triangulation + BA on the expanded per-sensor pose set, `rigsfm_runner.py` roughly lines 657–751) into a shared function taking `all_poses` + an image directory + settings as input, independent of which estimator produced the poses. Call it from both runners:
- `rigsfm_runner.py` → poses from Pi3.
- `equi_sfm_runner.py` → poses from panorama SfM.

This would fix both open problems in one move: real per-sensor triangulation replaces the sparse pano-level point cloud (density becomes comparable to the standard rig pipeline), and real 2D tracks would exist, making the currently-no-op color-sampling step (see "Known issues," item 3's neighbor — `_sample_and_write_colored_recon` currently colors 0 points here because there's no track data to sample against) actually do something, or become unnecessary since real triangulation naturally carries color.

**Main tradeoff:** this makes EquiSfM cost roughly what RigSfM's matching+triangulation+BA stage already costs (SIFT matching across 312 images isn't free) — trading away EquiSfM's current "nearly instant" property for real density. Open design question, not yet decided: should the glue step always run for EquiSfM going forward, or be exposed as a toggle (fast pano-only preview vs. full glue), so the cheap path still exists when a quick look is all that's needed?

**Status:** discussed, not yet implemented.
