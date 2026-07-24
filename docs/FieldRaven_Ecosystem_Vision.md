# FieldRaven — Ecosystem Vision & SplatPipe Integration

## Context for VS Code Claude

This document captures a vision workshopped between the developer and Claude (claude.ai) for expanding FieldRaven from a field work tracker into a full spatial data collection and coordination platform. It should be read alongside the existing handoff documents (`FieldRaven_Handoff.md`, `FieldRaven_Auth_Spec.md`, `FieldRaven_Web_Notes.md`, `FieldRaven_JobType_Spec.md`, `FieldRaven_Insta360_Spec.md`).

---

## The Big Picture

FieldRaven is evolving into a **three-part spatial data platform**:

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Mobile App    │     │    Web App      │     │  Desktop App    │
│  (field tool)   │────▶│  (coordinator)  │────▶│  (SplatPipe)    │
│                 │     │                 │     │                 │
│ GPS tracking    │     │ Job planning    │     │ Gaussian splats │
│ 360° capture    │     │ Client mgmt     │     │ Point clouds    │
│ Voice notes     │     │ Reports/gallery │     │ Photogrammetry  │
│ Job execution   │     │ Job queue       │     │ COLMAP export   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
          │                      │                      │
          └──────────────────────┴──────────────────────┘
                                 │
                           Firebase
                    (coordinator, not storage)
                    metadata + GPS + thumbnails
                    job queue + status + output links
```

**Key principle:** Firebase holds **coordination data only** — metadata, GPS tracks, thumbnails, job status, output links. Heavy files (raw 360° images, video, splat outputs) stay on local hardware and are never pushed to Firebase.

---

## The Three Components

### 1. Mobile App — Field Execution Tool
**Built:** Expo / React Native, Android, `com.fieldraven`

Current capabilities:
- Job timer with GPS start/end
- Phone camera + Insta360 X4 capture (SDK approved, integration in progress)
- Email reports with photos + satellite map
- Firebase Firestore + Storage backend
- Offline-first with upload queue

Planned additions (from `FieldRaven_JobType_Spec.md`):
- Project type selector (Simple Job, 360 Survey, Video, Timelapse, Other)
- Job setup screen (camera choice, resolution, trigger mode, intervals)
- GPS track recording during surveys
- Waypoint-guided job execution (pre-planned from web)
- Voice recording with auto-transcription
- Interval/distance-based auto capture
- Job completion → gallery published

### 2. Web App — Coordination Hub
**Built:** Next.js 16, TypeScript, Tailwind v4, Firebase, deployed at `app.fieldraven.ca`

Current capabilities:
- Login (Google + email/password + magic link)
- Dashboard, Jobs, Clients, Map, Settings
- Full Leaflet map reading from Firestore
- Public photo gallery with Pannellum 360° viewer (planned)
- Client management

Planned additions:
- **Job planner** — pre-plan survey jobs with waypoints, boundaries, capture intervals, target heights
- **Job queue** — assign jobs to specific devices/machines
- **SplatPipe bridge** — show processing status, display outputs
- **3D output viewer** — view gaussian splat results in browser (WebGPU)
- **Editable SMS/email templates** per client (CRM-like)
- **Voice note transcripts** alongside job data
- **Desktop job queue** — jobs waiting for desktop processing shown on login

### 3. Desktop App — SplatPipe (already built)
**Built:** Python 3, Tkinter GUI, PyInstaller Windows executable

**Tech stack summary:**
- OpenCV + FFmpeg — frame extraction and image manipulation
- py360convert — equirectangular ↔ perspective projection (core 360° processing)
- PyTorch + VGGT (Meta) — neural camera pose estimation (replaces traditional SfM)
- pycolmap — COLMAP-format output (cameras.txt, images.txt, points3D.txt)
- RealityScan (Epic) — Structure-from-Motion / photogrammetry
- Postshot (Jawset) — Gaussian Splat training (Splat3, MCMC, ADC profiles)
- Brush — open-source WebGPU Gaussian Splat trainer (alternative to Postshot)
- CuPy — optional CUDA acceleration with CPU fallback
- trimesh — point cloud manipulation, .glb export
- onnxruntime — sky segmentation filtering

**Output formats:** COLMAP text, .ply point clouds, .psht (Postshot splats), .glb (debug viz)

**Current workflow:** Fully automated once pointed at input files. No internet connectivity. No job queue — operator manually points it at files.

---

## The Missing Link — Firebase Coordination Bridge

### What needs to be built

**On the SplatPipe side (Python):**
Add Firebase connectivity using `firebase-admin` Python SDK. SplatPipe needs to:

1. **Poll Firebase** for jobs assigned to this machine (every 30s or on-demand)
2. **Pick up new jobs** — read job metadata, find files on local disk
3. **Report status** back to Firebase throughout processing:
   - `queued` → `processing` → `complete` → `error`
4. **Write output paths** back to Firebase when done
5. **Upload thumbnail/preview** of the splat output (small file, suitable for Firebase Storage)

```python
# Rough sketch — Firebase bridge for SplatPipe
import firebase_admin
from firebase_admin import credentials, firestore
import time

cred = credentials.Certificate('fieldraven-service-account.json')
firebase_admin.initialize_app(cred)
db = firestore.client()

def poll_job_queue(machine_id: str):
    """Check Firebase for jobs assigned to this machine"""
    jobs_ref = db.collection('processing_queue')
    pending = jobs_ref.where('assignedMachine', '==', machine_id)\
                      .where('status', '==', 'queued')\
                      .limit(1)\
                      .get()
    return [doc.to_dict() | {'id': doc.id} for doc in pending]

def update_job_status(job_id: str, status: str, extra: dict = {}):
    db.collection('processing_queue').document(job_id).update({
        'status': status,
        'updatedAt': firestore.SERVER_TIMESTAMP,
        **extra
    })
```

**On the Web App side (Next.js):**
Add a processing queue UI:
- Dashboard shows "X jobs waiting to process" on login
- Job detail page shows processing status (queued / processing / complete)
- When complete, shows link to output viewer
- Operator can assign a job to a specific machine ID

**On the Mobile App side (React Native):**
- After finishing a 360 Survey job, show "Ready to process" status
- Job metadata + GPS + thumbnails already in Firebase
- Raw files on camera SD card — user brings camera home, plugs in
- App shows "Waiting for desktop processing" state

---

## Firestore Data Model — Processing Queue

New collection: `processing_queue/{jobId}`

```javascript
{
  jobId: string,              // references users/{uid}/jobs/{jobId}
  uid: string,                // owner user ID
  clientName: string,         // denormalized for display
  jobType: '360survey' | 'video' | 'timelapse',
  status: 'queued' | 'processing' | 'complete' | 'error',
  assignedMachine: string,    // machine ID (set by operator in web app)
  
  // Input
  inputPath: string,          // local path on desktop machine where files are
  fileCount: number,          // number of input files expected
  
  // Processing config (from job setup)
  processingProfile: 'splat3' | 'mcmc' | 'adc' | 'pointcloud',
  targetResolution: 'low' | 'medium' | 'high',
  
  // Output (written by SplatPipe when done)
  outputPath: string,         // local path to output files
  outputFormat: 'psht' | 'ply' | 'glb',
  previewUrl: string,         // Firebase Storage URL of thumbnail/preview
  webViewerUrl: string,       // URL to view splat in browser (if applicable)
  
  // Status tracking
  progress: number,           // 0-100
  currentStep: string,        // e.g. "Extracting frames", "Running VGGT", "Training splat"
  errorMessage: string,       // if status == 'error'
  
  createdAt: timestamp,
  startedAt: timestamp,
  completedAt: timestamp,
  updatedAt: timestamp,
}
```

---

## Voice Recording + Transcription

**Mobile app addition:**

During any job (especially surveys), a voice recording button is available. The recording is:
1. Captured via `expo-av`
2. Timestamped and GPS-tagged at moment of recording
3. Stored locally with the job
4. Uploaded to Firebase Storage as `.m4a`
5. Transcription triggered via Google Cloud Speech-to-Text API (user is already authenticated with Google)

**Stored in Firestore:**
```javascript
voiceNotes: [
  {
    index: 0,
    audioUrl: string,         // Firebase Storage URL
    transcript: string,       // auto-transcribed text
    lat: number,
    lon: number,
    timestamp: number,
    associatedPhotoIndex: number | null,  // if taken near a photo capture
  }
]
```

**In the web app:**
- Voice notes shown chronologically in job detail page
- Transcript displayed as text, audio playable
- Associated with nearby photos on the gallery timeline

---

## Waypoint-Guided Job Execution (Pre-Planned Surveys)

**Web app — job planner:**
- Draw survey boundary on map (polygon tool)
- Set capture point grid (automatic based on overlap % and camera FOV)
- Or manually place waypoints
- Set target heights for gaussian splatting coverage
- Pull in topographic data (SRTM/DEM from public APIs — free)
- Export job plan to Firebase

**Mobile app — job execution:**
- Downloads pre-planned job from Firebase
- Shows waypoints on map
- GPS proximity detection — phone beeps/vibrates when within range of a waypoint
- "Ready to capture" prompt at each waypoint
- Marks waypoints complete as photos are taken
- Shows progress: "8/24 waypoints complete"
- Allows skipping inaccessible waypoints
- At end: "All waypoints complete — finish job?"

---

## Gaussian Splat Web Viewer

For delivering outputs to clients via the web app, the best options are:

**Option A — Luma AI embed (easiest)**
Upload .ply to Luma AI, embed their viewer. Requires Luma account, external dependency.

**Option B — Brush WebGPU viewer (already in your stack)**
Brush is already part of SplatPipe. It has a WebGPU-based viewer. Could be self-hosted.

**Option C — Three.js / babylonjs custom viewer**
Render .splat or .ply directly in browser. More work but full control.

**Recommendation:** Start with Option B since Brush is already in your stack. SplatPipe uploads the .splat file to Firebase Storage, web app loads it in a Brush-compatible WebGPU viewer.

---

## Editable SMS/Email Templates (CRM Features)

From voice memos — the web app client management needs:

**Per-client customizable templates:**
- SMS template: variables like `{clientFirstName}`, `{startTime}`, `{gpsLink}`, `{jobType}`
- Email report template: same variables plus `{duration}`, `{photoCount}`, `{galleryLink}`, `{mapLink}`
- General template with AI-assisted fill (optional later feature)

**Storage:** Templates stored per-client in Firestore:
```javascript
// In client document
smsTemplate: string,      // custom or null (falls back to global default)
emailTemplate: string,    // custom or null
```

**In web app:** Clients page gains a "Templates" section per client with a rich text editor (or at minimum a textarea with variable hints).

---

## Implementation Priorities

Given everything on the table, here is a suggested priority order:

### Immediate (unblock current work)
1. Complete Insta360 X4 SDK native module (separate spec)
2. Fix GPS track recording during 360 Survey jobs
3. Voice recording in mobile app

### Short term (complete the field→web pipeline)
4. Public gallery page with Pannellum 360° viewer
5. GPS track display on web map
6. Job type filtering in Log tab
7. Editable email/SMS templates in web app

### Medium term (add the coordination layer)
8. Firebase bridge in SplatPipe (Python firebase-admin integration)
9. Processing queue UI in web app dashboard
10. Job status tracking (queued → processing → complete)
11. Gaussian splat web viewer

### Longer term (pre-planned surveys)
12. Waypoint job planner in web app
13. Waypoint-guided execution in mobile app
14. Topographic data integration (SRTM/DEM)
15. Voice note transcription (Google Speech-to-Text)

---

## Open Questions (to resolve before building)

1. **SplatPipe input files** — when you return from the field, how do raw files currently get from the camera to the desktop? Manual SD card copy to a specific folder? This determines how the Firebase bridge finds input files.

2. **Output format for web viewer** — does SplatPipe currently output anything browser-viewable (.splat, .ksplat, .glb)? Or only .psht (Postshot-specific) and .ply?

3. **Machine ID** — how should a desktop machine identify itself to Firebase? Username + hostname? A user-defined name set in SplatPipe settings?

4. **Multi-machine** — is there a scenario where multiple desktop machines process jobs for the same Firebase account? (e.g. a more powerful machine for heavy jobs)

5. **SplatPipe rename** — from the voice memos, renaming SplatPipe to FieldRaven Desktop was mentioned. Worth doing when the Firebase bridge is added.

---

## Notes for VS Code Claude

- The mobile app is at `C:\Users\DenmanNic\Projects\FieldRaven`
- The web app is at `C:\Users\DenmanNic\Projects\FieldRaven-Web`
- SplatPipe (desktop) is a separate Python project — location TBD
- Both apps share the same Firebase project: `fieldraven-ffad8`
- Firebase config is in `src/firebase.ts` (web) and `src/firebase.js` (mobile)
- Firestore structure: `users/{uid}/jobs`, `users/{uid}/clients`
- New collection needed: `processing_queue/{jobId}` (described above)
- The workspace file is at `C:\Users\DenmanNic\Projects\fieldraven.code-workspace`
- Default to your own judgment on implementation details — this doc is directional, not prescriptive
