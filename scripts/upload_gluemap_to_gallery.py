"""
Upload a GlueMap Brush PLY to the FieldRaven gallery.

Usage (from FieldRaven_desktop directory):
    python scripts/upload_gluemap_to_gallery.py \
        --ply "C:/Users/DenmanNic/Desktop/Nile Creek GlueMap Test/export_30000.ply" \
        --name "Nile Creek GlueMap" \
        --thumbnail "C:/Users/DenmanNic/Desktop/Nile Creek GlueMap Test/brush_input/images/pano_camera0/IMG_20260603_144933_00_186.jpg"

Steps:  PLY -> SPZ -> RAD -> R2 upload -> Firestore gallery doc
"""
import sys
import argparse
import uuid
from pathlib import Path

# Allow importing from the backend package
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend import firebase_client, r2_client
from backend.pipeline_runner import (
    _convert_ply_to_spz,
    _convert_spz_to_rad,
    _count_gaussians_ply,
)


def upload_thumbnail(thumb_src: Path, job_id: str) -> "str | None":
    try:
        from PIL import Image
        thumb_path = thumb_src.parent / f"thumb_{job_id}.jpg"
        with Image.open(thumb_src) as img:
            img = img.convert("RGB")
            w, h = img.size
            # For equirectangular source — crop centre band
            if w > h * 1.5:
                img = img.crop((0, h // 3, w, h * 2 // 3))
            img.thumbnail((1280, 1280), Image.LANCZOS)
            img.save(str(thumb_path), "JPEG", quality=82, optimize=True)
        url = r2_client.upload_thumbnail(thumb_path, job_id)
        thumb_path.unlink(missing_ok=True)
        return url
    except Exception as e:
        print(f"  [thumbnail] Skipping: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Upload GlueMap PLY to FieldRaven gallery")
    parser.add_argument("--ply",       required=True,  help="Path to export_30000.ply")
    parser.add_argument("--name",      default="Nile Creek GlueMap", help="Gallery display name")
    parser.add_argument("--thumbnail", default=None,   help="Path to thumbnail source image")
    parser.add_argument("--job-id",    default=None,   help="Custom job ID (auto-generated if omitted)")
    args = parser.parse_args()

    ply_path = Path(args.ply)
    if not ply_path.exists():
        print(f"❌ PLY not found: {ply_path}")
        sys.exit(1)

    job_id = args.job_id or str(uuid.uuid4())[:20].replace("-", "")
    print(f"Job ID: {job_id}")
    print(f"PLY:    {ply_path} ({ply_path.stat().st_size / 1e9:.2f} GB)")

    # 1. Count Gaussians
    gaussian_count = _count_gaussians_ply(ply_path)
    print(f"Gaussians: {gaussian_count:,}")

    # 2. PLY -> SPZ
    print("Converting PLY -> SPZ…")
    spz_path = _convert_ply_to_spz(ply_path)
    print(f"  SPZ: {spz_path} ({spz_path.stat().st_size / 1e6:.1f} MB)")

    # 3. SPZ -> RAD
    print("Building LoD tree (.rad)…")
    rad_path = _convert_spz_to_rad(spz_path)
    print(f"  RAD: {rad_path} ({rad_path.stat().st_size / 1e6:.1f} MB)")

    # 4. Upload RAD to R2
    print("Uploading .rad to R2…")
    firebase_client.initialize()
    rad_url = r2_client.upload_rad(rad_path, job_id)
    print(f"  URL: {rad_url}")

    # 5. Thumbnail
    thumb_url = None
    if args.thumbnail:
        print("Uploading thumbnail…")
        thumb_url = upload_thumbnail(Path(args.thumbnail), job_id)
        print(f"  Thumbnail: {thumb_url}")

    # 6. Publish to Firestore gallery
    print("Publishing to gallery…")
    from google.cloud.firestore import SERVER_TIMESTAMP
    db = firebase_client.get_db()
    db.collection("gallery").document(job_id).set({
        "jobId":         job_id,
        "name":          args.name,
        "splatUrl":      rad_url,
        "gaussianCount": gaussian_count,
        "pipelineMode":  "gluemap",
        "createdAt":     SERVER_TIMESTAMP,
        **({"thumbnailUrl": thumb_url} if thumb_url else {}),
    })
    print(f"\n✅ Published to gallery/{job_id}")
    print(f"   Name:      {args.name}")
    print(f"   Gaussians: {gaussian_count:,}")
    print(f"   Mode:      gluemap")
    print(f"   URL:       {rad_url}")


if __name__ == "__main__":
    main()
