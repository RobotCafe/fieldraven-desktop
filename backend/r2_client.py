"""
Cloudflare R2 upload client for finished splat files.
Uses the S3-compatible API with boto3.

Config is read from config/r2_config.json (gitignored).
"""

import json
from pathlib import Path
from typing import Optional

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "r2_config.json"
_config: Optional[dict] = None


def _load_config() -> dict:
    global _config
    if _config is None:
        if not _CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"R2 config not found at {_CONFIG_PATH}. "
                "Create it with: endpoint, access_key_id, secret_access_key, bucket, public_base_url"
            )
        _config = json.loads(_CONFIG_PATH.read_text())
    return _config


def is_configured() -> bool:
    """Return True if r2_config.json exists and has the required keys."""
    try:
        cfg = _load_config()
        return all(k in cfg for k in ("endpoint", "access_key_id", "secret_access_key", "bucket", "public_base_url"))
    except Exception:
        return False


def upload_splat(local_path: str | Path, job_id: str) -> str:
    """
    Upload a .splat file to R2 under {job_id}/scene.splat.
    Returns the public HTTPS URL.
    """
    import boto3
    from botocore.config import Config

    cfg = _load_config()
    local_path = Path(local_path)

    s3 = boto3.client(
        "s3",
        endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["access_key_id"],
        aws_secret_access_key=cfg["secret_access_key"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

    key = f"{job_id}/scene.splat"
    file_size_mb = local_path.stat().st_size / (1024 * 1024)
    print(f"  [r2] Uploading {local_path.name} ({file_size_mb:.0f} MB) → {cfg['bucket']}/{key}")

    s3.upload_file(
        str(local_path),
        cfg["bucket"],
        key,
        ExtraArgs={
            "ContentType": "application/octet-stream",
            "CacheControl": "public, max-age=31536000, immutable",
        },
    )

    public_url = f"{cfg['public_base_url'].rstrip('/')}/{key}"
    print(f"  [r2] Uploaded -> {public_url}")
    return public_url


def upload_rad(local_path: str | Path, job_id: str) -> str:
    """
    Upload a .rad LoD splat file to R2 under {job_id}/scene-lod.rad.
    Returns the public HTTPS URL.
    """
    import boto3
    from botocore.config import Config

    cfg = _load_config()
    local_path = Path(local_path)

    s3 = boto3.client(
        "s3",
        endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["access_key_id"],
        aws_secret_access_key=cfg["secret_access_key"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

    key = f"{job_id}/scene-lod.rad"
    file_size_mb = local_path.stat().st_size / (1024 * 1024)
    print(f"  [r2] Uploading {local_path.name} ({file_size_mb:.0f} MB) → {cfg['bucket']}/{key}")

    s3.upload_file(
        str(local_path),
        cfg["bucket"],
        key,
        ExtraArgs={
            "ContentType": "application/octet-stream",
            "CacheControl": "public, max-age=31536000, immutable",
        },
    )

    public_url = f"{cfg['public_base_url'].rstrip('/')}/{key}"
    print(f"  [r2] Uploaded → {public_url}")
    return public_url


def upload_thumbnail(local_path: str | Path, job_id: str) -> str:
    """
    Upload a JPEG thumbnail to R2 under {job_id}/thumbnail.jpg.
    Returns the public HTTPS URL.
    """
    import boto3
    from botocore.config import Config

    cfg = _load_config()
    local_path = Path(local_path)

    s3 = boto3.client(
        "s3",
        endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["access_key_id"],
        aws_secret_access_key=cfg["secret_access_key"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

    key = f"{job_id}/thumbnail.jpg"
    print(f"  [r2] Uploading thumbnail -> {cfg['bucket']}/{key}")

    s3.upload_file(
        str(local_path),
        cfg["bucket"],
        key,
        ExtraArgs={
            "ContentType": "image/jpeg",
            "CacheControl": "public, max-age=86400",
        },
    )

    public_url = f"{cfg['public_base_url'].rstrip('/')}/{key}"
    print(f"  [r2] Thumbnail -> {public_url}")
    return public_url
