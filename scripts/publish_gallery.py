"""One-shot script to publish a splat to the Firestore gallery collection."""
import sys
from pathlib import Path

# Allow imports from backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import firebase_client
from google.cloud.firestore import SERVER_TIMESTAMP

def publish(doc_id: str, name: str, splat_url: str, gaussian_count: int, pipeline_mode: str = "rs_brush"):
    firebase_client.initialize()
    db = firebase_client.get_db()
    ref = db.collection("gallery").document(doc_id)
    ref.set({
        "name": name,
        "splatUrl": splat_url,
        "gaussianCount": gaussian_count,
        "pipelineMode": pipeline_mode,
        "createdAt": SERVER_TIMESTAMP,
    })
    print(f"Published gallery/{doc_id}: {name}")

if __name__ == "__main__":
    publish(
        doc_id="nile-creek-test",
        name="Nile Creek",
        splat_url="https://splats.fieldraven.ca/nile-creek-test/scene.splat",
        gaussian_count=5_440_406,
        pipeline_mode="rs_brush",
    )
