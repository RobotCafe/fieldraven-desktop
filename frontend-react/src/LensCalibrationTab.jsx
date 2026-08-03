// "Lens Calibration" tab — embeds the standalone lens-calibrator tool's
// workflow (board setup → generate/print → import & detect → calibrate →
// undistort → export) directly into the desktop app, plus named-profile
// save/list/delete so a colmap_fisheye pipeline job can select a saved
// front/back lens calibration by name.
//
// Deliberately self-contained: defines its own small styling primitives
// (matching App.jsx's palette/shape) rather than importing App.jsx's
// module-private ones, to avoid touching that file's core component set.
import { useState, useEffect } from 'react';
import { calibratorApi as api } from './calibratorApi';

// ─── Local styling primitives (mirrors App.jsx's palette) ─────────────────────
const T = {
  void: "#090c12", surface: "#141826", surfaceEl: "#1f263a",
  border: "#252d42", amber: "#e8a442", live: "#39e07a", danger: "#e05555",
  textPri: "#dde4f0", textSec: "#7a8aaa", textDim: "#3d4860",
};

function Label({ children, color, style = {} }) {
  return <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: ".6px",
    textTransform: "uppercase", color: color || T.textDim, ...style }}>{children}</span>;
}

function Btn({ children, onClick, disabled, variant = "primary", small, style = {} }) {
  const base = {
    display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6,
    padding: small ? "4px 10px" : "7px 16px",
    fontSize: small ? "11px" : "12px", fontWeight: 600,
    border: "1px solid", borderRadius: 4, cursor: disabled ? "not-allowed" : "pointer",
    transition: "all .15s", letterSpacing: ".3px", whiteSpace: "nowrap",
    opacity: disabled ? .4 : 1, fontFamily: "inherit",
  };
  const V = {
    primary: { background: T.amber, borderColor: T.amber, color: "#000" },
    ghost:   { background: "transparent", borderColor: T.border, color: T.textSec },
    live:    { background: T.live, borderColor: T.live, color: "#000" },
    danger:  { background: "transparent", borderColor: T.danger, color: T.danger },
  };
  return (
    <button onClick={disabled ? undefined : onClick} style={{ ...base, ...V[variant], ...style }}>
      {children}
    </button>
  );
}

function FieldRow({ label, children, hint }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "160px 1fr", alignItems: "start",
      gap: "6px 10px", marginBottom: 8 }}>
      <Label style={{ paddingTop: 7 }}>{label}</Label>
      <div>
        {children}
        {hint && <div style={{ fontSize: 10, color: T.textDim, marginTop: 2 }}>{hint}</div>}
      </div>
    </div>
  );
}

function Input({ value, onChange, type = "text", placeholder, style = {} }) {
  return (
    <input type={type} value={value} onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      style={{ width: "100%", background: T.void, border: `1px solid ${T.border}`,
        borderRadius: 3, padding: "5px 8px", color: T.textPri, fontSize: 12,
        outline: "none", fontFamily: "inherit", ...style }} />
  );
}

function Select({ value, onChange, options }) {
  return (
    <select value={value} onChange={e => onChange(e.target.value)}
      style={{ width: "100%", background: T.void, border: `1px solid ${T.border}`,
        borderRadius: 3, padding: "5px 8px", color: T.textPri, fontSize: 12,
        outline: "none", fontFamily: "inherit" }}>
      {options.map(o => <option key={o.value ?? o} value={o.value ?? o}>{o.label ?? o}</option>)}
    </select>
  );
}

function Accordion({ title, defaultOpen = true, children, accent }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div style={{ border: `1px solid ${T.border}`, borderRadius: 4, marginBottom: 6, overflow: "hidden" }}>
      <div onClick={() => setOpen(v => !v)}
        style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "7px 10px", background: T.surfaceEl, cursor: "pointer" }}>
        <Label color={accent || T.textDim}>{title}</Label>
        <span style={{ color: T.textDim, fontSize: 12 }}>{open ? "▾" : "▸"}</span>
      </div>
      {open && <div style={{ padding: 10, background: T.surface }}>{children}</div>}
    </div>
  );
}

function ErrorText({ children }) {
  if (!children) return null;
  return <div style={{ fontSize: 11, color: T.danger, marginTop: 6 }}>{children}</div>;
}

// ─── Board Setup ────────────────────────────────────────────────────────────
const DICTIONARIES = ['DICT_4X4_50', 'DICT_5X5_100', 'DICT_6X6_250', 'DICT_APRILTAG_36h11'];

function BoardSetupPanel({ board, onSaved }) {
  const [form, setForm] = useState(board || {
    name: 'default', squares_x: 10, squares_y: 7,
    square_size_mm: 30, marker_size_mm: 22, dictionary: 'DICT_5X5_100',
  });
  const [saving, setSaving] = useState(false);
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));

  const save = async () => {
    setSaving(true);
    const result = await api.setBoard({
      ...form,
      squares_x: Number(form.squares_x),
      squares_y: Number(form.squares_y),
      square_size_mm: Number(form.square_size_mm),
      marker_size_mm: Number(form.marker_size_mm),
    });
    setSaving(false);
    if (result.ok) onSaved(result.board);
  };

  return (
    <Accordion title="Board Profile" accent={T.amber}>
      <FieldRow label="Squares (X)"><Input type="number" value={form.squares_x} onChange={v => set('squares_x', v)} /></FieldRow>
      <FieldRow label="Squares (Y)"><Input type="number" value={form.squares_y} onChange={v => set('squares_y', v)} /></FieldRow>
      <FieldRow label="Square size (mm)" hint="Caliper-measured — not the print job's stated size.">
        <Input type="number" value={form.square_size_mm} onChange={v => set('square_size_mm', v)} />
      </FieldRow>
      <FieldRow label="Marker size (mm)"><Input type="number" value={form.marker_size_mm} onChange={v => set('marker_size_mm', v)} /></FieldRow>
      <FieldRow label="Dictionary">
        <Select value={form.dictionary} onChange={v => set('dictionary', v)}
          options={DICTIONARIES.map(d => ({ value: d, label: d }))} />
      </FieldRow>
      <Btn onClick={save} disabled={saving} style={{ marginTop: 6 }}>
        {saving ? 'Saving…' : 'Save Board Profile'}
      </Btn>
    </Accordion>
  );
}

// ─── Generate & Print ───────────────────────────────────────────────────────
const DPI_OPTIONS = [
  { value: 150, label: '150 DPI — draft / home inkjet' },
  { value: 300, label: '300 DPI — recommended for print shop' },
  { value: 600, label: '600 DPI — max sharpness, large file' },
];

function GenerateBoardPanel({ board }) {
  const [dpi, setDpi] = useState(300);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [generating, setGenerating] = useState(false);

  const generate = async () => {
    setGenerating(true); setError(null);
    const res = await api.generateBoard(dpi);
    setGenerating(false);
    if (res.error) return setError(res.error);
    setResult(res);
  };

  return (
    <Accordion title="Generate & Print Board" defaultOpen={false}>
      {!board ? (
        <div style={{ fontSize: 12, color: T.textSec }}>Save a board profile first — the generator uses those exact dimensions.</div>
      ) : (
        <>
          <FieldRow label="Resolution">
            <Select value={dpi} onChange={v => setDpi(Number(v))} options={DPI_OPTIONS} />
          </FieldRow>
          <Btn onClick={generate} disabled={generating}>{generating ? 'Rendering…' : 'Generate Image'}</Btn>
          <ErrorText>{error}</ErrorText>
          {result && (
            <div style={{ marginTop: 12, fontSize: 11, color: T.textSec, lineHeight: 1.6 }}>
              <div>Output: {result.pixel_width}x{result.pixel_height}px @ {result.dpi} DPI</div>
              <div style={{ color: T.live }}>Physical size: {result.width_mm}mm x {result.height_mm}mm</div>
              <a href={api.boardDownloadUrl()} download="charuco_board_print.png">
                <Btn variant="ghost" style={{ marginTop: 8 }}>Download PNG</Btn>
              </a>
              <div style={{ marginTop: 10, borderTop: `1px solid ${T.border}`, paddingTop: 8 }}>
                <div style={{ color: T.amber, marginBottom: 4 }}>Before you send it to print:</div>
                <div>• "Print at 100% / actual size — do not scale to fit page."</div>
                <div>• The board has a 50mm reference line baked in — measure it with calipers after printing.</div>
                <div>• Matte, not glossy — glossy glares and confuses corner detection.</div>
                <div>• Rigid substrate — a flexed print reads as lens distortion.</div>
              </div>
            </div>
          )}
        </>
      )}
    </Accordion>
  );
}

// ─── Import & Detect ────────────────────────────────────────────────────────
function DetectionPanel({ boardReady, onDetected }) {
  const [folder, setFolder] = useState('');
  const [images, setImages] = useState([]);
  const [results, setResults] = useState([]);
  const [scanning, setScanning] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [error, setError] = useState(null);

  const scan = async () => {
    setError(null); setScanning(true);
    const res = await api.scanImages(folder);
    setScanning(false);
    if (res.error) return setError(res.error);
    setImages(res.images); setResults([]);
  };

  const detect = async () => {
    setDetecting(true);
    const res = await api.runDetection();
    setDetecting(false);
    if (res.error) return setError(res.error);
    setResults(res.results);
    onDetected?.(res.results);
  };

  const toggle = async (filename, currentlyExcluded) => {
    await api.toggleImage(filename, !currentlyExcluded);
    setResults(r => r.map(x => x.filename === filename ? { ...x, excluded: !currentlyExcluded } : x));
  };

  const passCount = results.filter(r => r.success && !r.excluded).length;

  return (
    <Accordion title="Import & Detect" defaultOpen={false}>
      <FieldRow label="Folder path" hint="Raw per-lens fisheye frames — not the stitched equirect.">
        <div style={{ display: "flex", gap: 6 }}>
          <Input value={folder} onChange={setFolder} placeholder="C:\FieldRaven\Calibration\raw\front" />
          <Btn small onClick={scan} disabled={!boardReady || scanning || !folder}>{scanning ? '…' : 'Scan'}</Btn>
        </div>
      </FieldRow>
      {!boardReady && <div style={{ fontSize: 11, color: T.amber }}>Save a board profile first.</div>}
      <ErrorText>{error}</ErrorText>
      {images.length > 0 && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 8 }}>
          <span style={{ fontSize: 11, color: T.textSec }}>{images.length} images found</span>
          <Btn small onClick={detect} disabled={detecting}>{detecting ? 'Detecting…' : 'Run Detection'}</Btn>
        </div>
      )}
      {results.length > 0 && (
        <>
          <div style={{ fontSize: 11, color: T.textSec, margin: "10px 0 6px" }}>
            {passCount} / {results.length} usable — click a tile to include/exclude
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(110px, 1fr))", gap: 6 }}>
            {results.map(r => (
              <div key={r.filename} onClick={() => toggle(r.filename, r.excluded)}
                title="Click to toggle inclusion in calibration"
                style={{ border: `1px solid ${r.excluded ? T.danger : r.success ? T.live : T.danger}66`,
                  borderRadius: 3, overflow: "hidden", cursor: "pointer", opacity: r.excluded ? 0.4 : 1 }}>
                <img src={api.overlayUrl(r.filename)} alt={r.filename}
                  style={{ width: "100%", aspectRatio: "16/9", objectFit: "cover", display: "block" }} />
                <div style={{ padding: 4, fontSize: 9 }}>
                  <div style={{ color: T.textSec, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{r.filename}</div>
                  <div style={{ color: r.success ? T.live : T.danger }}>{r.num_corners} corners{r.excluded ? ' · excluded' : ''}</div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </Accordion>
  );
}

// ─── Run Calibration ────────────────────────────────────────────────────────
function CalibrationPanel({ onCalibrated }) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const run = async () => {
    setRunning(true); setError(null);
    const res = await api.runCalibration();
    setRunning(false);
    if (res.error) return setError(res.error);
    setResult(res);
    onCalibrated?.(res);
  };

  const maxError = result ? Math.max(...result.per_image_errors.map(e => e.error), 0.0001) : 1;

  return (
    <Accordion title="Run Calibration" defaultOpen={false}>
      <div style={{ fontSize: 11, color: T.textSec, marginBottom: 8, lineHeight: 1.5 }}>
        Fits fisheye intrinsics (fx,fy,cx,cy) and distortion (k1-k4). Target overall RMS well under 1px.
      </div>
      <Btn onClick={run} disabled={running}>{running ? 'Calibrating…' : 'Run cv2.fisheye.calibrate()'}</Btn>
      <ErrorText>{error}</ErrorText>

      {result && (
        <div style={{ marginTop: 12 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8, marginBottom: 10 }}>
            <Stat label="RMS error" value={result.overall_rms_error.toFixed(4)} unit="px" good={result.overall_rms_error < 0.5} />
            <Stat label="Images used" value={result.num_images_used} />
            <Stat label="fx / fy" value={`${result.fx.toFixed(1)} / ${result.fy.toFixed(1)}`} />
            <Stat label="cx / cy" value={`${result.cx.toFixed(1)} / ${result.cy.toFixed(1)}`} />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8, marginBottom: 12, fontSize: 11 }}>
            {['k1', 'k2', 'k3', 'k4'].map(k => (
              <div key={k} style={{ borderBottom: `1px solid ${T.border}`, paddingBottom: 4 }}>
                <span style={{ color: T.textDim, textTransform: "uppercase" }}>{k}</span>
                <div style={{ color: T.textPri }}>{result[k].toFixed(6)}</div>
              </div>
            ))}
          </div>
          <Label>Per-image reprojection error — worst first</Label>
          <div style={{ marginTop: 6 }}>
            {result.per_image_errors.map(e => (
              <div key={e.filename} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
                <span style={{ fontSize: 10, color: T.textDim, width: 130, flexShrink: 0,
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.filename}</span>
                <div style={{ flex: 1, height: 6, background: T.surfaceEl, borderRadius: 2, overflow: "hidden" }}>
                  <div style={{ height: "100%", width: `${Math.min(100, (e.error / maxError) * 100)}%`,
                    background: e.error > 0.5 ? T.danger : T.live, borderRadius: 2 }} />
                </div>
                <span style={{ fontSize: 10, color: T.textSec, width: 50, textAlign: "right" }}>{e.error.toFixed(4)}px</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </Accordion>
  );
}

function Stat({ label, value, unit, good }) {
  return (
    <div>
      <Label>{label}</Label>
      <div style={{ fontSize: 15, fontWeight: 700, color: good === undefined ? T.textPri : good ? T.live : T.amber }}>
        {value}{unit ? <span style={{ fontSize: 10, color: T.textDim, marginLeft: 3 }}>{unit}</span> : null}
      </div>
    </div>
  );
}

// ─── Undistort Preview ──────────────────────────────────────────────────────
function UndistortPanel({ images }) {
  const [selected, setSelected] = useState('');
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const run = async () => {
    if (!selected) return;
    setLoading(true); setError(null);
    const res = await api.undistortPreview(selected);
    setLoading(false);
    if (res.error) return setError(res.error);
    setPreview(res);
  };

  return (
    <Accordion title="Undistort Preview" defaultOpen={false}>
      <div style={{ fontSize: 11, color: T.textSec, marginBottom: 8 }}>
        Sanity-check on a held-out image — bowed lines should look straight after correction.
      </div>
      <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
        <Select value={selected} onChange={setSelected}
          options={[{ value: '', label: 'Select an image…' }, ...images.map(i => ({ value: i.filename, label: i.filename }))]} />
        <Btn small onClick={run} disabled={loading || !selected}>{loading ? '…' : 'Undistort'}</Btn>
      </div>
      <ErrorText>{error}</ErrorText>
      {preview && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <div>
            <Label>Before</Label>
            <img src={preview.before_url} alt="before" style={{ width: "100%", borderRadius: 3, border: `1px solid ${T.border}` }} />
          </div>
          <div>
            <Label color={T.live}>After</Label>
            <img src={preview.after_url} alt="after" style={{ width: "100%", borderRadius: 3, border: `1px solid ${T.live}66` }} />
          </div>
        </div>
      )}
    </Accordion>
  );
}

// ─── Export / Save Profile ──────────────────────────────────────────────────
function ExportPanel({ profiles, onProfilesChanged }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [profileName, setProfileName] = useState('');
  const [saveMsg, setSaveMsg] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);

  const load = async () => {
    const res = await api.exportCalibration();
    if (res.error) return setError(res.error);
    setData(res); setError(null);
  };

  const save = async () => {
    if (!profileName.trim()) return;
    const res = await api.saveProfile(profileName.trim());
    if (res.error) return setError(res.error);
    setSaveMsg(`Saved "${profileName.trim()}"`);
    setProfileName('');
    onProfilesChanged?.();
    setTimeout(() => setSaveMsg(''), 2000);
  };

  return (
    <Accordion title="Export" defaultOpen={false}>
      <Btn small onClick={load} style={{ marginBottom: 10 }}>Load Latest Calibration</Btn>
      <ErrorText>{error}</ErrorText>

      {data && (
        <>
          <div style={{ fontSize: 11, color: T.textSec, marginBottom: 10 }}>
            {data.camera_model} — {data.colmap_params_string}
          </div>
          <FieldRow label="Profile name" hint='e.g. "x4_front", "x4_back" — used by the COLMAP Fisheye pipeline mode.'>
            <div style={{ display: "flex", gap: 6 }}>
              <Input value={profileName} onChange={setProfileName} placeholder="x4_front" />
              <Btn small onClick={save} disabled={!profileName.trim()}>Save as Profile</Btn>
            </div>
          </FieldRow>
          {saveMsg && <div style={{ fontSize: 11, color: T.live }}>{saveMsg}</div>}

          <div style={{ marginTop: 14 }}>
            <div onClick={() => setShowAdvanced(v => !v)}
              style={{ cursor: "pointer", fontSize: 10, color: T.textDim, textTransform: "uppercase", letterSpacing: ".5px" }}>
              {showAdvanced ? '▾' : '▸'} Advanced / manual COLMAP CLI
            </div>
            {showAdvanced && (
              <div style={{ marginTop: 8 }}>
                <CodeBlock label="feature_extractor command" value={data.colmap_feature_extractor_snippet} />
                <CodeBlock label="mapper — lock intrinsics during BA" value={data.colmap_mapper_lock_snippet} />
              </div>
            )}
          </div>
        </>
      )}

      <div style={{ marginTop: 16, borderTop: `1px solid ${T.border}`, paddingTop: 10 }}>
        <Label>Saved Profiles ({profiles.length})</Label>
        {profiles.length === 0 && <div style={{ fontSize: 11, color: T.textDim, marginTop: 6 }}>None yet.</div>}
        {profiles.map(p => (
          <div key={p.name} style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
            padding: "5px 0", borderBottom: `1px solid ${T.border}`, fontSize: 11 }}>
            <span style={{ color: T.textPri }}>{p.name}</span>
            <span style={{ color: T.textDim }}>RMS {p.overall_rms_error?.toFixed(3)}px · {p.num_images_used} imgs</span>
            <Btn small variant="danger" onClick={async () => { await api.deleteProfile(p.name); onProfilesChanged?.(); }}>Delete</Btn>
          </div>
        ))}
      </div>
    </Accordion>
  );
}

function CodeBlock({ label, value }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <Label>{label}</Label>
      <pre style={{ background: T.void, border: `1px solid ${T.border}`, borderRadius: 3,
        padding: 8, fontSize: 10, color: T.live, overflowX: "auto", marginTop: 3 }}>{value}</pre>
    </div>
  );
}

// ─── Top-level tab ──────────────────────────────────────────────────────────
export default function LensCalibrationTab() {
  const [board, setBoard] = useState(null);
  const [detectionResults, setDetectionResults] = useState([]);
  const [profiles, setProfiles] = useState([]);

  const passedImages = detectionResults.filter(r => r.success && !r.excluded);

  const refreshProfiles = async () => {
    const res = await api.listProfiles();
    setProfiles(res.profiles || []);
  };

  useEffect(() => { refreshProfiles(); }, []);

  return (
    <div style={{ padding: 14, maxWidth: 760, overflowY: "auto" }}>
      <div style={{ fontSize: 11, color: T.textSec, marginBottom: 12, lineHeight: 1.5 }}>
        ChArUco fisheye calibration for the X4's front/back lenses — feeds real distortion
        coefficients into the "COLMAP Fisheye" pipeline mode instead of the default zero-distortion
        pinhole approximation. Run this once per lens.
      </div>
      <BoardSetupPanel board={board} onSaved={setBoard} />
      <GenerateBoardPanel board={board} />
      <DetectionPanel boardReady={!!board} onDetected={setDetectionResults} />
      <CalibrationPanel onCalibrated={() => {}} />
      <UndistortPanel images={passedImages} />
      <ExportPanel profiles={profiles} onProfilesChanged={refreshProfiles} />
    </div>
  );
}
