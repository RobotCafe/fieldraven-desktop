# Video Integration

*Last updated: 2026-07-24*

---

## Overview

This document captures the current state of video support in FieldRaven Desktop, what changed from the original SplatPipe V13 requirement, and what is still needed to make the `.insv` path fully functional.

---

## Original SplatPipe requirement

The V13 pipeline required an equirectangular `.mp4` as input. The pipeline assumed that whatever video was handed to it was *already stitched* — two spherical halves merged into a 2:1 equirectangular frame. The frame extraction stage then ran ffmpeg to pull individual JPEG frames at calculated timestamps, which were passed directly into view extraction (panorama_processing.py).

---

## Current state

### What formats the pipeline accepts

`pipeline.py` (`video_extensions` set, line 512):

```python
{'.mp4', '.mov', '.avi', '.insv', '.mkv'}
```

`.insv` is listed but **is not yet handled correctly** — see the gap below.

### Stage flow for video input

```
Stage 0  [not yet built]   .insv → equirect .mp4 / frames   (MediaSDK VideoStitcher)
Stage 1  Frame extraction   video → 01_frames/ JPEG           (video_extraction.py)
Stage 2  View extraction    ERP frames → 02_views/ crops      (panorama_processing.py)
Stage 3  VGGT / COLMAP      camera pose estimation            (vggt_training / colmap_worker)
```

Stage 1 uses `video_extraction.extract_frames_for_video()` from V13, which:
- Detects GPU (HEVC hardware decoder via ffmpeg `-c:v hevc_cuvid`)
- Falls back to CPU ffmpeg if GPU is not available
- Per-frame: spawns ffmpeg subprocess → seeks → extracts single JPEG

### What works today

| Input type | Status |
|---|---|
| `.mp4` equirectangular | ✅ End-to-end — frame extract → view extract → VGGT/COLMAP |
| `.insv` raw dual-fisheye | ⚠️ Frame extract runs but produces fisheye frames — view extraction fails |
| `.insp` raw dual-fisheye images | ✅ Already handled via `insp_stitch.exe` (`ImageStitcher` SDK) |
| Frame preview scrubber (UI) | ✅ Built: OpenCV `VideoCapture` seek + `imencode` → JPEG, no subprocess |

---

## The .insv gap

`.insv` files are raw dual-fisheye H.265 video in Insta360's proprietary container. FFmpeg and OpenCV can decode individual frames, but each frame is a side-by-side fisheye pair — **not equirectangular**. Feeding this into `panorama_processing.py` (which assumes 2:1 ERP input) produces garbage views.

Three options to fix this, in order of feasibility:

### Option A — Pre-stitch to equirectangular .mp4 then extract frames (recommended first step)

Build `insv_stitch.exe` using `VideoStitcher` from MediaSDK 3.1.3:

1. `SetInputPath([path_to.insv])` (or `[front.insv, rear.insv]` for 5.7K dual-file)
2. `SetOutputPath(out.mp4)`
3. Configure: stitch type, FlowState, DirectionLock, CUDA, output resolution
4. `StartStitch()` with progress callback
5. Output `.mp4` → hand off to existing Stage 1 (frame extraction)

This re-uses the entire existing pipeline with no changes to stages 1–3.

### Option B — Direct frame export from VideoStitcher (skip Stage 1 entirely)

`VideoStitcher` exposes:
- `SetImageSequenceInfo(output_dir, IMAGE_TYPE::JPEG)` — output directory for image sequence
- `SetExportFrameSequence(vector<uint64_t> frame_indices)` — which frame indices to export

Workflow:
1. Calculate frame indices from desired timestamps (`frame_index = timestamp × fps`)
2. Call VideoStitcher with `SetExportFrameSequence([0, 150, 300, ...])` 
3. SDK outputs stitched JPEG frames directly into `01_frames/`
4. Skip ffmpeg frame extraction stage entirely

This is more efficient (one pass vs two) but requires the frame selection math to happen before the SDK call, and the pipeline's `skip_stage1` logic needs updating.

### Option C — COLMAP dual-fisheye (research-grade, not near-term)

Treat each lens as a separate `OPENCV_FISHEYE` camera, configure a rig with a fixed baseline, and run COLMAP rig-aware matching directly on raw fisheye frames. This is the most geometrically accurate but requires significant pipeline rework and produces worse results unless the rig calibration is tight. Defer until Option A/B is proven.

---

## Frame preview (built)

### Backend endpoints

Both endpoints use **OpenCV `VideoCapture`** (same approach as `GPU_video_extraction.py` in V13) — no subprocess spawn, no temp file, no disk I/O for preview frames.

```
GET /api/jobs/{job_id}/video-info
```
- Returns `{duration, fps, width, height, frame_count}`
- Primary: `cv2.VideoCapture` props — instant (reads container header only)
- Fallback: `ffprobe --show_format` for containers where OpenCV reports 0 frames (e.g. some `.insv` variants)

```
GET /api/jobs/{job_id}/preview-frame?timestamp=12.5
```
- Returns JPEG bytes
- Primary: `cap.set(CAP_PROP_POS_MSEC, ms)` → `cap.read()` → `imencode` — all in-memory
- GPU path: if `cuda.getCudaEnabledDeviceCount() > 0`, uses `cuda_GpuMat` for the resize step
- Fallback: spawns ffmpeg subprocess if OpenCV cannot open the file

### Frontend scrubber (ExtractionTab)

- On video job selection → fetches `video-info` → sets scrubber range
- Scrubber updates `previewTs` immediately (smooth drag)
- 300ms debounce before firing `preview-frame` request (avoids hammering ffmpeg/OpenCV during fast drags)
- Canvas: draws real video frame + pitch/yaw overlay on top (same draw path as ERP image preview)
- "Calculate Frame List" button → computes timestamps from interval/count settings → populates frame gallery with lazy-loaded thumbnails
- Frame gallery thumbnails each hit `preview-frame` with their timestamp (browser lazy-loads)

---

## Gyroscope / IMU metadata

### What's in the SDK

`ins_common.h` defines:

```cpp
struct GyroData {
    int64_t timestamp;
    double ax, ay, az;   // accelerometer
    double gx, gy, gz;   // gyroscope
};

struct CameraInfo {
    // ...
    int64_t gyro_timestamp;
    int64_t sweep_timestamp;
};
```

`GetMediaFileInfo()` (free function in `ins_stitcher.h`) returns `MediaFileInfo`:

```cpp
struct MediaFileInfo {
    MediaFileType media_type;
    int width, height;
    double fps;
    int64_t bitrate;
    int64_t duration_ms;
};
```

`GyroData` is defined but there is **no exposed API to read raw gyro samples** from a file in the current SDK headers. The gyro data is consumed internally by FlowState.

### Practical horizon locking via SDK

The cleanest path for "up" orientation is already implemented in the SDK:

```
EnableFlowState(true) → EnableDirectionLock(true)
```

`EnableFlowState` uses the embedded gyroscope to produce smooth, stabilised output. `EnableDirectionLock` (depends on FlowState being enabled) locks the video to a fixed forward direction — effectively anchoring the horizon. This requires no gyro extraction code; the SDK handles it internally.

This maps directly to the FlowState + DirectionLock toggles already in the ExtractionTab's "Insta360 Stitch" settings panel.

### Manual gyro extraction (if needed later)

If raw IMU data is needed (e.g. to compute COLMAP orientation priors), `exiftool` can extract Insta360 telemetry:

```bash
exiftool -ee -G3 VID_xxx.insv > telemetry.txt
```

This outputs the embedded accelerometer + gyroscope samples at native rate (typically 200Hz). The data can be integrated to get orientation quaternions at any frame timestamp. This is a future research item — not needed while the SDK's DirectionLock covers the use case.

---

## Settings exposed in UI

The ExtractionTab "Insta360 Stitch" accordion (currently applies to `.insp` image stitching, will apply to `.insv` video stitching when `insv_stitch.exe` is built):

| Setting | SDK method | Notes |
|---|---|---|
| Stitch Type | `SetStitchType(TEMPLATE/OPTFLOW/DYNAMICSTITCH/AIFLOW)` | Template = fastest, AI = best seams |
| Lens Guard | `SetCameraAccessoryType(...)` | Corrects stitch for accessory glass |
| FlowState | `EnableFlowState(bool)` | Stabilisation using IMU; required for DirectionLock |
| Direction Lock | `EnableDirectionLock(bool)` | Locks horizon; requires FlowState on |
| CUDA | `EnableCuda(bool)` | Hardware GPU encode/decode |
| Output Width | `SetOutputSize(width, height)` | 3K / 4K / 6K / 12K |
| Color correction | `SetExposure/Highlights/Shadows/...` | Full Insta360 Studio equivalent |

---

## What needs to be built next

1. **`insv_stitch.exe`** — C++ binary using `VideoStitcher` from MediaSDK 3.1.3; called from `pipeline.py` as a new Stage 0 when input is `.insv`. Outputs equirectangular `.mp4` or image sequence.

2. **Pipeline stage 0 dispatch** — `pipeline.py` should detect `.insv` → run `insv_stitch.exe` → hand stitched output to existing Stage 1. The `create-video` Firestore job can carry `{'stitchSettings': {...}}` to pass FlowState/CUDA/resolution to the exe.

3. **`SetExportFrameSequence` path (Option B)** — once `insv_stitch.exe` exists, add a mode where it outputs frames directly to `01_frames/` using the SDK's image sequence export, bypassing ffmpeg entirely. Frame indices are pre-calculated from the settings.

4. **UI: show `.insv` stitch settings** — currently the Insta360 Stitch accordion shows for `isFR` (FieldRaven camera jobs) only. It should also show for `isVideo` when the video file extension is `.insv`.
