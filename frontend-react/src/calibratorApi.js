// Fetch wrapper for the embedded Lens Calibrator (backend/lens_calibrator.py).
// Same-origin, no auth — mirrors the standalone lens-calibrator tool's own
// frontend/src/api.js, plus the named-profile endpoints added when it was
// folded into this app.
const BASE = '/api/calibrator';

async function postJSON(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return res.json();
}

async function postForm(path, fields) {
  const form = new FormData();
  Object.entries(fields).forEach(([k, v]) => form.append(k, v));
  const res = await fetch(`${BASE}${path}`, { method: 'POST', body: form });
  return res.json();
}

async function getJSON(path) {
  const res = await fetch(`${BASE}${path}`);
  return res.json();
}

async function del(path) {
  const res = await fetch(`${BASE}${path}`, { method: 'DELETE' });
  return res.json();
}

export const calibratorApi = {
  setBoard:          (profile) => postJSON('/board', profile),
  getBoard:          () => getJSON('/board'),
  scanImages:        (folder) => postForm('/images/scan', { folder }),
  runDetection:      () => postJSON('/detect', {}),
  toggleImage:       (filename, excluded) => postForm('/images/toggle', { filename, excluded }),
  runCalibration:    () => postJSON('/calibrate', {}),
  undistortPreview:  (filename) => postForm('/undistort', { filename }),
  exportCalibration: () => getJSON('/export'),
  getState:          () => getJSON('/state'),
  overlayUrl:        (filename) => `${BASE}/overlay/${filename}`,
  generateBoard:     (dpi) => postForm('/board/generate', { dpi }),
  boardDownloadUrl:  () => `${BASE}/board/download`,
  // Named profiles — used by the colmap_fisheye pipeline mode to reference
  // a saved front/back lens calibration by name.
  saveProfile:       (name) => postJSON('/profiles/save', { name }),
  listProfiles:      () => getJSON('/profiles'),
  getProfile:        (name) => getJSON(`/profiles/${encodeURIComponent(name)}`),
  deleteProfile:     (name) => del(`/profiles/${encodeURIComponent(name)}`),
};
