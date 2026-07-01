"""
Pipeline configuration settings.
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PipelineSettings:
    # ── Frame extraction ─────────────────────────────────────────
    extraction_method: str = "interval"   # "interval" | "count"
    interval_value: float = 1.0
    interval_unit: str = "seconds"        # "seconds" | "frames"
    frame_count: int = 30
    frame_format: str = "jpg"
    ffmpeg_path: str = "ffmpeg"

    # ── View extraction (panorama rendering) ─────────────────────
    pitch_angles: List[float] = field(default_factory=lambda: [-30.0])
    yaw_steps: int = 6
    fov: float = 94.6

    # ── VGGT ─────────────────────────────────────────────────────
    run_vggt: bool = True
    conf_threshold: float = 50.0
    mask_sky: bool = True
    mask_black_bg: bool = False
    mask_white_bg: bool = False
    prediction_mode: str = "Depthmap and Camera Branch"
    temporal_sequencing: bool = True
    enable_sparse: bool = False
    sparse_target_points: int = 150000
    use_anchor_rig: bool = False
    anchor_view: str = "y00"
    rig_optimization_min_points: int = 500000
    use_rig_xmp: bool = False       # RS path: generate XMP rig sidecar files before alignment
    gps_priors_rs: bool = False     # RS path: include GPS position priors in XMP sidecars
    gps_priors_colmap: bool = False # COLMAP path: geo-register reconstruction using GPS positions

    # ── COLMAP ───────────────────────────────────────────────────
    run_colmap: bool = False
    colmap_mode: str = "rig"          # "rig" | "spherical"
    colmap_matcher: str = "sequential" # "sequential" | "exhaustive" | "vocabtree"
    horizon_ref: bool = True           # prepend pitch=0° sensor as rig reference to preserve pitch in cam_from_rig
    colmap_visualize: bool = False     # generate cameras.html visualizer after reconstruction
    colmap_correct_pitch: bool = True        # apply single global rotation to level the reconstruction using mean anchor camera orientation
    colmap_bin: Optional[str] = None   # path to colmap.exe; enables GPU via CLI for extraction+matching
    sky_sensitivity_threshold: int = 32
    colmap_image_width: int = 1920
    colmap_image_height: int = 1920

    # ── Training ─────────────────────────────────────────────────
    run_postshot: bool = False
    run_brush: bool = False
    postshot_path: Optional[str] = None
    brush_path: Optional[str] = None
    rs_path: Optional[str] = None
    rs_settings_path: Optional[str] = None

    # Postshot training options
    postshot_profile: str = "Splat3"
    postshot_max_image_size: int = 3840
    postshot_train_steps: int = 5
    postshot_max_splats: int = 3000
    postshot_anti_aliasing: bool = True
    postshot_show_train_error: bool = True
    postshot_store_context: bool = True
    postshot_export_ply: bool = True
    postshot_alpha_mask: bool = False
    postshot_sky_model: bool = False

    # Brush training options
    brush_total_steps: int = 30000
    brush_max_splats: int = 3000
    brush_max_resolution: int = 1920
    brush_seed: int = 42
    brush_export_every: int = 5000
    brush_rerun_logging: bool = False
    brush_spawn_viewer: bool = False

    # ── Paths ────────────────────────────────────────────────────
    jobs_base_dir: str = r"C:\FieldRaven\Jobs"
    # Path to the SplatPipe App directory containing video_extraction.py,
    # panorama_processing.py, vggt_training.py, etc.
    vggt_app_path: str = r"C:\Users\DenmanNic\Projects\3DGS Pipe V13 with VGGT\App"
    # Override: use this as the job root instead of jobs_base_dir / job_id.
    # Set by FieldRaven to the user-selected project directory so that
    # 01_frames/, 02_views/, 04_training/ land alongside import from camera/.
    project_dir: Optional[str] = None
