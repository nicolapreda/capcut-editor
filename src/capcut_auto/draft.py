"""Write a CapCut draft (draft_info.json + draft_meta_info.json) to disk.

Format reverse-engineered from CapCut 8.6.0 (Mac), schema new_version 169.0.0.
Time unit throughout is microseconds.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .models import US, SubtitleChunk, TimelineSegment


# Default CapCut projects directory on macOS
CAPCUT_PROJECTS_DIR = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def _us(seconds: float) -> int:
    return int(round(seconds * US))


# ---------------------------------------------------------------------------
# Default per-segment auxiliary materials. CapCut creates one of each per
# video segment; the IDs differ but the content is just defaults. We mirror
# that here so existing CapCut versions don't choke on shared/missing refs.
# ---------------------------------------------------------------------------

def _default_speed() -> dict:
    return {"id": _uid(), "type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None}


def _default_placeholder() -> dict:
    return {
        "id": _uid(), "type": "placeholder_info", "meta_type": "none",
        "res_path": "", "res_text": "", "error_path": "", "error_text": "",
    }


def _default_canvas() -> dict:
    return {
        "id": _uid(), "type": "canvas_color", "color": "", "blur": 0.0,
        "image": "", "album_image": "", "image_id": "", "image_name": "",
        "source_platform": 0, "team_id": "",
    }


def _default_anim() -> dict:
    return {"id": _uid(), "type": "sticker_animation", "animations": [], "multi_language_current": "none"}


def _default_sound_mapping() -> dict:
    return {"id": _uid(), "type": "sound_channel_mapping", "audio_channel_mapping": 0, "is_config_open": False}


def _default_vocal_sep() -> dict:
    return {
        "id": _uid(), "type": "vocal_separation", "choice": 0,
        "removed_sounds": [], "time_range": None,
        "production_path": "", "final_algorithm": "", "enter_from": "",
    }


# ---------------------------------------------------------------------------
# Text content payload — what CapCut puts into materials.texts[i].content
# (a JSON string with styles + text). We keep it minimal: white fill, black
# stroke, system font. User can restyle inside CapCut.
# ---------------------------------------------------------------------------

def _text_content(text: str, font_size: int = 12) -> str:
    payload = {
        "styles": [{
            "fill": {"content": {"solid": {"color": [1, 1, 1]}, "render_type": "solid"}, "alpha": 1.0},
            "range": [0, len(text)],
            "size": font_size,
            "font": {"path": "", "id": ""},
            "strokes": [{
                "content": {"solid": {"color": [0, 0, 0]}, "render_type": "solid"},
                "width": 0.08, "alpha": 1.0,
            }],
            "bold": False, "italic": False, "underline": False,
        }],
        "text": text,
    }
    return json.dumps(payload, ensure_ascii=False)


def _text_material(text: str, font_size: int = 12) -> dict:
    return {
        "id": _uid(), "type": "text", "sub_type": 0,
        "content": _text_content(text, font_size),
        "recognize_text": text,
        "text_color": "#ffffffff", "text_alpha": 1.0,
        "background_color": "", "background_alpha": 0.0, "background_style": 0,
        "border_color": "#000000ff", "border_alpha": 1.0, "border_width": 0.08, "border_mode": 0,
        "has_shadow": False, "shadow_color": "", "shadow_alpha": 0.0,
        "shadow_smoothing": 0.0, "shadow_distance": 5.0,
        "shadow_point": {"x": 0.0, "y": 0.0}, "shadow_angle": -45.0,
        "font_name": "", "font_title": "none", "font_size": float(font_size),
        "font_path": "", "font_id": "", "font_resource_id": "",
        "alignment": 1, "line_feed": 1, "letter_spacing": 0.0, "line_spacing": 0.02,
        "global_alpha": 1.0, "layer_weight": 1, "initial_scale": 1.0,
        "use_effect_default_color": False, "is_rich_text": False,
        "shape_clip_x": False, "shape_clip_y": False,
        "typesetting": 0, "text_size": 30,
        "bold_width": 0.0, "italic_degree": 0,
        "underline": False, "underline_width": 0.05, "underline_offset": 0.22,
        "ktv_color": "", "style_name": "",
        "text_curve": None, "text_loop_on_path": False, "offset_on_path": 0.0,
        "enable_path_typesetting": False, "text_exceeds_path_process_type": 0,
        "text_typesetting_paths": None, "text_typesetting_paths_file": "",
        "text_typesetting_path_index": 0,
        "combo_info": {"text_templates": []},
        "caption_template_info": {
            "resource_id": "", "third_resource_id": "", "resource_name": "",
            "category_id": "", "category_name": "", "effect_id": "",
            "request_id": "", "path": "", "is_new": False, "source_platform": 0,
        },
        "words": {"start_time": [], "end_time": [], "text": []},
        "current_words": {"start_time": [], "end_time": [], "text": []},
        "base_content": "",
        "text_to_audio_ids": [],
        "check_flag": 63,
        "recognize_task_id": "",
    }


# ---------------------------------------------------------------------------

def _video_material(clip) -> dict:
    return {
        "id": _uid(), "type": "video", "duration": _us(clip.duration),
        "path": str(clip.path), "media_path": str(clip.path), "local_id": "",
        "material_name": clip.path.name,
        "width": clip.width, "height": clip.height, "has_audio": clip.has_audio,
        "reverse_path": "", "intensifies_path": "", "reverse_intensifies_path": "",
        "intensifies_audio_path": "", "cartoon_path": "",
        "category_id": "", "category_name": "", "material_id": "", "material_url": "",
        "crop": {
            "upper_left_x": 0.0, "upper_left_y": 0.0,
            "upper_right_x": 1.0, "upper_right_y": 0.0,
            "lower_left_x": 0.0, "lower_left_y": 1.0,
            "lower_right_x": 1.0, "lower_right_y": 1.0,
        },
        "crop_ratio": "free", "crop_scale": 1.0,
        "audio_fade": None, "extra_type_option": 0,
        "stable": {"stable_level": 0, "matrix_path": "", "time_range": {"start": 0, "duration": 0}},
        "matting": {
            "flag": 0, "path": "", "interactiveTime": [],
            "has_use_quick_brush": False, "strokes": [],
            "has_use_quick_eraser": False, "expansion": 0, "feather": 0,
            "reverse": False, "custom_matting_id": "", "enable_matting_stroke": False,
        },
        "source": 0, "source_platform": 0, "formula_id": "", "check_flag": 62978047,
    }


def _video_segment(seg: TimelineSegment, material_id: str, extra_refs: list[str]) -> dict:
    return {
        "id": _uid(),
        "material_id": material_id,
        "extra_material_refs": extra_refs,
        "source_timerange": {"start": _us(seg.keep.src_start), "duration": _us(seg.keep.duration)},
        "target_timerange": {"start": _us(seg.timeline_start), "duration": _us(seg.timeline_end - seg.timeline_start)},
        "render_timerange": {"start": 0, "duration": 0},
        "desc": "", "state": 0, "speed": 1.0,
        "is_loop": False, "is_tone_modify": False, "reverse": False,
        "intensifies_audio": False, "cartoon": False,
        "volume": 1.0, "last_nonzero_volume": 1.0,
        "clip": {
            "scale": {"x": 1.0, "y": 1.0}, "rotation": 0.0,
            "transform": {"x": 0.0, "y": 0.0},
            "flip": {"vertical": False, "horizontal": False}, "alpha": 1.0,
        },
        "uniform_scale": {"on": True, "value": 1.0},
        "render_index": 0, "keyframe_refs": [],
        "enable_lut": True, "enable_adjust": True, "enable_hsl": False, "visible": True,
        "group_id": "", "enable_color_curves": True, "enable_hsl_curves": True,
        "track_render_index": 0,
        "hdr_settings": {"mode": 1, "intensity": 1.0, "nits": 1000},
        "enable_color_wheels": True, "track_attribute": 0,
        "is_placeholder": False, "template_id": "",
        "enable_smart_color_adjust": False, "template_scene": "default",
        "common_keyframes": [], "caption_info": None,
        "responsive_layout": {
            "enable": False, "target_follow": "",
            "size_layout": 0, "horizontal_pos_layout": 0, "vertical_pos_layout": 0,
        },
        "enable_color_match_adjust": False, "enable_color_correct_adjust": False,
        "enable_adjust_mask": False, "raw_segment_id": "", "lyric_keyframes": None,
        "enable_video_mask": True, "digital_human_template_group_id": "",
        "color_correct_alg_result": "", "source": "segmentsourcenormal",
        "enable_mask_stroke": False, "enable_mask_shadow": False,
        "enable_color_adjust_pro": False,
    }


def _text_segment(start_us: int, dur_us: int, material_id: str, extra_refs: list[str], y_offset: float = -0.55) -> dict:
    return {
        "id": _uid(),
        "material_id": material_id,
        "extra_material_refs": extra_refs,
        "source_timerange": None,
        "target_timerange": {"start": start_us, "duration": dur_us},
        "render_timerange": {"start": 0, "duration": 0},
        "desc": "", "state": 0, "speed": 1.0,
        "is_loop": False, "is_tone_modify": False, "reverse": False,
        "intensifies_audio": False, "cartoon": False,
        "volume": 1.0, "last_nonzero_volume": 1.0,
        "clip": {
            "scale": {"x": 1.0, "y": 1.0}, "rotation": 0.0,
            "transform": {"x": 0.0, "y": y_offset},
            "flip": {"vertical": False, "horizontal": False}, "alpha": 1.0,
        },
        "uniform_scale": {"on": True, "value": 1.0},
        "render_index": 14000, "keyframe_refs": [],
        "enable_lut": False, "enable_adjust": False, "enable_hsl": False, "visible": True,
        "group_id": "", "enable_color_curves": True, "enable_hsl_curves": True,
        "track_render_index": 2,
        "hdr_settings": None,
        "enable_color_wheels": True, "track_attribute": 0,
        "is_placeholder": False, "template_id": "",
        "enable_smart_color_adjust": False, "template_scene": "default",
        "common_keyframes": [], "caption_info": None,
        "responsive_layout": {
            "enable": False, "target_follow": "",
            "size_layout": 0, "horizontal_pos_layout": 0, "vertical_pos_layout": 0,
        },
        "enable_color_match_adjust": False, "enable_color_correct_adjust": False,
        "enable_adjust_mask": False, "raw_segment_id": "", "lyric_keyframes": None,
        "enable_video_mask": True, "digital_human_template_group_id": "",
        "color_correct_alg_result": "", "source": "segmentsourcenormal",
        "enable_mask_stroke": False, "enable_mask_shadow": False,
        "enable_color_adjust_pro": False,
    }


# ---------------------------------------------------------------------------

def build_draft(
    name: str,
    timeline: list[TimelineSegment],
    subtitles: list[SubtitleChunk],
    canvas_w: int = 1080,
    canvas_h: int = 1920,
    fps: float = 30.0,
    projects_dir: Path = CAPCUT_PROJECTS_DIR,
) -> Path:
    """Write the draft folder and return its path."""
    draft_id = _uid()
    folder = projects_dir / name
    folder.mkdir(parents=True, exist_ok=True)

    materials = {
        "videos": [], "audios": [], "texts": [],
        "speeds": [], "placeholder_infos": [], "canvases": [],
        "material_animations": [], "sound_channel_mappings": [], "vocal_separations": [],
        # below are kept empty but the keys must exist
        "flowers": [], "tail_leaders": [], "images": [], "effects": [], "stickers": [],
        "transitions": [], "audio_effects": [], "audio_fades": [], "beats": [],
        "placeholders": [], "common_mask": [], "chromas": [], "text_templates": [],
        "realtime_denoises": [], "audio_pannings": [], "audio_pitch_shifts": [],
        "video_trackings": [], "hsl": [], "drafts": [], "color_curves": [],
        "hsl_curves": [], "primary_color_wheels": [], "log_color_wheels": [],
        "video_effects": [], "audio_balances": [], "handwrites": [],
        "manual_deformations": [], "manual_beautys": [], "plugin_effects": [],
        "green_screens": [], "shapes": [], "material_colors": [],
        "digital_humans": [], "digital_human_model_dressing": [], "smart_crops": [],
        "ai_translates": [], "audio_track_indexes": [], "loudnesses": [],
        "vocal_beautifys": [], "vocal_separations": [], "smart_relights": [],
        "time_marks": [], "multi_language_refs": [],
        "video_shadows": [], "video_strokes": [], "video_radius": [],
    }
    # de-dup the vocal_separations key (one of the dups above is intentional;
    # reset to empty list to be safe)
    materials["vocal_separations"] = []

    # ---- one video material per unique source clip ----
    src_to_mat: dict[Path, str] = {}
    for seg in timeline:
        clip = seg.keep.source
        if clip.path not in src_to_mat:
            vm = _video_material(clip)
            materials["videos"].append(vm)
            src_to_mat[clip.path] = vm["id"]

    # ---- video segments + their per-segment extras ----
    video_segments = []
    for seg in timeline:
        speed = _default_speed()
        placeholder = _default_placeholder()
        canvas = _default_canvas()
        anim = _default_anim()
        smap = _default_sound_mapping()
        vsep = _default_vocal_sep()
        materials["speeds"].append(speed)
        materials["placeholder_infos"].append(placeholder)
        materials["canvases"].append(canvas)
        materials["material_animations"].append(anim)
        materials["sound_channel_mappings"].append(smap)
        materials["vocal_separations"].append(vsep)
        extras = [speed["id"], placeholder["id"], canvas["id"], anim["id"], smap["id"], vsep["id"]]
        video_segments.append(_video_segment(seg, src_to_mat[seg.keep.source.path], extras))

    # ---- text materials + segments ----
    text_segments = []
    for sub in subtitles:
        tm = _text_material(sub.text)
        materials["texts"].append(tm)
        anim = _default_anim()
        materials["material_animations"].append(anim)
        start_us = _us(sub.timeline_start)
        dur_us = max(1, _us(sub.timeline_end - sub.timeline_start))
        text_segments.append(_text_segment(start_us, dur_us, tm["id"], [anim["id"]]))

    tracks = [
        {"id": _uid(), "type": "video", "attribute": 0, "flag": 0, "segments": video_segments, "is_default_name": True, "name": ""},
    ]
    if text_segments:
        tracks.append({"id": _uid(), "type": "text", "attribute": 0, "flag": 0, "segments": text_segments, "is_default_name": True, "name": ""})

    total_dur_us = _us(timeline[-1].timeline_end) if timeline else 0

    draft_info = {
        "id": draft_id,
        "version": 360000,
        "new_version": "169.0.0",
        "name": "",
        "duration": total_dur_us,
        "create_time": 0, "update_time": 0,
        "fps": fps,
        "is_drop_frame_timecode": False,
        "color_space": -1,
        "config": {
            "video_mute": False, "record_audio_last_index": 1,
            "extract_audio_last_index": 1, "original_sound_last_index": 1,
            "subtitle_recognition_id": "", "subtitle_taskinfo": [],
            "lyrics_recognition_id": "", "lyrics_taskinfo": [],
            "subtitle_sync": True, "lyrics_sync": True, "voice_change_sync": False,
            "sticker_max_index": 1, "adjust_max_index": 1,
            "material_save_mode": 0, "export_range": None,
            "maintrack_adsorb": True, "combination_max_index": 1,
            "attachment_info": [], "zoom_info_params": None,
            "system_font_list": [],
            "multi_language_mode": "none", "multi_language_main": "none",
            "multi_language_current": "none", "multi_language_list": [],
            "subtitle_keywords_config": None, "use_float_render": False,
        },
        "canvas_config": {"ratio": "original", "width": canvas_w, "height": canvas_h, "background": None},
        "tracks": tracks,
        "group_container": None,
        "materials": materials,
        "keyframes": {"videos": [], "audios": [], "texts": [], "stickers": [], "filters": [], "adjusts": [], "handwrites": [], "effects": []},
        "keyframe_graph_list": [],
        "platform": {"os": "mac", "os_version": "26.3.1", "app_id": 359289, "app_version": "8.6.0", "app_source": "cc", "device_id": "", "hard_disk_id": "", "mac_address": ""},
        "last_modified_platform": {"os": "mac", "os_version": "26.3.1", "app_id": 359289, "app_version": "8.6.0", "app_source": "cc", "device_id": "", "hard_disk_id": "", "mac_address": ""},
        "mutable_config": None, "cover": None, "retouch_cover": None, "extra_info": None,
        "relationships": [],
        "render_index_track_mode_on": True, "free_render_index_mode_on": False,
        "static_cover_image_path": "", "source": "default",
        "time_marks": None, "path": "", "lyrics_effects": [],
        "uneven_animation_template_info": {"composition": "", "content": "", "order": "", "sub_template_info_list": []},
        "draft_type": "video",
        "smart_ads_info": {"page_from": "", "routine": "", "draft_url": ""},
        "function_assistant_info": {
            "smart_rec_applied": False, "fixed_rec_applied": False,
            "auto_adjust": False, "auto_adjust_segid_list": [],
            "color_correction": False, "color_correction_segid_list": [],
            "enhance_quality": False, "smooth_slow_motion": False,
            "deflicker_segid_list": [], "video_noise_segid_list": [],
            "enhance_quality_segid_list": [], "smart_segid_list": [],
            "retouch": False, "retouch_segid_list": [],
            "enhande_voice": False, "enhance_voice_segid_list": [],
            "audio_noise_segid_list": [],
            "auto_caption": False, "auto_caption_segid_list": [],
            "auto_caption_template_id": "",
            "caption_opt": False, "caption_opt_segid_list": [],
            "eye_correction": False, "eye_correction_segid_list": [],
            "normalize_loudness": False, "normalize_loudness_segid_list": [],
            "normalize_loudness_audio_denoise_segid_list": [],
            "auto_adjust_fixed": False, "auto_adjust_fixed_value": 50.0,
            "color_correction_fixed": False, "color_correction_fixed_value": 50.0,
            "normalize_loudness_fixed": False, "enhande_voice_fixed": False,
            "retouch_fixed": False, "enhance_quality_fixed": False,
            "smooth_slow_motion_fixed": False,
            "fps": {"num": 0, "den": 1},
        },
    }

    # ---- write draft_info.json ----
    (folder / "draft_info.json").write_text(json.dumps(draft_info, ensure_ascii=False))

    # ---- write draft_meta_info.json (so CapCut shows it in the UI) ----
    now_us = int(time.time() * 1_000_000)
    meta_materials_values = []
    for path, mat_id in src_to_mat.items():
        clip = next(s.keep.source for s in timeline if s.keep.source.path == path)
        meta_materials_values.append({
            "ai_group_type": "", "create_time": int(time.time()),
            "duration": _us(clip.duration), "enter_from": 0, "extra_info": clip.path.name,
            "file_Path": str(clip.path), "height": clip.height, "id": str(uuid.uuid4()),
            "import_time": int(time.time()), "import_time_ms": now_us,
            "item_source": 1, "md5": "", "metetype": "video",
            "roughcut_time_range": {"duration": _us(clip.duration), "start": 0},
            "sub_time_range": {"duration": -1, "start": -1},
            "type": 0, "width": clip.width,
        })

    draft_meta = {
        "cloud_draft_cover": False, "cloud_draft_sync": False,
        "cloud_package_completed_time": "",
        "draft_cloud_capcut_purchase_info": "", "draft_cloud_last_action_download": False,
        "draft_cloud_package_type": "", "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "draft_cover.jpg", "draft_deeplink_url": "",
        "draft_enterprise_info": {"draft_enterprise_extra": "", "draft_enterprise_id": "", "draft_enterprise_name": "", "enterprise_material": []},
        "draft_fold_path": str(folder),
        "draft_id": draft_id,
        "draft_is_ae_produce": False, "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False, "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False, "draft_is_cloud_temp_draft": False,
        "draft_is_from_deeplink": "false", "draft_is_invisible": False,
        "draft_is_web_article_video": False,
        "draft_materials": [{"type": 0, "value": meta_materials_values}],
        "draft_materials_copied_info": [],
        "draft_name": name,
        "draft_need_rename_folder": False, "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": str(projects_dir),
        "draft_segment_extra_info": [],
        "draft_timeline_materials_size_": 0,
        "draft_type": "", "draft_web_article_video_enter_from": "",
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1, "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_parent_entry_id": -1, "tm_draft_cloud_space_id": -1,
        "tm_draft_cloud_user_id": -1,
        "tm_draft_create": now_us, "tm_draft_modified": now_us, "tm_draft_removed": 0,
        "tm_duration": total_dur_us,
    }
    (folder / "draft_meta_info.json").write_text(json.dumps(draft_meta, ensure_ascii=False))

    return folder
