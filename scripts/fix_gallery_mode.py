"""
One-off script to fix pipelineMode on gallery Firestore documents
where the wrong mode was recorded (defaulted to 'rs_brush').

Usage:
    python scripts/fix_gallery_mode.py

Lists all gallery docs, shows current pipelineMode, and lets you
correct any that are wrong.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend import firebase_client

firebase_client.initialize()
db = firebase_client.get_db()

print("Fetching gallery documents...\n")
docs = list(db.collection("gallery").stream())

if not docs:
    print("No gallery documents found.")
    sys.exit(0)

for doc in docs:
    d = doc.to_dict()
    name = d.get("name", "?")
    mode = d.get("pipelineMode", "MISSING")
    created = d.get("createdAt")
    print(f"  {doc.id[:12]}…  mode={mode:12}  name={name}")

print()
print("Enter a job ID prefix (or full ID) to fix, then the correct mode.")
print("Correct modes: rs_brush | colmap | vggt")
print("Press Ctrl+C to exit.\n")

while True:
    try:
        prefix = input("Job ID (or prefix): ").strip()
        if not prefix:
            continue
        matches = [doc for doc in docs if doc.id.startswith(prefix)]
        if not matches:
            print(f"  No match for '{prefix}'")
            continue
        if len(matches) > 1:
            print(f"  Ambiguous — {len(matches)} matches. Use more characters.")
            continue
        doc = matches[0]
        d = doc.to_dict()
        print(f"  Found: {doc.id}  name={d.get('name')}  current mode={d.get('pipelineMode')}")
        new_mode = input("  New mode (rs_brush / colmap / vggt): ").strip()
        if new_mode not in ("rs_brush", "colmap", "vggt"):
            print(f"  Unknown mode '{new_mode}' — skipped")
            continue
        db.collection("gallery").document(doc.id).update({"pipelineMode": new_mode})
        print(f"  Updated {doc.id} → pipelineMode={new_mode}\n")
    except KeyboardInterrupt:
        print("\nDone.")
        break
