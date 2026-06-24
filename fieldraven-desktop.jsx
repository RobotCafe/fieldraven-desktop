import { useState, useRef, useEffect, useCallback } from "react";

// ─── Design Tokens ────────────────────────────────────────────────────────────
const T = {
  // Base surfaces
  void:      "#090c12",
  base:      "#0e1220",
  surface:   "#141826",
  surfaceHi: "#1a2030",
  surfaceEl: "#1f263a",
  border:    "#252d42",
  borderHi:  "#303a54",

  // Accent: topographic amber — warm, precise, fieldwork-coded
  amber:     "#e8a442",
  amberDim:  "#a06c22",
  amberGlow: "#f0b85a",

  // Live/active: electric green
  live:      "#39e07a",
  liveDim:   "#1a6638",

  // Danger
  danger:    "#e05555",
  dangerDim: "#6b2020",

  // Info
  info:      "#5599ff",

  // Text
  textPri:   "#dde4f0",
  textSec:   "#7a8aaa",
  textDim:   "#3d4860",
  textAmber: "#e8a442",

  // Source type colours
  frColor:   "#39e07a",   // FieldRaven jobs — green (live, captured)
  vidColor:  "#5599ff",   // Video — blue
  imgColor:  "#cc77ff",   // Image folder — purple
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
        {hint && <div style={{ fontSize:10, color:T.textDim, marginTop:3 }}>{hint}</div>}
      </div>
    </div>
  );
}

function Input({ value, onChange, type="text", placeholder, disabled, style={} }) {
  return (
    <input type={type} value={value} onChange={e=>onChange(e.target.value)}
      placeholder={placeholder} disabled={disabled}
      style={{ width:"100%", background:T.void, border:`1px solid ${T.border}`,
        borderRadius:3, padding:"5px 8px", color:T.textPri, fontSize:12,
        outline:"none", boxSizing:"border-box", fontFamily:"inherit", ...style }} />
  );
}

function Select({ value, onChange, options, style={} }) {
  return (
    <select value={value} onChange={e=>onChange(e.target.value)}
      style={{ width:"100%", background:T.void, border:`1px solid ${T.border}`,
        borderRadius:3, padding:"5px 8px", color:T.textPri, fontSize:12,
        outline:"none", fontFamily:"inherit", ...style }}>
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

// ─── Stat Card ────────────────────────────────────────────────────────────────
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

// ─── Mock FieldRaven jobs ─────────────────────────────────────────────────────
const MOCK_FR_JOBS = [
  { id:"fr001", client:"Coastal Surveys Ltd", date:"2026-06-14", photos:48, gps:"49.5432, -124.8821", status:"ready", jobType:"360 Survey" },
  { id:"fr002", client:"Denman Island Trust", date:"2026-06-11", photos:32, gps:"49.5218, -124.7934", status:"ready", jobType:"360 Survey" },
  { id:"fr003", client:"BC Hydro Inspection", date:"2026-06-08", photos:64, gps:"49.6102, -124.9241", status:"processing", jobType:"Video Survey" },
  { id:"fr004", client:"Slow Ocean Internal", date:"2026-06-01", photos:24, gps:"49.5380, -124.8455", status:"complete", jobType:"360 Survey" },
];

// ─── Queue Panel ──────────────────────────────────────────────────────────────
function QueuePanel({ queue, setQueue, selected, setSelected }) {
  const addMock = (type) => {
    const names = {
      video:  ["site_walkthrough_01.mp4","panorama_beach.mov","survey_run_B.insv"],
      folder: ["images_equirect_01","raw_stills_set_A","beach_captures"],
    };
    const pick = names[type][Math.floor(Math.random()*3)];
    const item = { id:`${type}_${Date.now()}`, type, name:pick };
    setQueue(q=>[...q, item]);
    setSelected(item);
  };

  const groups = [
    { type:"fieldraven", label:"FieldRaven Jobs", items: queue.filter(i=>i.type==="fieldraven") },
    { type:"video",      label:"Video Queue",     items: queue.filter(i=>i.type==="video") },
    { type:"folder",     label:"Image Folders",   items: queue.filter(i=>i.type==="folder") },
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
                  </div>
                ))}
            </div>

            {type !== "fieldraven" && (
              <div style={{ display:"flex", gap:3 }}>
                <Btn small variant="ghost"
                  style={{ flex:1, fontSize:10, borderColor:`${cfg.color}44`, color:cfg.color }}
                  onClick={()=>addMock(type)}>
                  + Add
                </Btn>
                {items.length > 0 && (
                  <Btn small variant="ghost"
                    onClick={()=>{
                      if(selected && items.find(i=>i.id===selected.id)) setSelected(null);
                      setQueue(q=>q.filter(i=>i.type!==type));
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
          <div style={{ marginTop:4, fontSize:11, color:SOURCE_TYPES[selected.type].color,
            overflow:"hidden", textOverflow:"ellipsis" }}>
            {SOURCE_TYPES[selected.type].icon} {selected.name}
          </div>
          <div style={{ marginTop:6 }}>
            <Btn small variant="danger" full onClick={()=>{
              setQueue(q=>q.filter(i=>i.id!==selected.id));
              setSelected(null);
            }}>Remove</Btn>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── FieldRaven Tab ───────────────────────────────────────────────────────────
function FieldRavenTab({ queue, setQueue, setSelected, setActiveMainTab }) {
  const [filter, setFilter] = useState("all");
  const [expanded, setExpanded] = useState(null);

  const filtered = filter==="all" ? MOCK_FR_JOBS
    : MOCK_FR_JOBS.filter(j=>j.status===filter);

  const queueJob = (job) => {
    if (queue.find(i=>i.id===job.id)) return;
    const item = { id:job.id, type:"fieldraven", name:`${job.client} — ${job.date}`,
      client:job.client, photos:job.photos, gps:job.gps, jobType:job.jobType };
    setQueue(q=>[...q, item]);
    setSelected(item);
    setActiveMainTab(1); // jump to pipeline tab
  };

  const statusColor = s => s==="ready"?"#39e07a":s==="processing"?T.amber:s==="complete"?T.textDim:T.danger;
  const statusLabel = s => s==="ready"?"Ready to Queue":s==="processing"?"Processing":s==="complete"?"Done":s;

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:10, height:"100%", overflowY:"auto" }}>
      {/* Machine status */}
      <div style={{ display:"flex", alignItems:"center", gap:10, padding:"10px 12px",
        background:T.surface, borderRadius:5, border:`1px solid ${T.border}` }}>
        <div style={{ display:"flex", alignItems:"center", gap:6 }}>
          <Pill color={T.live} />
          <span style={{ fontSize:12, color:T.live, fontWeight:600 }}>Desktop registered</span>
        </div>
        <span style={{ fontSize:11, color:T.textDim, fontFamily:"monospace" }}>
          machine · denman-studio-01
        </span>
        <div style={{ marginLeft:"auto", fontSize:11, color:T.textDim }}>
          Firebase ·
          <span style={{ color:T.live }}> connected</span>
        </div>
      </div>

      {/* Stats */}
      <div style={{ display:"grid", gridTemplateColumns:"repeat(4,1fr)", gap:8 }}>
        <StatCard value={MOCK_FR_JOBS.length} label="Field Jobs" />
        <StatCard value={MOCK_FR_JOBS.filter(j=>j.status==="ready").length}
          label="Ready" color={T.live} />
        <StatCard value={MOCK_FR_JOBS.filter(j=>j.status==="processing").length}
          label="Processing" color={T.amber} />
        <StatCard value={MOCK_FR_JOBS.filter(j=>j.status==="complete").length}
          label="Complete" color={T.textDim} />
      </div>

      {/* Filter */}
      <div style={{ display:"flex", gap:4 }}>
        {["all","ready","processing","complete"].map(f=>(
          <button key={f} onClick={()=>setFilter(f)}
            style={{ padding:"5px 12px", borderRadius:3, fontSize:11, fontWeight:600,
              border:`1px solid ${filter===f?T.amber:T.border}`,
              background: filter===f ? `${T.amber}22` : "transparent",
              color: filter===f ? T.amber : T.textSec, cursor:"pointer", fontFamily:"inherit" }}>
            {f==="all"?"All":f.charAt(0).toUpperCase()+f.slice(1)}
          </button>
        ))}
      </div>

      {/* Job list */}
      <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
        {filtered.map(job=>(
          <div key={job.id}
            style={{ background:T.surface, border:`1px solid ${expanded===job.id?T.amber+"44":T.border}`,
              borderRadius:5, overflow:"hidden", transition:"border-color .2s" }}>
            {/* Header row */}
            <div style={{ display:"flex", alignItems:"center", padding:"10px 12px", gap:10 }}>
              <span style={{ fontSize:18 }}>🦅</span>
              <div style={{ flex:1 }}>
                <div style={{ fontSize:13, fontWeight:600, color:T.textPri }}>{job.client}</div>
                <div style={{ fontSize:11, color:T.textSec, marginTop:1 }}>
                  {job.date} · {job.photos} photos · {job.jobType}
                </div>
              </div>
              <Badge color={statusColor(job.status)}>{statusLabel(job.status)}</Badge>
              <Btn small variant="ghost" onClick={()=>setExpanded(expanded===job.id?null:job.id)}>
                {expanded===job.id?"▲":"▼"}
              </Btn>
            </div>

            {/* Expanded detail */}
            {expanded===job.id && (
              <div style={{ borderTop:`1px solid ${T.border}`, padding:"10px 12px",
                background:T.surfaceEl, display:"flex", flexDirection:"column", gap:8 }}>
                <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:8 }}>
                  <div>
                    <Label>GPS</Label>
                    <div style={{ fontSize:11, color:T.textSec, fontFamily:"monospace", marginTop:2 }}>
                      {job.gps}
                    </div>
                  </div>
                  <div>
                    <Label>Job Type</Label>
                    <div style={{ fontSize:11, color:T.textSec, marginTop:2 }}>{job.jobType}</div>
                  </div>
                  <div>
                    <Label>Photo Files</Label>
                    <div style={{ fontSize:11, color:T.textSec, fontFamily:"monospace", marginTop:2 }}>
                      {job.photos}× .insp files
                    </div>
                  </div>
                  <div>
                    <Label>Camera Import</Label>
                    <div style={{ fontSize:11, color: job.status==="ready"?T.live:T.textDim, marginTop:2 }}>
                      {job.status==="ready"?"Files matched":"Pending"}
                    </div>
                  </div>
                </div>

                {/* Sample filenames */}
                <div style={{ background:T.void, borderRadius:3, padding:"6px 8px",
                  fontFamily:"monospace", fontSize:10, color:T.textDim }}>
                  {["IMG_20260614_091203_00_001.insp",
                    "IMG_20260614_091245_00_002.insp",
                    `...and ${job.photos-2} more`].map((f,i)=>(
                    <div key={i} style={{ color: i===2?T.textDim:T.textSec }}>{f}</div>
                  ))}
                </div>

                <div style={{ display:"flex", gap:8, justifyContent:"flex-end" }}>
                  <Btn small variant="ghost">View on Map</Btn>
                  <Btn small variant="live"
                    disabled={job.status==="complete"||queue.find(i=>i.id===job.id)}
                    onClick={()=>queueJob(job)}>
                    {queue.find(i=>i.id===job.id) ? "✓ In Queue" : "Send to Pipeline →"}
                  </Btn>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Frame & View Extraction Tab ──────────────────────────────────────────────
function ExtractionTab({ selected, settings, setSettings }) {
  const [frames, setFrames] = useState([]);
  const [currentFrame, setCurrentFrame] = useState(0);
  const canvasRef = useRef();
  const isFolder = selected?.type === "folder" || selected?.type === "fieldraven";

  useEffect(()=>{
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0,0,W,H);

    if (!selected) {
      ctx.fillStyle = T.surfaceEl; ctx.fillRect(0,0,W,H);
      ctx.fillStyle = T.textDim; ctx.font="12px monospace";
      ctx.textAlign="center";
      ctx.fillText("Select an item from the queue to preview", W/2, H/2);
      return;
    }

    // Background gradient — equirectangular sky/ground split
    const sky = ctx.createLinearGradient(0,0,0,H);
    sky.addColorStop(0,"#0a1428"); sky.addColorStop(.5,"#142040"); sky.addColorStop(1,"#0e1828");
    ctx.fillStyle = sky; ctx.fillRect(0,0,W,H);

    // Horizon
    ctx.strokeStyle=`${T.amber}33`; ctx.lineWidth=1; ctx.setLineDash([3,6]);
    ctx.beginPath(); ctx.moveTo(0,H/2); ctx.lineTo(W,H/2); ctx.stroke();
    ctx.setLineDash([]);

    // Pitch/yaw overlays
    const pitches = settings.pitchAngles.split(",").map(Number).filter(n=>!isNaN(n));
    const yaw = Math.max(1,parseInt(settings.yawSteps)||6);
    const fov = parseFloat(settings.fov)||94.6;
    const colors = [T.frColor, T.vidColor, T.imgColor, T.amber, "#ff66aa", "#ffaa33","#66ddff","#aa66ff"];
    const rW = W/yaw, rH = (fov/180)*H;

    pitches.forEach((pitch,pi)=>{
      const cy = H/2 - (pitch/90)*(H/2);
      for(let y=0;y<yaw;y++){
        const x=(y/yaw)*W;
        const c=colors[(pi*yaw+y)%colors.length];
        ctx.globalAlpha=settings.overlayOpacity*0.18;
        ctx.fillStyle=c; ctx.fillRect(x,cy-rH/2,rW,rH);
        ctx.globalAlpha=settings.overlayOpacity;
        ctx.strokeStyle=c; ctx.lineWidth=1.5;
        ctx.strokeRect(x+.5,cy-rH/2+.5,rW-1,rH-1);
        ctx.globalAlpha=1;
      }
    });

    // Source label
    const cfg = SOURCE_TYPES[selected.type];
    ctx.fillStyle=cfg.color; ctx.font="bold 10px monospace"; ctx.textAlign="left";
    ctx.fillText(`${cfg.icon} ${selected.name}`, 8, 14);
  }, [selected, settings, frames.length]);

  const doExtract = () => {
    if (!selected) return;
    const count = parseInt(settings.frameCount)||30;
    setFrames(Array.from({length:count},(_,i)=>i));
    setCurrentFrame(0);
  };

  return (
    <div style={{ display:"grid", gridTemplateColumns:"1fr 252px", gap:10, height:"100%", overflow:"hidden" }}>
      {/* Left */}
      <div style={{ display:"flex", flexDirection:"column", gap:8, overflow:"hidden" }}>
        <canvas ref={canvasRef} width={680} height={190}
          style={{ width:"100%", height:190, borderRadius:4, border:`1px solid ${T.border}`,
            background:T.void, flexShrink:0 }} />

        {/* Gallery */}
        <div style={{ flex:1, background:T.void, border:`1px solid ${T.border}`, borderRadius:4,
          padding:6, overflowY:"auto" }}>
          {frames.length===0
            ? <div style={{ color:T.textDim, fontSize:11, textAlign:"center", padding:20 }}>
                {isFolder ? "Images loaded — ready for view extraction"
                  : "Extract frames to populate gallery"}
              </div>
            : <div style={{ display:"flex", flexWrap:"wrap", gap:3 }}>
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
              </div>}
        </div>

        {/* Preview label */}
        <div style={{ background:T.surface, border:`1px solid ${T.border}`, borderRadius:4,
          padding:"6px 10px", fontSize:11, color:T.textSec, flexShrink:0 }}>
          <span style={{ color:T.amber }}>360° Extracted View Preview</span>
          {frames.length>0 && <span style={{ marginLeft:8, color:T.textDim, fontFamily:"monospace" }}>
            frame {String(currentFrame+1).padStart(3,"0")}/{frames.length}
          </span>}
        </div>
      </div>

      {/* Right: settings */}
      <div style={{ overflowY:"auto", display:"flex", flexDirection:"column", gap:0 }}>

        {/* Frame extraction — only for video */}
        <Accordion title="Frame Extraction"
          accent={isFolder ? T.textDim : T.amber}
          defaultOpen={!isFolder}>
          {isFolder ? (
            <div style={{ fontSize:11, color:T.textDim, fontStyle:"italic" }}>
              {selected?.type==="fieldraven"
                ? "FieldRaven jobs use pre-stitched equirectangular JPEGs — frame extraction not needed."
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

        <Accordion title="360° View Settings" accent={T.amber}>
          <FieldRow label="Pitch Angles">
            <Input value={settings.pitchAngles}
              onChange={v=>setSettings(s=>({...s,pitchAngles:v}))}
              placeholder="-50, -7" />
          </FieldRow>
          <FieldRow label="Yaw Steps">
            <Input type="number" value={settings.yawSteps}
              onChange={v=>setSettings(s=>({...s,yawSteps:v}))} />
          </FieldRow>
          <FieldRow label="Field of View">
            <Input type="number" value={settings.fov}
              onChange={v=>setSettings(s=>({...s,fov:v}))} />
          </FieldRow>
        </Accordion>

        <Accordion title="Overlay" defaultOpen={false}>
          <FieldRow label="Opacity">
            <div style={{ display:"flex", alignItems:"center", gap:8 }}>
              <input type="range" min={0} max={1} step={.05} value={settings.overlayOpacity}
                onChange={e=>setSettings(s=>({...s,overlayOpacity:+e.target.value}))}
                style={{ flex:1, accentColor:T.amber }} />
              <span style={{ fontSize:10, color:T.textDim, width:28 }}>
                {Math.round(settings.overlayOpacity*100)}%
              </span>
            </div>
          </FieldRow>
        </Accordion>

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

        <div style={{ marginTop:6 }}>
          <Btn onClick={doExtract}
            disabled={!selected || (isFolder && frames.length>0)}
            full variant={isFolder?"subtle":"primary"}>
            {isFolder ? "✓ Images Ready" : "Extract Frames"}
          </Btn>
        </div>
      </div>
    </div>
  );
}

// ─── Alignment Tab ────────────────────────────────────────────────────────────
function AlignmentTab({ settings, setSettings }) {
  const { skipRS, runVggt, runPostshot, runBrush } = settings;
  const plan = skipRS
    ? runVggt
      ? `VGGT → COLMAP → Training: ${[runPostshot&&"Postshot",runBrush&&"Brush"].filter(Boolean).join(", ")||"None"}`
      : runPostshot ? "Direct Postshot (handles alignment internally)" : "⚠️ No method selected"
    : [runPostshot&&"Postshot export",runBrush&&"Brush export"].filter(Boolean).join(" + ") || "RealityScan align only";

  return (
    <div style={{ overflowY:"auto", height:"100%" }}>
      <Accordion title="Alignment Method" accent={T.amber}>
        <Toggle checked={skipRS} label="Skip RealityScan"
          onChange={v=>setSettings(s=>({...s,skipRS:v,runBrush:v&&!s.runVggt?false:s.runBrush}))} />
        {skipRS && (
          <div style={{ marginTop:10, paddingLeft:10, borderLeft:`2px solid ${T.border}` }}>
            <Toggle checked={runVggt} label="Use VGGT for camera pose estimation"
              onChange={v=>setSettings(s=>({...s,runVggt:v,runBrush:!v?false:s.runBrush}))} />
            {runVggt && (
              <div style={{ marginTop:10 }}>
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
                      ["vggtTemporal","Temporal Sequencing"]].map(([k,l])=>(
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
          </div>
        )}
      </Accordion>

      <Accordion title="Training" accent={T.amber}>
        <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
          <Toggle checked={runPostshot} label="Run Postshot Training"
            onChange={v=>setSettings(s=>({...s,runPostshot:v}))} />
          <Toggle checked={runBrush} label="Run Brush Training"
            disabled={skipRS&&!runVggt}
            onChange={v=>setSettings(s=>({...s,runBrush:v}))} />
        </div>
      </Accordion>

      <Accordion title="Pipeline Plan" accent={T.live}>
        <div style={{ fontFamily:"monospace", fontSize:11, color:T.textSec, lineHeight:1.7 }}>
          {plan}
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
function ConfigTab({ settings, setSettings }) {
  const S = k => ({ value:settings[k], onChange:v=>setSettings(s=>({...s,[k]:v})) });
  const paths = [
    ["ffmpeg","FFmpeg Executable"],["rs","RealityScan Executable"],
    ["postshot","Postshot CLI"],["brush","Brush CLI"],
    ["rsSettings","RS Settings Folder"],["vggt","VGGT Project"],["vggtModel","VGGT Model"],
  ];
  return (
    <div style={{ overflowY:"auto", height:"100%" }}>
      <SectionHead>Dependency Paths</SectionHead>
      {paths.map(([k,l])=>(
        <FieldRow key={k} label={l}>
          <div style={{ display:"flex", gap:4 }}>
            <Input {...S(k)} placeholder={`Path to ${l.toLowerCase()}`} />
            <Btn small variant="ghost">…</Btn>
          </div>
        </FieldRow>
      ))}
      <div style={{ marginTop:12 }}>
        <SectionHead>Machine</SectionHead>
        <FieldRow label="Machine Name">
          <Input value={settings.machineName} onChange={v=>setSettings(s=>({...s,machineName:v}))} />
        </FieldRow>
      </div>
      <div style={{ marginTop:12 }}>
        <Btn onClick={()=>{}}>Save Configuration</Btn>
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
    :/warn/i.test(line)?T.amber:/success|complete|✅/i.test(line)?T.live
    :/🎬|extracting|processing/i.test(line)?"#ffee55":T.textSec;
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

// ─── Pipeline Tab wrapper (contains sub-tabs) ─────────────────────────────────
const PIPE_TABS = [
  "Frame & View Extraction","Alignment","Postshot","Brush","Configuration"
];

function PipelineTab({ queue, setQueue, selected, setSelected, settings, setSettings }) {
  const [pipeTab, setPipeTab] = useState(0);
  return (
    <div style={{ display:"flex", height:"100%", overflow:"hidden", gap:10 }}>
      {/* Input queue */}
      <QueuePanel queue={queue} setQueue={setQueue} selected={selected} setSelected={setSelected} />

      {/* Main pipeline content */}
      <div style={{ flex:1, display:"flex", flexDirection:"column", overflow:"hidden" }}>
        {/* Sub-tab bar */}
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

        {/* Sub-tab content */}
        <div style={{ flex:1, background:T.surfaceHi, border:`1px solid ${T.border}`,
          borderRadius:"0 4px 4px 4px", padding:10, overflow:"hidden",
          display:"flex", flexDirection:"column" }}>
          {pipeTab===0 && <ExtractionTab selected={selected} settings={settings} setSettings={setSettings} />}
          {pipeTab===1 && <AlignmentTab settings={settings} setSettings={setSettings} />}
          {pipeTab===2 && <PostshotTab settings={settings} setSettings={setSettings} />}
          {pipeTab===3 && <BrushTab settings={settings} setSettings={setSettings} />}
          {pipeTab===4 && <ConfigTab settings={settings} setSettings={setSettings} />}
        </div>
      </div>
    </div>
  );
}

// ─── Active Job Tab ───────────────────────────────────────────────────────────
function ActiveJobTab({ processing, progress, batchProgress, logs, queue }) {
  const stages = ["Import","Stitch","Extract","Align","Train","Output"];
  const activeStage = Math.floor((progress/100)*stages.length);

  if (!processing && progress===0) return (
    <div style={{ display:"flex", alignItems:"center", justifyContent:"center",
      height:"100%", color:T.textDim, fontSize:13 }}>
      No active job — run the pipeline from the Pipeline tab
    </div>
  );

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:12 }}>
      <div style={{ display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:8 }}>
        <StatCard value={`${progress}%`} label="Current Job" color={T.amber} />
        <StatCard value={`${queue.length}`} label="Queue Depth" />
        <StatCard value={processing?"Running":"Idle"} label="Status"
          color={processing?T.live:T.textDim} />
      </div>

      {/* Stage stepper */}
      <div style={{ background:T.surface, border:`1px solid ${T.border}`, borderRadius:5, padding:12 }}>
        <Label style={{ marginBottom:10, display:"block" }}>Pipeline Stages</Label>
        <div style={{ display:"flex", alignItems:"center" }}>
          {stages.map((s,i)=>(
            <div key={i} style={{ display:"flex", alignItems:"center", flex: i<stages.length-1?1:"auto" }}>
              <div style={{ display:"flex", flexDirection:"column", alignItems:"center", gap:4 }}>
                <div style={{ width:10, height:10, borderRadius:"50%",
                  background: i<activeStage?T.live:i===activeStage&&processing?T.amber:T.surfaceEl,
                  border:`2px solid ${i<activeStage?T.live:i===activeStage&&processing?T.amber:T.border}`,
                  boxShadow: i===activeStage&&processing?`0 0 8px ${T.amber}88`:"none",
                  transition:"all .3s" }} />
                <span style={{ fontSize:9, color: i<activeStage?T.live:i===activeStage?T.amber:T.textDim,
                  fontWeight: i===activeStage?700:400, whiteSpace:"nowrap" }}>
                  {s}
                </span>
              </div>
              {i<stages.length-1 && (
                <div style={{ flex:1, height:1, background: i<activeStage?T.live:T.border,
                  margin:"0 4px", marginBottom:14, transition:"background .3s" }} />
              )}
            </div>
          ))}
        </div>
      </div>

      <ProgressBar value={progress} label="Current" color={T.amber} />
      {queue.length>1 && <ProgressBar value={batchProgress} label="Batch" color={T.live} />}

      {/* Console inline */}
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
function HistoryTab() {
  const items = [
    { id:1, name:"Coastal Surveys Ltd — 2026-06-10", type:"fieldraven", status:"complete", duration:"42m", output:"splat3" },
    { id:2, name:"site_walkthrough_01.mp4",          type:"video",      status:"complete", duration:"18m", output:"mcmc" },
    { id:3, name:"images_equirect_01",               type:"folder",     status:"error",    duration:"—",   output:"—" },
  ];
  return (
    <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
      {items.map(it=>{
        const cfg = SOURCE_TYPES[it.type];
        const sc = it.status==="complete"?T.live:it.status==="error"?T.danger:T.amber;
        return (
          <div key={it.id} style={{ display:"flex", alignItems:"center", gap:10, padding:"10px 12px",
            background:T.surface, border:`1px solid ${T.border}`, borderRadius:5 }}>
            <span style={{ fontSize:16 }}>{cfg.icon}</span>
            <div style={{ flex:1 }}>
              <div style={{ fontSize:12, fontWeight:600, color:T.textPri }}>{it.name}</div>
              <div style={{ fontSize:10, color:T.textSec, marginTop:2, fontFamily:"monospace" }}>
                {it.duration} · {it.output}
              </div>
            </div>
            <Badge color={sc}>{it.status}</Badge>
            {it.status==="complete" && <Btn small variant="ghost">View</Btn>}
          </div>
        );
      })}
    </div>
  );
}

// ─── Defaults ─────────────────────────────────────────────────────────────────
const defaultSettings = {
  // extraction
  extractionMethod:"interval", intervalValue:1, intervalUnit:"seconds",
  frameCount:30, frameFormat:"jpg",
  pitchAngles:"-50, -7", yawSteps:"6", fov:"94.6", overlayOpacity:0.6,
  // alignment
  skipRS:false, runVggt:false, runPostshot:true, runBrush:false,
  vggtConf:50, vggtSky:32, vggtMaskSky:true, vggtShowCam:true, vggtTemporal:true,
  vggtMode:"depthmap",
  // postshot
  postshotProfile:"Splat MCMC", postshotMaxSize:3840, postshotSteps:30,
  postshotMaxSplats:1000, postshotAA:true, postshotError:false,
  postshotContext:false, postshotPly:false, postshotAlpha:false, postshotSky:false,
  // brush
  brushSteps:30000, brushSplats:5000000, brushRes:1920, brushSeed:42,
  brushRerun:false, brushViewer:false,
  // config
  ffmpeg:"", rs:"", postshot:"", brush:"", rsSettings:"", vggt:"", vggtModel:"",
  machineName:"denman-studio-01",
  // global
  projectDir:"",
};

// ─── Main tabs ────────────────────────────────────────────────────────────────
const MAIN_TABS = [
  { id:"fieldraven", label:"🦅 FieldRaven",  color:T.frColor },
  { id:"pipeline",   label:"⚙ Pipeline",    color:T.amber },
  { id:"active",     label:"▶ Active Job",  color:T.live },
  { id:"history",    label:"◷ History",     color:T.textSec },
];

// ─── Root ─────────────────────────────────────────────────────────────────────
export default function FieldRavenDesktop() {
  const [activeMainTab, setActiveMainTab] = useState(0);
  const [settings, setSettings] = useState(defaultSettings);
  const [queue, setQueue] = useState([]);
  const [selected, setSelected] = useState(null);
  const [processing, setProcessing] = useState(false);
  const [progress, setProgress] = useState(0);
  const [batchProgress, setBatchProgress] = useState(0);
  const [statusMsg, setStatusMsg] = useState("Ready · GPU enabled ✅");
  const [consoleVisible, setConsoleVisible] = useState(true);
  const [logs, setLogs] = useState(["🦅 FieldRaven Desktop — Ready"]);

  const addLog = m => setLogs(l=>[...l.slice(-200), m]);

  const runPipeline = () => {
    if (!queue.length) return;
    setProcessing(true); setProgress(0); setBatchProgress(0);
    setStatusMsg("🚀 Pipeline running...");
    addLog(`🚀 Starting batch · ${queue.length} job(s)`);
    let p=0;
    const iv = setInterval(()=>{
      p+=2; setProgress(Math.min(p,100));
      if(p%20===0){
        setBatchProgress(Math.min(Math.round(p),100));
        addLog(p<20?"📥 Importing camera files..."
          :p<40?"🧵 Stitching .insp → equirectangular..."
          :p<60?"🎬 Extracting 360° views..."
          :p<80?"📐 Running VGGT alignment..."
          :"🧠 Training Gaussian splats...");
        setStatusMsg(`Processing ${p}%`);
      }
      if(p>=100){ clearInterval(iv); setProcessing(false);
        setStatusMsg("✅ Complete"); addLog("✅ Pipeline complete"); setBatchProgress(100);
        setActiveMainTab(3);
      }
    },120);
  };

  const currentTab = MAIN_TABS[activeMainTab];

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100vh",
      background:T.base, color:T.textPri,
      fontFamily:"-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
      fontSize:13, overflow:"hidden" }}>

      {/* ── Top bar ── */}
      <div style={{ display:"flex", alignItems:"center", gap:10, padding:"7px 14px",
        background:T.void, borderBottom:`1px solid ${T.border}`, flexShrink:0 }}>

        {/* Brand */}
        <div style={{ display:"flex", alignItems:"baseline", gap:6, marginRight:8 }}>
          <span style={{ fontSize:15, fontWeight:800, letterSpacing:".8px", color:T.amber }}>
            FIELDRAVEN
          </span>
          <span style={{ fontSize:9, color:T.textDim, letterSpacing:".4px",
            textTransform:"uppercase" }}>desktop</span>
        </div>

        {/* Output dir */}
        <div style={{ display:"flex", alignItems:"center", gap:5, flex:1 }}>
          <Label>Output</Label>
          <Input value={settings.projectDir}
            onChange={v=>setSettings(s=>({...s,projectDir:v}))}
            placeholder="Select output directory…"
            style={{ maxWidth:340, fontSize:11 }} />
          <Btn small variant="ghost">…</Btn>
        </div>

        {/* Actions */}
        <Btn onClick={runPipeline} disabled={!queue.length||processing} variant="live">
          ▶ Run Pipeline
        </Btn>
        <Btn disabled={!processing} variant="danger">✕ Cancel</Btn>
        <Btn small variant="ghost" onClick={()=>setConsoleVisible(v=>!v)}>
          {consoleVisible?"Hide Log":"Log"}
        </Btn>

        {/* Machine pill */}
        <div style={{ display:"flex", alignItems:"center", gap:5, padding:"3px 8px",
          background:T.surface, borderRadius:3, border:`1px solid ${T.border}` }}>
          <Pill color={T.live} />
          <span style={{ fontSize:10, color:T.textDim, fontFamily:"monospace" }}>
            {settings.machineName}
          </span>
        </div>
      </div>

      {/* ── Main tabs ── */}
      <div style={{ display:"flex", gap:1, padding:"0 14px",
        background:T.void, borderBottom:`1px solid ${T.border}`, flexShrink:0 }}>
        {MAIN_TABS.map((t,i)=>(
          <div key={t.id} onClick={()=>setActiveMainTab(i)}
            style={{ padding:"8px 16px", fontSize:12, fontWeight:600, cursor:"pointer",
              color: activeMainTab===i ? t.color : T.textDim,
              borderBottom:`2px solid ${activeMainTab===i?t.color:"transparent"}`,
              transition:"all .15s", whiteSpace:"nowrap" }}>
            {t.label}
          </div>
        ))}
        {queue.length>0 && (
          <div style={{ marginLeft:"auto", display:"flex", alignItems:"center",
            padding:"0 8px", gap:6 }}>
            <Label>Queue</Label>
            {queue.map(it=>(
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
          <FieldRavenTab queue={queue} setQueue={setQueue} setSelected={setSelected}
            setActiveMainTab={setActiveMainTab} />
        )}
        {activeMainTab===1 && (
          <PipelineTab queue={queue} setQueue={setQueue} selected={selected} setSelected={setSelected}
            settings={settings} setSettings={setSettings} />
        )}
        {activeMainTab===2 && (
          <ActiveJobTab processing={processing} progress={progress}
            batchProgress={batchProgress} logs={logs} queue={queue} />
        )}
        {activeMainTab===3 && <HistoryTab />}
      </div>

      {/* ── Status bar ── */}
      <div style={{ display:"flex", alignItems:"center", gap:12, padding:"4px 14px",
        background:T.void, borderTop:`1px solid ${T.border}`, flexShrink:0 }}>
        <span style={{ flex:1, fontSize:11, color:T.textSec }}>{statusMsg}</span>
        {processing && queue.length>1 && (
          <ProgressBar value={batchProgress} label="Batch" color={T.live} style={{ width:180 }} />
        )}
        {processing && (
          <ProgressBar value={progress} label="Current" style={{ width:180 }} />
        )}
      </div>

      {/* ── Console ── */}
      <Console logs={logs} visible={consoleVisible} />
    </div>
  );
}
