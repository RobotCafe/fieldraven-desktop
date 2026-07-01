import { useState, useRef, useEffect, useCallback, useMemo } from "react";

// ─── Design Tokens ────────────────────────────────────────────────────────────
const T = {
  void:      "#090c12",
  base:      "#0e1220",
  surface:   "#141826",
  surfaceHi: "#1a2030",
  surfaceEl: "#1f263a",
  border:    "#252d42",
  borderHi:  "#303a54",
  amber:     "#e8a442",
  amberDim:  "#a06c22",
  amberGlow: "#f0b85a",
  live:      "#39e07a",
  liveDim:   "#1a6638",
  danger:    "#e05555",
  dangerDim: "#6b2020",
  info:      "#5599ff",
  textPri:   "#dde4f0",
  textSec:   "#7a8aaa",
  textDim:   "#3d4860",
  textAmber: "#e8a442",
  frColor:   "#39e07a",
  vidColor:  "#5599ff",
  imgColor:  "#cc77ff",
};

// ─── Micro primitives ─────────────────────────────────────────────────────────
function Btn({ children, onClick, disabled, variant="primary", small, full, style={} }) {
  const base = {
    display:"inline-flex", alignItems:"center", justifyContent:"center", gap:6,
    padding: small ? "4px 10px" : "7px 16px",
    fontSize: small ? "11px" : "12px", fontWeight:600,
    border:"1px solid", borderRadius:4, cursor: disabled ? "not-allowed" : "pointer",
    transition:"all .15s", letterSpacing:".3px", whiteSpace:"nowrap",
    opacity: disabled ? .4 : 1, width: full ? "100%" : undefined,
    fontFamily:"inherit",
  };
  const V = {
    primary: { background:T.amber,    borderColor:T.amber,    color:"#000" },
    ghost:   { background:"transparent", borderColor:T.border, color:T.textSec },
    live:    { background:T.live,     borderColor:T.live,     color:"#000" },
    danger:  { background:"transparent", borderColor:T.danger, color:T.danger },
    info:    { background:"transparent", borderColor:T.info,   color:T.info },
    subtle:  { background:T.surfaceEl, borderColor:T.border,  color:T.textSec },
  };
  return (
    <button onClick={disabled ? undefined : onClick}
      style={{...base, ...V[variant], ...style}}>
      {children}
    </button>
  );
}

function Label({ children, color, style={} }) {
  return <span style={{ fontSize:10, fontWeight:700, letterSpacing:".6px",
    textTransform:"uppercase", color: color||T.textDim, ...style }}>{children}</span>;
}

function FieldRow({ label, children, hint }) {
  return (
    <div style={{ display:"grid", gridTemplateColumns:"160px 1fr", alignItems:"start",
      gap:"6px 10px", marginBottom:8 }}>
      <Label style={{ paddingTop:7 }}>{label}</Label>
      <div>
        {children}
        {hint && <div style={{ fontSize:10, color:T.textDim, marginTop:2 }}>{hint}</div>}
      </div>
    </div>
  );
}

function Input({ value, onChange, onBlur, type="text", placeholder, disabled, style={} }) {
  return (
    <input type={type} value={value} onChange={e=>onChange(e.target.value)}
      onBlur={onBlur ? e=>onBlur(e.target.value) : undefined}
      placeholder={placeholder} disabled={disabled}
      style={{ width:"100%", background:T.void, border:`1px solid ${T.border}`,
        borderRadius:3, padding:"5px 8px", color:T.textPri, fontSize:12,
        outline:"none", fontFamily:"inherit", ...style }} />
  );
}

function Select({ value, onChange, options, disabled }) {
  return (
    <select value={value} onChange={e=>onChange(e.target.value)} disabled={disabled}
      style={{ width:"100%", background:T.void, border:`1px solid ${T.border}`,
        borderRadius:3, padding:"5px 8px", color:T.textPri, fontSize:12,
        outline:"none", fontFamily:"inherit" }}>
      {options.map(o=><option key={o.value||o} value={o.value||o}>{o.label||o}</option>)}
    </select>
  );
}

function Toggle({ checked, onChange, label, disabled }) {
  return (
    <label style={{ display:"flex", alignItems:"center", gap:8,
      cursor: disabled?"not-allowed":"pointer", opacity:disabled?.4:1 }}>
      <div onClick={()=>!disabled&&onChange(!checked)}
        style={{ width:30, height:17, borderRadius:9, position:"relative",
          background: checked ? T.amber : T.border, transition:"background .2s", flexShrink:0 }}>
        <div style={{ position:"absolute", top:2, left:checked?13:2,
          width:13, height:13, borderRadius:"50%", background:"#fff", transition:"left .2s" }} />
      </div>
      <span style={{ fontSize:12, color: disabled?T.textDim:T.textSec }}>{label}</span>
    </label>
  );
}

function Radio({ value, checked, onChange, label }) {
  return (
    <label style={{ display:"flex", alignItems:"center", gap:6,
      fontSize:12, color:T.textSec, cursor:"pointer" }}>
      <input type="radio" checked={checked} onChange={()=>onChange(value)}
        style={{ accentColor:T.amber }} />
      {label}
    </label>
  );
}

function Accordion({ title, defaultOpen=true, children, accent }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div style={{ border:`1px solid ${T.border}`, borderRadius:4, marginBottom:6, overflow:"hidden" }}>
      <div onClick={()=>setOpen(v=>!v)}
        style={{ display:"flex", alignItems:"center", justifyContent:"space-between",
          padding:"7px 10px", background:T.surfaceEl, cursor:"pointer" }}>
        <Label color={accent||T.textDim}>{title}</Label>
        <span style={{ color:T.textDim, fontSize:12 }}>{open?"▾":"▸"}</span>
      </div>
      {open && <div style={{ padding:10, background:T.surface }}>{children}</div>}
    </div>
  );
}

function ProgressBar({ value, color, label, style={} }) {
  return (
    <div style={{ display:"flex", alignItems:"center", gap:8, ...style }}>
      {label && <span style={{ fontSize:10, color:T.textDim, width:50, flexShrink:0 }}>{label}</span>}
      <div style={{ flex:1, height:4, background:T.surfaceEl, borderRadius:2, overflow:"hidden" }}>
        <div style={{ height:"100%", width:`${Math.max(0,Math.min(100,value))}%`,
          background: color||T.amber, borderRadius:2, transition:"width .4s" }} />
      </div>
      <span style={{ fontSize:10, color:T.textDim, width:28, textAlign:"right" }}>{value}%</span>
    </div>
  );
}

function SectionHead({ children, color }) {
  return (
    <div style={{ fontSize:10, fontWeight:700, letterSpacing:".7px", textTransform:"uppercase",
      color: color||T.amber, marginBottom:8, paddingBottom:5,
      borderBottom:`1px solid ${T.border}` }}>
      {children}
    </div>
  );
}

function Badge({ children, color }) {
  return (
    <span style={{ fontSize:9, fontWeight:700, letterSpacing:".5px", textTransform:"uppercase",
      padding:"2px 6px", borderRadius:3, background:`${color||T.amber}22`,
      color: color||T.amber, border:`1px solid ${color||T.amber}44` }}>
      {children}
    </span>
  );
}

function Pill({ color }) {
  return <div style={{ width:8, height:8, borderRadius:"50%", background:color, flexShrink:0,
    boxShadow:`0 0 6px ${color}88` }} />;
}

function StatCard({ value, label, sub, color }) {
  return (
    <div style={{ background:T.surface, border:`1px solid ${T.border}`, borderRadius:6, padding:"14px 12px" }}>
      <div style={{ fontSize:10, color:T.textDim, letterSpacing:".5px", textTransform:"uppercase", marginBottom:4 }}>{label}</div>
      <div style={{ fontSize:22, fontWeight:800, color: color||T.amber, lineHeight:1 }}>{value}</div>
      {sub && <div style={{ fontSize:10, color:T.textDim, marginTop:3, fontFamily:"monospace" }}>{sub}</div>}
    </div>
  );
}

// ─── Source type config ───────────────────────────────────────────────────────
const SOURCE_TYPES = {
  fieldraven: { color:T.frColor,  icon:"🦅", label:"FieldRaven Job" },
  video:      { color:T.vidColor, icon:"🎬", label:"Video" },
  folder:     { color:T.imgColor, icon:"📁", label:"Image Folder" },
};

// ─── API & config helpers ─────────────────────────────────────────────────────
async function apiFetch(user, path, method = 'GET', body = null) {
  const token = await user.getIdToken();
  const res = await fetch(path, {
    method,
    headers: {
      'Authorization': `Bearer ${token}`,
      ...(body != null ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body != null ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}`);
  return res.json();
}

const API_TO_UI = {
  extraction_method:'extractionMethod', interval_value:'intervalValue',
  interval_unit:'intervalUnit', frame_count:'frameCount', frame_format:'frameFormat',
  pitch_angles_str:'pitchAngles', yaw_steps:'yawSteps', fov:'fov',
  skip_realityscan:'skipRS', run_vggt:'runVggt', run_postshot:'runPostshot',
  run_brush:'runBrush', vggt_conf_threshold:'vggtConf',
  sky_sensitivity_threshold:'vggtSky', vggt_mask_sky:'vggtMaskSky',
  vggt_show_camera:'vggtShowCam', vggt_temporal_sequencing:'vggtTemporal',
  vggt_prediction_mode:'vggtMode', vggt_use_anchor_rig:'vggtAnchorRig',
  export_xmp:'exportXmp', gps_priors_rs:'gpsTriggersRS', gps_priors_colmap:'gpsPriorsColmap',
  run_colmap:'runColmap', colmap_mode:'colmapMode', colmap_matcher:'colmapMatcher', horizon_ref:'horizonRef', colmap_visualize:'colmapVisualize', colmap_correct_pitch:'colmapCorrectPitch', colmap_orientation_align:'colmapOrientationAlign',
  postshot_profile:'postshotProfile', postshot_max_image_size:'postshotMaxSize',
  postshot_train_steps:'postshotSteps', postshot_max_splats:'postshotMaxSplats',
  postshot_anti_aliasing:'postshotAA', postshot_show_train_error:'postshotError',
  postshot_store_context:'postshotContext', postshot_export_ply:'postshotPly',
  postshot_alpha_mask:'postshotAlpha', postshot_sky_model:'postshotSky',
  brush_total_steps:'brushSteps', brush_max_splats:'brushSplats',
  brush_max_resolution:'brushRes', brush_seed:'brushSeed',
  brush_rerun_logging:'brushRerun', brush_spawn_viewer:'brushViewer',
  ffmpeg_path:'ffmpeg', rs_path:'rs', postshot_path:'postshot',
  brush_path:'brush', rs_settings_path:'rsSettings', vggt_path:'vggt', colmap_bin:'colmapBin',
  insp_stitch_type:'inspStitchType', insp_lens_guard:'inspLensGuard',
  insp_flowstate:'inspFlowState', insp_cuda:'inspCuda',
  insp_workers:'inspWorkers',
};

function parseApiVal(v) {
  if (v === 'True')  return true;
  if (v === 'False') return false;
  const n = Number(v);
  return (!isNaN(n) && v !== '') ? n : v;
}

// Keys whose values must always remain strings (never coerced to number)
const STRING_SETTINGS = new Set(['pitchAngles', 'vggtMode', 'postshotProfile',
  'extractionMethod', 'intervalUnit', 'frameFormat', 'ffmpeg', 'rs', 'postshot',
  'brush', 'rsSettings', 'vggt', 'colmapBin', 'inspStitchType', 'inspLensGuard',
  'inspOutputWidth', 'inspWorkers', 'colmapMode', 'colmapMatcher']);

function apiConfigToSettings(cfg) {
  const out = {};
  for (const [ak, uk] of Object.entries(API_TO_UI)) {
    if (ak in cfg) out[uk] = STRING_SETTINGS.has(uk) ? String(cfg[ak]) : parseApiVal(cfg[ak]);
  }
  return out;
}

function settingsToApiConfig(settings) {
  const UI_TO_API = Object.fromEntries(Object.entries(API_TO_UI).map(([a,b])=>[b,a]));
  const out = {};
  for (const [uk, ak] of Object.entries(UI_TO_API)) {
    if (uk in settings) out[ak] = String(settings[uk]);
  }
  return out;
}

function fmtDate(ts) {
  if (!ts) return '—';
  try {
    return new Date(typeof ts === 'number' ? ts : ts).toLocaleDateString('en-CA',
      { year:'numeric', month:'short', day:'numeric' });
  } catch { return '—'; }
}

function fmtGps(gps) {
  if (!gps) return null;
  if (typeof gps === 'object' && gps.lat != null)
    return `${Number(gps.lat).toFixed(4)}, ${Number(gps.lon).toFixed(4)}`;
  return null;
}

function statusColor(s) {
  return s==='complete'?T.live : s==='processing'?T.amber : s==='error'?T.danger
    : s==='queued'?T.info : T.textSec;
}

// ─── Queue Panel ──────────────────────────────────────────────────────────────
function QueuePanel({ pqItems, localQueue, setLocalQueue, selected, setSelected, onCancelPq, onDeletePq, onAddImageFolder, onAddVideoFile }) {
  const queuedOrProcessing = j => j.status === 'queued' || j.status === 'processing';
  const frItems = pqItems
    .filter(j => queuedOrProcessing(j) && j.jobType !== 'local_folder' && j.jobType !== 'local_video')
    .map(j => ({ id: j.docId||j.id, type:'fieldraven', name: j.name||j.clientName||'Field Job', status: j.status }));
  const vidItems = pqItems
    .filter(j => j.jobType === 'local_video')
    .map(j => ({ id: j.docId||j.id, type:'video', name: j.name||'Video', status: j.status }));
  const imgItems = pqItems
    .filter(j => j.jobType === 'local_folder')
    .map(j => ({ id: j.docId||j.id, type:'folder', name: j.name||'Image Folder', status: j.status }));

  const groups = [
    { type:"fieldraven", label:"FieldRaven Jobs", items: frItems },
    { type:"video",      label:"Video Queue",     items: vidItems },
    { type:"folder",     label:"Image Folders",   items: imgItems },
  ];

  return (
    <div style={{ width:196, flexShrink:0, background:T.surface, border:`1px solid ${T.border}`,
      borderRadius:6, padding:10, display:"flex", flexDirection:"column", gap:10, overflowY:"auto" }}>

      {groups.map(({ type, label, items })=>{
        const cfg = SOURCE_TYPES[type];
        return (
          <div key={type}>
            <div style={{ display:"flex", alignItems:"center", gap:5, marginBottom:5 }}>
              <Pill color={cfg.color} />
              <Label color={cfg.color}>{label}</Label>
            </div>

            <div style={{ minHeight:40, background:T.void, border:`1px solid ${T.border}`,
              borderRadius:3, padding:3, marginBottom:4 }}>
              {items.length === 0
                ? <div style={{ color:T.textDim, fontSize:10, padding:"6px 4px", textAlign:"center" }}>
                    {type==="fieldraven" ? "Queue from FieldRaven tab" : "No items"}
                  </div>
                : items.map(it=>(
                  <div key={it.id} onClick={()=>setSelected(it)}
                    style={{ display:"flex", alignItems:"center", gap:5, padding:"4px 5px",
                      borderRadius:2, cursor:"pointer",
                      background: selected?.id===it.id ? `${cfg.color}22` : "transparent",
                      border: `1px solid ${selected?.id===it.id ? cfg.color+"55" : "transparent"}`,
                      marginBottom:1 }}>
                    <span style={{ fontSize:11 }}>{cfg.icon}</span>
                    <span style={{ fontSize:11, color: selected?.id===it.id ? cfg.color : T.textSec,
                      overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", flex:1 }}>
                      {it.name}
                    </span>
                    {it.status && it.status !== 'queued' && (
                      <span style={{ fontSize:9, color:statusColor(it.status), flexShrink:0 }}>
                        {it.status}
                      </span>
                    )}
                  </div>
                ))}
            </div>

            {type === "fieldraven" ? (
              selected && frItems.find(i=>i.id===selected.id) && (
                <Btn small variant="danger" full onClick={()=>{
                  onCancelPq(selected.id);
                  setSelected(null);
                }}>Cancel Job</Btn>
              )
            ) : (
              <div style={{ display:"flex", gap:3 }}>
                <Btn small variant="ghost"
                  style={{ flex:1, fontSize:10, borderColor:`${cfg.color}44`, color:cfg.color }}
                  onClick={()=> type==='folder' ? onAddImageFolder?.() : onAddVideoFile?.()}>
                  + Add
                </Btn>
                {items.length > 0 && (
                  <Btn small variant="ghost"
                    onClick={()=>{
                      if(selected && items.find(i=>i.id===selected.id)) setSelected(null);
                      // Local jobs (local_folder / local_video) are deleted from Firestore
                      // entirely when removed — not just failed/cancelled.
                      items.forEach(it => { if (it.status) onDeletePq?.(it.id); });
                      setLocalQueue(q=>q.filter(i=>i.type!==type));
                    }}>
                    ✕
                  </Btn>
                )}
              </div>
            )}
          </div>
        );
      })}

      {selected && (
        <div style={{ marginTop:"auto", padding:"8px", background:T.void, borderRadius:3,
          border:`1px solid ${T.border}` }}>
          <Label>Selected</Label>
          <div style={{ marginTop:4, fontSize:11, color:SOURCE_TYPES[selected.type]?.color||T.textSec,
            overflow:"hidden", textOverflow:"ellipsis" }}>
            {SOURCE_TYPES[selected.type]?.icon} {selected.name}
          </div>
          {selected.type !== 'fieldraven' && (
            <div style={{ marginTop:6 }}>
              <Btn small variant="danger" full onClick={()=>{
                // Firestore-backed items (e.g. image-folder jobs, jobType
                // local_folder / local_video jobs are deleted from Firestore entirely.
                if (selected.status) onDeletePq?.(selected.id);
                setLocalQueue(q=>q.filter(i=>i.id!==selected.id));
                setSelected(null);
              }}>Remove</Btn>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const JOB_TYPE_LABELS = {
  simple:    'Simple',
  '360survey': '360 Survey',
  video:     'Video',
  timelapse: 'Timelapse',
  other:     'Other',
};

// ─── FieldRaven Tab ───────────────────────────────────────────────────────────
function FieldRavenTab({ fieldJobs, loading, pqItems, machineInfo, cameraStatus, onQueueJob, setActiveMainTab, queuedIds }) {
  const [statusFilter, setStatusFilter] = useState("all");
  const [clientSearch, setClientSearch] = useState("");
  const [typeFilter, setTypeFilter]     = useState("all");
  const [expanded, setExpanded]         = useState(null);

  const getJobPqStatus = (job) => {
    const pq = pqItems.find(j => j.userJobId === job.id);
    return pq ? pq.status : null;
  };

  const displayStatus = (job) => {
    const pqStatus = getJobPqStatus(job);
    return pqStatus || 'ready';
  };

  // Derive unique clients and job types for filter dropdowns
  const uniqueClients = [...new Set(fieldJobs.map(j => j.clientName).filter(Boolean))].sort();
  const uniqueTypes   = [...new Set(fieldJobs.map(j => j.jobType).filter(Boolean))].sort();

  const filtered = fieldJobs.filter(j => {
    if (statusFilter !== 'all' && displayStatus(j) !== statusFilter) return false;
    if (typeFilter !== 'all' && (j.jobType || 'simple') !== typeFilter) return false;
    if (clientSearch.trim()) {
      const q = clientSearch.trim().toLowerCase();
      if (!(j.clientName || '').toLowerCase().includes(q)) return false;
    }
    return true;
  });

  const counts = {
    all:        fieldJobs.length,
    ready:      fieldJobs.filter(j=>!getJobPqStatus(j)).length,
    queued:     fieldJobs.filter(j=>getJobPqStatus(j)==='queued').length,
    processing: fieldJobs.filter(j=>getJobPqStatus(j)==='processing').length,
    complete:   fieldJobs.filter(j=>getJobPqStatus(j)==='complete').length,
  };

  const isAlreadyQueued = (job) => {
    const s = getJobPqStatus(job);
    return s === 'queued' || s === 'processing';
  };

  const selectStyle = {
    background: T.surfaceEl, border: `1px solid ${T.border}`, borderRadius: 3,
    color: T.textSec, fontSize: 11, padding: "5px 8px", fontFamily: "inherit",
    cursor: "pointer", outline: "none",
  };

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:10, height:"100%", overflowY:"auto" }}>
      {/* Machine + camera status */}
      <div style={{ display:"flex", alignItems:"center", gap:10, padding:"10px 12px",
        background:T.surface, borderRadius:5, border:`1px solid ${T.border}`, flexWrap:"wrap" }}>
        <div style={{ display:"flex", alignItems:"center", gap:6 }}>
          <Pill color={machineInfo ? T.live : T.textDim} />
          <span style={{ fontSize:12, color: machineInfo?T.live:T.textDim, fontWeight:600 }}>
            {machineInfo ? 'Desktop registered' : 'Connecting...'}
          </span>
        </div>
        {machineInfo && (
          <span style={{ fontSize:11, color:T.textDim, fontFamily:"monospace" }}>
            {machineInfo.machine_name}
          </span>
        )}
        <div style={{ marginLeft:"auto", display:"flex", alignItems:"center", gap:12 }}>
          {machineInfo && (
            <span style={{ fontSize:11, color:T.textDim }}>
              Firebase · <span style={{ color:T.live }}>connected</span>
            </span>
          )}
          {cameraStatus && (
            <span style={{ fontSize:11, color:T.textDim }}>
              Camera ·{' '}
              <span style={{ color: cameraStatus.camera_connected ? T.live : T.textDim }}>
                {cameraStatus.camera_connected
                  ? `${cameraStatus.camera_drive} · ${cameraStatus.file_count} files`
                  : 'not connected'}
              </span>
            </span>
          )}
        </div>
      </div>

      {/* Stats */}
      <div style={{ display:"grid", gridTemplateColumns:"repeat(4,1fr)", gap:8 }}>
        <StatCard value={counts.all} label="Field Jobs" />
        <StatCard value={counts.ready} label="Ready" color={T.live} />
        <StatCard value={counts.queued + counts.processing} label="In Pipeline" color={T.amber} />
        <StatCard value={counts.complete} label="Complete" color={T.textDim} />
      </div>

      {/* Filters */}
      <div style={{ display:"flex", gap:8, flexWrap:"wrap", alignItems:"center" }}>
        {/* Status pills */}
        <div style={{ display:"flex", gap:4, flexWrap:"wrap" }}>
          {[["all","All"],["ready","Ready"],["queued","Queued"],["processing","Processing"],["complete","Complete"]].map(([f,l])=>(
            <button key={f} onClick={()=>setStatusFilter(f)}
              style={{ padding:"5px 10px", borderRadius:3, fontSize:11, fontWeight:600,
                border:`1px solid ${statusFilter===f?T.amber:T.border}`,
                background: statusFilter===f ? `${T.amber}22` : "transparent",
                color: statusFilter===f ? T.amber : T.textSec, cursor:"pointer", fontFamily:"inherit" }}>
              {l}
            </button>
          ))}
        </div>

        <div style={{ width:1, height:20, background:T.border, flexShrink:0 }} />

        {/* Client search with autocomplete */}
        <input
          value={clientSearch}
          onChange={e => setClientSearch(e.target.value)}
          placeholder="Search client…"
          list="client-list"
          style={{ ...selectStyle, width:140, padding:"5px 8px" }}
        />
        <datalist id="client-list">
          {uniqueClients.map(c => <option key={c} value={c} />)}
        </datalist>

        {/* Job type dropdown */}
        <select value={typeFilter} onChange={e=>setTypeFilter(e.target.value)} style={selectStyle}>
          <option value="all">All types</option>
          {uniqueTypes.map(t => (
            <option key={t} value={t}>{JOB_TYPE_LABELS[t] || t}</option>
          ))}
        </select>

        {/* Clear all filters */}
        {(statusFilter !== 'all' || typeFilter !== 'all' || clientSearch) && (
          <button onClick={()=>{ setStatusFilter('all'); setTypeFilter('all'); setClientSearch(''); }}
            style={{ ...selectStyle, color:T.amber, border:`1px solid ${T.amber}44`, background:"transparent" }}>
            ✕ Clear
          </button>
        )}

        <span style={{ marginLeft:"auto", fontSize:11, color:T.textDim }}>
          {filtered.length} / {fieldJobs.length}
        </span>
      </div>

      {/* Job list */}
      {loading ? (
        <div style={{ color:T.textDim, fontSize:12, textAlign:"center", padding:20 }}>
          Loading jobs...
        </div>
      ) : filtered.length === 0 ? (
        <div style={{ color:T.textDim, fontSize:12, textAlign:"center", padding:20 }}>
          {statusFilter === 'all' && !clientSearch && typeFilter === 'all'
            ? 'No field jobs found. Complete a job in the mobile app first.'
            : 'No jobs match the current filters.'}
        </div>
      ) : (
        <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
          {filtered.map(job => {
            const jobStatus = displayStatus(job);
            const sc = statusColor(jobStatus);
            const gps = fmtGps(job.gpsStart);
            const date = fmtDate(job.startTime);
            const inQueue = isAlreadyQueued(job);

            return (
              <div key={job.id}
                style={{ background:T.surface, border:`1px solid ${expanded===job.id?T.amber+"44":T.border}`,
                  borderRadius:5, overflow:"hidden", transition:"border-color .2s" }}>
                <div style={{ display:"flex", alignItems:"center", padding:"10px 12px", gap:10 }}>
                  <span style={{ fontSize:18 }}>🦅</span>
                  <div style={{ flex:1 }}>
                    <div style={{ fontSize:13, fontWeight:600, color:T.textPri }}>{job.clientName || 'Unknown Client'}</div>
                    <div style={{ fontSize:11, color:T.textSec, marginTop:1 }}>
                      {date} · {job.photoCount ?? 0} photos{job.jobType ? ` · ${job.jobType}` : ''}
                    </div>
                  </div>
                  <Badge color={sc}>{jobStatus}</Badge>
                  <Btn small variant="ghost" onClick={()=>setExpanded(expanded===job.id?null:job.id)}>
                    {expanded===job.id?"▲":"▼"}
                  </Btn>
                </div>

                {expanded===job.id && (
                  <div style={{ borderTop:`1px solid ${T.border}`, padding:"10px 12px",
                    background:T.surfaceEl, display:"flex", flexDirection:"column", gap:8 }}>
                    <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:8 }}>
                      {gps && (
                        <div>
                          <Label>GPS</Label>
                          <div style={{ fontSize:11, color:T.textSec, fontFamily:"monospace", marginTop:2 }}>{gps}</div>
                        </div>
                      )}
                      {job.jobType && (
                        <div>
                          <Label>Job Type</Label>
                          <div style={{ fontSize:11, color:T.textSec, marginTop:2 }}>{job.jobType}</div>
                        </div>
                      )}
                      <div>
                        <Label>Photo Files</Label>
                        <div style={{ fontSize:11, color:T.textSec, fontFamily:"monospace", marginTop:2 }}>
                          {job.photoCount ?? 0}x .insp files
                        </div>
                      </div>
                      {job.notes && (
                        <div>
                          <Label>Notes</Label>
                          <div style={{ fontSize:11, color:T.textSec, marginTop:2 }}>{job.notes}</div>
                        </div>
                      )}
                    </div>

                    <div style={{ display:"flex", gap:8, justifyContent:"flex-end" }}>
                      <Btn small variant="live"
                        disabled={inQueue || jobStatus === 'complete'}
                        onClick={()=>onQueueJob(job)}>
                        {inQueue ? `✓ ${jobStatus}` : "Send to Pipeline →"}
                      </Btn>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Frame & View Extraction Tab ──────────────────────────────────────────────
function ExtractionTab({ selected, settings, setSettings, cameraStatus, importedFiles, projectDirs, onImport,
  importStep, importPct, stitching, stitchStep, stitchPct }) {
  const [frames, setFrames]           = useState([]);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [importing, setImporting]     = useState(false);
  const [importError, setImportError] = useState(null);
  const [selectedFile, setSelectedFile] = useState(null);
  const canvasRef  = useRef();
  const imgCacheRef = useRef(new Map());

  const isFR     = selected?.type === 'fieldraven';
  const isFolder = selected?.type === 'folder' || isFR;

  // Files already on disk for this job — FieldRaven (camera) jobs AND local
  // image-folder jobs both browse a raw file gallery; only 'video' falls
  // through to the extracted-frames preview below.
  const jobFiles  = isFolder ? (importedFiles[selected?.id] || null) : null;
  const hasFiles  = jobFiles && jobFiles.total > 0;
  const projectDir = isFolder ? (projectDirs?.[selected?.id] || null) : null;

  // Camera availability
  const camConnected = cameraStatus?.camera_connected;
  const camDrive     = cameraStatus?.camera_drive;
  const camCount     = cameraStatus?.file_count ?? 0;

  // Reset gallery when selection changes
  useEffect(() => { setFrames([]); setCurrentFrame(0); setImportError(null); setSelectedFile(null); }, [selected?.id]);

  // Inline preview URL: selected gallery file takes priority, else first JPEG
  const firstJpgName = hasFiles
    ? ((jobFiles?.files || []).find(f => f.ext === '.jpg' || f.ext === '.jpeg')?.name || null)
    : null;
  const currentFileName = selectedFile || firstJpgName;
  const currentPreviewUrl = (isFolder && currentFileName && selected?.id)
    ? `/api/jobs/${selected.id}/input/${encodeURIComponent(currentFileName)}?projectDir=${encodeURIComponent(projectDir || '')}`
    : null;

  // Pre-load gallery images into cache so canvas draws are instant on click
  useEffect(() => {
    if (!isFolder || !hasFiles || !selected?.id) return;
    const jpgs = (jobFiles?.files || []).filter(f => f.ext === '.jpg' || f.ext === '.jpeg');
    jpgs.slice(0, 30).forEach(f => {
      const url = `/api/jobs/${selected.id}/input/${encodeURIComponent(f.name)}?projectDir=${encodeURIComponent(projectDir || '')}`;
      if (!imgCacheRef.current.has(url)) {
        const img = new Image();
        img.onload = () => { imgCacheRef.current.set(url, img); };
        img.src = url;
      }
    });
  }, [isFolder, hasFiles, jobFiles, selected?.id, projectDir]);

  // Stable label used in canvas — prevents canvas redraw on every stitch poll when nothing visible changed
  const canvasFileLabel = useMemo(() => {
    if (!hasFiles) return '';
    const jpgC = (jobFiles?.files || []).filter(f => f.ext === '.jpg' || f.ext === '.jpeg').length;
    return jpgC > 0 ? `${jpgC} equirectangular` : `${jobFiles?.total ?? 0} files`;
  }, [hasFiles, jobFiles]);

  // Canvas
  useEffect(()=>{
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0,0,W,H);

    if (!selected) {
      ctx.fillStyle = T.surfaceEl; ctx.fillRect(0,0,W,H);
      return;
    }

    if (isFolder && !hasFiles) {
      ctx.fillStyle = T.void; ctx.fillRect(0,0,W,H);
      ctx.strokeStyle = `${T.amber}55`; ctx.lineWidth = 1; ctx.setLineDash([4,6]);
      ctx.strokeRect(2,2,W-4,H-4);
      ctx.setLineDash([]);
      return;
    }

    function drawOverlays() {
      ctx.strokeStyle=`${T.amber}33`; ctx.lineWidth=1; ctx.setLineDash([3,6]);
      ctx.beginPath(); ctx.moveTo(0,H/2); ctx.lineTo(W,H/2); ctx.stroke();
      ctx.setLineDash([]);
      const pitches = String(settings.pitchAngles).split(",").map(Number).filter(n=>!isNaN(n) && n !== 0);
      const yaw = Math.max(1,parseInt(settings.yawSteps)||6);
      const fov = parseFloat(settings.fov)||94.6;
      const colors = [T.frColor, T.vidColor, T.imgColor, T.amber, "#ff66aa", "#ffaa33","#66ddff","#aa66ff"];
      // Width based on actual FOV (not step spacing) — this shows true overlap between views
      // Matches panorama_processing.py: box_w = (fov / 360.0) * img_w
      const rW = (fov/360)*W;
      const rH = (fov/180)*H;
      const stepPx = W/yaw;

      const drawRect = (x, cy) => {
        ctx.fillRect(x, cy-rH/2, rW, rH);
        // Wrap right side back to left edge
        if (x+rW > W) ctx.fillRect(x-W, cy-rH/2, rW, rH);
        // Wrap left side to right edge
        if (x < 0) ctx.fillRect(x+W, cy-rH/2, rW, rH);
      };
      const strokeRect = (x, cy) => {
        ctx.strokeRect(x+.5, cy-rH/2+.5, rW-1, rH-1);
        if (x+rW > W) ctx.strokeRect(x-W+.5, cy-rH/2+.5, rW-1, rH-1);
        if (x < 0) ctx.strokeRect(x+W+.5, cy-rH/2+.5, rW-1, rH-1);
      };

      pitches.forEach((pitch,pi)=>{
        const cy = H/2 - (pitch/90)*(H/2);
        // Positive pitches get a half-step yaw offset — matches panorama_processing.py line 66
        const offset = pitch > 0 ? stepPx/2 : 0;
        for(let y=0;y<yaw;y++){
          // Center of this view on the canvas
          const cx = (y*stepPx + offset) % W;
          const x  = cx - rW/2;
          const c=colors[(pi*yaw+y)%colors.length];
          ctx.globalAlpha=settings.overlayOpacity*0.18;
          ctx.fillStyle=c; drawRect(x, cy);
          ctx.globalAlpha=settings.overlayOpacity;
          ctx.strokeStyle=c; ctx.lineWidth=1.5; strokeRect(x, cy);
          ctx.globalAlpha=1;
        }
      });
      const cfg = SOURCE_TYPES[selected.type];
      ctx.fillStyle=cfg.color; ctx.font="bold 10px monospace"; ctx.textAlign="left";
      ctx.fillText(`${cfg.icon} ${selected.name}`, 8, 14);
      if (hasFiles && canvasFileLabel) {
        ctx.fillStyle = T.live; ctx.textAlign = "right";
        ctx.fillText(canvasFileLabel, W-8, 14);
      }
    }

    const drawGradientBg = () => {
      const sky = ctx.createLinearGradient(0,0,0,H);
      sky.addColorStop(0,"#0a1428"); sky.addColorStop(.5,"#142040"); sky.addColorStop(1,"#0e1828");
      ctx.fillStyle = sky; ctx.fillRect(0,0,W,H);
    };

    if (currentPreviewUrl) {
      const cached = imgCacheRef.current.get(currentPreviewUrl);
      if (cached) {
        // Draw synchronously — zero flicker
        ctx.drawImage(cached, 0, 0, W, H);
        ctx.globalAlpha = 0.35; ctx.fillStyle = '#000'; ctx.fillRect(0,0,W,H); ctx.globalAlpha = 1;
        drawOverlays();
      } else {
        const img = new Image();
        img.onload = () => {
          imgCacheRef.current.set(currentPreviewUrl, img);
          ctx.clearRect(0,0,W,H);
          ctx.drawImage(img, 0, 0, W, H);
          ctx.globalAlpha = 0.35; ctx.fillStyle = '#000'; ctx.fillRect(0,0,W,H); ctx.globalAlpha = 1;
          drawOverlays();
        };
        img.onerror = () => { drawGradientBg(); drawOverlays(); };
        drawGradientBg(); drawOverlays(); // draw placeholder immediately while loading
        img.src = currentPreviewUrl;
      }
      return;
    }

    drawGradientBg();
    drawOverlays();
  }, [selected, settings, hasFiles, camConnected, camDrive, isFolder, currentPreviewUrl, canvasFileLabel]);

  const doExtract = () => {
    if (!selected) return;
    const count = parseInt(settings.frameCount)||30;
    setFrames(Array.from({length:count},(_,i)=>i));
    setCurrentFrame(0);
  };

  const handleImport = async () => {
    if (!camConnected) { setImportError("No camera found. Connect your Insta360 and try again."); return; }
    setImporting(true); setImportError(null);
    try {
      await onImport(selected.id, camDrive);
    } catch (e) {
      setImportError(e.message);
    } finally {
      setImporting(false);
    }
  };

  // Gallery content
  const galleryContent = () => {
    if (isFolder) {
      if (!hasFiles) return null;
      const jpgFiles = (jobFiles.files || []).filter(f => f.ext === '.jpg' || f.ext === '.jpeg');
      const inspCount = (jobFiles.files || []).filter(f => f.ext === '.insp').length;
      if (jpgFiles.length === 0) {
        return (
          <div style={{ color:T.textDim, fontSize:11, textAlign:"center", padding:20 }}>
            {inspCount > 0
              ? (stitching ? `Converting ${inspCount} .insp files to equirectangular…` : `${inspCount} .insp files — awaiting conversion`)
              : 'No equirectangular images found'}
          </div>
        );
      }
      const activeFile = selectedFile || firstJpgName;
      return (
        <div style={{ display:"flex", flexWrap:"wrap", gap:4 }}>
          {jpgFiles.slice(0, 200).map((f,i) => {
            const isActive = f.name === activeFile;
            return (
              <div key={i}
                onClick={() => setSelectedFile(f.name)}
                style={{ width:88, height:60, borderRadius:2, background:T.surfaceEl,
                  border:`2px solid ${isActive ? T.amber : T.border}`,
                  overflow:'hidden', position:'relative', flexShrink:0,
                  cursor:'pointer', boxSizing:'border-box',
                  transition:'border-color .1s' }}>
                <img
                  src={`/api/jobs/${selected.id}/input/${encodeURIComponent(f.name)}?projectDir=${encodeURIComponent(projectDir || '')}&thumb=true`}
                  alt={f.name}
                  style={{ width:'100%', height:'100%', objectFit:'cover' }}
                  loading="lazy"
                  onError={e => { e.currentTarget.style.opacity = '0.2'; }}
                />
              </div>
            );
          })}
        </div>
      );
    }
    if (frames.length === 0) return (
      <div style={{ color:T.textDim, fontSize:11, textAlign:"center", padding:20 }}>
        Extract frames to populate gallery
      </div>
    );
    return (
      <div style={{ display:"flex", flexWrap:"wrap", gap:3 }}>
        {frames.map(i=>(
          <div key={i} onClick={()=>setCurrentFrame(i)}
            style={{ width:52, height:36, borderRadius:2, background:T.surfaceEl,
              border:`1px solid ${i===currentFrame?T.amber:T.border}`,
              cursor:"pointer", display:"flex", alignItems:"center",
              justifyContent:"center", fontSize:9, color:i===currentFrame?T.amber:T.textDim,
              fontFamily:"monospace" }}>
            {String(i+1).padStart(3,"0")}
          </div>
        ))}
      </div>
    );
  };

  return (
    <div style={{ display:"grid", gridTemplateColumns:"1fr 252px", gap:10, height:"100%", overflow:"hidden" }}>
      <div style={{ display:"flex", flexDirection:"column", gap:8, overflow:"hidden" }}>

        {/* Canvas + text overlays */}
        <div style={{ position:"relative", flexShrink:0 }}>
          <canvas ref={canvasRef} width={680} height={340}
            style={{ width:"100%", height:"auto", borderRadius:4,
              border:`1px solid ${isFolder && !hasFiles ? T.amber+'44' : T.border}`,
              background:T.void, display:"block" }} />

          {/* "Select item" overlay */}
          {!selected && (
            <div style={{ position:"absolute", inset:0, display:"flex",
              alignItems:"center", justifyContent:"center", pointerEvents:"none" }}>
              <span style={{ fontSize:12, color:T.textDim }}>
                Select an item from the queue to preview
              </span>
            </div>
          )}

          {/* "No images imported" overlay */}
          {selected && isFolder && !hasFiles && (
            <div style={{ position:"absolute", inset:0, display:"flex",
              flexDirection:"column", alignItems:"center", justifyContent:"center",
              pointerEvents:"none", gap:6 }}>
              <span style={{ fontSize:12, fontWeight:600, color:T.amber }}>
                No images imported
              </span>
              <span style={{ fontSize:11, color:T.textDim, textAlign:"center",
                padding:"0 24px" }}>
                {isFR
                  ? (camConnected
                    ? `Camera connected (${camDrive}) — click Import from Camera`
                    : 'Connect your Insta360 camera and click Import from Camera')
                  : 'No images found in this folder'}
              </span>
            </div>
          )}
        </div>


        {/* Stitch progress strip */}
        {isFR && stitching && (
          <div style={{ background:`${T.info}0d`, border:`1px solid ${T.info}33`,
            borderRadius:4, padding:"10px 12px", flexShrink:0 }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"baseline", marginBottom:4 }}>
              <div style={{ fontSize:12, color:T.info, fontWeight:600 }}>
                Converting .insp → equirectangular
              </div>
              {stitchPct > 0 && (
                <div style={{ fontSize:10, color:T.info, fontFamily:'monospace' }}>{stitchPct}%</div>
              )}
            </div>
            <div style={{ fontSize:10, color:T.textDim, marginBottom:4, fontFamily:'monospace',
              overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
              {stitchStep || 'Initialising…'}
            </div>
            <div style={{ height:3, background:T.border, borderRadius:2 }}>
              <div style={{ height:'100%', background:T.info, borderRadius:2,
                width:`${Math.min(100, (stitchPct / 50) * 100)}%`, transition:'width .5s' }} />
            </div>
          </div>
        )}

        {/* Gallery */}
        <div style={{ flex:1, background:T.void, border:`1px solid ${T.border}`, borderRadius:4,
          padding:6, overflowY:"auto" }}>
          {isFolder && !hasFiles ? (
            <div style={{ color:T.textDim, fontSize:11, textAlign:"center", padding:20 }}>
              {isFR ? 'Import camera files to populate this area' : 'No images found in this folder'}
            </div>
          ) : (
            galleryContent()
          )}
        </div>

        {/* Footer label */}
        <div style={{ background:T.surface, border:`1px solid ${T.border}`, borderRadius:4,
          padding:"6px 10px", fontSize:11, color:T.textSec, flexShrink:0,
          display:"flex", alignItems:"center", gap:8 }}>
          <span style={{ color:T.amber }}>
            {isFolder ? '360 Source Files' : '360 Extracted View Preview'}
          </span>
          {hasFiles && (() => {
            const jpgCount = (jobFiles?.files||[]).filter(f=>f.ext==='.jpg'||f.ext==='.jpeg').length;
            const inspCount = (jobFiles?.files||[]).filter(f=>f.ext==='.insp').length;
            return <span style={{ color: jpgCount > 0 ? T.live : T.amber, fontFamily:"monospace" }}>
              {jpgCount > 0 ? `${jpgCount} files ready` : `${inspCount} .insp (not converted)`}
            </span>;
          })()}
          {!isFolder && frames.length > 0 && (
            <span style={{ color:T.textDim, fontFamily:"monospace" }}>
              frame {String(currentFrame+1).padStart(3,"0")}/{frames.length}
            </span>
          )}
          {isFR && hasFiles && (
            <Btn small variant="ghost" style={{ marginLeft:"auto" }}
              disabled={importing}
              onClick={handleImport}>
              {importing ? 'Importing...' : 'Re-import'}
            </Btn>
          )}
        </div>
      </div>

      {/* Right panel */}
      <div style={{ overflowY:"auto", display:"flex", flexDirection:"column", gap:0 }}>

        {/* Insta360 stitch settings — only for FieldRaven jobs */}
        {isFR && <Accordion title="Insta360 Stitch" accent={T.info} defaultOpen={true}>
          <FieldRow label="Stitch Type">
            <select value={settings.inspStitchType}
              onChange={e=>setSettings(s=>({...s,inspStitchType:e.target.value}))}
              style={{ width:"100%", background:T.void, border:`1px solid ${T.border}`,
                borderRadius:3, padding:"5px 8px", color:T.textPri, fontSize:12, fontFamily:"inherit" }}>
              <option value="template">Template (fastest)</option>
              <option value="optflow">Optical Flow</option>
              <option value="dynamic">Dynamic Stitch</option>
              <option value="ai">AI Stitch</option>
            </select>
          </FieldRow>
          <FieldRow label="Lens Guard">
            <select value={settings.inspLensGuard}
              onChange={e=>setSettings(s=>({...s,inspLensGuard:e.target.value}))}
              style={{ width:"100%", background:T.void, border:`1px solid ${T.border}`,
                borderRadius:3, padding:"5px 8px", color:T.textPri, fontSize:12, fontFamily:"inherit" }}>
              <option value="none">None</option>
              <option value="a">Lens Guard A (X3/X4/X5)</option>
              <option value="s">Lens Guard S (X3/X4/X5)</option>
              <option value="as">Lens Guard AS (X4)</option>
              <option value="waterproof">Dive Case</option>
            </select>
          </FieldRow>
          <FieldRow label="FlowState">
            <div style={{ display:"flex", alignItems:"center", gap:8, paddingTop:4 }}>
              <input type="checkbox" checked={!!settings.inspFlowState}
                onChange={e=>setSettings(s=>({...s,inspFlowState:e.target.checked}))}
                style={{ accentColor:T.info }} />
              <span style={{ fontSize:11, color: settings.inspFlowState ? T.info : T.textDim }}>
                {settings.inspFlowState ? 'Enabled' : 'Disabled'}
              </span>
            </div>
          </FieldRow>
          <FieldRow label="CUDA">
            <div style={{ display:"flex", alignItems:"center", gap:8, paddingTop:4 }}>
              <input type="checkbox" checked={!!settings.inspCuda}
                onChange={e=>setSettings(s=>({...s,inspCuda:e.target.checked}))}
                style={{ accentColor:T.info }} />
              <span style={{ fontSize:11, color: settings.inspCuda ? T.info : T.textDim }}>
                {settings.inspCuda ? 'Enabled' : 'Disabled'}
              </span>
            </div>
          </FieldRow>
          <FieldRow label="Output Width">
            <select value={settings.inspOutputWidth}
              onChange={e=>setSettings(s=>({...s,inspOutputWidth:e.target.value}))}
              style={{ width:"100%", background:T.void, border:`1px solid ${T.border}`,
                borderRadius:3, padding:"5px 8px", color:T.textPri, fontSize:12, fontFamily:"inherit" }}>
              <option value="11968">12K — native (slowest)</option>
              <option value="5984">6K — half res (fast)</option>
              <option value="3840">4K (faster)</option>
              <option value="2880">3K (fastest)</option>
            </select>
          </FieldRow>
          <FieldRow label="Workers">
            <select value={settings.inspWorkers}
              onChange={e=>setSettings(s=>({...s,inspWorkers:e.target.value}))}
              style={{ width:"100%", background:T.void, border:`1px solid ${T.border}`,
                borderRadius:3, padding:"5px 8px", color:T.textPri, fontSize:12, fontFamily:"inherit" }}>
              <option value="1">1 (sequential)</option>
              <option value="2">2 parallel</option>
              <option value="3">3 parallel</option>
              <option value="4">4 parallel</option>
            </select>
          </FieldRow>
        </Accordion>}

        <Accordion title="Frame Extraction"
          accent={isFolder ? T.textDim : T.amber}
          defaultOpen={!isFolder}>
          {isFolder ? (
            <div style={{ fontSize:11, color:T.textDim, fontStyle:"italic" }}>
              {isFR
                ? "FieldRaven .insp files are stitched to equirectangular by the pipeline — frame extraction is not needed here."
                : "Image folders are used directly — frame extraction not needed."}
            </div>
          ) : <>
            <FieldRow label="Method">
              <div style={{ display:"flex", gap:12 }}>
                <Radio value="interval" checked={settings.extractionMethod==="interval"}
                  onChange={v=>setSettings(s=>({...s,extractionMethod:v}))} label="Interval" />
                <Radio value="count" checked={settings.extractionMethod==="count"}
                  onChange={v=>setSettings(s=>({...s,extractionMethod:v}))} label="Count" />
              </div>
            </FieldRow>
            {settings.extractionMethod==="interval" ? <>
              <FieldRow label="Interval Value">
                <Input type="number" value={settings.intervalValue}
                  onChange={v=>setSettings(s=>({...s,intervalValue:v}))} />
              </FieldRow>
              <FieldRow label="Unit">
                <div style={{ display:"flex", gap:12 }}>
                  <Radio value="seconds" checked={settings.intervalUnit==="seconds"}
                    onChange={v=>setSettings(s=>({...s,intervalUnit:v}))} label="Seconds" />
                  <Radio value="frames" checked={settings.intervalUnit==="frames"}
                    onChange={v=>setSettings(s=>({...s,intervalUnit:v}))} label="Frames" />
                </div>
              </FieldRow>
            </> :
              <FieldRow label="Total Frames">
                <Input type="number" value={settings.frameCount}
                  onChange={v=>setSettings(s=>({...s,frameCount:v}))} />
              </FieldRow>
            }
            <FieldRow label="Format">
              <div style={{ display:"flex", gap:12 }}>
                <Radio value="jpg" checked={settings.frameFormat==="jpg"}
                  onChange={v=>setSettings(s=>({...s,frameFormat:v}))} label="JPEG" />
                <Radio value="png" checked={settings.frameFormat==="png"}
                  onChange={v=>setSettings(s=>({...s,frameFormat:v}))} label="PNG" />
              </div>
            </FieldRow>
          </>}
        </Accordion>

        <Accordion title="360 View Settings" accent={T.amber}>
          {(() => {
            // Parse pitchAngles string → 4 slots (0 = off)
            const raw = String(settings.pitchAngles).split(',')
              .map(v => parseInt(v.trim())).filter(v => !isNaN(v) && v !== 0);
            const slots = [raw[0]||0, raw[1]||0, raw[2]||0, raw[3]||0];
            const updateSlot = (i, raw) => {
              const v = parseInt(raw);
              const next = [...slots];
              next[i] = isNaN(v) ? 0 : Math.min(90, Math.max(-90, v));
              const str = next.filter(x => x !== 0).join(', ');
              setSettings(s => ({...s, pitchAngles: str || '0'}));
            };
            return (
              <div style={{ marginBottom:8 }}>
                <Label style={{ display:'block', marginBottom:5 }}>Pitch Angles</Label>
                <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:4 }}>
                  {slots.map((val, i) => (
                    <div key={i} style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:2 }}>
                      <input type="number" min={-90} max={90} step={1}
                        value={val !== 0 ? val : ''}
                        onChange={e => updateSlot(i, e.target.value)}
                        placeholder="off"
                        style={{ width:'100%', boxSizing:'border-box',
                          background: val !== 0 ? T.surfaceEl : T.void,
                          border:`1px solid ${val !== 0 ? T.amber+'88' : T.border}`,
                          borderRadius:3, padding:'5px 2px',
                          color: val !== 0 ? T.textPri : T.textDim,
                          fontSize:11, textAlign:'center', fontFamily:'monospace' }} />
                      <span style={{ fontSize:9, color: val !== 0 ? T.amber : T.textDim }}>
                        {val !== 0 ? `${val}°` : 'off'}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })()}
          <FieldRow label="Yaw Steps">
            <Input type="number" value={settings.yawSteps}
              onChange={v=>setSettings(s=>({...s,yawSteps:v}))} />
          </FieldRow>
          <FieldRow label="Field of View">
            <Input type="number" value={settings.fov}
              onChange={v=>setSettings(s=>({...s,fov:v}))} />
          </FieldRow>
          <Toggle checked={!!settings.horizonRef} label="Horizon Reference View (adds pitch=0° sensor for vertical anchoring)"
            onChange={v=>setSettings(s=>({...s,horizonRef:v}))} />
        </Accordion>

        <Accordion title="Overlay" defaultOpen={false}>
          <div style={{ display:"flex", alignItems:"center", gap:8 }}>
            <Label style={{ flexShrink:0 }}>Opacity</Label>
            <input type="range" min={0} max={1} step={.05} value={settings.overlayOpacity}
              onChange={e=>setSettings(s=>({...s,overlayOpacity:+e.target.value}))}
              style={{ flex:1, minWidth:0, accentColor:T.amber }} />
            <span style={{ fontSize:10, color:T.textDim, width:28, textAlign:"right", flexShrink:0 }}>
              {Math.round(settings.overlayOpacity*100)}%
            </span>
          </div>
        </Accordion>

        {!isFolder && (
          <Accordion title="Frame Navigator" defaultOpen={false}>
            {frames.length===0
              ? <div style={{ fontSize:11, color:T.textDim }}>Extract frames to enable</div>
              : <>
                <div style={{ display:"flex", gap:3, justifyContent:"center", marginBottom:8 }}>
                  {[["⏮",()=>setCurrentFrame(0)],["◀",()=>setCurrentFrame(f=>Math.max(0,f-1))],
                    ["▶",()=>setCurrentFrame(f=>Math.min(frames.length-1,f+1))],
                    ["⏭",()=>setCurrentFrame(frames.length-1)]].map(([icon,fn],i)=>(
                    <Btn key={i} small variant="ghost" onClick={fn}>{icon}</Btn>
                  ))}
                </div>
                <input type="range" min={0} max={frames.length-1} value={currentFrame}
                  onChange={e=>setCurrentFrame(+e.target.value)}
                  style={{ width:"100%", accentColor:T.amber }} />
                <div style={{ textAlign:"center", fontSize:10, color:T.textDim, marginTop:3,
                  fontFamily:"monospace" }}>
                  {currentFrame+1} / {frames.length}
                </div>
              </>}
          </Accordion>
        )}

        <div style={{ marginTop:6, display:"flex", flexDirection:"column", gap:6 }}>
          {isFR ? (
            <>
              <Btn
                onClick={handleImport}
                disabled={importing}
                full
                variant={
                  hasFiles      ? 'subtle'  :
                  !projectDir   ? 'primary' :
                  camConnected  ? 'primary' : 'ghost'
                }>
                {importing
                  ? (importPct > 0 ? `Copying… ${importPct}%` : 'Checking…')
                  : hasFiles
                    ? `✓ ${jobFiles.total} files — Ready`
                    : !projectDir
                      ? 'Create Project Directory'
                      : camConnected
                        ? `Import from Camera (${camCount} files)`
                        : 'Connect Camera to Import'}
              </Btn>
              {importing && importStep && (
                <div style={{ fontSize:10, color:T.textDim, fontFamily:'monospace',
                  overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
                  {importStep}
                </div>
              )}
              {importError && (
                <div style={{ fontSize:11, color:T.danger }}>{importError}</div>
              )}
              {/* Camera status indicator */}
              <div style={{ display:"flex", alignItems:"center", gap:6, padding:"5px 8px",
                background:T.void, border:`1px solid ${T.border}`, borderRadius:3 }}>
                <div style={{ width:6, height:6, borderRadius:"50%", flexShrink:0,
                  background: camConnected ? T.live : T.danger,
                  boxShadow: camConnected ? `0 0 4px ${T.live}88` : `0 0 4px ${T.danger}88` }} />
                <span style={{ fontSize:10, color: camConnected ? T.live : T.danger }}>
                  {camConnected ? `Camera connected · ${camDrive}` : 'No camera detected'}
                </span>
                {camConnected && (
                  <span style={{ fontSize:10, color:T.textDim, marginLeft:"auto" }}>
                    {camCount} files
                  </span>
                )}
              </div>
              {/* Output directory */}
              <div style={{ display:"flex", alignItems:"center", gap:6, padding:"5px 8px",
                background:T.void, border:`1px solid ${projectDir ? T.border : T.amber+'44'}`,
                borderRadius:3 }}>
                <span style={{ fontSize:9, color:T.textDim, flexShrink:0, textTransform:"uppercase", letterSpacing:1 }}>Dir</span>
                <span style={{ fontSize:10, color: projectDir ? T.textSec : T.amber,
                  whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis",
                  fontFamily:"monospace", flex:1, minWidth:0 }}>
                  {projectDir || 'No project folder — set on import'}
                </span>
              </div>
            </>
          ) : isFolder ? (
            <div style={{ display:"flex", alignItems:"center", gap:6, padding:"5px 8px",
              background:T.void, border:`1px solid ${T.border}`, borderRadius:3 }}>
              <span style={{ fontSize:9, color:T.textDim, flexShrink:0, textTransform:"uppercase", letterSpacing:1 }}>Dir</span>
              <span style={{ fontSize:10, color:T.textSec,
                whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis",
                fontFamily:"monospace", flex:1, minWidth:0 }}>
                {projectDir || 'Unknown'}
              </span>
            </div>
          ) : (
            <Btn onClick={doExtract} disabled={!selected} full variant="primary">
              Extract Frames
            </Btn>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Alignment Tab ────────────────────────────────────────────────────────────
function AlignmentTab({ settings, setSettings }) {
  const { runPostshot, runBrush } = settings;

  // Derive a single active mode from the underlying flags
  const mode = settings.runColmap ? 'colmap' : (settings.runVggt ? 'vggt' : 'rs');

  const setMode = (m) => setSettings(s => ({
    ...s,
    skipRS:    m !== 'rs',
    runVggt:   m === 'vggt',
    runColmap: m === 'colmap',
    // Brush only makes sense after COLMAP or RS; clear it for VGGT
    runBrush: m === 'vggt' ? false : s.runBrush,
  }));

  const MODES = [
    { id:'rs',     label:'RealityScan', desc:'Epic photogrammetry — high accuracy, requires RS licence' },
    { id:'colmap', label:'COLMAP',      desc:'Open-source SfM — no licence required, needs COLMAP binary' },
    { id:'vggt',   label:'VGGT',        desc:'AI pose estimation — GPU-based, fastest for small captures' },
  ];

  return (
    <div style={{ overflowY:"auto", height:"100%" }}>

      {/* ── Mode selector ───────────────────────────────────────── */}
      <Accordion title="Alignment Method" accent={T.amber}>
        <div style={{ display:'flex', gap:4, marginBottom:10 }}>
          {MODES.map(({ id, label }) => (
            <button key={id} onClick={() => setMode(id)}
              style={{
                flex:1, padding:"6px 4px", fontSize:11, fontWeight:600,
                borderRadius:4, cursor:"pointer", border:"none",
                background: mode === id ? T.amber : T.surfaceEl,
                color: mode === id ? T.void : T.textSec,
                transition:"background .15s",
              }}>
              {label}
            </button>
          ))}
        </div>
        <div style={{ fontSize:10, color:T.textDim, marginBottom:12 }}>
          {MODES.find(m => m.id === mode)?.desc}
        </div>

        {/* ── RealityScan options ───────────────────────────────── */}
        {mode === 'rs' && (
          <div style={{ display:'flex', flexDirection:'column', gap:6 }}>
            <Toggle checked={settings.exportXmp} label="Rig-Aware XMP (inject rig geometry into RS)"
              onChange={v=>setSettings(s=>({...s,exportXmp:v}))} />
            {settings.exportXmp && (
              <div style={{ paddingLeft:12, borderLeft:`2px solid ${T.border}` }}>
                <Toggle checked={!!settings.gpsTriggersRS} label="Include GPS position priors in XMP"
                  onChange={v=>setSettings(s=>({...s,gpsTriggersRS:v}))} />
                <div style={{ color:T.textDim, fontSize:10, marginTop:2 }}>
                  Requires GPS captured during survey. Uses ~3–10 m accuracy as a draft prior.
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── COLMAP options ────────────────────────────────────── */}
        {mode === 'colmap' && (
          <div style={{ display:"flex", flexDirection:"column", gap:10 }}>
            <FieldRow label="Mode">
              <div style={{ display:"flex", gap:12 }}>
                <Radio value="rig" checked={settings.colmapMode==="rig"}
                  onChange={v=>setSettings(s=>({...s,colmapMode:v}))} label="Perspective-Rig" />
                <Radio value="spherical" checked={settings.colmapMode==="spherical"}
                  onChange={v=>setSettings(s=>({...s,colmapMode:v}))} label="Spherical" />
              </div>
            </FieldRow>
            <FieldRow label="Matcher">
              <div style={{ display:"flex", gap:12, flexWrap:"wrap" }}>
                {["sequential","exhaustive","vocabtree"].map(m=>(
                  <Radio key={m} value={m} checked={settings.colmapMatcher===m}
                    onChange={v=>setSettings(s=>({...s,colmapMatcher:v}))} label={m} />
                ))}
              </div>
            </FieldRow>
            <Toggle checked={settings.colmapCorrectPitch !== false} label="Align reconstruction to rig 0° pitch reference"
              onChange={v=>setSettings(s=>({...s,colmapCorrectPitch:v}))} />
            <Toggle checked={!!settings.colmapOrientationAlign} label="Refine level using scene geometry (IMAGE_ORIENTATION)"
              disabled={!settings.colmapBin}
              onChange={v=>setSettings(s=>({...s,colmapOrientationAlign:v}))}
              title={!settings.colmapBin ? "Requires COLMAP binary path to be set" : undefined} />
            <Toggle checked={!!settings.gpsPriorsColmap} label="Geo-register reconstruction using GPS"
              onChange={v=>setSettings(s=>({...s,gpsPriorsColmap:v}))} />
            {settings.gpsPriorsColmap && (
              <div style={{ color:T.textDim, fontSize:10 }}>
                Requires GPS captured during survey + COLMAP binary path set.
                Aligns the reconstruction to real-world GPS coordinates (ECEF).
              </div>
            )}
            <Toggle checked={!!settings.colmapVisualize} label="Generate camera visualizer (cameras.html)"
              onChange={v=>setSettings(s=>({...s,colmapVisualize:v}))} />
          </div>
        )}

        {/* ── VGGT options ──────────────────────────────────────── */}
        {mode === 'vggt' && (
          <div>
            <Accordion title="VGGT Options" defaultOpen={false}>
              <FieldRow label="Confidence">
                <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                  <input type="range" min={0} max={100} value={settings.vggtConf}
                    onChange={e=>setSettings(s=>({...s,vggtConf:+e.target.value}))}
                    style={{ flex:1, accentColor:T.amber }} />
                  <span style={{ fontSize:10, color:T.textDim, width:28 }}>{settings.vggtConf}%</span>
                </div>
              </FieldRow>
              <FieldRow label="Sky Sensitivity">
                <div style={{ display:"flex", gap:8, alignItems:"center" }}>
                  <input type="range" min={8} max={128} value={settings.vggtSky}
                    onChange={e=>setSettings(s=>({...s,vggtSky:+e.target.value}))}
                    style={{ flex:1, accentColor:T.amber }} />
                  <span style={{ fontSize:10, color:T.textDim, width:28 }}>{settings.vggtSky}</span>
                </div>
              </FieldRow>
              <div style={{ display:"flex", flexWrap:"wrap", gap:10, marginTop:6 }}>
                {[["vggtMaskSky","Filter Sky"],["vggtShowCam","Show Camera Frustums"],
                  ["vggtTemporal","Temporal Sequencing"],["vggtAnchorRig","Anchor+Rig Mode"]].map(([k,l])=>(
                  <Toggle key={k} checked={settings[k]} label={l}
                    onChange={v=>setSettings(s=>({...s,[k]:v}))} />
                ))}
              </div>
              <FieldRow label="Prediction Mode">
                <div style={{ display:"flex", gap:12, flexWrap:"wrap" }}>
                  <Radio value="depthmap" checked={settings.vggtMode==="depthmap"}
                    onChange={v=>setSettings(s=>({...s,vggtMode:v}))} label="Depthmap" />
                  <Radio value="pointmap" checked={settings.vggtMode==="pointmap"}
                    onChange={v=>setSettings(s=>({...s,vggtMode:v}))} label="Pointmap" />
                </div>
              </FieldRow>
            </Accordion>
          </div>
        )}
      </Accordion>

      {/* ── Training ────────────────────────────────────────────── */}
      <Accordion title="Training" accent={T.amber}>
        <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
          <Toggle checked={runPostshot} label="Run Postshot Training"
            onChange={v=>setSettings(s=>({...s,runPostshot:v}))} />
          <Toggle checked={runBrush} label="Run Brush Training"
            disabled={mode === 'vggt'}
            onChange={v=>setSettings(s=>({...s,runBrush:v}))} />
          {mode === 'vggt' && (
            <div style={{ color:T.textDim, fontSize:10 }}>Brush training uses COLMAP/RS output — not available in VGGT mode.</div>
          )}
        </div>
      </Accordion>

      {/* ── Pipeline summary ─────────────────────────────────────── */}
      <Accordion title="Pipeline Summary" accent={T.live}>
        <div style={{ fontFamily:"monospace", fontSize:11, color:T.textSec, lineHeight:1.8 }}>
          {mode === 'rs'     && `RealityScan alignment${settings.exportXmp?' + XMP rig priors':''}`}
          {mode === 'colmap' && `COLMAP ${settings.colmapMode} (${settings.colmapMatcher} matcher)`}
          {mode === 'vggt'   && `VGGT ${settings.vggtMode} pose estimation`}
          {'\n'}
          {[runPostshot&&'→ Postshot training', runBrush&&'→ Brush training'].filter(Boolean).join('\n') || (mode==='vggt'?'':'→ Alignment only (no training selected)')}
        </div>
      </Accordion>
    </div>
  );
}

// ─── Postshot Tab ─────────────────────────────────────────────────────────────
function PostshotTab({ settings, setSettings }) {
  const S = k => ({ value:settings[k], onChange:v=>setSettings(s=>({...s,[k]:v})) });
  return (
    <div style={{ overflowY:"auto", height:"100%" }}>
      <FieldRow label="Profile">
        <Select {...S("postshotProfile")} options={["Splat MCMC","Splat3","Splat ADC"]} />
      </FieldRow>
      <FieldRow label="Max Image Size" hint="Default 3840">
        <Input type="number" {...S("postshotMaxSize")} />
      </FieldRow>
      <FieldRow label="Train Steps (k)" hint="Default 30">
        <Input type="number" {...S("postshotSteps")} />
      </FieldRow>
      <FieldRow label="Max Splats (k)" hint="MCMC only">
        <Input type="number" {...S("postshotMaxSplats")} />
      </FieldRow>
      <div style={{ marginTop:10 }}>
        <SectionHead>Options</SectionHead>
        <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:10 }}>
          {[["postshotAA","Anti-Aliasing"],["postshotError","Show Train Error"],
            ["postshotContext","Store Context"],["postshotPly","Export PLY"],
            ["postshotAlpha","Zero Alpha Mask"],["postshotSky","Sky Model"]]
            .map(([k,l])=>(
            <Toggle key={k} checked={settings[k]} label={l}
              onChange={v=>setSettings(s=>({...s,[k]:v}))} />
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Brush Tab ────────────────────────────────────────────────────────────────
function BrushTab({ settings, setSettings }) {
  const S = k => ({ value:settings[k], onChange:v=>setSettings(s=>({...s,[k]:v})) });
  return (
    <div style={{ overflowY:"auto", height:"100%" }}>
      <SectionHead>Training</SectionHead>
      <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:"0 16px" }}>
        <FieldRow label="Total Steps"><Input type="number" {...S("brushSteps")} /></FieldRow>
        <FieldRow label="Max Splats"><Input type="number" {...S("brushSplats")} /></FieldRow>
        <FieldRow label="Max Resolution"><Input type="number" {...S("brushRes")} /></FieldRow>
        <FieldRow label="Seed"><Input type="number" {...S("brushSeed")} /></FieldRow>
      </div>
      <div style={{ marginTop:10 }}>
        <SectionHead>Options</SectionHead>
        <div style={{ display:"flex", gap:20, flexWrap:"wrap" }}>
          <Toggle checked={settings.brushRerun} label="Rerun.io Logging"
            onChange={v=>setSettings(s=>({...s,brushRerun:v}))} />
          <Toggle checked={settings.brushViewer} label="Spawn Viewer"
            onChange={v=>setSettings(s=>({...s,brushViewer:v}))} />
        </div>
      </div>
    </div>
  );
}

// ─── Config Tab ───────────────────────────────────────────────────────────────
function ConfigTab({ settings, setSettings, machineInfo, onSaveConfig }) {
  const S = k => ({ value:settings[k]||'', onChange:v=>setSettings(s=>({...s,[k]:v})) });
  const paths = [
    ["ffmpeg","FFmpeg Executable"],["rs","RealityScan Executable"],
    ["postshot","Postshot CLI"],["brush","Brush CLI"],
    ["rsSettings","RS Settings Folder"],["vggt","VGGT Project"],
    ["colmapBin","COLMAP Binary (.exe)"],
  ];
  return (
    <div style={{ overflowY:"auto", height:"100%" }}>
      <SectionHead>Dependency Paths</SectionHead>
      {paths.map(([k,l])=>(
        <FieldRow key={k} label={l}>
          <div style={{ display:"flex", gap:4 }}>
            <Input {...S(k)} placeholder={`Path to ${l.toLowerCase()}`} />
          </div>
        </FieldRow>
      ))}
      <div style={{ marginTop:12 }}>
        <SectionHead>Machine</SectionHead>
        <FieldRow label="Machine ID">
          <Input value={machineInfo?.machine_id || '—'} onChange={()=>{}} disabled />
        </FieldRow>
        <FieldRow label="Machine Name">
          <Input value={machineInfo?.machine_name || '—'} onChange={()=>{}} disabled />
        </FieldRow>
      </div>
      <div style={{ marginTop:12 }}>
        <Btn onClick={onSaveConfig}>Save Configuration</Btn>
      </div>
    </div>
  );
}

// ─── Console ──────────────────────────────────────────────────────────────────
function Console({ logs, visible }) {
  const endRef = useRef();
  useEffect(()=>{ endRef.current?.scrollIntoView({behavior:"smooth"}); },[logs]);
  if (!visible) return null;
  const col = line => /error|failed/i.test(line)?T.danger
    :/warn/i.test(line)?T.amber:/success|complete/i.test(line)?T.live
    :/extracting|processing/i.test(line)?"#ffee55":T.textSec;
  return (
    <div style={{ height:130, background:T.void, borderTop:`1px solid ${T.border}`,
      padding:"8px 12px", overflowY:"auto", fontFamily:"monospace", fontSize:11 }}>
      <div style={{ color:T.amber, marginBottom:4, fontWeight:700, fontSize:10,
        letterSpacing:".5px", textTransform:"uppercase" }}>Console</div>
      {logs.map((line,i)=>(
        <div key={i} style={{ color:col(line), lineHeight:1.6 }}>
          <span style={{ color:T.textDim }}>› </span>{line}
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}

// ─── Pipeline Tab ─────────────────────────────────────────────────────────────
const PIPE_TABS = ["Frame & View Extraction","Alignment","Postshot","Brush","Configuration"];

function PipelineTab({ pqItems, localQueue, setLocalQueue, selected, setSelected,
    settings, setSettings, onSaveConfig, onCancelPq, onDeletePq, machineInfo,
    cameraStatus, importedFiles, projectDirs, onImport, onAddImageFolder, onAddVideoFile,
    importStep, importPct, stitching, stitchStep, stitchPct }) {
  const [pipeTab, setPipeTab] = useState(0);
  return (
    <div style={{ display:"flex", height:"100%", overflow:"hidden", gap:10 }}>
      <QueuePanel pqItems={pqItems} localQueue={localQueue} setLocalQueue={setLocalQueue}
        selected={selected} setSelected={setSelected} onCancelPq={onCancelPq}
        onDeletePq={onDeletePq} onAddImageFolder={onAddImageFolder} onAddVideoFile={onAddVideoFile} />

      <div style={{ flex:1, display:"flex", flexDirection:"column", overflow:"hidden" }}>
        <div style={{ display:"flex", gap:1, marginBottom:0, flexShrink:0, overflowX:"auto" }}>
          {PIPE_TABS.map((t,i)=>(
            <div key={i} onClick={()=>setPipeTab(i)}
              style={{ padding:"6px 12px", fontSize:11, fontWeight:600, cursor:"pointer",
                borderRadius:"4px 4px 0 0", whiteSpace:"nowrap",
                background: pipeTab===i ? T.surfaceHi : "transparent",
                color: pipeTab===i ? T.amber : T.textDim,
                borderBottom: `2px solid ${pipeTab===i?T.amber:"transparent"}`,
                transition:"all .15s" }}>
              {t}
            </div>
          ))}
        </div>

        <div style={{ flex:1, background:T.surfaceHi, border:`1px solid ${T.border}`,
          borderRadius:"0 4px 4px 4px", padding:10, overflow:"hidden",
          display:"flex", flexDirection:"column" }}>
          {pipeTab===0 && <ExtractionTab selected={selected} settings={settings} setSettings={setSettings}
            cameraStatus={cameraStatus} importedFiles={importedFiles} projectDirs={projectDirs} onImport={onImport}
            importStep={importStep} importPct={importPct}
            stitching={stitching} stitchStep={stitchStep} stitchPct={stitchPct} />}
          {pipeTab===1 && <AlignmentTab settings={settings} setSettings={setSettings} />}
          {pipeTab===2 && <PostshotTab settings={settings} setSettings={setSettings} />}
          {pipeTab===3 && <BrushTab settings={settings} setSettings={setSettings} />}
          {pipeTab===4 && <ConfigTab settings={settings} setSettings={setSettings}
            machineInfo={machineInfo} onSaveConfig={onSaveConfig} />}
        </div>
      </div>
    </div>
  );
}

// ─── Active Job Tab ───────────────────────────────────────────────────────────
const _STAGES_VGGT     = { labels:['Frames','Views','Align','COLMAP'],
  keys:['frame_extraction','view_extraction','vggt_alignment','colmap_export'] };
const _STAGES_RS_BRUSH = { labels:['Frames','Views','Align','Brush'],
  keys:['frame_extraction','view_extraction','realityscan','brush_training'] };

function ActiveJobTab({ currentJob, progress, statusMsg, logs, currentStage, pipelineMode }) {
  const stageDef = pipelineMode === 'rs_brush' ? _STAGES_RS_BRUSH : _STAGES_VGGT;
  const stageIdx = stageDef.keys.indexOf(currentStage);
  // Fall back to proportional mapping when stage name isn't known yet
  const activeIdx = stageIdx >= 0
    ? stageIdx
    : Math.min(stageDef.labels.length - 1, Math.floor((progress / 100) * stageDef.labels.length));
  const isRunning = currentJob && (currentJob.status === 'processing');
  const isComplete = currentJob?.status === 'complete' || progress === 100;

  if (!currentJob && progress === 0) return (
    <div style={{ display:"flex", alignItems:"center", justifyContent:"center",
      height:"100%", color:T.textDim, fontSize:13 }}>
      No active job — queue a field job and click Run Pipeline
    </div>
  );

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:12 }}>
      <div style={{ display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:8 }}>
        <StatCard value={`${progress}%`} label="Progress" color={T.amber} />
        <StatCard value={currentJob?.name || '—'} label="Job" sub={currentJob?.clientName} />
        <StatCard value={isRunning ? "Running" : currentJob?.status || "Idle"} label="Status"
          color={isRunning ? T.live : isComplete ? T.live : T.textDim} />
      </div>

      <div style={{ background:T.surface, border:`1px solid ${T.border}`, borderRadius:5, padding:12 }}>
        <Label style={{ marginBottom:10, display:"block" }}>Pipeline Stages</Label>
        <div style={{ display:"flex", alignItems:"center" }}>
          {stageDef.labels.map((s,i)=>(
            <div key={i} style={{ display:"flex", alignItems:"center", flex: i<stageDef.labels.length-1?1:"auto" }}>
              <div style={{ display:"flex", flexDirection:"column", alignItems:"center", gap:4 }}>
                <div style={{ width:10, height:10, borderRadius:"50%",
                  background: isComplete||i<activeIdx?T.live:i===activeIdx&&isRunning?T.amber:T.surfaceEl,
                  border:`2px solid ${isComplete||i<activeIdx?T.live:i===activeIdx&&isRunning?T.amber:T.border}`,
                  boxShadow: i===activeIdx&&isRunning?`0 0 8px ${T.amber}88`:"none",
                  transition:"all .3s" }} />
                <span style={{ fontSize:9, color: isComplete||i<activeIdx?T.live:i===activeIdx?T.amber:T.textDim,
                  fontWeight: i===activeIdx?700:400, whiteSpace:"nowrap" }}>
                  {s}
                </span>
              </div>
              {i<stageDef.labels.length-1 && (
                <div style={{ flex:1, height:1, background: isComplete||i<activeIdx?T.live:T.border,
                  margin:"0 4px", marginBottom:14, transition:"background .3s" }} />
              )}
            </div>
          ))}
        </div>
      </div>

      <ProgressBar value={progress} label="Current" color={T.amber} />

      {statusMsg && (
        <div style={{ fontSize:11, color:T.textSec, fontFamily:"monospace",
          background:T.void, padding:"6px 10px", borderRadius:3, border:`1px solid ${T.border}` }}>
          {statusMsg}
        </div>
      )}

      <div style={{ background:T.void, border:`1px solid ${T.border}`, borderRadius:4,
        padding:"8px 10px", maxHeight:200, overflowY:"auto", fontFamily:"monospace", fontSize:11 }}>
        {logs.slice(-20).map((l,i)=>(
          <div key={i} style={{ color:T.textSec, lineHeight:1.6 }}>
            <span style={{ color:T.textDim }}>› </span>{l}
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── History Tab ──────────────────────────────────────────────────────────────
function HistoryTab({ history, loading }) {
  if (loading) return (
    <div style={{ color:T.textDim, fontSize:12, textAlign:"center", padding:20 }}>Loading...</div>
  );
  if (!history.length) return (
    <div style={{ color:T.textDim, fontSize:12, textAlign:"center", padding:20 }}>
      No completed jobs yet.
    </div>
  );

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:6, overflowY:"auto", height:"100%" }}>
      {history.map(it => {
        const sc = statusColor(it.status);
        const created = fmtDate(it.createdAt);
        return (
          <div key={it.docId} style={{ display:"flex", alignItems:"center", gap:10, padding:"10px 12px",
            background:T.surface, border:`1px solid ${T.border}`, borderRadius:5 }}>
            <span style={{ fontSize:16 }}>🦅</span>
            <div style={{ flex:1 }}>
              <div style={{ fontSize:12, fontWeight:600, color:T.textPri }}>{it.name || it.clientName || it.docId}</div>
              <div style={{ fontSize:10, color:T.textSec, marginTop:2, fontFamily:"monospace" }}>
                {created}{it.outputFormat ? ` · ${it.outputFormat}` : ''}
              </div>
              {it.currentStep && it.status !== 'complete' && (
                <div style={{ fontSize:10, color:T.textDim, marginTop:1 }}>{it.currentStep}</div>
              )}
            </div>
            <Badge color={sc}>{it.status}</Badge>
          </div>
        );
      })}
    </div>
  );
}

// ─── Menu Bar ─────────────────────────────────────────────────────────────────
function MenuBar({ menus }) {
  const [open, setOpen] = useState(null);

  return (
    <div style={{ display:"flex", alignItems:"center", position:"relative" }}>
      {open && (
        <div onClick={() => setOpen(null)}
          style={{ position:"fixed", inset:0, zIndex:199 }} />
      )}
      {menus.map(menu => (
        <div key={menu.id} style={{ position:"relative", zIndex:200 }}>
          <div
            onClick={() => setOpen(open === menu.id ? null : menu.id)}
            style={{
              padding:"3px 9px", fontSize:11, cursor:"pointer", borderRadius:3,
              userSelect:"none", letterSpacing:".2px",
              color: open === menu.id ? T.amber : T.textSec,
              background: open === menu.id ? `${T.amber}12` : "transparent",
              transition:"color .1s, background .1s",
            }}>
            {menu.label}
          </div>
          {open === menu.id && (
            <div style={{
              position:"absolute", top:"calc(100% + 4px)", left:0, minWidth:200,
              background:T.surface, border:`1px solid ${T.border}`,
              borderRadius:5, boxShadow:"0 10px 30px rgba(0,0,0,.5)",
              padding:"4px 0",
            }}>
              {menu.items.map((item, i) =>
                item.separator ? (
                  <div key={i} style={{ height:1, background:T.border, margin:"3px 6px" }} />
                ) : (
                  <div key={i}
                    onClick={() => { if (!item.disabled) { setOpen(null); item.action?.(); } }}
                    style={{
                      padding:"6px 14px", fontSize:11, cursor: item.disabled ? "default" : "pointer",
                      color: item.disabled ? T.textDim : T.textPri,
                      display:"flex", justifyContent:"space-between", alignItems:"center",
                      transition:"background .1s",
                    }}
                    onMouseEnter={e => { if (!item.disabled) e.currentTarget.style.background = T.surfaceHi; }}
                    onMouseLeave={e => { e.currentTarget.style.background = "transparent"; }}>
                    <span>{item.label}</span>
                    {item.shortcut && (
                      <span style={{ fontSize:10, color:T.textDim, marginLeft:20 }}>{item.shortcut}</span>
                    )}
                  </div>
                )
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ─── Project State Modal ──────────────────────────────────────────────────────
const _STAGE_LABELS = {
  import:           { label: "Import",             icon: "📷" },
  view_extraction:  { label: "View Extraction",    icon: "🖼" },
  realityscan:      { label: "RealityScan",        icon: "📐" },
  colmap_alignment: { label: "COLMAP Alignment",   icon: "📐" },
  vggt_alignment:   { label: "VGGT Alignment",     icon: "🔬" },
  brush_training:   { label: "Brush Training",     icon: "🎨" },
  colmap_export:    { label: "COLMAP Export",      icon: "📦" },
};

function ProjectStateModal({ state, onContinue, onRerunFrom, onStartOver, onCancel }) {
  const [rerunStage, setRerunStage] = useState('');
  if (!state) return null;

  const { stages, nextStage, completedStages, projectDir, pipelineMode } = state;
  const _mode = pipelineMode || 'rs_brush';
  const allStages = _mode === 'colmap'
    ? ['import', 'view_extraction', 'colmap_alignment', 'brush_training']
    : _mode === 'vggt'
    ? ['import', 'view_extraction', 'vggt_alignment', 'brush_training']
    : ['import', 'view_extraction', 'realityscan', 'brush_training'];
  const shortDir = projectDir.length > 55 ? '…' + projectDir.slice(-52) : projectDir;

  const stageDetail = (key) => {
    const s = stages[key] || {};
    if (!s.done) return 'Not started';
    if (key === 'import')            return `${s.stitched || 0} files`;
    if (key === 'view_extraction')   return `${s.views || 0} views`;
    if (key === 'realityscan')       return `${s.images || 0} images`;
    if (key === 'colmap_alignment')  return s.cameras ? `${s.cameras} txt file(s)` : 'Done';
    if (key === 'vggt_alignment')    return 'Done';
    if (key === 'brush_training')    return `${s.plyFiles || 0} PLY file(s)`;
    return 'Done';
  };

  const nextLabel = nextStage ? (_STAGE_LABELS[nextStage]?.label || nextStage) : null;
  const rerunableStages = completedStages.filter(s => s !== 'import');

  return (
    <div style={{ position:"fixed", inset:0, zIndex:500, background:"rgba(0,0,0,.65)",
      display:"flex", alignItems:"center", justifyContent:"center" }}>
      <div style={{ background:T.surface, border:`1px solid ${T.borderHi}`, borderRadius:8,
        width:440, padding:"24px 28px", boxShadow:"0 8px 32px rgba(0,0,0,.6)" }}>

        <div style={{ fontSize:13, fontWeight:700, color:T.textPri, marginBottom:4 }}>
          Project Found
        </div>
        <div style={{ fontSize:10, color:T.textDim, fontFamily:"monospace",
          marginBottom:18, wordBreak:"break-all" }}>
          {shortDir}
        </div>

        {/* Stage list */}
        <div style={{ marginBottom:20 }}>
          {allStages.map(key => {
            const s    = stages[key] || {};
            const meta = _STAGE_LABELS[key] || { label: key, icon: "•" };
            return (
              <div key={key} style={{ display:"flex", alignItems:"center", gap:10,
                padding:"5px 0", borderBottom:`1px solid ${T.border}` }}>
                <span style={{ fontSize:13 }}>{s.done ? '✅' : '⬜'}</span>
                <span style={{ fontSize:12, color: s.done ? T.textPri : T.textDim, flex:1 }}>
                  {meta.label}
                </span>
                <span style={{ fontSize:11, color:T.textDim }}>
                  {stageDetail(key)}
                </span>
              </div>
            );
          })}
        </div>

        {/* Actions */}
        <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
          {nextLabel && (
            <Btn variant="live" full onClick={() => onContinue(nextStage)}>
              Continue from {nextLabel}
            </Btn>
          )}

          {rerunableStages.length > 0 && (
            <div style={{ display:"flex", gap:8 }}>
              <select value={rerunStage} onChange={e => setRerunStage(e.target.value)}
                style={{ flex:1, background:T.void, border:`1px solid ${T.border}`,
                  borderRadius:3, padding:"5px 8px", color: rerunStage ? T.textPri : T.textDim,
                  fontSize:12, fontFamily:"inherit" }}>
                <option value="">Rerun from stage…</option>
                {rerunableStages.map(s => (
                  <option key={s} value={s}>{_STAGE_LABELS[s]?.label || s}</option>
                ))}
              </select>
              <Btn variant="ghost" disabled={!rerunStage}
                onClick={() => rerunStage && onRerunFrom(rerunStage)}>
                Rerun
              </Btn>
            </div>
          )}

          <Btn variant="danger" full onClick={onStartOver}>
            Start Over (keep import)
          </Btn>
        </div>

        <div style={{ marginTop:14, textAlign:"right" }}>
          <Btn variant="ghost" small onClick={onCancel}>Cancel</Btn>
        </div>
      </div>
    </div>
  );
}

// ─── Defaults ─────────────────────────────────────────────────────────────────
const defaultSettings = {
  extractionMethod:"interval", intervalValue:1, intervalUnit:"seconds",
  frameCount:30, frameFormat:"jpg",
  pitchAngles:"-50, -7", yawSteps:"6", fov:"94.6", overlayOpacity:0.6,
  skipRS:false, runVggt:false, runPostshot:true, runBrush:false,
  vggtConf:50, vggtSky:32, vggtMaskSky:true, vggtShowCam:true, vggtTemporal:true,
  vggtMode:"depthmap", vggtAnchorRig:false, exportXmp:false, gpsTriggersRS:false, gpsPriorsColmap:false,
  runColmap:false, colmapMode:"rig", colmapMatcher:"sequential", horizonRef:true, colmapVisualize:false, colmapCorrectPitch:true, colmapOrientationAlign:false,
  postshotProfile:"Splat MCMC", postshotMaxSize:3840, postshotSteps:30,
  postshotMaxSplats:1000, postshotAA:true, postshotError:false,
  postshotContext:false, postshotPly:false, postshotAlpha:false, postshotSky:false,
  brushSteps:30000, brushSplats:5000000, brushRes:1920, brushSeed:42,
  brushRerun:false, brushViewer:false,
  ffmpeg:"", rs:"", postshot:"", brush:"", rsSettings:"", vggt:"", colmapBin:"",
  inspStitchType:"ai", inspLensGuard:"none", inspFlowState:true, inspCuda:true,
  inspOutputWidth:"5984", inspWorkers:"2",
  projectDir:"",
};

const MAIN_TABS = [
  { id:"fieldraven", label:"🦅 FieldRaven",  color:T.frColor },
  { id:"pipeline",   label:"⚙ Pipeline",    color:T.amber },
  { id:"active",     label:"▶ Active Job",  color:T.live },
  { id:"history",    label:"◷ History",     color:T.textSec },
];

// ─── Root ─────────────────────────────────────────────────────────────────────
export default function FieldRavenDesktop({ user, onSignOut }) {
  const [activeMainTab, setActiveMainTab] = useState(0);
  const [settings, setSettings]         = useState(defaultSettings);
  const [localQueue, setLocalQueue]     = useState([]);
  const [selected, setSelected]         = useState(null);
  const [consoleVisible, setConsoleVisible] = useState(true);
  const [logs, setLogs]                 = useState(["FieldRaven Desktop — Ready"]);

  const [machineInfo, setMachineInfo]   = useState(null);
  const [fieldJobs, setFieldJobs]       = useState([]);
  const [fieldJobsLoading, setFieldJobsLoading] = useState(false);
  const [pqItems, setPqItems]           = useState([]);
  const [currentJobId, setCurrentJobId] = useState(null);
  const [progress, setProgress]         = useState(0);
  const [statusMsg, setStatusMsg]       = useState("Ready");
  const [currentStage, setCurrentStage] = useState('');
  const [pipelineMode, setPipelineMode] = useState('vggt');
  const [history, setHistory]           = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [cameraStatus, setCameraStatus]   = useState(null);
  const [importedFiles, setImportedFiles] = useState({}); // { [jobId]: { files, total } }
  const [projectDirs, setProjectDirs]       = useState({}); // { [jobId]: string }
  const [lastBrowseDir, setLastBrowseDir]   = useState('C:\\Users');
  const [projectState, setProjectState]     = useState(null); // modal data from /api/project/state
  const [importingJobId, setImportingJobId] = useState(null);
  const [stitchingJobId, setStitchingJobId] = useState(null);
  const [stitchingProjectDir, setStitchingProjectDir] = useState(null);
  const [importStep, setImportStep]         = useState('');
  const [importPct, setImportPct]           = useState(0);
  const [stitchStep, setStitchStep]         = useState('');
  const [stitchPct, setStitchPct]           = useState(0);

  const addLog = m => setLogs(l => [...l.slice(-200), m]);

  const api = useCallback(
    (path, method = 'GET', body = null) => apiFetch(user, path, method, body),
    [user]
  );

  // ── Initial load ──────────────────────────────────────────
  useEffect(() => {
    api('/api/health')
      .then(d => setMachineInfo(d))
      .catch(() => {});
    api('/api/config')
      .then(cfg => setSettings(s => ({ ...s, ...apiConfigToSettings(cfg) })))
      .catch(() => {});
  }, [api]);

  // ── Field jobs ────────────────────────────────────────────
  const loadFieldJobs = useCallback(() => {
    setFieldJobsLoading(true);
    api('/api/user-jobs')
      .then(d => setFieldJobs(d.jobs || []))
      .catch(() => {})
      .finally(() => setFieldJobsLoading(false));
  }, [api]);

  useEffect(() => {
    if (activeMainTab !== 0) return;
    loadFieldJobs();
    const iv = setInterval(loadFieldJobs, 30_000);
    return () => clearInterval(iv);
  }, [activeMainTab, loadFieldJobs]);

  // ── Processing queue polling ──────────────────────────────
  const loadQueue = useCallback(() => {
    api('/api/jobs/queue').then(d => {
      // Dedup by docId/id — a processing local job appears in both d.queued and d.current
      const seenIds = new Set();
      const fresh = [...(d.queued || []), ...(d.current ? [d.current] : [])].filter(j => {
        const id = j.docId || j.id;
        if (seenIds.has(id)) return false;
        seenIds.add(id);
        return true;
      });
      // Preserve any openProjectFolder-injected items that arrived right before
      // this poll fired and aren't yet in the server response.
      setPqItems(prev => {
        const freshIds = new Set(fresh.map(j => j.docId || j.id));
        const pinned = prev.filter(
          j => (j.jobType === 'local_folder' || j.jobType === 'local_video') &&
               !freshIds.has(j.docId || j.id)
        );
        return [...fresh, ...pinned];
      });
      if (d.current?.docId && !currentJobId) {
        setCurrentJobId(d.current.docId);
      }
    }).catch(() => {});
  }, [api, currentJobId]);

  useEffect(() => {
    loadQueue();
    const iv = setInterval(loadQueue, 8000);
    return () => clearInterval(iv);
  }, [loadQueue]);

  // ── Active job progress polling ───────────────────────────
  useEffect(() => {
    if (!currentJobId) return;
    let lastStep = '';
    const iv = setInterval(async () => {
      try {
        const d = await api(`/api/jobs/${currentJobId}/status`);
        setProgress(d.progress || 0);
        if (d.currentStage) setCurrentStage(d.currentStage);
        if (d.pipelineMode) setPipelineMode(d.pipelineMode);
        if (d.currentStep && d.currentStep !== lastStep) {
          lastStep = d.currentStep;
          setStatusMsg(d.currentStep);
          addLog(d.currentStep);
        }
        if (d.status === 'complete' || d.status === 'error' || d.status === 'cancelled') {
          clearInterval(iv);
          setCurrentJobId(null);
          setCurrentStage('');
          setPipelineMode('vggt');
          if (d.status !== 'complete') setProgress(0);
          addLog(d.status === 'complete' ? 'Pipeline complete' : `Pipeline ${d.status}: ${d.currentStep}`);
          loadQueue();
        }
      } catch {}
    }, 2000);
    return () => clearInterval(iv);
  }, [currentJobId, api]);

  // ── History ───────────────────────────────────────────────
  const loadHistory = useCallback(() => {
    setHistoryLoading(true);
    api('/api/jobs/history')
      .then(d => setHistory(d.jobs || []))
      .catch(() => {})
      .finally(() => setHistoryLoading(false));
  }, [api]);

  useEffect(() => {
    if (activeMainTab === 3) loadHistory();
  }, [activeMainTab, loadHistory]);

  // ── Camera status ─────────────────────────────────────────
  useEffect(() => {
    const check = () => api('/api/camera/status').then(setCameraStatus).catch(() => {});
    check();
    const iv = setInterval(check, 15000);
    return () => clearInterval(iv);
  }, [api]);

  // ── Load project dir state (called explicitly, never on startup) ─
  const loadProjectDir = useCallback(async (jobId, dir) => {
    if (!dir) return;
    try {
      const data = await api(`/api/project/config?dir=${encodeURIComponent(dir)}`);
      if (data.files && data.files.length > 0) {
        setImportedFiles(prev => ({ ...prev, [jobId]: { files: data.files, total: data.total, path: data.path } }));
        addLog(`Restored ${data.total} files from ${dir}`);
      }
      // Write config so this dir is permanently associated with the job
      api('/api/project/config', 'POST', { dir, jobId }).catch(() => {});
    } catch (e) {
      addLog(`Could not read project dir: ${e.message}`);
    }
  }, [api]);

  // ── Open project folder — detect history and show modal if needed ─
  const openProjectFolder = useCallback(async (dir) => {
    if (!dir) return;
    setLastBrowseDir(dir);

    // 1. Read the save file to discover the stored jobId, imported files, and
    //    saved settings. This works regardless of what (if anything) is selected
    //    in the queue — the save file is the source of truth.
    let jobId = null;
    let jobType = 'folder';
    let savedSettings = null;
    try {
      const data = await api(`/api/project/config?dir=${encodeURIComponent(dir)}`);
      jobId        = data.config?.jobId   || null;
      savedSettings = data.config?.settings || null;
      // Populate the gallery immediately so it's ready before anything else
      if (data.files && data.files.length > 0) {
        setImportedFiles(prev => ({
          ...prev,
          [jobId]: { files: data.files, total: data.total, path: data.path },
        }));
      }
    } catch (e) {
      addLog(`Could not read project: ${e.message}`);
      return;
    }

    if (!jobId) {
      // No save file (new/unregistered folder) — fall back to wiring up whatever
      // job is currently selected, the same as the old Browse… button behaviour.
      setSettings(s => ({ ...s, projectDir: dir }));
      if (selected?.id) {
        setProjectDirs(prev => ({ ...prev, [selected.id]: dir }));
        loadProjectDir(selected.id, dir);
      }
      return;
    }

    // 2. Wire up the project directory for this job
    setProjectDirs(prev => ({ ...prev, [jobId]: dir }));
    setSettings(s => ({ ...s, projectDir: dir }));
    if (savedSettings) {
      setSettings(s => ({ ...s, ...apiConfigToSettings(savedSettings) }));
    }

    // 3. Fetch the specific job doc by id (works regardless of its Firestore status —
    //    poll_for_jobs only returns 'queued' docs, so a completed/errored project
    //    would never appear via loadQueue alone).  Inject it into pqItems so the
    //    Image Folders queue box shows it immediately, then select it.
    const dirName = dir.split(/[\\/]/).filter(Boolean).pop() || 'Project';
    try {
      const jobData = await api(`/api/jobs/${jobId}/status`);
      if (jobData) {
        const entry = { ...jobData, docId: jobId };
        setPqItems(prev => prev.some(j => (j.docId||j.id) === jobId) ? prev : [entry, ...prev]);
        setSelected({ id: jobId, type: jobType, name: jobData.name || dirName, status: jobData.status });
      } else {
        setSelected({ id: jobId, type: jobType, name: dirName, status: 'queued' });
      }
    } catch {
      setSelected({ id: jobId, type: jobType, name: dirName, status: 'queued' });
    }
    loadQueue();

    // 4. Check pipeline history and show the resume modal if appropriate
    try {
      const state = await api(`/api/project/state?dir=${encodeURIComponent(dir)}`);
      const hasMeaningfulHistory = state?.hasHistory &&
        (state.completedStages || []).some(s => s !== 'import');
      if (hasMeaningfulHistory) {
        setProjectState(state);
      }
    } catch (e) {
      // non-fatal — gallery is already populated, user can still run
    }
  }, [api, loadQueue, loadProjectDir, selected, setSettings, addLog]);

  // ── Resume a project from a specific stage ────────────────────
  const runPipelineResume = useCallback(async (projectDir, jobId, startFrom) => {
    setProjectState(null);
    try {
      addLog(`Resuming from ${startFrom || 'beginning'}…`);
      // Always use the current UI settings — openProjectFolder already populates
      // them from the save file, so if the user hasn't changed anything they're
      // identical. If the user DID change them (e.g. switching RS → COLMAP), their
      // changes should take effect rather than being overridden by the stale save.
      const apiCfg = settingsToApiConfig(settings);
      const r = await api('/api/project/resume', 'POST', {
        dir: projectDir, jobId, startFrom: startFrom || '',
        settings: apiCfg,
      });
      setCurrentJobId(r.jobId);
      setProgress(0);
      setCurrentStage('');
      const _mode = settings.runColmap ? 'colmap' : (!settings.runVggt && settings.runBrush ? 'rs_brush' : 'vggt');
      setPipelineMode(_mode);
      setStatusMsg(`Resuming from ${startFrom || 'beginning'}`);
      addLog(`Pipeline resumed — jobId: ${r.jobId}`);
      setActiveMainTab(2);
      loadQueue();
    } catch (e) {
      addLog(`Resume failed: ${e.message}`);
    }
  }, [api, loadQueue, settings]);

  // ── Auto-save settings to fieldraven.json when they change ──
  useEffect(() => {
    if (!selected?.id || !projectDirs[selected.id]) return;
    const dir = projectDirs[selected.id];
    const jobId = selected.id;
    const timer = setTimeout(() => {
      api('/api/project/config', 'POST', { dir, jobId, settings: settingsToApiConfig(settings) }).catch(() => {});
    }, 1500);
    return () => clearTimeout(timer);
  }, [settings, selected, projectDirs, api]);

  // ── Import progress polling ───────────────────────────────
  useEffect(() => {
    if (!importingJobId) { setImportStep(''); setImportPct(0); return; }
    let lastStep = '';
    const iv = setInterval(async () => {
      try {
        const s = await api(`/api/jobs/${importingJobId}/status`);
        if (s.currentStep && s.currentStep !== lastStep) {
          lastStep = s.currentStep;
          setImportStep(s.currentStep);
          addLog(s.currentStep);
        }
        if (s.progress != null) setImportPct(s.progress);
      } catch {}
    }, 1200);
    return () => clearInterval(iv);
  }, [importingJobId, api]);

  // ── Stitch progress polling ───────────────────────────────
  useEffect(() => {
    if (!stitchingJobId || !stitchingProjectDir) { setStitchStep(''); setStitchPct(0); return; }
    let lastStep = '';
    let pollCount = 0;
    const pdParam = encodeURIComponent(stitchingProjectDir);
    const refreshFiles = () => {
      api(`/api/jobs/${stitchingJobId}/files?projectDir=${pdParam}`)
        .then(data => setImportedFiles(prev => ({ ...prev, [stitchingJobId]: data })))
        .catch(() => {});
    };
    const iv = setInterval(async () => {
      try {
        const s = await api(`/api/jobs/${stitchingJobId}/status`);
        if (s.currentStep && s.currentStep !== lastStep) {
          lastStep = s.currentStep;
          setStitchStep(s.currentStep);
          addLog(s.currentStep);
        }
        if (s.progress != null) setStitchPct(s.progress);
        pollCount++;
        if (pollCount % 3 === 0) refreshFiles();
        if (s.currentStep?.includes('Converted') || s.progress >= 50) {
          clearInterval(iv);
          refreshFiles();
          setStitchingJobId(null);
          setStitchingProjectDir(null);
          addLog('Conversion complete.');
        }
      } catch {}
    }, 1200);
    return () => clearInterval(iv);
  }, [stitchingJobId, stitchingProjectDir, api]);

  // ── Actions ───────────────────────────────────────────────
  const onImport = useCallback(async (jobId, sourceDrive) => {
    let projectDir = projectDirs[jobId] || null;

    // ── Phase 1: Create / verify project directory ─────────────
    if (!projectDir) {
      addLog('Prompting for project folder…');
      const browsed = await api(`/api/browse/folder?initial=${encodeURIComponent(lastBrowseDir)}`);
      if (!browsed.path) { addLog('Cancelled.'); return; }
      projectDir = browsed.path;
      setLastBrowseDir(browsed.path);
      setProjectDirs(prev => ({ ...prev, [jobId]: projectDir }));
      setSettings(s => ({ ...s, projectDir: browsed.path }));
      addLog(`Project folder: ${projectDir}`);
    }

    // ── Phase 2: Check for existing project (fieldraven.json + files) ─
    try {
      const proj = await api(`/api/project/config?dir=${encodeURIComponent(projectDir)}`);
      if (proj.total > 0) {
        // Folder already has imported files — load and stop
        setImportedFiles(prev => ({ ...prev, [jobId]: { files: proj.files, total: proj.total, path: proj.path } }));
        addLog(`Loaded existing project: ${proj.total} files from ${projectDir}`);
        api('/api/project/config', 'POST', { dir: projectDir, jobId }).catch(() => {});
        return;
      }
    } catch {}

    // ── Phase 3: Camera import (only if camera is connected) ───
    if (!sourceDrive) {
      addLog('Project folder set. Connect camera to import files.');
      return;
    }

    addLog(`Importing from ${sourceDrive}…`);
    setStitchingJobId(null);
    setImportingJobId(jobId);
    try {
      const result = await api('/api/camera/import', 'POST', { jobId, sourceDrive, projectDir });

      // 3. No filename list — ask user to select files manually
      if (result.needsManualSelect) {
        addLog('No file list for this job — opening manual file picker…');
        const initial = encodeURIComponent(result.cameraDrive || sourceDrive);
        const picked = await api(`/api/browse/files?initial=${initial}`);
        if (!picked.paths || picked.paths.length === 0) {
          addLog('No files selected — import aborted.');
          return;
        }
        addLog(`Importing ${picked.paths.length} manually selected files…`);
        const manualResult = await api('/api/camera/import-manual', 'POST', {
          jobId, filePaths: picked.paths, projectDir,
        });
        addLog(`Import complete: ${manualResult.imported} copied, ${manualResult.skipped} skipped`);
      } else {
        addLog(`Import complete: ${result.imported} files copied, ${result.skipped} skipped`);
      }
    } finally {
      setImportingJobId(null);
    }

    // 4. Refresh file list and persist project config
    const pdParam = encodeURIComponent(projectDir);
    const files = await api(`/api/jobs/${jobId}/files?projectDir=${pdParam}`);
    setImportedFiles(prev => ({ ...prev, [jobId]: files }));
    api('/api/project/config', 'POST', { dir: projectDir, jobId }).catch(() => {});

    // 5. Trigger immediate .insp → equirectangular conversion in background
    const inspFiles = (files.files || []).filter(f => f.ext === '.insp');
    if (inspFiles.length > 0) {
      addLog(`Starting conversion of ${inspFiles.length} .insp files…`);
      setStitchingJobId(jobId);
      setStitchingProjectDir(projectDir);
      api(`/api/jobs/${jobId}/stitch`, 'POST')
        .then(r => addLog(r.message))
        .catch(e => { addLog(`Conversion note: ${e.message}`); setStitchingJobId(null); setStitchingProjectDir(null); });
    }
  }, [api, projectDirs, lastBrowseDir, setSettings]);

  // Entry point for a project whose photos already exist on disk (not a field job,
  // not a camera import) — establish/create a project folder, pick the source photos
  // folder, optionally copy them into "imported photos", then register a job so it
  // runs through the same pipeline as an accepted FieldRaven job.
  const onAddImageFolder = useCallback(async () => {
    addLog('Select or create a project folder…');
    const dirRes = await api(`/api/browse/folder?initial=${encodeURIComponent(lastBrowseDir)}`);
    if (!dirRes.path) { addLog('Cancelled.'); return; }
    const projectDir = dirRes.path;
    setLastBrowseDir(projectDir);

    addLog('Select the folder containing your photos…');
    const srcRes = await api(`/api/browse/folder?initial=${encodeURIComponent(projectDir)}&title=${encodeURIComponent('Select the folder containing your photos')}`);
    if (!srcRes.path) { addLog('Cancelled.'); return; }
    const sourceFolder = srcRes.path;

    const doCopy = window.confirm(
      `Copy photos from:\n${sourceFolder}\n\ninto the project folder as "imported photos"?`
    );
    if (!doCopy) { addLog('Import skipped — no photos copied.'); return; }

    let created;
    try {
      const folderName = sourceFolder.split(/[\\/]/).filter(Boolean).pop() || 'Imported Photos';
      created = await api('/api/jobs/create-local', 'POST', { name: folderName, projectDir });
    } catch (e) {
      addLog(`Could not create project: ${e.message}`);
      return;
    }
    setProjectDirs(prev => ({ ...prev, [created.processingJobId]: projectDir }));

    addLog(`Importing photos from ${sourceFolder}…`);
    try {
      const result = await api('/api/project/import-folder', 'POST', {
        jobId: created.processingJobId, projectDir, sourceFolder,
      });
      addLog(`Import complete: ${result.imported} copied, ${result.skipped} skipped`);
    } catch (e) {
      addLog(`Import failed: ${e.message}`);
      return;
    }

    // Populate the gallery the same way the camera-import flow does.
    const pdParam = encodeURIComponent(projectDir);
    const files = await api(`/api/jobs/${created.processingJobId}/files?projectDir=${pdParam}`);
    setImportedFiles(prev => ({ ...prev, [created.processingJobId]: files }));

    api('/api/project/config', 'POST', { dir: projectDir, jobId: created.processingJobId }).catch(() => {});
    addLog(`Project ready: ${created.name}`);
    loadQueue();
  }, [api, lastBrowseDir, loadQueue, setImportedFiles]);

  const onQueueJob = useCallback(async (job) => {
    try {
      const result = await api('/api/jobs/queue-for-processing', 'POST', { userJobId: job.id });
      addLog(`Queued: ${result.name}`);
      loadQueue();
      setActiveMainTab(1);
    } catch (e) {
      addLog(`Failed to queue: ${e.message}`);
    }
  }, [api, loadQueue]);

  const runPipeline = useCallback(async () => {
    if (!selected) return;
    if (selected.type === 'fieldraven' || selected.type === 'folder') {
      try {
        addLog(`Accepting job...`);
        await api('/api/jobs/accept', 'POST', { jobId: selected.id });
        // Save current settings to fieldraven.json before starting
        const projectDir = projectDirs[selected.id];
        if (projectDir) {
          api('/api/project/config', 'POST', { dir: projectDir, jobId: selected.id, settings: settingsToApiConfig(settings) }).catch(() => {});
        }
        addLog('Starting pipeline...');
        await api(`/api/jobs/${selected.id}/start`, 'POST', settingsToApiConfig(settings));
        setCurrentJobId(selected.id);
        setProgress(0);
        setCurrentStage('');
        const _mode = settings.runColmap ? 'colmap' : (!settings.runVggt && settings.runBrush ? 'rs_brush' : 'vggt');
        setPipelineMode(_mode);
        setStatusMsg('Pipeline started');
        addLog('Pipeline running');
        setSelected(null);
        setActiveMainTab(2);
        loadQueue();
      } catch (e) {
        addLog(`Pipeline error: ${e.message}`);
      }
    }
  }, [selected, api, pqItems, loadQueue, settings, projectDirs]);

  const cancelPipeline = useCallback(async () => {
    if (!currentJobId) return;
    try {
      await api(`/api/jobs/${currentJobId}/cancel`, 'POST', {});
      addLog('Job cancelled');
      setCurrentJobId(null);
      setProgress(0);
      loadQueue();
    } catch {}
  }, [currentJobId, api, loadQueue]);

  const onCancelPq = useCallback(async (jobId) => {
    try {
      await api(`/api/jobs/${jobId}/cancel`, 'POST', {});
      if (selected?.id === jobId) setSelected(null);
      loadQueue();
    } catch {}
  }, [api, loadQueue, selected]);

  const onDeletePq = useCallback(async (jobId) => {
    try {
      await api(`/api/jobs/${jobId}`, 'DELETE');
      // Remove immediately from local state; don't wait for next poll
      setPqItems(prev => prev.filter(j => (j.docId || j.id) !== jobId));
      if (selected?.id === jobId) setSelected(null);
    } catch {}
  }, [api, selected]);

  const onAddVideoFile = useCallback(async () => {
    addLog('Select or create a project folder for this video…');
    const dirRes = await api(`/api/browse/folder?initial=${encodeURIComponent(lastBrowseDir)}`);
    if (!dirRes.path) { addLog('Cancelled.'); return; }
    const projectDir = dirRes.path;
    setLastBrowseDir(projectDir);

    addLog('Select the video file…');
    const fileRes = await api(`/api/browse/video?initial=${encodeURIComponent(projectDir)}`);
    if (!fileRes.path) { addLog('Cancelled.'); return; }

    let created;
    try {
      created = await api('/api/jobs/create-video', 'POST', { projectDir, videoPath: fileRes.path });
    } catch (e) {
      addLog(`Could not create video project: ${e.message}`);
      return;
    }
    addLog(`Video project ready: ${created.name}`);
    loadQueue();
  }, [api, lastBrowseDir, loadQueue]);

  const onSaveConfig = useCallback(async () => {
    try {
      await api('/api/config', 'PUT', settingsToApiConfig(settings));
      addLog('Configuration saved');
    } catch (e) {
      addLog(`Save failed: ${e.message}`);
    }
  }, [api, settings]);

  // ── Derived ───────────────────────────────────────────────
  const isProcessing = !!currentJobId;
  const currentJob   = pqItems.find(j => (j.docId||j.id) === currentJobId) || null;
  const displayQueue = [
    ...pqItems
      .filter(j => j.status === 'queued' || j.status === 'processing')
      .map(j => ({ id: j.docId||j.id, type:'fieldraven', name: j.name||j.clientName||'Field Job' })),
    ...localQueue,
  ];

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100vh",
      background:T.base, color:T.textPri,
      fontFamily:"-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
      fontSize:13, overflow:"hidden" }}>

      {/* ── Project State Modal ── */}
      {projectState && (
        <ProjectStateModal
          state={projectState}
          onContinue={async (nextStage) => {
            await runPipelineResume(projectState.projectDir, projectState.jobId, nextStage);
          }}
          onRerunFrom={async (stage) => {
            try {
              addLog(`Preparing rerun from ${stage}…`);
              await api('/api/project/prepare', 'POST', { dir: projectState.projectDir, startFrom: stage });
              await runPipelineResume(projectState.projectDir, projectState.jobId, stage);
            } catch (e) {
              addLog(`Rerun failed: ${e.message}`);
              setProjectState(null);
            }
          }}
          onStartOver={async () => {
            try {
              addLog('Clearing output (views, alignment, training) — import kept…');
              await api('/api/project/prepare', 'POST', { dir: projectState.projectDir, startFrom: 'view_extraction' });
              addLog('Ready — adjust settings then click Run.');
              // Keep the project dir loaded so the user can tweak settings and run manually
              if (selected?.id) loadProjectDir(selected.id, projectState.projectDir);
              setProjectState(null);
            } catch (e) {
              addLog(`Start over failed: ${e.message}`);
              setProjectState(null);
            }
          }}
          onCancel={() => {
            setProjectState(null);
            // Still apply the directory even if user cancels the run
            if (selected?.id) loadProjectDir(selected.id, projectState.projectDir);
          }}
        />
      )}

      {/* ── Title / menu bar ── */}
      <div style={{ display:"flex", alignItems:"center", gap:4, padding:"5px 14px",
        background:T.void, borderBottom:`1px solid ${T.border}`, flexShrink:0 }}>

        {/* Logo */}
        <div style={{ display:"flex", alignItems:"baseline", gap:5, marginRight:6 }}>
          <span style={{ fontSize:15, fontWeight:800, letterSpacing:".8px", color:T.amber }}>
            FIELDRAVEN
          </span>
          <span style={{ fontSize:9, color:T.textDim, letterSpacing:".4px", textTransform:"uppercase" }}>
            desktop
          </span>
        </div>

        {/* File / Edit / Help menus */}
        <MenuBar menus={[
          {
            id:"file", label:"File",
            items:[
              { label:"Open Saved Project (fieldraven.json)…", action: async () => {
                  const initial = settings.projectDir || lastBrowseDir;
                  const r = await api(`/api/browse/file?type=json&initial=${encodeURIComponent(initial)}`).catch(()=>null);
                  if (r?.path) {
                    // Derive project dir from the JSON file's parent folder
                    const dir = r.path.substring(0, r.path.lastIndexOf('\\')) || r.path;
                    openProjectFolder(dir);
                  }
              }},
              { label:"Save Project", disabled: !settings.projectDir, action: () => {
                  addLog("Save Project — coming soon");
              }},
              { separator:true },
              { label:"Open Output in Explorer", disabled: !settings.projectDir, action: () => {
                  api('/api/browse/open-folder', 'POST', { path: settings.projectDir }).catch(()=>{});
              }},
            ],
          },
          {
            id:"edit", label:"Edit",
            items:[
              { label:"Clear Log", action: () => setLogs([]) },
            ],
          },
          {
            id:"help", label:"Help",
            items:[
              { label:"Quick Start Guide", action: () => addLog("Quick Start Guide — coming soon") },
              { label:"Parameter Guide",   action: () => addLog("Parameter Guide — coming soon") },
              { label:"Troubleshooting",   action: () => addLog("Troubleshooting — coming soon") },
              { separator:true },
              { label:"About FieldRaven Desktop", action: () => addLog("FieldRaven Desktop — v1.0") },
            ],
          },
        ]} />

        {/* Spacer */}
        <div style={{ flex:1 }} />

        {/* Action buttons */}
        <Btn onClick={runPipeline}
          disabled={!selected || isProcessing || (selected.type !== 'fieldraven' && selected.type !== 'folder')}
          variant="live">
          ▶ Run Pipeline
        </Btn>
        <Btn disabled={!isProcessing} variant="danger" onClick={cancelPipeline}>
          ✕ Cancel
        </Btn>
        <Btn small variant="ghost" onClick={()=>setConsoleVisible(v=>!v)}>
          {consoleVisible ? "Hide Log" : "Log"}
        </Btn>

        {/* Machine status */}
        <div style={{ display:"flex", alignItems:"center", gap:5, padding:"3px 8px",
          background:T.surface, borderRadius:3, border:`1px solid ${T.border}` }}>
          <Pill color={machineInfo ? T.live : T.textDim} />
          <span style={{ fontSize:10, color:T.textDim, fontFamily:"monospace" }}>
            {machineInfo?.machine_name || 'connecting...'}
          </span>
        </div>

        <Btn small variant="ghost" onClick={onSignOut} style={{ fontSize:10 }}>
          Sign Out
        </Btn>
      </div>

      {/* ── Tab row ── */}
      <div style={{ display:"flex", alignItems:"stretch", gap:1, padding:"0 14px",
        background:T.void, borderBottom:`1px solid ${T.border}`, flexShrink:0 }}>

        {/* Main tabs */}
        {MAIN_TABS.map((t,i)=>(
          <div key={t.id} onClick={()=>setActiveMainTab(i)}
            style={{ padding:"7px 14px", fontSize:12, fontWeight:600, cursor:"pointer",
              color: activeMainTab===i ? t.color : T.textDim,
              borderBottom:`2px solid ${activeMainTab===i?t.color:"transparent"}`,
              transition:"all .15s", whiteSpace:"nowrap" }}>
            {t.label}
          </div>
        ))}

        {/* Output directory — right of History */}
        <div style={{ display:"flex", alignItems:"center", gap:6, marginLeft:8,
          padding:"0 8px", borderLeft:`1px solid ${T.border}` }}>
          <span style={{ fontSize:10, color:T.textDim, whiteSpace:"nowrap" }}>📁</span>
          <Input value={settings.projectDir}
            onChange={v=>{
              setSettings(s=>({...s,projectDir:v}));
              if (selected?.id) setProjectDirs(prev=>({...prev,[selected.id]:v}));
            }}
            onBlur={v=>{
              if (selected?.id && v) loadProjectDir(selected.id, v);
            }}
            placeholder="Output directory…"
            style={{ width:260, fontSize:10 }} />
          <Btn small variant="ghost" onClick={async () => {
            const initial = settings.projectDir || lastBrowseDir;
            const r = await api(`/api/browse/folder?initial=${encodeURIComponent(initial)}`).catch(()=>null);
            if (r?.path) openProjectFolder(r.path);
          }}>
            Browse…
          </Btn>
        </div>

        {/* Queue pills */}
        {displayQueue.length > 0 && (
          <div style={{ marginLeft:"auto", display:"flex", alignItems:"center",
            padding:"0 8px", gap:6 }}>
            <Label>Queue</Label>
            {displayQueue.map(it=>(
              <div key={it.id}
                style={{ display:"flex", alignItems:"center", gap:4, padding:"2px 7px",
                  background:`${SOURCE_TYPES[it.type].color}18`,
                  border:`1px solid ${SOURCE_TYPES[it.type].color}44`,
                  borderRadius:3, fontSize:10, color:SOURCE_TYPES[it.type].color }}>
                {SOURCE_TYPES[it.type].icon}
                <span style={{ maxWidth:80, overflow:"hidden", textOverflow:"ellipsis",
                  whiteSpace:"nowrap" }}>{it.name}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Tab content ── */}
      <div style={{ flex:1, overflow:"hidden", padding:10 }}>
        {activeMainTab===0 && (
          <FieldRavenTab
            fieldJobs={fieldJobs} loading={fieldJobsLoading}
            pqItems={pqItems} machineInfo={machineInfo} cameraStatus={cameraStatus}
            onQueueJob={onQueueJob} setActiveMainTab={setActiveMainTab}
          />
        )}
        {activeMainTab===1 && (
          <PipelineTab
            pqItems={pqItems} localQueue={localQueue} setLocalQueue={setLocalQueue}
            selected={selected} setSelected={setSelected}
            settings={settings} setSettings={setSettings}
            onSaveConfig={onSaveConfig} onCancelPq={onCancelPq} onDeletePq={onDeletePq}
            machineInfo={machineInfo}
            cameraStatus={cameraStatus} importedFiles={importedFiles} projectDirs={projectDirs} onImport={onImport}
            onAddImageFolder={onAddImageFolder} onAddVideoFile={onAddVideoFile}
            importStep={importStep} importPct={importPct}
            stitching={!!stitchingJobId} stitchStep={stitchStep} stitchPct={stitchPct}
          />
        )}
        {activeMainTab===2 && (
          <ActiveJobTab
            currentJob={currentJob} progress={progress}
            statusMsg={statusMsg} logs={logs}
            currentStage={currentStage} pipelineMode={pipelineMode}
          />
        )}
        {activeMainTab===3 && (
          <HistoryTab history={history} loading={historyLoading} />
        )}
      </div>

      {/* ── Status bar ── */}
      <div style={{ display:"flex", alignItems:"center", gap:12, padding:"4px 14px",
        background:T.void, borderTop:`1px solid ${T.border}`, flexShrink:0 }}>
        <span style={{ flex:1, fontSize:11, color:T.textSec }}>{statusMsg}</span>
        {isProcessing && (
          <ProgressBar value={progress} label="Current" style={{ width:180 }} />
        )}
      </div>

      <Console logs={logs} visible={consoleVisible} />
    </div>
  );
}
