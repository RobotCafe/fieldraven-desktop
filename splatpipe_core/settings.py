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
    colmap_correct_pitch: bool = True        # rotate entire reconstruction so the mean 0°-pitch extraction views align with zero pitch/roll in COLMAP world space
    colmap_orientation_align: bool = False   # after pitch correction, run colmap model_orientation_aligner --method IMAGE_ORIENTATION to refine level using scene geometry (requires colmap_bin)
    colmap_mapper: str = "incremental"       # "incremental" (pycolmap rig-aware) | "global" (GLOMAP, requires colmap_bin, no rig constraints)
    colmap_vocab_tree: str = ""              # path to COLMAP vocab tree .bin; enables a second vocab_tree_matcher pass after sequential matching for loop closure (requires colmap_bin)
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

    # ── RigSfM ───────────────────────────────────────────────────
    run_rigsfm: bool = False
    rigsfm_anchor_sensor: int = 0         # pano_cameraX index used as the Pi3 anchor (single mode)
    rigsfm_quad_anchors: bool = False     # stage 4 horizon crops (yaw 0/90/180/270°) instead of 1 sensor
    rigsfm_matcher: str = "sequential"    # "sequential" | "exhaustive"

    # ── EquiSfM ──────────────────────────────────────────────────
    run_equisfm: bool = False
    equisfm_matcher: str = "sequential"   # "sequential" | "exhaustive"
    equisfm_triangulate: bool = False     # real per-sensor SIFT triangulation glue (poses stay fixed); off until validated on real jobs

    # ── GlueMap ──────────────────────────────────────────────────
    run_gluemap: bool = False
    gluemap_backbone: str = "pi3"           # pi3 | pi3x | vggt | map_anything
    gluemap_skip_doppelgangers: bool = True  # skip two-view covisibility stage (faster)
    gluemap_coarse_only: bool = False        # stop after global BA, skip SIFT refinement
    gluemap_is_sequential: bool = True       # sequential/video mode (temporal pairing)
    gluemap_num_neighbors: int = 100         # SALAD retrieval neighbours per image
    gluemap_batch_size: int = 60             # two-view inference batch size (16GB VRAM; use 30 for <12GB)
    gluemap_num_track_per_img: int = 512     # VGGSfM tracks per image (512 halves tracking time, same quality as 1024)
    gluemap_wsl_home: str = "/home/decosson" # WSL2 home directory
    gluemap_wsl_distro: str = "Ubuntu-22.04" # WSL2 distribution name

    # ── Paths ────────────────────────────────────────────────────
    jobs_base_dir: str = r"C:\FieldRaven\Jobs"
    # Path to the SplatPipe App directory containing video_extraction.py,
    # panorama_processing.py, vggt_training.py, etc.
    vggt_app_path: str = r"C:\Users\DenmanNic\Projects\3DGS Pipe V13 with VGGT\App"
    # Override: use this as the job root instead of jobs_base_dir / job_id.
    # Set by FieldRaven to the user-selected project directory so that
    # 01_frames/, 02_views/, 04_training/ land alongside import from camera/.
    project_dir: Optional[str] = None
