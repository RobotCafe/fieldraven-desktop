"""
Convert a 3DGS .ply file to the compact .splat binary format (32 bytes/Gaussian).

Format layout per Gaussian:
  bytes  0-11: xyz position      (3 × float32)
  bytes 12-23: scale sx/sy/sz    (3 × float32, exp-transformed)
  bytes 24-27: color rgba        (4 × uint8)
  bytes 28-31: rotation quat     (4 × uint8, normalized [-1,1]→[0,255])
"""

import numpy as np
from pathlib import Path

SH_C0 = 0.28209479177387814


def find_latest_ply(training_dir: str) -> Path | None:
    """Return the highest-step .ply in 04_training/, or None."""
    import re
    d = Path(training_dir)
    if not d.exists():
        return None
    candidates = list(d.glob("*.ply"))
    if not candidates:
        return None
    # Prefer the one with the highest step number in filename
    def _step(p: Path) -> int:
        m = re.search(r'(\d+)', p.name)
        return int(m.group(1)) if m else 0
    return max(candidates, key=_step)


def convert_ply_to_splat(ply_path: str | Path, splat_path: str | Path) -> int:
    """
    Convert a 3DGS .ply file to .splat format.
    Returns the number of Gaussians written.
    Raises on missing properties or I/O errors.
    """
    from plyfile import PlyData

    ply_path = Path(ply_path)
    splat_path = Path(splat_path)

    print(f"  [ply->splat] Reading {ply_path.name} ({ply_path.stat().st_size / 1e9:.2f} GB)...")
    plydata = PlyData.read(str(ply_path))
    verts = plydata['vertex']
    n = len(verts)
    print(f"  [ply->splat] {n:,} Gaussians -- converting...")

    # ── Positions ────────────────────────────────────────────────
    xyz = np.column_stack([
        verts['x'].astype(np.float32),
        verts['y'].astype(np.float32),
        verts['z'].astype(np.float32),
    ])

    # ── Scale (exp transform) ─────────────────────────────────────
    scales = np.exp(np.column_stack([
        verts['scale_0'].astype(np.float32),
        verts['scale_1'].astype(np.float32),
        verts['scale_2'].astype(np.float32),
    ]))

    # ── Color: SH DC → RGB uint8 ──────────────────────────────────
    r = np.clip((0.5 + SH_C0 * verts['f_dc_0']) * 255, 0, 255).astype(np.uint8)
    g = np.clip((0.5 + SH_C0 * verts['f_dc_1']) * 255, 0, 255).astype(np.uint8)
    b = np.clip((0.5 + SH_C0 * verts['f_dc_2']) * 255, 0, 255).astype(np.uint8)

    # ── Alpha: sigmoid(opacity) → uint8 ──────────────────────────
    a = np.clip(
        1.0 / (1.0 + np.exp(-verts['opacity'].astype(np.float32))) * 255,
        0, 255,
    ).astype(np.uint8)

    # ── Rotation quaternion → uint8 [0,255] ───────────────────────
    quats = np.column_stack([
        verts['rot_0'].astype(np.float32),
        verts['rot_1'].astype(np.float32),
        verts['rot_2'].astype(np.float32),
        verts['rot_3'].astype(np.float32),
    ])
    norms = np.linalg.norm(quats, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    quats /= norms
    rot8 = np.clip((quats + 1.0) * 127.5, 0, 255).astype(np.uint8)

    # ── Sort by alpha descending (streaming viewers render most-visible first) ─
    order = np.argsort(-a)

    # ── Pack into (n, 32) uint8 buffer ────────────────────────────
    buf = np.zeros((n, 32), dtype=np.uint8)

    xyz_s  = xyz[order].astype(np.float32)
    scl_s  = scales[order].astype(np.float32)
    buf[:, 0:12]  = xyz_s.view(np.uint8).reshape(n, 12)
    buf[:, 12:24] = scl_s.view(np.uint8).reshape(n, 12)
    buf[:, 24] = r[order]
    buf[:, 25] = g[order]
    buf[:, 26] = b[order]
    buf[:, 27] = a[order]
    buf[:, 28] = rot8[order, 0]
    buf[:, 29] = rot8[order, 1]
    buf[:, 30] = rot8[order, 2]
    buf[:, 31] = rot8[order, 3]

    splat_path.write_bytes(buf.tobytes())
    size_mb = splat_path.stat().st_size / (1024 * 1024)
    print(f"  [ply->splat] {n:,} Gaussians -> {splat_path.name} ({size_mb:.0f} MB)")
    return n
