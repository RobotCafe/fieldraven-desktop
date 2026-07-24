# Adding a New Alignment Mode

Replace `newmode` / `NewMode` / `New Mode` with your actual name throughout.
Internal IDs (used in Firestore, file paths, stage keys) must be lowercase_snake — e.g. `newmode_alignment`.
User-facing labels can be anything — e.g. `"FastRig"`.

---

## 1. `splatpipe_core/types.py`

Add to the `PipelineStage` enum:

```python
NEWMODE_ALIGNMENT = "newmode_alignment"
```

---

## 2. `splatpipe_core/settings.py`

Add fields to `PipelineSettings`:

```python
# ── NewMode ──────────────────────────────────────────────────
run_newmode: bool = False
newmode_some_option: str = "default"   # add as many as needed
```

---

## 3. `splatpipe_core/pipeline.py`

### 3a. Skip-stage sets
Add `'newmode_alignment'` to every `_SKIP_STAGE*` set that should skip earlier stages when resuming from this one.

### 3b. Pipeline block
Copy the GluMap or RigGluemap block as a template. Add after the GluMap block:

```python
if getattr(settings, "run_newmode", False):
    from .newmode_runner import run_newmode_pipeline
    colmap_dir      = job_dir / "03_alignment" / "colmap"
    brush_input_dir = training_dir / "brush_input"
    skip_newmode    = start_from in ("brush_training",) and brush_input_dir.exists()
    if skip_newmode:
        report(PipelineStage.NEWMODE_ALIGNMENT, 100, "NewMode alignment skipped — resuming")
    else:
        run_newmode_pipeline(views_dir, colmap_dir, brush_input_dir, settings, report, cancel_event, job_dir)
    # validation + training calls + return  (copy from rigsfm block)
```

---

## 4. `backend/pipeline_runner.py`

### 4a. Stage range maps (near top of file, after the GluMap ones)

```python
_STAGE_RANGE_NEWMODE = {
    "frame_extraction":    (5,  20),
    "view_extraction":     (20, 45),
    "newmode_alignment":   (45, 82),
    "brush_training":      (82, 97),
}
_STAGE_RANGE_NEWMODE_POST_STITCH = {
    "frame_extraction":    (47, 57),
    "view_extraction":     (57, 72),
    "newmode_alignment":   (72, 88),
    "brush_training":      (88, 97),
}
```

### 4b. `_build_settings()` — read from `_ui_settings`

In the `if ui:` block, after the GluMap lines:

```python
if "run_newmode" in ui:          s.run_newmode          = _to_bool(ui["run_newmode"])
if "newmode_some_option" in ui:  s.newmode_some_option  = ui["newmode_some_option"]
```

### 4c. `_worker()` — flags, stage map, `_mode`

```python
# Flag
use_newmode = getattr(settings, "run_newmode", False)

# Add to _no_sfm exclusion (so RS+Brush is not the fallback)
_no_sfm = not settings.run_vggt and not use_colmap and not use_gluemap and not use_rigsfm and not use_newmode

# Stage map selector — add BEFORE the else/VGGT fallback
if use_newmode:
    stage_map = _STAGE_RANGE_NEWMODE_POST_STITCH if insp_count else _STAGE_RANGE_NEWMODE
    print(f"  → Stage map: NewMode {'(post-stitch)' if insp_count else '(direct)'}")

# _mode in on_progress — add to the chain
_mode = ("newmode"  if use_newmode
         else "rigsfm"  if use_rigsfm
         ...)
```

---

## 5. `backend/server.py`

### 5a. `/api/project/state` — pipeline_mode detection

```python
run_newmode = _b(saved_settings.get("run_newmode"), False)

# In the if/elif chain:
elif run_newmode:
    pipeline_mode = "newmode"
```

### 5b. `/api/project/state` — alignment done check

Add before the `stages[...]` assignments:

```python
# NewMode: output lands at 03_alignment/newmode/sparse_txt
newmode_sparse = project_dir / "03_alignment" / "newmode" / "sparse_txt"
newmode_done   = newmode_sparse.exists() and any(
    (newmode_sparse / f).exists()
    for f in ("cameras.txt", "images.txt", "cameras.bin", "images.bin")
)

stages["newmode_alignment"] = {
    "done":        newmode_done,
    "completedAt": saved_stages.get("newmode_alignment", {}).get("completedAt"),
}
```

### 5c. `/api/project/state` — stage_order

```python
elif pipeline_mode == "newmode":
    stage_order = ["import", "view_extraction", "newmode_alignment", "brush_training"]
```

### 5d. `/api/project/resume` — `_ui_settings`

```python
"run_newmode":          saved_settings.get("run_newmode", False),
"newmode_some_option":  saved_settings.get("newmode_some_option", "default"),
```

### 5e. `_STAGE_DIRS_TO_DELETE`

```python
"newmode_alignment": ["03_alignment", "04_training"],
```

---

## 6. `frontend-react/src/App.jsx`

### 6a. `API_TO_UI` mapping

```js
run_newmode:'runNewmode', newmode_some_option:'newmodeSomeOption',
```

### 6b. `STRING_SETTINGS`

Add any settings whose values are strings (not numbers/booleans):

```js
'newmodeSomeOption',
```

### 6c. `defaultSettings`

```js
runNewmode: false, newmodeSomeOption: 'default',
```

### 6d. `mode` derivation

```js
const mode = settings.runColmap   ? 'colmap'
           : settings.runGluemap  ? 'gluemap'
           : settings.runRigsfm   ? 'rigsfm'
           : settings.runNewmode  ? 'newmode'   // ← add here
           : settings.runVggt     ? 'vggt'
           : 'rs';
```

### 6e. `setMode`

```js
runNewmode: m === 'newmode',
```

### 6f. `MODES` array

```js
{ id:'newmode', label:'New Mode Label', desc:'One-line description for the user' },
```

### 6g. Options panel (after GluMap / RigGluemap blocks)

```jsx
{mode === 'newmode' && (
  <div>
    <Accordion title="New Mode Options" defaultOpen={false}>
      <FieldRow label="Some Option">
        ...
      </FieldRow>
    </Accordion>
  </div>
)}
```

### 6h. Pipeline summary line

```jsx
{mode === 'newmode' && `New Mode — ${settings.newmodeSomeOption}`}
```

### 6i. `_STAGES_NEWMODE` constant

```js
const _STAGES_NEWMODE = {
  labels: ['Frames', 'Views', 'New Mode Label', 'Brush'],
  keys:   ['frame_extraction', 'view_extraction', 'newmode_alignment', 'brush_training'],
};
```

### 6j. `stageDef` in `ActiveJobTab`

```js
: pipelineMode === 'newmode'  ? _STAGES_NEWMODE
```

### 6k. `allStages` in `ProjectStateModal`

```js
: _mode === 'newmode'
? ['import', 'view_extraction', 'newmode_alignment', 'brush_training']
```

### 6l. `STAGE_LABELS`

```js
newmode_alignment: { label: "New Mode Alignment", icon: "🗺️" },
```

### 6m. `_mode` in `runPipeline` and `runPipelineResume`

```js
settings.runNewmode ? 'newmode' :
```
(add to both places — search for `runRigsfm ? 'rigsfm'`)

### 6n. Validation message in `runPipeline`

Add the new label to the "no alignment method selected" error string.

### 6o. Rebuild

```bash
cd frontend-react
npm run build
```

---

## Quick Checklist

- [ ] `types.py` — add `PipelineStage` value
- [ ] `settings.py` — add `run_newmode` + option fields
- [ ] `pipeline.py` — skip sets + pipeline block
- [ ] `pipeline_runner.py` — stage range maps
- [ ] `pipeline_runner.py` — `_build_settings` reads `_ui_settings`
- [ ] `pipeline_runner.py` — `_worker` flags + stage map + `_mode`
- [ ] `server.py` — `pipeline_mode` detection
- [ ] `server.py` — alignment done check + `stages[...]`
- [ ] `server.py` — `stage_order` branch
- [ ] `server.py` — resume `_ui_settings`
- [ ] `server.py` — `_STAGE_DIRS_TO_DELETE`
- [ ] `App.jsx` — `API_TO_UI` mapping
- [ ] `App.jsx` — `STRING_SETTINGS`
- [ ] `App.jsx` — `defaultSettings`
- [ ] `App.jsx` — `mode` derivation
- [ ] `App.jsx` — `setMode`
- [ ] `App.jsx` — `MODES` array
- [ ] `App.jsx` — options panel
- [ ] `App.jsx` — pipeline summary line
- [ ] `App.jsx` — `_STAGES_NEWMODE` constant
- [ ] `App.jsx` — `stageDef` in `ActiveJobTab`
- [ ] `App.jsx` — `allStages` in `ProjectStateModal`
- [ ] `App.jsx` — `STAGE_LABELS`
- [ ] `App.jsx` — `_mode` in `runPipeline` + `runPipelineResume`
- [ ] `App.jsx` — validation message
- [ ] `npm run build`
