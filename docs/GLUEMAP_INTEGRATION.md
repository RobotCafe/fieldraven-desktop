# GlueMap Integration — Running Notes

## Overview

GlueMap is a global SfM pipeline from colmap/gluemap that replaces the COLMAP
incremental + vocab-tree approach with:
1. **SALAD retrieval** — image neighbour graph (replaces sequential matcher + vocab tree)
2. **Pi3 / VGGT / MapAnything inference** — neural multi-view pose estimation in star configs
3. **Global BA** — rotation averaging + bundle adjustment
4. **Refinement** — VGGSfM SIFT track snapping + augmented BA

Output: COLMAP format (cameras/images/points3D) → Brush step unchanged.

---

## Environment

- **WSL2 distro:** Ubuntu-22.04
- **GPU:** RTX 4070 Ti Super, 16GB VRAM
- **Conda env:** `gluemap` (Python 3.11, CUDA 12.4, PyTorch 2.4.1 from conda-forge)
- **Install path:** `~/gluemap`
- **Checkpoints:** `~/gluemap/checkpoints/`
  - `pi3.safetensors` — 3.6 GB (Pi3 backbone, default)
  - `dino_salad.ckpt` — 336 MB (SALAD retrieval)
  - `vggsfm_v2_0_0_track_predictor.bin` — 187 MB (refinement tracker)
  - `checkpoint-dg+visym.pth` — 2.7 GB (Doppelgangers++, optional)
- **Auto-downloaded at first run:**
  - `dinov2_vitb14_pretrain.pth` — 330 MB (DINOv2, used by SALAD)
  - `aliked-n16.pth` — 2.6 MB (ALIKED keypoints, used in refinement)

---

## Performance

### First test run — Nile Creek (2026-07-02)
- **Input:** 28 frames × 7 sensors = 196 perspective images (pano_camera0–6)
- **Backbone:** Pi3, `skip_doppelgangers=True`, `is_sequential=True`
- **Image size:** 1572×1572 per sensor, focal length 1886.40px (SIMPLE_PINHOLE)

| Stage | Time | Notes |
|---|---|---|
| SALAD retrieval | ~48s | Includes 40s DINOv2 model load (cached after first run) |
| Neighbour graph | ~2s | 14,704 edges from 196 images |
| Pi3 forward pass | 546s (~9 min) | Neural multi-view inference |
| VGGSfM tracking | 1025s (~17 min) | **Actual bottleneck** — not Pi3 |
| Rotation averaging | <1s | Ceres, 196/196 poses, 210 pairs filtered |
| Similarity averaging | <1s | Ceres, 11 iterations, CONVERGENCE |
| Virtual tracks | — | 321,087 valid points |
| SIFT extraction (GPU) | ~53s | 10,000–12,000 features/image |
| SIFT matching | ~53s | 0.889 min, 13 blocks |
| Track snapping | <1s | 144,866 / 1,292,454 snapped |
| Triangulation | ~11s | 439,887 3D points |
| Augmented BA iter 1 | ~62s | Ceres 65 iters, cost 3.44M→1.38M, CONVERGENCE |
| Augmented BA iter 2 | 112s | triangulation 7.5s, filter 11.2s, BA 91.9s |
| **Total** | **2262.8s (37.7 min)** | Without RoPE2D + xFormers + num_track_per_img tuning |

**Reprojection quality after BA iteration 1:**
- Mean angular error: 0.5277°
- Median angular error: 0.3053°
- <0.5°: 72.7% of observations

**Key insight — tracking is the real bottleneck:**
VGGSfM tracking (1025s) takes nearly 2× longer than Pi3 forward pass (546s).
Reducing `--num_track_per_img` (default 1024) to 512 would roughly halve tracking time
with modest impact on track density. Worth testing on second run.

### Known slow paths (with fixes)

| Issue | Impact | Fix | Status |
|---|---|---|---|
| RoPE2D PyTorch fallback | ~4–5× slower inference | Build `curope` CUDA extension | ✅ Built (`curope.cpython-311-x86_64-linux-gnu.so`) |
| xFormers missing | ~20–40% slower attention | `micromamba install -c conda-forge xformers` | ✅ Installed |
| DINOv2 model load (~40s) | One-time startup cost | None needed — cached after first run | ✅ Cached |
| Doppelgangers++ skipped | Slightly less covisibility accuracy | Enable when speed allows | 🔲 Skipped for now |
| curope non-contiguous tensor crash | Run 2 crash with batch_size=60 + xFormers | Patch curope2d.py (see below) | ✅ Patched |

### curope patches — three-part fix for xFormers + bfloat16 + autocast (2026-07-02)

**Problem:** When xFormers' memory-efficient attention is active, the Q/K/V tensors can have
non-contiguous memory layout. The compiled RoPE2D CUDA kernel (`rope_2d` in `kernels.cu:91`)
checks `tokens.is_contiguous()` and throws if not satisfied. The original code passes
`tokens.transpose(1,2)` — a non-contiguous view — to the kernel, which worked in run 1
(xFormers wasn't active yet) but crashes with `RuntimeError: tokens are not contiguous`
once xFormers is running.

**Fix applied to:**
`~/gluemap/thirdparty/doppelgangers-plusplus/dust3r/croco/models/curope/curope2d.py`

Old `cuRoPE2D.forward`:
```python
def forward(self, tokens, positions):
    cuRoPE2D_func.apply( tokens.transpose(1,2), positions, self.base, self.F0 )
    return tokens
```

New `cuRoPE2D.forward` (copy-then-apply, safe for inference):
```python
def forward(self, tokens, positions):
    # xFormers can produce non-contiguous tensors; kernel requires contiguous memory.
    # GlueMap is inference-only so we use copy-then-apply instead of in-place.
    t = tokens.transpose(1, 2).contiguous()
    _kernels.rope_2d(t, positions, self.base, self.F0)
    return t.transpose(1, 2).contiguous()
```

This bypasses the `cuRoPE2D_func` autograd wrapper (gradient flow not needed for inference)
and makes a contiguous copy before the kernel call. Two small extra copies per attention block —
negligible versus the savings from xFormers.

The original file had the comment `# tokens = tokens.clone() # uncomment this if inplace doesn't work`
already anticipating this — but uncomment-only would break the in-place semantics (clone creates
a new tensor the kernel modifies but the original `tokens` is returned unchanged). The copy-then-apply
pattern is the correct fix.

**Fix 2 — `kernels.cu`: `tokens.type()` → `tokens.scalar_type()` + recompile**
`AT_DISPATCH_FLOATING_TYPES_AND_HALF(tokens.type(), ...)` uses the deprecated C++ `.type()` method
which calls `dispatchKeyToBackend`. With xFormers + bfloat16 autocast active, tokens carry the
`AutocastPrivateUse1` dispatch key (xFormers registers a PrivateUse1 custom backend). The
`dispatchKeyToBackend` function doesn't know this key → `RuntimeError: Unrecognized tensor type ID`.

Fix: change `tokens.type()` to `tokens.scalar_type()` in `kernels.cu` line ~101.
`scalar_type()` reads the dtype enum directly from `TensorImpl` without touching dispatch.
Then recompile: `cd ~/gluemap/thirdparty/doppelgangers-plusplus/dust3r/croco/models/curope &&
~/.local/bin/micromamba run -n gluemap bash -c "CUDA_HOME=\$CONDA_PREFIX python setup.py build_ext --inplace"`

**Fix 3 — `curope2d.py`: cast bfloat16 to float16 before kernel**
After fix 2, the kernel correctly reads the scalar type, but `AT_DISPATCH_FLOATING_TYPES_AND_HALF`
only covers float32/float64/float16. Pi3 runs in bfloat16 (`torch.cuda.amp.autocast(dtype=bfloat16)`),
giving `NotImplementedError: "rope_2d_cuda" not implemented for 'BFloat16'`.

Fix: cast `tokens` from bfloat16 to float16 before the kernel call, then cast back. Precision
difference for RoPE position encoding is negligible (same exponent range, slightly fewer mantissa bits).

**Final state of `curope2d.py` `forward` method:**
```python
def forward(self, tokens, positions):
    orig_dtype = tokens.dtype
    if orig_dtype == torch.bfloat16:
        tokens = tokens.to(torch.float16)
    t = tokens.transpose(1, 2).contiguous()
    _kernels.rope_2d(t, positions, self.base, self.F0)
    return t.transpose(1, 2).to(orig_dtype).contiguous()
```

### Second run — RoPE2D compiled + xFormers + num_track_per_img 512 (2026-07-02)
- **Settings:** `--batch_size 60 --num_track_per_img 512 --skip_doppelgangers --is_sequential`
- **Optimisations active:** compiled curope CUDA extension, xFormers, cached DINOv2

| Stage | Run 1 | Run 2 | Speedup |
|---|---|---|---|
| SALAD retrieval | ~48s | 15.0s | 3.2× (DINOv2 cached) |
| Pi3 forward | 546s | 499s | 1.1× |
| VGGSfM tracking | **1025s** | **266s** | **3.85×** |
| SIFT extraction | ~53s | 64s | — |
| Track snapping | <1s | 0.7s | — |
| Refinement BA | ~174s | 337s | 0.5× (more virtual tracks, more inner iterations) |
| **Total pipeline** | **2262.8s (37.7 min)** | **1313.4s (21.9 min)** | **1.72×** |

**Quality run 2:**
- 141,549 real tracks, 4,953 virtual tracks
- Median angular reprojection error: 0.0980° (run 1: 0.1071°)
- <0.5° observations: 94.1% (run 1: 93.9%)

**Analysis:**
- Tracking speedup (3.85×) is the dominant win — `num_track_per_img 512` halved track density with no quality loss
- Pi3 forward improved only 9% because Pi3 uses bfloat16 autocast, and the bf16→fp16 cast added before each RoPE kernel call partially offsets the RoPE2D + xFormers speedup
- Refinement BA is slower (2×) because run 2 produced more virtual tracks (16K → 5K after filtering vs run 1's 3K), adding more Ceres residuals
- To further reduce BA time: add bfloat16 dispatch to the C++ kernel (eliminating the bf16→fp16 cast) could save ~50s on Pi3 forward

**Final reconstruction quality (run 1):**
- 166,822 real tracks, 990,747 observations
- 3,119 virtual tracks
- Median reprojection error: 0.1071° after refinement iter 2 (excellent)
- \>93.9% of observations under 0.5° angular error

### Tuning knobs
- `--num_track_per_img` default 1024 — **biggest lever**: halving to 512 halves VGGSfM tracking (the actual bottleneck at 1025s vs 546s Pi3 forward pass). **TODO: expose in desktop UI**
- `--batch_size` default 30 — try 60 with 16GB VRAM (halves forward pass count). **TODO: expose in desktop UI**
- `--num_neighbors` default 100 — reduce for speed, increase for loop closure coverage
- `--coarse_only` — skips SIFT refinement stage, faster but lower accuracy
- `--skip_doppelgangers` — skip two-view covisibility (recommended for sequential captures)

---

## Integration Status

### Backend (FieldRaven_desktop)

| File | Change | Status |
|---|---|---|
| `splatpipe_core/types.py` | Added `GLUEMAP_ALIGNMENT` stage | ✅ |
| `splatpipe_core/settings.py` | Added `run_gluemap`, `gluemap_backbone`, `gluemap_wsl_*` etc. | ✅ |
| `splatpipe_core/gluemap_runner.py` | New runner — WSL2 subprocess, progress parsing, output copy | ✅ |
| `splatpipe_core/pipeline.py` | GlueMap branch inserted before RS branch | ✅ |
| `backend/pipeline_runner.py` | Stage maps, mode detection, `_pipeline_mode="gluemap"` | ✅ |

### Frontend / Web (TODO)
- [ ] AlignmentTab: add `[GlueMap]` as 4th mode button alongside RS / COLMAP / VGGT
- [ ] Settings panel: backbone selector (pi3 / pi3x / vggt / map_anything), skip_dg toggle, coarse_only toggle
- [ ] `App.jsx`: wire `run_gluemap` through API config mapping
- [ ] `fieldraven-web`: add `gluemap` to `pipelineMode` union type and `PIPELINE_MODE_LABELS`
- [ ] `ActiveJobTab.tsx`: add `STAGES_GLUEMAP` stage definition

### GlueMap output → brush_input
GlueMap writes two COLMAP-format directories inside `write_path/`:
- `write_path/coarse/` — after global BA, before SIFT refinement
- `write_path/gluemap_aba/` — **final output** after augmented BA (confirmed from first run)

Also writes `pipeline_timing.pth` at `write_path/` root.

The runner checks `gluemap_aba/` first, then `coarse/` as fallback. `_find_colmap_output()`
in `gluemap_runner.py` is updated to reflect this.

---

## First Quality Test — Nile Creek (2026-07-02)

**Input:** 196 images (28 frames × 7 sensors), GlueMap Pi3 backbone, run 1 output

### Brush initialization issue: missing point colors
GlueMap's augmented BA writes all 3D points with RGB(0,0,0). COLMAP's own pipeline
samples pixel colors from the source images during reconstruction. Brush initializes
Gaussian SH coefficients from point colors — starting from black gives a completely
black viewer until training converges (many thousands of steps).

**Fix applied:** Python script using pycolmap + PIL to sample pixel colors from the
nearest observation for each point, then write text-format COLMAP files.
This is now **automatic in `gluemap_runner.py`** (step 6, after copying output).

**Also fixed:** Remove `.bin` files from brush_input — having both `.bin` and `.txt`
can confuse Brush; text format only is cleaner.

### Binary vs text format
GlueMap outputs binary COLMAP format (`.bin`). Brush reads text format (`.txt`).
pycolmap's `Reconstruction.write_text()` converts correctly.
The runner now always writes text and never copies `.bin` files to brush_input.

### Result
**Excellent reconstruction quality — scene looks great at 4,000 steps, very promising
at 18,000 steps (30,000 total).** Point cloud density and camera poses verified in COLMAP
GUI before training — 196 images well-posed, very dense point cloud (166K points vs ~10K
from COLMAP). Some "strange poses" observed on sibling sensors (pano_camera1-6) — see
Open Problems below.

Comparable or better visual quality than the full COLMAP rig-constrained path,
despite GlueMap having no rig constraints at all. The global BA appears to compensate.
At 18K steps described as "really really good, very promising." Training completed at
30,000 steps — side-by-side scene vs dataset comparison shows tight alignment across
the full scene (waterfall, fallen log, green pool, rock faces, vegetation). Strong
result from the first GlueMap run with unoptimised settings (no RoPE2D CUDA kernel,
no xFormers, default num_track_per_img=1024).

**Quality vs RS+Brush (first qualitative comparison, 2026-07-02):**
GlueMap+Brush preserves noticeably higher detail than the RS+Brush examples. Likely
reasons:
- GlueMap's global BA produces more accurate camera poses → Brush initializes
  Gaussians from a better-aligned point cloud → sharper detail
- 166K triangulated 3D points vs RS's sparse initialization gives Brush more to work
  with across the full scene
- No rig constraints in GlueMap yet (sibling poses are free) — rig-constrained
  GlueMap (anchor-only + triangulation) may push quality even further

Uploaded to gallery as "Nile Creek GlueMap" (job: dbf2a410ed2849d19, 5.6M Gaussians,
pipelineMode: gluemap).

---

## Open Problems

### 1. VGGT anchor-only inference — analysis, false alarm, and actual gap

**The pipeline:**
- Anchor images are extracted from equirectangular panoramas at a specific pitch angle
  (e.g., pitch=-30°) using `panorama_processing.get_virtual_rotations`
- VGGT receives those tilted images, so its predicted anchor poses have the -30° tilt
  "baked in" — the poses are in VGGT's own world coordinate frame
- `expand_anchor_to_rig` then builds sibling poses around each anchor

**How `expand_anchor_to_rig` works (and why it is correct):**

```python
global_pitch = -original_pitch   # e.g. -(-30°) = +30°
```

- Step 1: Counter-tilt the rig coordinate system by +30° (undoing the anchor's baked-in pitch)
- Step 2: Yaw-rotate each sibling around that counter-tilted up-axis
- Step 3: Re-apply individual_pitch (-30°) to each sibling camera
- Net effect: sibling cameras end up level/balanced around the anchor with correct yaw distribution

This was arrived at through significant iteration and trial — part of a larger effort to
cobble together the entire pipeline from scratch without a formal math or programming
background. The `global_pitch = -original_pitch` line is NOT a hack — it is the correct
formula for the coordinate system, arrived at through intuition and persistence.
Confirmed working.

**False alarm (2026-07-02):** An earlier analysis compared `expand_anchor_to_rig`'s yaw
rotation sign against `panorama_processing.get_virtual_rotations` and found a sign difference,
concluding the rotation direction was wrong. This analysis was incorrect. The difference exists
because the two functions operate in DIFFERENT coordinate frames:
- `panorama_processing.get_virtual_rotations` uses Y-up world convention (`up=[0,1,0]`)
- `expand_anchor_to_rig` operates in VGGT's world frame, which has a different orientation

The sign difference is real but expected. The rotation in `expand_anchor_to_rig` is correct
for VGGT's coordinate frame. A code change was briefly made then immediately reverted.
**No changes were made to the final state of `vggt_training.py`.**

**Dead code in `vggt_training.py` (never called at runtime):**
These are artifacts from earlier approaches tried during the rig alignment work.
They are harmless but unused:
- `align_rig_to_anchor_pose` + `calculate_rigid_body_transform` — earlier rigid-body approach, replaced by `expand_anchor_to_rig`
- `look_at_rotation_opencv` — standalone utility, never wired into pipeline
- `debug_rig_coordinate_system` — debug helper, never called
- `validate_rig_z_y_axis_angles` — explicitly commented out inside `expand_anchor_to_rig`
- `extract_yaw_pitch_roll_from_c2w` — utility, never called

**Actual call graph (runtime):**
```
app_callbacks.py
├── run_full_pipeline
│   ├── VGGTProcessor.initialize / process_vggt_inference
│   ├── unproject_depth_map_to_point_map  (VGGT import)
│   ├── [anchor+rig] expand_anchor_to_rig → convert_w2c_to_c2w
│   ├── [count only] get_virtual_rotations  (len() only, NOT used for pose computation)
│   ├── apply_quality_filters
│   ├── apply_rig_optimization → optimize_points_for_rig_coverage
│   ├── [if enabled] apply_sparse_filter → create_sparse_point_cloud_for_3dgs
│   └── create_glb_scene
├── write_colmap_files → _project_points_for_colmap → _project_points_vectorized
├── save_ply
└── create_simple_glb_viewer
```

**The only actual remaining problem — no depth/points for sibling views:**
VGGT only runs on anchor images, so `world_points_from_depth` only covers the anchor's
field of view. Sibling cameras have correct poses but no 3D points in their exclusive view
cone. This is what caused Brush training to fail for sibling views — not a rotation bug.

**Proposed fix for the point cloud gap:**
1. VGGT on anchor (pano_camera0) → N poses + depth-based point cloud (anchor FOV only)
2. `expand_anchor_to_rig` → correct sibling poses for all sensors (already correct)
3. **COLMAP `point_triangulator` with fixed poses** → extract SIFT features from ALL
   images and triangulate 3D points visible across multiple registered cameras
4. Write COLMAP format → Brush gets full multi-view point cloud coverage

The triangulation step is fast (seconds–minutes) and fills in what the sibling views see.

**Status (2026-07-02): Triangulation implemented.**

Files changed:
- `vggt_training.py` — added `collect_all_rig_images()`, `triangulate_rig_points()`,
  `fov` param to `write_colmap_files()` (uses known FOV for accurate focal length),
  `anchor_image_paths`/`image_dir` added to `run_full_pipeline()` return dict
- `app_callbacks.py` — `fov` now read from settings and passed through; triangulation
  called after `write_colmap_files` in anchor+rig mode
- `triangulate_worker.py` (new) — Python 3.14 subprocess (like colmap_worker.py):
  reads intrinsics from cameras.txt, extracts SIFT from `sparse_dir/../images/`
  (already flat-copied by app_callbacks before run_full_pipeline), sequential matches,
  triangulates with fixed poses from images.txt, overwrites points3D.txt

**Note on image pre-copying:** app_callbacks.py flat-copies ALL images from views_dir
(anchor + all siblings from every frame subdirectory) to `vggt_output_dir/images/`
before run_full_pipeline is called. The triangulation worker relies on this and does
no image copying of its own.

**FOV — confirmed correct behaviour:**
The pipeline handles focal length through a resize → estimate → scale-back loop:

1. `load_and_preprocess_images(image_paths)` resizes each anchor image to 518px before
   passing it to VGGT (VGGT's required input resolution)
2. VGGT estimates `vggt_fx` / `vggt_fy` at 518px resolution from the image content
   — no pre-calibration needed, it predicts FOV per-image
3. `write_colmap_files` scales back to the actual image resolution:
   ```python
   resize_ratio = colmap_image_width / 518   # e.g. 1920 / 518 ≈ 3.71
   fx = vggt_fx * resize_ratio               # focal length at actual image size
   ```

This means cameras.txt contains intrinsics that are consistent with both the camera poses
(which were also computed at 518px then implicitly scaled) and the actual image files on
disk. The triangulation worker reads these directly from cameras.txt — correct by design.

Overriding with the panorama extraction FOV would break this consistency: the camera
poses were derived from VGGT's intrinsic estimate, not the extraction FOV, so triangulating
with a different focal length would produce 3D points that don't align with the poses.

A `fov` parameter was added to `write_colmap_files` as an optional escape hatch but must
NOT be activated unless VGGT's estimate is confirmed to be significantly wrong.

### 2. GlueMap anchor-only (future, large captures)

For captures with 500+ images, even 2s/it becomes significant. An anchor-only
GlueMap run (28 images instead of 196) with rig-derived sibling poses and
COLMAP triangulation would be ~7× faster. Less pressing until we confirm GlueMap
quality first.

### 3. Output structure verification

GlueMap's exact output directory layout is unknown until the first run completes.
The runner searches multiple candidate locations. Need to confirm which path
`gluemap-demo` actually writes to and update `_find_colmap_output()` if needed.

### 4. GlueMap progress parsing

The `_parse_stage()` keyword heuristics in `gluemap_runner.py` are guesses based
on expected log strings. Need to verify against actual gluemap-demo output and
refine the stage → percentage mapping.

---

## Test Commands

### Run GlueMap manually from WSL2
```bash
~/.local/bin/micromamba run -n gluemap gluemap-demo \
  --images_path "/mnt/c/Users/DenmanNic/Desktop/Nile Creek GlueMap Test/images" \
  --write_path /tmp/gluemap_nile_test \
  --intrinsics_mode PER_FOLDER \
  --chosen_model pi3 \
  --path_feedforward ~/gluemap/checkpoints/pi3.safetensors \
  --path_retrieval   ~/gluemap/checkpoints/dino_salad.ckpt \
  --path_tracker     ~/gluemap/checkpoints/vggsfm_v2_0_0_track_predictor.bin \
  --path_dg          ~/gluemap/checkpoints/checkpoint-dg+visym.pth \
  --skip_doppelgangers \
  --is_sequential \
  --batch_size 60 \
  --num_track_per_img 512
```

### Build RoPE2D CUDA extension (one-time)
```bash
micromamba activate gluemap
cd ~/gluemap/thirdparty/doppelgangers-plusplus/dust3r/croco/models/curope
CUDA_HOME=$CONDA_PREFIX python setup.py build_ext --inplace
```

---

## Next Steps

1. ✅ ~~Observe first run to completion~~ — output at `gluemap_aba/`, 37.7 min, 166K points
2. ✅ ~~Inspect output~~ — excellent quality in COLMAP GUI, good quality at 1000 Brush steps
3. ✅ ~~Second run~~ — 21.9 min total, 1.72× faster; tracking 3.85× faster with num_track_per_img 512
4. **Anchor-only mode** — run GlueMap on pano_camera0 only → derive siblings via rig → triangulate
5. **Detect/fix misaligned siblings** — parse frames.bin/rigs.bin to identify bad sibling poses, recompute from anchor
6. **Wire up UI** — AlignmentTab 4th mode button + settings (backbone, skip_dg, coarse_only)
7. **VGGT anchor fix** — add COLMAP triangulation step after rig pose recovery (same pattern as anchor-only GlueMap)
8. **End-to-end pipeline test** — insp stitch → view extraction → GlueMap → Brush → gallery upload
