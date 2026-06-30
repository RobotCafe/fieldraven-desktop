"""
verify_rs_rig.py — check whether RealityScan used the XMP rig priors.

Reads the COLMAP export in COLMAP_for_Brush/ and answers two questions:

  1. ZERO-BASELINE CHECK: In a rig-constrained reconstruction all sensors within
     one rig frame share the SAME optical centre.  We detect this by looking for
     groups of cameras that are very close together (near-zero pairwise distance).
     In pure independent SfM every camera has a distinct world position.

  2. FOCAL-LENGTH CONSISTENCY: A rig-constrained reconstruction should use a
     single calibrated focal length for all sensors (they are the same physical
     lens).  RS used SIMPLE_RADIAL with a per-camera focal length; wide variation
     within what should be a single rig is another indicator of independent SfM.

Usage:
    python tools/verify_rs_rig.py <path_to_COLMAP_for_Brush>
"""
import sys
import math
from pathlib import Path

COLMAP_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path(r"C:\Users\DenmanNic\Desktop\FolderTest\03_alignment\COLMAP_for_Brush")


def quat_to_rot(qw, qx, qy, qz):
    """Quaternion -> 3×3 rotation matrix (row-major list)."""
    return [
        [1-2*(qy*qy+qz*qz),  2*(qx*qy-qz*qw),  2*(qx*qz+qy*qw)],
        [2*(qx*qy+qz*qw),  1-2*(qx*qx+qz*qz),  2*(qy*qz-qx*qw)],
        [2*(qx*qz-qy*qw),  2*(qy*qz+qx*qw),  1-2*(qx*qx+qy*qy)],
    ]


def mat_vec(R, t):
    """Camera centre = -R^T t."""
    cx = -(R[0][0]*t[0] + R[1][0]*t[1] + R[2][0]*t[2])
    cy = -(R[0][1]*t[0] + R[1][1]*t[1] + R[2][1]*t[2])
    cz = -(R[0][2]*t[0] + R[1][2]*t[1] + R[2][2]*t[2])
    return (cx, cy, cz)


def dist(a, b):
    return math.sqrt(sum((x-y)**2 for x, y in zip(a, b)))


# ── Parse Images.txt ─────────────────────────────────────────────────────────
images_txt = COLMAP_DIR / "Images.txt"
cameras_txt = COLMAP_DIR / "Cameras.txt"

for p in (images_txt, cameras_txt):
    if not p.exists():
        # Try capitalised names (RS exports with capital first letter)
        cap = p.parent / p.name.capitalize()
        if not cap.exists():
            sys.exit(f"Cannot find {p}")

# normalise paths
def find(name):
    p = COLMAP_DIR / name
    if p.exists(): return p
    c = COLMAP_DIR / name.capitalize()
    if c.exists(): return c
    # case-insensitive search
    for f in COLMAP_DIR.iterdir():
        if f.name.lower() == name.lower(): return f
    return None

images_path  = find("images.txt")
cameras_path = find("cameras.txt")

# Read focal lengths per camera_id
focal_by_cam = {}
with open(cameras_path, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line or line.startswith("#"): continue
        parts = line.split()
        cam_id = int(parts[0])
        focal  = float(parts[4])  # SIMPLE_RADIAL: w h f cx cy k
        focal_by_cam[cam_id] = focal

# Read image poses
centres  = []       # list of (name, centre_xyz, focal)
skip_pts = False    # every second non-comment line is keypoints — skip those
with open(images_path, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line or line.startswith("#"):
            skip_pts = False
            continue
        if skip_pts:
            skip_pts = False
            continue
        skip_pts = True
        parts = line.split()
        if len(parts) < 10: continue
        img_id, qw, qx, qy, qz, tx, ty, tz, cam_id, name = \
            int(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]), \
            float(parts[4]), float(parts[5]), float(parts[6]), float(parts[7]), \
            int(parts[8]), parts[9]
        R   = quat_to_rot(qw, qx, qy, qz)
        ctr = mat_vec(R, (tx, ty, tz))
        centres.append((name, ctr, focal_by_cam.get(cam_id, 0.0)))

print(f"\n=== RS XMP Rig Verification ===")
print(f"Registered cameras : {len(centres)}")
print(f"Expected (28×7)    : 196")
print(f"Dropped            : {196 - len(centres)}")

# ── Focal-length analysis ────────────────────────────────────────────────────
focals = [f for _, _, f in centres]
f_min, f_max = min(focals), max(focals)
f_mean = sum(focals) / len(focals)
f_std  = math.sqrt(sum((f-f_mean)**2 for f in focals) / len(focals))
print(f"\nFocal length (pixels) across all {len(centres)} cameras:")
print(f"  mean={f_mean:.1f}  std={f_std:.1f}  min={f_min:.1f}  max={f_max:.1f}")
print(f"  (rig-constrained -> single focal; independent SfM -> spread across cameras)")

# ── Zero-baseline check: nearest-neighbour distances ─────────────────────────
# For each camera find its 6 nearest neighbours and the 7th.
# In a 7-sensor rig the 6 siblings sit at ≈0 distance; gap to 7th is large.
# In pure independent SfM all distances are substantial.
ctrs = [c for _, c, _ in centres]
n = len(ctrs)
within_rig_dists = []    # distance to 6th nearest neighbour
across_rig_dists = []    # distance to 7th nearest neighbour
for i in range(n):
    dists_i = sorted(dist(ctrs[i], ctrs[j]) for j in range(n) if j != i)
    within_rig_dists.append(dists_i[5])   # 6th nearest (index 5)
    across_rig_dists.append(dists_i[6])   # 7th nearest (index 6)

mean_within = sum(within_rig_dists) / len(within_rig_dists)
mean_across = sum(across_rig_dists) / len(across_rig_dists)

print(f"\nZero-baseline / rig-grouping test:")
print(f"  Mean dist to 6th nearest neighbour : {mean_within:.4f}")
print(f"  Mean dist to 7th nearest neighbour : {mean_across:.4f}")
print(f"  Ratio (7th/6th)                    : {mean_across/mean_within:.1f}×")
print()
if mean_within < 0.001:
    print("RESULT OK XMP RIG PRIORS USED — cameras within each rig frame share a")
    print("         common optical centre (mean within-rig distance ≈ 0).")
elif mean_across / mean_within > 20:
    print("RESULT OK RIG GROUPING DETECTED — large gap between within-rig and")
    print("         cross-rig distances suggests RS honoured the rig geometry.")
else:
    print("RESULT WARN  NO RIG GROUPING DETECTED — all inter-camera distances are")
    print("          similar.  RS likely ran independent SfM, ignoring the rig")
    print("          priors.  The XMP files were present but may not have been")
    print("          recognised or were only used for initial pose hints.")
    print()
    print("  Likely causes:")
    print("  • RealityScan CLI (-addFolder) may not load .jpg.xmp sidecars —")
    print("    try renaming them to .xmp (without the .jpg prefix).")
    print("  • xcr:PosePrior='exact' with absolute position=(0,0,0) for every")
    print("    camera contradicts a moving-camera sequence; RS may have rejected")
    print("    the priors and fallen back to independent SfM.")
    print("  • The rig UUID may need to be a consistent fixed value (not")
    print("    regenerated per run) for RS to recognise the rig across runs.")

# ── Sample clusters ───────────────────────────────────────────────────────────
print(f"\nSample camera centres (first 21 = 3 expected rig frames × 7 sensors):")
for i, (name, ctr, f) in enumerate(centres[:21]):
    print(f"  [{i:3d}] {name:20s}  C=({ctr[0]:8.3f}, {ctr[1]:8.3f}, {ctr[2]:8.3f})  f={f:.1f}")
