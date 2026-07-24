'use strict';
const { app, BrowserWindow } = require('electron');
const { spawn }              = require('child_process');
const path                   = require('path');
const http                   = require('http');

const PORT    = 8081;
const PYTHON  = 'C:\\Users\\DenmanNic\\AppData\\Local\\Programs\\Python\\Python313\\python.exe';
const PROJECT = app.isPackaged
  ? path.join(process.env.USERPROFILE, 'Projects', 'FieldRaven_desktop')
  : path.join(__dirname, '..', '..');

let win           = null;
let pythonProcess = null;

// ── Server readiness ────────────────────────────────────────────────────────

function serverReady(maxAttempts = 40) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const check = () => {
      const req = http.get(`http://localhost:${PORT}/api/health`, res => {
        res.resume();
        if (res.statusCode < 400) return resolve();
        retry();
      });
      req.on('error', retry);
      req.setTimeout(500, () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (++attempts >= maxAttempts) {
        return reject(new Error('FieldRaven server did not start in time'));
      }
      setTimeout(check, 500);
    };
    check();
  });
}

// ── Python process ──────────────────────────────────────────────────────────

async function ensurePythonRunning() {
  // If a server is already up (e.g. launched manually), skip spawning
  try { await serverReady(1); return; } catch {}

  console.log('[electron] Starting Python server…');
  pythonProcess = spawn(
    PYTHON,
    ['-X', 'utf8', 'main.py', '--no-browser'],
    {
      cwd: PROJECT,
      windowsHide: true,   // no separate console window
      stdio: 'pipe',
    }
  );

  pythonProcess.stdout.on('data', d => process.stdout.write(d));
  pythonProcess.stderr.on('data', d => process.stderr.write(d));
  pythonProcess.on('exit', code => {
    console.log(`[electron] Python exited (code ${code})`);
    pythonProcess = null;
  });

  await serverReady(40);   // wait up to 20 s (40 × 500 ms)
  console.log('[electron] Server ready');
}

// ── Window ──────────────────────────────────────────────────────────────────

function createWindow() {
  win = new BrowserWindow({
    width:          1440,
    height:         900,
    minWidth:       900,
    minHeight:      600,
    title:          'FieldRaven Desktop',
    autoHideMenuBar: true,
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
    },
  });

  win.loadURL(`http://localhost:${PORT}`);

  // Uncomment to open DevTools on launch:
  // win.webContents.openDevTools();

  win.on('closed', () => { win = null; });
}

// ── App lifecycle ───────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  try {
    await ensurePythonRunning();
    createWindow();
  } catch (err) {
    console.error('[electron] Startup failed:', err.message);
    app.quit();
  }
});

app.on('window-all-closed', () => {
  if (pythonProcess) {
    console.log('[electron] Killing Python server…');
    pythonProcess.kill();
    pythonProcess = null;
  }
  app.quit();
});

// macOS: re-create window when dock icon is clicked and no windows are open
app.on('activate', () => {
  if (win === null) createWindow();
});
