"""
Bridge between FieldRaven Desktop and the SplatPipe INI configuration file.

Reads and writes `360_SplatPipe_config.ini` so both the web UI and the
Tkinter SplatPipe app share a single source of truth for pipeline settings.
"""
import configparser
from pathlib import Path
from typing import Any

SPLATPIPE_INI = Path(r"C:\Users\DenmanNic\Projects\3DGS Pipe V13 with VGGT\360_SplatPipe_config.ini")

# Mapping: section_name → {flat_key: ini_key}
# Postshot and Brush have overlapping INI keys (e.g. max_splats) so they get a prefix.
_SECTION_MAP: dict[str, dict[str, str]] = {
    "Extraction": {
        "extraction_method": "extraction_method",
        "interval_value":    "interval_value",
        "interval_unit":     "interval_unit",
        "frame_count":       "frame_count",
        "frame_format":      "frame_format",
        "pitch_angles_str":  "pitch_angles_str",
        "yaw_steps":         "yaw_steps",
        "fov":               "fov",
        "horizon_ref":       "horizon_ref",
    },
    "Alignment": {
        "run_vggt":                      "run_vggt",
        "run_postshot":                  "run_postshot",
        "run_brush":                     "run_brush",
        "export_xmp":                    "export_xmp",
        "skip_realityscan":              "skip_realityscan",
        "vggt_conf_threshold":           "vggt_conf_threshold",
        "vggt_mask_sky":                 "vggt_mask_sky",
        "vggt_mask_black_bg":            "vggt_mask_black_bg",
        "vggt_mask_white_bg":            "vggt_mask_white_bg",
        "vggt_prediction_mode":          "vggt_prediction_mode",
        "vggt_temporal_sequencing":      "vggt_temporal_sequencing",
        "vggt_enable_sparse":            "vggt_enable_sparse",
        "vggt_sparse_target":            "vggt_sparse_target",
        "vggt_use_anchor_rig":           "vggt_use_anchor_rig",
        "vggt_anchor_view":              "vggt_anchor_view",
        "vggt_rig_optimization_min_points": "vggt_rig_optimization_min_points",
        "vggt_show_camera":              "vggt_show_camera",
        "sky_sensitivity_threshold":     "sky_sensitivity_threshold",
        "run_colmap":                    "run_colmap",
        "colmap_mode":                   "colmap_mode",
        "colmap_matcher":                "colmap_matcher",
        "colmap_visualize":              "colmap_visualize",
        "colmap_correct_pitch":          "colmap_correct_pitch",
        "colmap_correct_translation":    "colmap_correct_translation",
    },
    "Postshot": {
        "postshot_profile":          "profile",
        "postshot_max_image_size":   "max_image_size",
        "postshot_train_steps":      "train_steps",
        "postshot_max_splats":       "max_splats",
        "postshot_anti_aliasing":    "anti_aliasing",
        "postshot_show_train_error": "show_train_error",
        "postshot_store_context":    "store_context",
        "postshot_export_ply":       "export_ply",
        "postshot_alpha_mask":       "alpha_mask",
        "postshot_sky_model":        "sky_model",
    },
    "Brush": {
        "brush_total_steps":    "total_steps",
        "brush_max_splats":     "max_splats",
        "brush_max_resolution": "max_resolution",
        "brush_seed":           "seed",
        "brush_export_every":   "export_every",
        "brush_rerun_logging":  "rerun_logging",
        "brush_spawn_viewer":   "spawn_viewer",
    },
    "Paths": {
        "ffmpeg_path":        "ffmpeg_path",
        "rs_path":            "rs_path",
        "postshot_path":      "postshot_path",
        "brush_path":         "brush_path",
        "rs_settings_path":   "rs_settings_path",
        "vggt_path":          "vggt_path",
        "colmap_bin":         "colmap_bin",
    },
    "InspStitch": {
        "insp_stitch_type":   "stitch_type",
        "insp_lens_guard":    "lens_guard",
        "insp_flowstate":     "flowstate",
        "insp_cuda":          "cuda",
        "insp_output_width":  "output_width",
        "insp_workers":       "workers",
    },
}

_DEFAULTS: dict[str, str] = {
    # Extraction
    "extraction_method": "interval",
    "interval_value":    "1.0",
    "interval_unit":     "seconds",
    "frame_count":       "30",
    "frame_format":      "jpg",
    "pitch_angles_str":  "-30",
    "yaw_steps":         "6",
    "fov":               "94.6",
    "horizon_ref":       "True",
    # Alignment
    "run_vggt":                         "True",
    "run_postshot":                     "False",
    "run_brush":                        "False",
    "export_xmp":                       "False",
    "skip_realityscan":                 "True",
    "vggt_conf_threshold":              "0.0",
    "vggt_mask_sky":                    "False",
    "vggt_mask_black_bg":               "False",
    "vggt_mask_white_bg":               "False",
    "vggt_prediction_mode":             "Depthmap and Camera Branch",
    "vggt_temporal_sequencing":         "True",
    "vggt_enable_sparse":               "False",
    "vggt_sparse_target":               "150000",
    "vggt_use_anchor_rig":              "False",
    "vggt_anchor_view":                 "y00",
    "vggt_rig_optimization_min_points": "500000",
    "vggt_show_camera":                 "True",
    "sky_sensitivity_threshold":        "32",
    "run_colmap":                       "False",
    "colmap_mode":                      "rig",
    "colmap_matcher":                   "sequential",
    "colmap_visualize":                 "False",
    "colmap_correct_pitch":             "True",
    "colmap_correct_translation":       "True",
    # Postshot
    "postshot_profile":          "Splat3",
    "postshot_max_image_size":   "3840",
    "postshot_train_steps":      "5",
    "postshot_max_splats":       "3000",
    "postshot_anti_aliasing":    "True",
    "postshot_show_train_error": "True",
    "postshot_store_context":    "True",
    "postshot_export_ply":       "True",
    "postshot_alpha_mask":       "False",
    "postshot_sky_model":        "False",
    # Brush
    "brush_total_steps":    "30000",
    "brush_max_splats":     "3000",
    "brush_max_resolution": "1920",
    "brush_seed":           "42",
    "brush_export_every":   "5000",
    "brush_rerun_logging":  "False",
    "brush_spawn_viewer":   "False",
    # InspStitch
    "insp_stitch_type":  "ai",
    "insp_lens_guard":   "none",
    "insp_flowstate":    "True",
    "insp_cuda":         "True",
    "insp_output_width": "5984",  # half of native 11968 — 4x faster, still 6K
    "insp_workers":      "2",
    # Paths
    "ffmpeg_path":        "",
    "rs_path":            "",
    "postshot_path":      "",
    "brush_path":         "",
    "rs_settings_path":   "",
    "vggt_path": r"C:\Users\DenmanNic\Projects\3DGS Pipe V13 with VGGT",
    "colmap_bin":         "",
}

# Simple in-process cache — cleared on every save
_cache: dict[str, str] = {}


def _make_parser() -> configparser.RawConfigParser:
    p = configparser.RawConfigParser()
    p.optionxform = str  # preserve key case
    return p


def load() -> dict[str, str]:
    """Return all settings as a flat string dict (merged with defaults for missing keys)."""
    global _cache
    if _cache:
        return dict(_cache)

    result = dict(_DEFAULTS)

    if not SPLATPIPE_INI.exists():
        print(f"⚠️  SplatPipe INI not found: {SPLATPIPE_INI}")
        _cache = result
        return dict(result)

    config = _make_parser()
    config.read(str(SPLATPIPE_INI), encoding="utf-8")

    for section, key_map in _SECTION_MAP.items():
        if not config.has_section(section):
            continue
        for flat_key, ini_key in key_map.items():
            if config.has_option(section, ini_key):
                result[flat_key] = config.get(section, ini_key)

    _cache = result
    return dict(result)


def save(data: dict[str, Any]) -> None:
    """Write a subset of flat settings back into the INI file."""
    global _cache

    config = _make_parser()
    if SPLATPIPE_INI.exists():
        config.read(str(SPLATPIPE_INI), encoding="utf-8")

    # Build reverse map: flat_key → (section, ini_key)
    reverse: dict[str, tuple[str, str]] = {}
    for section, key_map in _SECTION_MAP.items():
        for flat_key, ini_key in key_map.items():
            reverse[flat_key] = (section, ini_key)

    for flat_key, value in data.items():
        if flat_key not in reverse:
            continue
        section, ini_key = reverse[flat_key]
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, ini_key, str(value))

    with open(str(SPLATPIPE_INI), "w", encoding="utf-8") as f:
        config.write(f)

    _cache.clear()


def get(key: str, default: Any = None) -> Any:
    return load().get(key, _DEFAULTS.get(key, default))


def get_all() -> dict[str, str]:
    return load()
