"""
splatpipe_core — headless pipeline extracted from SplatPipe (3DGS Pipe V13 with VGGT).

Public API:
    run_pipeline(job_id, input_path, settings, on_progress, cancel_event) -> PipelineResult
    PipelineSettings  — all tunable parameters
    PipelineResult    — structured result
    PipelineStage     — enum of pipeline stages
    StageProgress     — progress event fired via on_progress callback
"""

from .pipeline import run_pipeline
from .settings import PipelineSettings
from .types import PipelineResult, PipelineStage, StageProgress

__all__ = [
    "run_pipeline",
    "PipelineSettings",
    "PipelineResult",
    "PipelineStage",
    "StageProgress",
]
