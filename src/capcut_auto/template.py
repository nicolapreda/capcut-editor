"""Template-based draft generation.

Loads an existing CapCut draft (e.g. CASA RIFUGIO) and replaces ONLY the main
video track + subtitle track segments, preserving audio (music + SFX), effects,
filters, and any overlay tracks. New subtitles are cloned from one of the
template's existing subtitles so they keep the same font/animation/style.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .draft import (
    CAPCUT_PROJECTS_DIR,
    _default_anim,
    _default_canvas,
    _default_placeholder,
    _default_sound_mapping,
    _default_speed,
    _default_vocal_sep,
    _uid,
    _us,
    _video_material,
    _video_segment,
)
from .emphasis import Emphasis
from .models import SubtitleChunk, TimelineSegment


# ---------------------------------------------------------------------------
# Audio classification — for SFX redistribution
# ---------------------------------------------------------------------------

# substring → category, checked in order
_TRANSITION_HINTS = ("whoosh", "swish", "swoosh", "transition", "riser", "swipe")
_EMPHASIS_HINTS = ("ding", "explosion", "boom", "chakin", "money", "ka-ching",
                   "kaching", "notification", "alert", "pop", "tap", "click",
                   "magic", "reveal", "tada", "ta-da")


def _classify_audio(material: dict) -> str:
    """Return 'transition' | 'emphasis' | 'music' for an audios[] material."""
    name = (material.get("name") or "").lower()
    typ = material.get("type", "")
    dur_us = material.get("duration", 0) or 0
    dur_s = dur_us / 1_000_000

    if typ == "music" or dur_s > 30:
        return "music"
    if any(k in name for k in _TRANSITION_HINTS):
        return "transition"
    if any(k in name for k in _EMPHASIS_HINTS):
        return "emphasis"
    # default: short clip with unclear name → treat as emphasis (less disruptive)
    return "emphasis"


def redistribute_sfx(info: dict, timeline: list[TimelineSegment]) -> dict:
    """Move transition SFX onto cut boundaries; stretch background music to full duration.

    Returns a summary dict {transitions_placed, music_stretched, emphasis_spread}.
    Emphasis SFX get redistributed evenly across the timeline so they don't all
    cluster in dead zones from the template's original timing.
    """
    if not timeline:
        return {"transitions_placed": 0, "music_stretched": 0, "emphasis_spread": 0}

    total_us = _us(timeline[-1].timeline_end)
    # cut boundaries = end of each segment except the very last
    boundaries_us = [_us(ts.timeline_end) for ts in timeline[:-1]]

    audios_by_id = {a["id"]: a for a in info.get("materials", {}).get("audios", [])}

    transition_segs: list[dict] = []
    emphasis_segs: list[dict] = []
    music_segs: list[tuple[dict, dict]] = []

    for tr in info["tracks"]:
        if tr["type"] != "audio":
            continue
        for s in tr["segments"]:
            mat = audios_by_id.get(s.get("material_id"))
            if not mat:
                continue
            cat = _classify_audio(mat)
            if cat == "transition":
                transition_segs.append(s)
            elif cat == "emphasis":
                emphasis_segs.append(s)
            else:
                music_segs.append((s, mat))

    # --- transitions land on cut boundaries (centered on the cut) ---
    placed = 0
    for i, seg in enumerate(transition_segs):
        dur = seg["target_timerange"]["duration"]
        if i < len(boundaries_us):
            cut_t = boundaries_us[i]
            seg["target_timerange"]["start"] = max(0, cut_t - dur // 2)
            placed += 1
        else:
            # cycle: extra transitions go on additional boundaries if any, or just
            # spread proportionally; for simplicity park them past the end (CapCut
            # will still show them, user can drop/delete).
            extra_idx = i - len(boundaries_us)
            if boundaries_us:
                cut_t = boundaries_us[extra_idx % len(boundaries_us)]
                seg["target_timerange"]["start"] = max(0, cut_t - dur // 2)
            else:
                seg["target_timerange"]["start"] = total_us + 100_000

    # --- emphasis SFX spread evenly across the timeline ---
    spread = 0
    if emphasis_segs and total_us > 0:
        n = len(emphasis_segs)
        step = total_us // (n + 1)
        for i, seg in enumerate(emphasis_segs, start=1):
            dur = seg["target_timerange"]["duration"]
            seg["target_timerange"]["start"] = max(0, i * step - dur // 2)
            spread += 1

    # --- background music: extend across full timeline ---
    stretched = 0
    for seg, mat in music_segs:
        mat_dur = mat.get("duration", 0) or 0
        seg["target_timerange"]["start"] = 0
        seg["target_timerange"]["duration"] = min(total_us, mat_dur) if mat_dur else total_us
        if seg.get("source_timerange"):
            seg["source_timerange"]["start"] = 0
            seg["source_timerange"]["duration"] = min(total_us, mat_dur) if mat_dur else total_us
        stretched += 1

    return {"transitions_placed": placed, "emphasis_spread": spread, "music_stretched": stretched}


# ---------------------------------------------------------------------------
# Emphasis text injection — clone a non-subtitle text style and add overlays
# ---------------------------------------------------------------------------

def _find_overlay_style(info: dict, exclude_track_id: str) -> _SubtitleStyle | None:
    """Pick a text style from any text track other than the subtitle track."""
    for tr in info["tracks"]:
        if tr["type"] != "text" or tr["id"] == exclude_track_id:
            continue
        if not tr["segments"]:
            continue
        try:
            return _extract_subtitle_style(info, tr)
        except ValueError:
            continue
    return None


def _emphasis_track(template_track: dict | None = None) -> dict:
    """A fresh text track for emphasis overlays."""
    return {
        "id": _uid(), "type": "text", "attribute": 0, "flag": 0,
        "segments": [], "is_default_name": True, "name": "",
    }


@dataclass
class _SubtitleStyle:
    """A clonable subtitle style extracted from an existing draft."""
    text_template: dict          # one materials.text_templates[i] entry
    inner_text: dict             # the materials.texts[i] entry it references
    extra_refs: list[str]        # extra_material_refs from the segment (animations etc.)
    seg_clip: dict               # clip transform/scale from the segment
    seg_render_index: int
    seg_track_render_index: int


def _find_main_video_track(info: dict) -> dict:
    """Track containing the speech footage: video type, attribute 0, most segments."""
    candidates = [t for t in info["tracks"] if t["type"] == "video" and t.get("attribute", 0) == 0]
    if not candidates:
        raise ValueError("Template has no main video track (type=video, attribute=0).")
    return max(candidates, key=lambda t: len(t["segments"]))


def _find_subtitle_track(info: dict) -> dict:
    """The text track with the most segments — typically the subtitle track."""
    candidates = [t for t in info["tracks"] if t["type"] == "text"]
    if not candidates:
        raise ValueError("Template has no text track to use for subtitles.")
    return max(candidates, key=lambda t: len(t["segments"]))


def _extract_subtitle_style(info: dict, sub_track: dict) -> _SubtitleStyle:
    """Pull one subtitle's template + inner text material as a style sample."""
    if not sub_track["segments"]:
        raise ValueError("Template's subtitle track has no segments to clone style from.")
    seg = sub_track["segments"][0]
    mid = seg["material_id"]

    # find the matching text_template or texts entry
    text_template = None
    inner_text = None
    for tt in info["materials"].get("text_templates", []):
        if tt["id"] == mid:
            text_template = tt
            break
    if text_template is not None:
        # follow text_info_resources to the inner text material
        tir = text_template.get("text_info_resources", [])
        if tir:
            inner_id = tir[0].get("text_material_id")
            for tx in info["materials"].get("texts", []):
                if tx["id"] == inner_id:
                    inner_text = tx
                    break
    else:
        # plain text material (no template wrapper)
        for tx in info["materials"].get("texts", []):
            if tx["id"] == mid:
                inner_text = tx
                break

    if inner_text is None:
        raise ValueError("Couldn't extract subtitle style from template.")

    return _SubtitleStyle(
        text_template=text_template,
        inner_text=inner_text,
        extra_refs=list(seg.get("extra_material_refs", [])),
        seg_clip=copy.deepcopy(seg.get("clip", {
            "scale": {"x": 1.0, "y": 1.0}, "rotation": 0.0,
            "transform": {"x": 0.0, "y": -0.35},
            "flip": {"vertical": False, "horizontal": False}, "alpha": 1.0,
        })),
        seg_render_index=seg.get("render_index", 14000),
        seg_track_render_index=seg.get("track_render_index", 0),
    )


def _clone_subtitle(style: _SubtitleStyle, text: str, start_us: int, dur_us: int) -> tuple[dict, dict | None, dict]:
    """Return (segment, new_text_template_or_None, new_inner_text) for one subtitle.

    The caller adds new_text_template (if any) to materials.text_templates and
    new_inner_text to materials.texts.
    """
    # clone inner text material with new text + updated style range
    new_inner = copy.deepcopy(style.inner_text)
    new_inner["id"] = _uid()
    new_inner["recognize_text"] = text
    # update the content JSON: replace text and range length
    try:
        c = json.loads(new_inner["content"])
        c["text"] = text
        new_len = len(text)
        for st in c.get("styles", []):
            if "range" in st and len(st["range"]) == 2:
                st["range"][1] = new_len
        new_inner["content"] = json.dumps(c, ensure_ascii=False)
    except (json.JSONDecodeError, KeyError):
        pass  # if the content isn't standard JSON, leave it

    # clone template wrapper (if used)
    new_template = None
    if style.text_template is not None:
        new_template = copy.deepcopy(style.text_template)
        new_template["id"] = _uid()
        # repoint to new inner text
        for tir in new_template.get("text_info_resources", []):
            tir["text_material_id"] = new_inner["id"]
            # update attach_info duration so the template renders for the right length
            if "attach_info" in tir:
                tir["attach_info"]["start_time"] = 0
                tir["attach_info"]["duration"] = dur_us
        # the template itself may carry origin/current word_info — clear text fields
        for fld in ("origin_word_info", "current_word_info"):
            if fld in new_template and isinstance(new_template[fld], dict):
                new_template[fld]["text"] = text
                new_template[fld]["end_time"] = dur_us

    material_id = new_template["id"] if new_template else new_inner["id"]

    segment = {
        "id": _uid(),
        "material_id": material_id,
        "extra_material_refs": list(style.extra_refs),
        "source_timerange": None,
        "target_timerange": {"start": start_us, "duration": dur_us},
        "render_timerange": {"start": 0, "duration": 0},
        "desc": "", "state": 0, "speed": 1.0,
        "is_loop": False, "is_tone_modify": False, "reverse": False,
        "intensifies_audio": False, "cartoon": False,
        "volume": 1.0, "last_nonzero_volume": 1.0,
        "clip": copy.deepcopy(style.seg_clip),
        "uniform_scale": {"on": True, "value": 1.0},
        "render_index": style.seg_render_index,
        "keyframe_refs": [],
        "enable_lut": False, "enable_adjust": False, "enable_hsl": False, "visible": True,
        "group_id": "", "enable_color_curves": True, "enable_hsl_curves": True,
        "track_render_index": style.seg_track_render_index,
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
    return segment, new_template, new_inner


# ---------------------------------------------------------------------------

def build_from_template(
    template_name: str,
    out_name: str,
    timeline: list[TimelineSegment],
    subtitles: list[SubtitleChunk],
    emphasis: list[Emphasis] | None = None,
    redistribute_sfx_enabled: bool = True,
    projects_dir: Path = CAPCUT_PROJECTS_DIR,
) -> Path:
    src = projects_dir / template_name
    if not (src / "draft_info.json").exists():
        raise FileNotFoundError(f"Template draft not found: {src}")

    info = json.loads((src / "draft_info.json").read_text())

    main_video = _find_main_video_track(info)
    sub_track = _find_subtitle_track(info)
    style = _extract_subtitle_style(info, sub_track)
    overlay_style = _find_overlay_style(info, exclude_track_id=sub_track["id"])

    # ---- replace main video track segments ----
    main_video["segments"] = []

    # ensure materials dict has all the buckets we'll write to (template may have empties)
    for bucket in ("videos", "speeds", "placeholder_infos", "canvases",
                   "material_animations", "sound_channel_mappings",
                   "vocal_separations", "text_templates", "texts"):
        info["materials"].setdefault(bucket, [])

    # add new video materials (one per unique source path)
    src_to_mat: dict[Path, str] = {}
    for ts in timeline:
        clip = ts.keep.source
        if clip.path not in src_to_mat:
            vm = _video_material(clip)
            info["materials"]["videos"].append(vm)
            src_to_mat[clip.path] = vm["id"]

    for ts in timeline:
        speed = _default_speed()
        ph = _default_placeholder()
        canv = _default_canvas()
        anim = _default_anim()
        sm = _default_sound_mapping()
        vsep = _default_vocal_sep()
        info["materials"]["speeds"].append(speed)
        info["materials"]["placeholder_infos"].append(ph)
        info["materials"]["canvases"].append(canv)
        info["materials"]["material_animations"].append(anim)
        info["materials"]["sound_channel_mappings"].append(sm)
        info["materials"]["vocal_separations"].append(vsep)
        extras = [speed["id"], ph["id"], canv["id"], anim["id"], sm["id"], vsep["id"]]
        main_video["segments"].append(
            _video_segment(ts, src_to_mat[ts.keep.source.path], extras)
        )

    # ---- replace subtitle track segments ----
    sub_track["segments"] = []
    for sub in subtitles:
        start_us = _us(sub.timeline_start)
        dur_us = max(1, _us(sub.timeline_end - sub.timeline_start))
        seg, new_tmpl, new_inner = _clone_subtitle(style, sub.text, start_us, dur_us)
        if new_tmpl is not None:
            info["materials"]["text_templates"].append(new_tmpl)
        info["materials"]["texts"].append(new_inner)
        sub_track["segments"].append(seg)

    # ---- emphasis text overlays (on a fresh text track) ----
    emphasis_track_id: str | None = None
    if emphasis and overlay_style is not None:
        em_track = _emphasis_track()
        emphasis_track_id = em_track["id"]
        for em in emphasis:
            start_us = _us(em.timeline_start)
            dur_us = max(1, _us(em.timeline_end - em.timeline_start))
            seg, new_tmpl, new_inner = _clone_subtitle(overlay_style, em.text, start_us, dur_us)
            if new_tmpl is not None:
                info["materials"]["text_templates"].append(new_tmpl)
            info["materials"]["texts"].append(new_inner)
            em_track["segments"].append(seg)
        info["tracks"].append(em_track)

    # ---- redistribute SFX along new cut boundaries ----
    if redistribute_sfx_enabled:
        redistribute_sfx(info, timeline)

    # ---- trim everything past the new total duration ----
    # The video timeline (T0) defines the *real* video length. Any template
    # overlay / audio / extra track segment that starts past the end is dropped;
    # anything straddling the end is truncated.
    new_total_us = _us(timeline[-1].timeline_end) if timeline else 0
    _own_tracks = {main_video["id"], sub_track["id"]}
    if emphasis_track_id:
        _own_tracks.add(emphasis_track_id)
    for tr in info["tracks"]:
        if tr["id"] in _own_tracks:
            continue
        kept: list[dict] = []
        for s in tr["segments"]:
            tt = s.get("target_timerange") or {}
            start = tt.get("start", 0) or 0
            dur = tt.get("duration", 0) or 0
            if start >= new_total_us:
                continue  # past the end — drop
            if start + dur > new_total_us:
                tt["duration"] = max(1, new_total_us - start)
            kept.append(s)
        tr["segments"] = kept

    info["duration"] = new_total_us

    # ---- write new draft folder ----
    new_id = _uid()
    info["id"] = new_id
    out = projects_dir / out_name
    out.mkdir(parents=True, exist_ok=True)
    (out / "draft_info.json").write_text(json.dumps(info, ensure_ascii=False))

    # ---- draft_meta_info: rebuild fresh so CapCut shows the new draft ----
    now_us = int(time.time() * 1_000_000)
    meta_values = []
    for path, _mid in src_to_mat.items():
        clip = next(ts.keep.source for ts in timeline if ts.keep.source.path == path)
        meta_values.append({
            "ai_group_type": "", "create_time": int(time.time()),
            "duration": _us(clip.duration), "enter_from": 0, "extra_info": clip.path.name,
            "file_Path": str(clip.path), "height": clip.height, "id": str(uuid.uuid4()),
            "import_time": int(time.time()), "import_time_ms": now_us,
            "item_source": 1, "md5": "", "metetype": "video",
            "roughcut_time_range": {"duration": _us(clip.duration), "start": 0},
            "sub_time_range": {"duration": -1, "start": -1},
            "type": 0, "width": clip.width,
        })

    # Also include audio/effect materials referenced by template tracks so CapCut
    # treats them as already-imported assets. Easiest: copy template's draft_meta_info
    # and just add our new video entries.
    tmpl_meta_path = src / "draft_meta_info.json"
    if tmpl_meta_path.exists():
        meta = json.loads(tmpl_meta_path.read_text())
        # find or create the type=0 materials bucket
        added = False
        for bucket in meta.get("draft_materials", []):
            if bucket.get("type") == 0:
                bucket["value"].extend(meta_values)
                added = True
                break
        if not added:
            meta.setdefault("draft_materials", []).append({"type": 0, "value": meta_values})
        meta["draft_id"] = new_id
        meta["draft_fold_path"] = str(out)
        meta["draft_name"] = out_name
        meta["tm_draft_create"] = now_us
        meta["tm_draft_modified"] = now_us
        meta["tm_duration"] = new_total_us
    else:
        meta = {
            "draft_id": new_id, "draft_fold_path": str(out),
            "draft_name": out_name, "draft_root_path": str(projects_dir),
            "draft_materials": [{"type": 0, "value": meta_values}],
            "tm_draft_create": now_us, "tm_draft_modified": now_us,
            "tm_duration": new_total_us,
        }
    (out / "draft_meta_info.json").write_text(json.dumps(meta, ensure_ascii=False))

    return out
