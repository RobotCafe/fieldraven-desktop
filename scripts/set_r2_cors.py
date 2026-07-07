"""
Configure CORS on the R2 splats bucket to allow requests from all
FieldRaven web origins (fieldraven.ca, app.fieldraven.ca, localhost).

Run once after adding a new public domain:
  python scripts/set_r2_cors.py
"""

import json
from pathlib import Path

import boto3
from botocore.config import Config

_CONFIG = Path(__file__).parent.parent / "config" / "r2_config.json"

cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))

s3 = boto3.client(
    "s3",
    endpoint_url=cfg["endpoint"],
    aws_access_key_id=cfg["access_key_id"],
    aws_secret_access_key=cfg["secret_access_key"],
    region_name="auto",
    config=Config(signature_version="s3v4"),
)

cors_config = {
    "CORSRules": [
        {
            "AllowedHeaders": ["*"],
            "AllowedMethods": ["GET", "HEAD"],
            "AllowedOrigins": [
                "https://fieldraven.ca",
                "https://www.fieldraven.ca",
                "https://app.fieldraven.ca",
                "https://fieldraven-web.vercel.app",
                "http://localhost:3000",
                "http://localhost:3001",
            ],
            "ExposeHeaders": ["Content-Length", "Content-Type", "ETag"],
            "MaxAgeSeconds": 86400,
        }
    ]
}

s3.put_bucket_cors(Bucket=cfg["bucket"], CORSConfiguration=cors_config)
print(f"✅  CORS configured on bucket '{cfg['bucket']}'")
print("   Allowed origins:")
for o in cors_config["CORSRules"][0]["AllowedOrigins"]:
    print(f"     {o}")
