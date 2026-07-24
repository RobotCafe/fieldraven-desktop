"""
Shared type definitions for the SplatPipe pipeline.
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from enum import Enum


class PipelineStage(str, Enum):
    IDLE = "idle"
    FRAME_EXTRACTION = "frame_extraction"
    VIEW_EXTRACTION = "view_extraction"
    REALITYSCAN = "realityscan"
    VGGT_ALIGNMENT = "vggt_alignment"
    COLMAP_ALIGNMENT = "colmap_alignment"
    COLMAP_EXPORT = "colmap_export"
    GLUEMAP_ALIGNMENT = "gluemap_alignment"
    RIGSFM_ALIGNMENT = "rigsfm_alignment"
    EQUISFM_ALIGNMENT = "equisfm_alignment"
    BRUSH_TRAINING = "brush_training"
    POSTSHOT_TRAINING = "postshot_training"
    TRAINING = "training"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class StageProgress:
    stage: PipelineStage
    progress: int           # 0-100
    message: str
    detail: Optional[str] = None


@dataclass
class PipelineResult:
    success: bool
    job_id: str
    output_dir: str
    glb_path: Optional[str] = None
    colmap_dir: Optional[str] = None
    error: Optional[str] = None
    stats: Dict[str, Any] = field(default_factory=dict)
