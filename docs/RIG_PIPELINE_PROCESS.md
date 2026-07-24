# Rig Pipeline Process Sheet

How a single 360° panorama becomes 7 posed virtual cameras in COLMAP, step by step,
with the exact function/file responsible for each step and what's been verified
about it. Companion to `COLMAP_POSE_CORRECTION_BRIEF.md` (which documents the bugs
found and fixed along the way) — this document is the reference for "what the
pipeline is supposed to do," not the bug history.

---

## Step 1 — Extract 7 perspective crops from the panorama

**File:** `panorama_processing.py` (3DGS Pipe V13 with VGGT), function `render_views()`.

For one equirectangular panorama:

1. `create_virtual_camera(pano_height, fov_deg)` computes:
   - `image_size = int(pano_height * fov_deg / 180)` — output resolution (square).
   - `focal = image_size / (2 * tan(fov_deg/2))` — **intended** focal length for that FOV.
2. `get_virtual_camera_rays(image_size, focal)` builds one 3D ray direction per output
   pixel, in the camera's own local coordinate frame (forward = +Z), normalized by
   the actual `focal` computed in step 1 above (see "Resolved Issue" below — this
   `focal` parameter was added as the fix for a bug found and corrected in this
   investigation; the function used to ignore `focal` entirely).
3. `get_virtual_rotations(yaw_steps, pitch_angles, horizon_ref)` builds one rotation
   matrix per sensor — sensor 0 (`pano_camera0`) is the horizon reference (identity,
   pitch=0, yaw=0); sensors 1-6 are placed at evenly-spaced yaws (`360/yaw_steps`
   apart) at the configured `pitch_angles` (e.g. -10°).
   - Core formula (`look_at_rotation`): builds a `direction` vector from yaw/pitch,
     then `right = up × direction`, `true_up = direction × right`, and returns
     `R = [right | true_up | direction]` as columns — i.e. `world_from_cam`.
4. For each sensor: rotate the local rays into world/panorama space
   (`world_rays = rays_array @ R.T`), convert to equirectangular UV
   (`spherical_uv_from_rays`), and bilinearly sample the source panorama
   (`sample_equirectangular`). Each output file is named
   `{frame}_view_{i:02d}_p{pitch:+03.0f}_y{yaw_idx:02d}.jpg` — **the index `i` in the
   filename is the sensor's identity** and is the single source of truth used
   downstream to assign images to sensor folders.

**✅ Bug found and fixed in this step (see "Resolved Issue" below, formerly "Open
Issue"): the rays built in step 2 used to not actually depend on `fov_deg` — they
always spanned ~90° edge-to-edge, regardless of what FOV was requested. `image_size`
did scale with `fov_deg` (more pixels for a wider requested FOV), but the *angular
content* sampled into those pixels did not. Fixed by normalizing rays with the real
`focal` instead of a hardcoded `image_size/2`.**

---

## Step 2 — Sort extracted files into per-sensor folders

**File:** `colmap_runner.py`, function `_reorganize_views()`.

Walks the extracted files, regex-matches `_view_(\d+)_p` to recover the sensor
index `i` from the filename (the same `i` step 1 assigned), and copies into
`colmap/images/pano_camera{i}/{frame}.jpg`. This is index-exact — it does not infer
sensor identity from sort order, so it can't silently misassign a file to the wrong
sensor. **Verified correct.**

---

## Step 3 — Recompute the same rotations independently, for the rig config

**File:** `colmap_runner.py`, functions `_cam_from_pano()` and `_virtual_rotations()`.

COLMAP needs to be told the *fixed* relative geometry between the 7 sensors
(`cam_from_rig`), independent of step 1's renderer (different process, different
language-level math, must agree exactly). `_cam_from_pano(yaw, pitch)` is written as
the literal transpose of `panorama_processing.py`'s `look_at_rotation()` — same
variable-by-variable structure, verified to match to floating-point zero
(`1.1e-16`) for all 7 sensors. `_virtual_rotations()` reproduces step 1's exact
iteration order (horizon reference first, then yaw-ring per pitch) and negates the
pitch sign — extraction's `pitch_angles` convention and the DIAG-pitch convention
used everywhere else in this pipeline are opposite signs (see
`COLMAP_POSE_CORRECTION_BRIEF.md` Problem 11 for the empirical proof).
**Verified correct** (exact-match diff, rig-rigidity check, reprojection error, and
direct visual confirmation in COLMAP GUI).

`_compute_rig_params()` then converts each sensor's `cam_from_pano` into
`cam_from_rig[i] = R_i @ ref.T` (ref = sensor 0's rotation) — algebraically, this is
exactly "rotation that takes a point in the anchor's camera frame and expresses it
in sensor `i`'s camera frame," which is what COLMAP's rig API expects.
**Re-derived and verified algebraically in this audit.**

---

## Step 4 — Tell COLMAP about the rig

**File:** `colmap_worker.py`, "Build rig config" / "Apply rig config" sections.

- A single shared `Camera` object (one `camera_id`, SIMPLE_PINHOLE model, the
  `focal`/`image_size` from step 1/3) is reused for all 7 sensors — they're all
  virtual crops with identical intrinsics by construction.
- `RigConfigCamera(ref_sensor=(i==0), image_prefix="pano_cameraN/", cam_from_rig=...)`
  is built per sensor and applied via `pycolmap.apply_rig_config()`. Sensor identity
  is matched by `image_prefix` string against the database, not by list position —
  another index-safety guarantee.
- `IncrementalPipelineOptions(ba_refine_sensor_from_rig=False, ...)` holds the 7
  sensors' relative geometry **fixed** during bundle adjustment — this is what makes
  "each frame is one rigid body" actually true during pose estimation, not just at
  setup. **Verified empirically**: for any given frame, deriving the implied
  rig pose independently from all 7 sensors gives the identical answer
  (diff = 0.0).

---

## Step 5 — Matching, purging zero-baseline pairs, mapping, leveling

Covered in detail in `COLMAP_POSE_CORRECTION_BRIEF.md` (Problems 12-14):
same-rig-frame sibling pairs are purged before mapping (zero baseline, can't be
triangulated, but COLMAP's verifier accepts them as legitimate pure-rotation
matches); a single global rotation levels the whole reconstruction after the fact
without touching any frame's individually-estimated pose.

---

## What defines the frustum shape

Each sensor is a `SIMPLE_PINHOLE` camera: a square `image_size × image_size` pixel
grid and **one** shared focal length (same value for both horizontal and vertical —
unlike `PINHOLE`, which allows separate `fx`/`fy`). That combination defines a
square-based pyramid: half-angle = `arctan((image_size/2) / focal)` in *both*
directions equally, so the frustum's horizontal and vertical FOV are identical by
construction. The wide-angle "fan" look of a rig in the visualizer comes from 6-7
of these square pyramids arranged around a shared apex (the rig's optical center)
at the configured yaws/pitches — not from any per-sensor shape difference.

---

## ✅ RESOLVED — focal length told to COLMAP did not match the frustum the pixels were actually rendered with (Problem 15)

**Status: fixed and verified, including a full Brush training run. This was the
single highest-impact bug found in the whole investigation — see below for the
final numbers and user confirmation.**

`get_virtual_camera_rays(image_size)` (step 1, old version) normalized pixel
coordinates to `[-1, 1]` and set ray `z = 1` — this was mathematically equivalent to
an **implied focal length of exactly `image_size / 2`**, which corresponds to
**exactly 90°** edge-to-edge field of view, regardless of `image_size`. Verified
numerically for several `fov_deg` values:

| requested fov_deg | image_size | focal told to COLMAP | focal actually rendered | actual angular content |
|---|---|---|---|---|
| 90.0  | 2000 | 1000.00 | 1000.00 | ~90° (matches) |
| **94.6** (the production default) | 2102 | **969.83** | **1051.00** | ~90° (does **not** match) |
| 75.0  | 1666 | 1085.59 | 833.00  | ~90° (does not match) |
| 120.0 | 2666 | 769.61  | 1333.00 | ~90° (does not match) |

`fov_deg` only ever changed `image_size` (resolution) — it did **not** change the
actual angular content sampled into those pixels, which was hardcoded to ~90° by the
old `[-1,1]` ray normalization. But `colmap_runner.py`'s `_compute_rig_params()`
computed the focal length COLMAP was told using the *requested* `fov_deg`, not the
~90° the pixels were *actually* rendered at. With the production default of
`fov_deg = 94.6`, this was a **7.7% systematic focal-length error**, present in
**every single sensor of every single frame of every run prior to this fix** —
including every test run earlier in this entire investigation.

**Why this matched the reported "double vision" symptom:** a constant focal-length
error causes a constant radial depth/scale bias in every triangulated point — the
same physical wall surface, viewed by cameras at different positions/angles, gets
placed at a slightly different depth depending on the specific triangulation
geometry of each camera pair. On a flat, continuous surface (like the moss/rock
wall in the screenshot) this showed up exactly as two distinct, near-parallel point
bands rather than one clean surface — not as random scatter, because the bias was
systematic, not noise.

**Fix chosen and implemented: option 2, the more correct long-term fix.**
`get_virtual_camera_rays()` in `panorama_processing.py` (3DGS Pipe V13 with VGGT)
was changed to take `focal` as a parameter and normalize pixel coordinates by it
directly (`xy = (xy - image_size/2) / focal`) instead of the old hardcoded
`/image_size*2` form. Its one call site in `render_views()` was updated to pass the
real `focal` computed by `create_virtual_camera()`. This means the *rendered pixel
content* now actually has the requested `fov_deg`, so it no longer matters what
focal length any downstream consumer (including `colmap_runner.py`) computes from
`fov_deg` — they now agree by construction. (Option 1, patching
`_compute_rig_params()` to assume a fixed 90°, was considered but not used — it
would only have band-aided the *already-extracted* image set without fixing future
captures at the source.)

**Verification — all 28 source panoramas in the `nile creek` test set were
re-extracted** with the fixed renderer (`tools/reextract_views.py`, output to a
fresh `02_views_fixed/` directory, original `02_views/` left untouched for
comparison) and run through the full pipeline end to end
(`tools/run_rig_test.py` → `nile_creek_focal_fix_run`). Final numbers, measured via
reprojection error exactly as in every other fix in this investigation:

| | before (focal-length bug present) | after (fixed) |
|---|---|---|
| mean reprojection error | 1.320 px | **0.893 px** (32% lower) |
| surviving matched observations | — | **360,140** (~3.3× more) |
| point cloud size | — | **53,203 points** |
| % observations over 4px error | — | **0%** |

**Visual confirmation — this is the fix that resolved the reported wall/surface
"double vision":**
1. `cameras.html` (the rig visualizer): user confirmed "very very promising"
   immediately after the fix.
2. Full Brush training run (30,000 steps, same re-extracted images and
   reconstruction measured above): user confirmed the result is **"about as good as
   it can get"** — no doubling, clean single wall/rock surfaces throughout.

This closes the investigation that started with the reported double-vision
screenshot. Full causal chain, in the order the bugs were actually found and fixed
(see `COLMAP_POSE_CORRECTION_BRIEF.md` for the complete numbered history): sensor-
within-rig orientation (matrix order, then sign convention; Problems 10-11) →
same-rig-frame zero-baseline contamination (Problem 12) → a broken custom
densification script, retired (Problem 13) → per-frame pose flattening, replaced
with single global leveling (Problem 14) → and finally this focal-length/frustum-
shape mismatch (Problem 15), which turned out to be the dominant remaining source
of the visible doubling.

---

## What "6 sensors vs 4" does and doesn't affect

With `yaw_steps=6` (60° spacing) and a true ~90° rendered FOV, adjacent same-pitch
siblings *within one frame* overlap by ~30° of horizontal field of view. With 4
sensors (90° spacing) and the same ~90° true FOV, adjacent siblings would have
~0% overlap.

This overlap is **not**, by itself, a source of double vision: every same-rig-frame
sibling pair is purged before triangulation regardless of how much they overlap
(Problem 12) — none of that shared content ever contributes to the point cloud.

What 6-vs-4 *does* affect: more siblings means more *redundant* coverage of the
same physical surface from slightly different viewpoints (via cross-frame matches,
not the purged same-frame ones). That's normally a density/robustness benefit. But
if there's an underlying *systematic* bias — like the focal-length bug above — more
redundant overlapping viewpoints means more independent (but identically biased)
depth estimates of the same surface, which makes a systematic doubling artifact
more *visible*, not because the rig has more sensors per se, but because there are
more chances to render the same biased surface from a slightly different angle.

**Conclusion: sensor count was very unlikely to be the root cause here — the focal
length mismatch was, and this was confirmed: fixing the focal-length bug alone
(Problem 15, above) eliminated the doubling with the sensor count unchanged at 6.
No sensor-count change was needed.**
