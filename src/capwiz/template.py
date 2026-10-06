"""Template-based draft generation.

Loads an existing CapCut draft (e.g. CASA RIFUGIO) and replaces the main video
track + subtitle track segments, preserving effects, filters and overlay tracks.
Audio is either kept as-is or rebuilt from the AI's sound plan (music stretched
or dropped, sound effects placed only where Claude decided). Every new subtitle
is a full clone of one of the template's own subtitles — same text template,
animation, effect, font and position — with only its text and timing changed.
"""
from __future__ import annotations

import copy
import json
import statistics
import time
import uuid
from collections import Counter
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
    _text_material,
    _text_segment,
    _uid,
    _us,
    _video_material,
    _video_segment,
    cover_track,
)
from .emphasis import Emphasis
from .models import CoverShot, SubtitleChunk, TimelineSegment


# ---------------------------------------------------------------------------
# Audio classification — for SFX redistribution
# ---------------------------------------------------------------------------

# substring → category, checked in order
_TRANSITION_HINTS = ("whoosh", "swish", "swoosh", "transition", "riser", "swipe")
_EMPHASIS_HINTS = ("ding", "explosion", "boom", "chakin", "money", "ka-ching",
                   "kaching", "notification", "alert", "pop", "tap", "click",
                   "magic", "reveal", "tada", "ta-da")


def _seg_seconds(seg: dict) -> float:
    return ((seg.get("target_timerange") or {}).get("duration", 0) or 0) / 1_000_000


def _classify_audio(material: dict, seg_seconds: float = 0.0) -> str:
    """Return 'transition' | 'emphasis' | 'music' | 'leftover' for a template
    audio segment.

    Judged on how the template *uses* it, not on the length of the source file:
    a 1-second snippet of a 2-minute file is not a music bed.
      * `video_original_sound` is audio detached from a video of the project the
        template was copied from (someone else's voice/ambience) → leftover;
      * music library tracks, or anything laid for 10 s or more → music;
      * the rest are short effects, told apart by name.
    """
    name = (material.get("name") or "").lower()
    typ = material.get("type", "")

    if typ == "video_original_sound":
        return "leftover"
    if typ == "music" or seg_seconds >= 10:
        return "music"
    if any(k in name for k in _TRANSITION_HINTS):
        return "transition"
    if any(k in name for k in _EMPHASIS_HINTS):
        return "emphasis"
    # default: short clip with unclear name → treat as emphasis (less disruptive)
    return "emphasis"


_CATEGORY_LABEL = {"transition": "transizione", "emphasis": "enfasi / effetto"}


def template_sound_inventory(template_name: str,
                             projects_dir: Path = CAPCUT_PROJECTS_DIR) -> dict:
    """Sounds and music available in a template, for the AI to choose from."""
    info = json.loads((projects_dir / template_name / "draft_info.json").read_text())
    audios_by_id = {a["id"]: a for a in info.get("materials", {}).get("audios", [])}
    sounds: dict[str, dict] = {}
    music: dict[str, dict] = {}
    for tr in info["tracks"]:
        if tr["type"] != "audio":
            continue
        for s in tr["segments"]:
            mat = audios_by_id.get(s.get("material_id"))
            if not mat:
                continue
            name = mat.get("name") or "suono"
            cat = _classify_audio(mat, _seg_seconds(s))
            if cat == "leftover":
                continue                      # old project's audio: never offered
            if cat == "music":
                music[name] = {"name": name, "duration": (mat.get("duration") or 0) / 1e6}
                continue
            entry = sounds.setdefault(name, {
                "name": name, "count": 0, "category": _CATEGORY_LABEL.get(cat, cat),
                "duration": (s.get("target_timerange") or {}).get("duration", 0) / 1e6
                            or (mat.get("duration") or 0) / 1e6,
            })
            entry["count"] += 1
    return {"sounds": list(sounds.values()), "music": list(music.values())}


def _clone_segment(info: dict, seg: dict) -> dict:
    """Deep-copy a segment with fresh ids for it and its per-segment materials."""
    bucket_of: dict[str, str] = {}
    for bucket, items in info.get("materials", {}).items():
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and "id" in it:
                    bucket_of[it["id"]] = bucket
    new = copy.deepcopy(seg)
    new["id"] = _uid()
    refs = []
    for ref in seg.get("extra_material_refs", []):
        bucket = bucket_of.get(ref)
        src = next((m for m in info["materials"].get(bucket, []) if m.get("id") == ref), None) \
            if bucket else None
        if src is None:
            refs.append(ref)
            continue
        dup = copy.deepcopy(src)
        dup["id"] = _uid()
        info["materials"][bucket].append(dup)
        refs.append(dup["id"])
    new["extra_material_refs"] = refs
    return new


def _overlaps(track: dict, start: int, dur: int) -> bool:
    for s in track["segments"]:
        tt = s.get("target_timerange") or {}
        a, d = tt.get("start", 0) or 0, tt.get("duration", 0) or 0
        if start < a + d and a < start + dur:
            return True
    return False


def _match_sound(name: str, protos: dict[str, tuple[dict, dict]]) -> tuple[dict, dict] | None:
    if name in protos:
        return protos[name]
    low = name.lower()
    for key, proto in protos.items():
        if key.lower() == low:
            return proto
    for key, proto in protos.items():
        if low in key.lower() or key.lower() in low:
            return proto
    return None


def apply_sound_plan(info: dict, plan, total_us: int, log) -> None:
    """Rebuild the audio tracks from the AI's sound plan.

    Every non-music sound effect of the template is removed, then re-placed only
    where Claude asked for it (cloning the template segment so volume/fades are
    kept). Music is stretched over the whole reel or removed, as decided.
    """
    audios_by_id = {a["id"]: a for a in info.get("materials", {}).get("audios", [])}
    audio_tracks = [tr for tr in info["tracks"] if tr["type"] == "audio"]

    def mat_of(seg: dict) -> dict | None:
        return audios_by_id.get(seg.get("material_id"))

    protos: dict[str, tuple[dict, dict]] = {}
    music_keep = {name.lower(): keep for name, keep, _why in plan.music}
    music_laid: set[str] = set()
    leftovers: set[str] = set()
    for tr in audio_tracks:
        kept_segs = []
        for s in tr["segments"]:
            mat = mat_of(s)
            if mat is None:
                continue
            name = mat.get("name") or "suono"
            cat = _classify_audio(mat, _seg_seconds(s))
            if cat == "leftover":
                leftovers.add(name)
                continue
            if cat == "music":
                if not music_keep.get(name.lower(), True):
                    continue                      # AI chose to drop this music
                if name in music_laid:
                    continue                      # template had it in pieces: one bed is enough
                music_laid.add(name)
                mat_dur = mat.get("duration", 0) or 0
                length = min(total_us, mat_dur) if mat_dur else total_us
                s["target_timerange"]["start"] = 0
                s["target_timerange"]["duration"] = length
                if s.get("source_timerange"):
                    s["source_timerange"]["start"] = 0
                    s["source_timerange"]["duration"] = length
                kept_segs.append(s)
            else:
                protos.setdefault(name, (tr, s))  # remember one sample per sound
        tr["segments"] = kept_segs
    if leftovers:
        log(f"Tolto l'audio staccato dai video del progetto precedente: "
            f"{', '.join(sorted(leftovers))}")

    for name, at, _why in plan.placements:
        proto = _match_sound(name, protos)
        if proto is None:
            log(f"  ⚠ suono «{name}» richiesto dall'AI non esiste nel template: ignorato")
            continue
        home_track, sample = proto
        seg = _clone_segment(info, sample)
        start = int(at * 1_000_000)
        dur = seg["target_timerange"]["duration"]
        seg["target_timerange"]["start"] = start
        track = next((t for t in [home_track, *audio_tracks] if not _overlaps(t, start, dur)), None)
        if track is None:
            track = copy.deepcopy({k: v for k, v in home_track.items() if k != "segments"})
            track["id"] = _uid()
            track["segments"] = []
            info["tracks"].append(track)
            audio_tracks.append(track)
        track["segments"].append(seg)

    for tr in audio_tracks:
        tr["segments"].sort(key=lambda s: (s.get("target_timerange") or {}).get("start", 0))


# ---------------------------------------------------------------------------
# Emphasis text injection — clone a non-subtitle text style and add overlays
# ---------------------------------------------------------------------------

def _find_overlay_style(info: dict, exclude_track_ids: set[str]) -> _SubtitleStyle | None:
    """Pick a text style from any text track that isn't a subtitle track."""
    for tr in info["tracks"]:
        if tr["type"] != "text" or tr["id"] in exclude_track_ids:
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


def _emphasis_clip() -> dict:
    """Sensible defaults for a mid-video emphasis text: upper-third, scale 1.0,
    no rotation. Avoids inheriting weird template overlay positions/rotations."""
    return {
        "scale": {"x": 1.0, "y": 1.0},
        "rotation": 0.0,
        "transform": {"x": 0.0, "y": 0.28},   # upper-third
        "flip": {"vertical": False, "horizontal": False},
        "alpha": 1.0,
    }


# ---------------------------------------------------------------------------
# Intro title customization
# ---------------------------------------------------------------------------

def _is_emoji_like(text: str) -> bool:
    """True if the text has no Latin letters — likely an emoji-only overlay."""
    s = text.strip()
    if not s:
        return True
    return not any(c.isalpha() and ord(c) < 128 for c in s)


def _replace_text_in_material(text_material: dict, new_text: str) -> None:
    """Update a `texts` material in-place to display new_text, preserving style."""
    text_material["recognize_text"] = new_text
    try:
        c = json.loads(text_material["content"])
        c["text"] = new_text
        new_len = len(new_text)
        for st in c.get("styles", []):
            if "range" in st and len(st["range"]) == 2:
                st["range"][1] = new_len
        text_material["content"] = json.dumps(c, ensure_ascii=False)
    except (json.JSONDecodeError, KeyError):
        pass


def _intro_text_candidates(info: dict, max_start_us: int = 3_000_000,
                           exclude_track_ids: set[str] | None = None
                           ) -> list[tuple[int, int, dict]]:
    """Find text overlay materials in the first `max_start_us` microseconds
    that contain Latin-letter text (not pure emoji). Returns list sorted by
    (start_time, track_index) of (start_us, track_idx, text_material)."""
    exclude_track_ids = exclude_track_ids or set()
    texts_by_id = {tx["id"]: tx for tx in info["materials"].get("texts", [])}
    out: list[tuple[int, int, dict]] = []
    for ti, t in enumerate(info["tracks"]):
        if t["type"] != "text" or t["id"] in exclude_track_ids:
            continue
        for s in t["segments"]:
            tt = s.get("target_timerange") or {}
            start = tt.get("start", 0) or 0
            if start >= max_start_us:
                continue
            mat = texts_by_id.get(s.get("material_id"))
            if mat is None:
                continue
            try:
                content = json.loads(mat["content"])
                shown = content.get("text", "")
            except (json.JSONDecodeError, KeyError):
                shown = mat.get("recognize_text") or ""
            if _is_emoji_like(shown) or len(shown.strip()) <= 1:
                continue
            out.append((start, ti, mat))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def replace_intro_titles(info: dict, intro_title: str,
                         exclude_track_ids: set[str] | None = None,
                         max_start_us: int = 3_000_000) -> int:
    """Replace the template's intro title overlays with `intro_title`.

    If the template splits its title across N overlays (like "SOGNO" + "AVVERATO"),
    the user's title is split across the same N slots — one space-separated word
    per slot, with any remainder concatenated into the last slot. Surplus slots
    beyond the title's word count are blanked so we never produce a mix like
    "CASA AVVERATO".

    Also resets the segment's clip (rotation → 0, scale → 1.0, x → 0) so the
    new text never inherits a 44° tilt or an oversized scale that would push it
    off the canvas. Vertical position is preserved.
    """
    if not intro_title.strip():
        return 0
    candidates = _intro_text_candidates(info, max_start_us=max_start_us,
                                        exclude_track_ids=exclude_track_ids)
    if not candidates:
        return 0
    parts = intro_title.strip().split()
    n = len(candidates)
    chunks: list[str]
    if len(parts) <= n:
        chunks = parts + [""] * (n - len(parts))
    elif len(parts) <= 4:
        chunks = parts[: n - 1] + [" ".join(parts[n - 1:])]
    else:
        # a long title (one dictated by the script): spread it evenly over the slots
        size, extra = divmod(len(parts), n)
        chunks, i = [], 0
        for k in range(n):
            take = size + (1 if k < extra else 0)
            chunks.append(" ".join(parts[i:i + take]))
            i += take

    # build mat_id → list[segment] index so we can also reset segment clips
    seg_index: dict[str, list[dict]] = {}
    for tr in info["tracks"]:
        for s in tr.get("segments", []):
            seg_index.setdefault(s.get("material_id", ""), []).append(s)

    for chunk, (_start, _ti, mat) in zip(chunks, candidates):
        _replace_text_in_material(mat, chunk.upper())
        for seg in seg_index.get(mat["id"], []):
            clip = seg.setdefault("clip", {})
            clip["rotation"] = 0.0
            clip["scale"] = {"x": 1.0, "y": 1.0}
            clip.setdefault("transform", {"x": 0.0, "y": 0.0})["x"] = 0.0
            # keep transform.y so the title stays roughly where the template designer put it
    return n


# ---------------------------------------------------------------------------
# Clean out all of the template's residual text overlays
# (intros we didn't replace, "3, 2, 1, vai!" countdowns, mid-video labels…)
# ---------------------------------------------------------------------------

def clear_template_texts(info: dict, exclude_track_ids: set[str]) -> int:
    """Drop every text segment from non-excluded text tracks whose displayed
    text contains Latin letters. Emoji-only overlays survive (they're often
    decorative and harmless on a new video). Returns the number of segments
    dropped.
    """
    texts_by_id = {tx["id"]: tx for tx in info.get("materials", {}).get("texts", [])}
    dropped = 0
    for tr in info["tracks"]:
        if tr["type"] != "text" or tr["id"] in exclude_track_ids:
            continue
        kept: list[dict] = []
        for s in tr["segments"]:
            mat = texts_by_id.get(s.get("material_id"))
            if mat is None:
                kept.append(s)
                continue
            try:
                shown = json.loads(mat["content"]).get("text", "")
            except (json.JSONDecodeError, KeyError):
                shown = mat.get("recognize_text") or ""
            if _is_emoji_like(shown) or len(shown.strip()) <= 1:
                kept.append(s)
            else:
                dropped += 1
        tr["segments"] = kept
    return dropped


@dataclass
class _SubtitleStyle:
    """A clonable overlay-text style extracted from an existing draft."""
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


def _extract_subtitle_style(info: dict, sub_track: dict) -> _SubtitleStyle:
    """Pull one text's template + inner text material as a style sample."""
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


def _clone_overlay_text(style: _SubtitleStyle, text: str, start_us: int, dur_us: int) -> tuple[dict, dict | None, dict]:
    """Return (segment, None, new_text_material) for one on-screen text
    (emphasis overlays). A plain `texts` material styled like the sample;
    subtitles don't go through here, see `_clone_caption`.
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

    new_template = None
    material_id = new_inner["id"]

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
# Subtitles — every new one is a full clone of a real template subtitle
# ---------------------------------------------------------------------------
#
# A CapCut subtitle is not one object but a small bundle:
#   segment ─ material_id ──────────► text_templates[] (the "text template" look)
#           └ extra_material_refs ─► material_animations[] (caption animation)
#                                    effects[]             (text effect)
#   text_template.text_info_resources[0] ─ text_material_id ─► texts[] (the words)
# Each subtitle owns its whole bundle. Sharing any piece between subtitles (or
# leaving `words` from another one) makes CapCut draw the wrong text, so a clone
# copies all of it with fresh ids and rewrites every text-bearing field.

_SUB_PUNCT = ".,;:!?…"


def _materials_index(info: dict) -> dict[str, tuple[str, dict]]:
    """material id → (bucket name, material)."""
    out: dict[str, tuple[str, dict]] = {}
    for bucket, items in info.get("materials", {}).items():
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict) and "id" in it:
                out[it["id"]] = (bucket, it)
    return out


def _text_of(index: dict, seg: dict) -> tuple[dict | None, dict | None]:
    """(text-template wrapper or None, inner text material or None) of a text segment."""
    bucket, mat = index.get(seg.get("material_id"), (None, None))
    if bucket == "texts":
        return None, mat
    if bucket == "text_templates":
        tir = (mat.get("text_info_resources") or [{}])[0]
        b2, inner = index.get(tir.get("text_material_id"), (None, None))
        return mat, inner if b2 == "texts" else None
    return None, None


def _shown_text(inner: dict) -> str:
    try:
        return json.loads(inner["content"]).get("text", "") or ""
    except (KeyError, TypeError, json.JSONDecodeError):
        return inner.get("recognize_text") or ""


def _caption_tracks(info: dict, index: dict) -> list[dict]:
    """The template's subtitle tracks, the one with most subtitles first.

    CapCut marks caption tracks with flag 1 and builds captions on
    `text_template_subtitle` materials. Drafts captioned some other way (this
    tool's own output, for one) are recognised by shape: a run of short texts.
    """
    def is_caption(tr: dict) -> bool:
        if tr["type"] != "text" or not tr["segments"]:
            return False
        if tr.get("flag") == 1:
            return True
        return any("subtitle" in (index.get(s.get("material_id"), (None, {}))[1].get("type") or "")
                   for s in tr["segments"])

    tracks = [t for t in info["tracks"] if is_caption(t)]
    if not tracks:
        for t in info["tracks"]:
            if t["type"] == "text" and len(t["segments"]) >= 4:
                if statistics.median(_seg_seconds(s) for s in t["segments"]) <= 3.5:
                    tracks.append(t)
    return sorted(tracks, key=lambda t: -len(t["segments"]))


@dataclass
class _SizeModel:
    """How big the template's subtitles are drawn, learnt from the template:
    lets a clone declare a box that fits its own text."""
    per_char: float
    max_width: float
    line_height: float
    line_step: float

    def estimate(self, text: str) -> tuple[float, float]:
        per_line = max(1, int(self.max_width / self.per_char))
        lines, cur, longest = 1, 0, 0
        for w in text.split():
            add = len(w) + (1 if cur else 0)
            if cur and cur + add > per_line:
                longest, lines, cur = max(longest, cur), lines + 1, len(w)
            else:
                cur += add
        longest = max(longest, cur, 1)
        return (min(self.max_width, longest * self.per_char),
                self.line_height + (lines - 1) * self.line_step)


def _size_model(index: dict, track: dict) -> _SizeModel | None:
    samples = []
    for s in track["segments"]:
        wrapper, inner = _text_of(index, s)
        if wrapper is None or inner is None:
            continue
        att = (wrapper.get("text_info_resources") or [{}])[0].get("attach_info") or {}
        w, h, n = att.get("original_size_width"), att.get("original_size_height"), len(_shown_text(inner).strip())
        if w and h and n >= 3:
            samples.append((n, float(w), float(h)))
    if len(samples) < 2:
        return None
    line_h = min(h for _, _, h in samples)
    one_line = [w / n for n, w, h in samples if h <= line_h * 1.2]
    heights = sorted({round(h) for _, _, h in samples})
    steps = [b - a for a, b in zip(heights, heights[1:]) if b - a > line_h * 0.3]
    return _SizeModel(per_char=statistics.median(one_line),
                      max_width=max(w for _, w, _ in samples), line_height=line_h,
                      line_step=statistics.median(steps) if steps else line_h * 0.6)


@dataclass
class _CaptionProto:
    """One real subtitle of the template, used as the mould for the new ones."""
    seg: dict
    wrapper: dict | None          # text_templates entry (None for plain-text subtitles)
    inner: dict                   # texts entry
    sizes: _SizeModel | None
    render_indexes: list[int]     # the z-order values the template's subtitles used
    plain_words: bool             # template keeps `words` lowercase and unpunctuated


def _caption_proto(index: dict, track: dict) -> _CaptionProto | None:
    """Pick the most typical subtitle of the track: usual position, a single
    style, never trimmed by hand (its animation still spans its length) and,
    among those, the one closest to the usual length."""
    cands = [(s, *_text_of(index, s)) for s in track["segments"]]
    cands = [c for c in cands if c[2] is not None]
    if not cands:
        return None

    def pos(s: dict) -> tuple:
        clip = s.get("clip") or {}
        tr, sc = clip.get("transform") or {}, clip.get("scale") or {}
        return (round(tr.get("x", 0.0), 3), round(tr.get("y", 0.0), 3), round(sc.get("x", 1.0), 3))

    usual_pos = Counter(pos(c[0]) for c in cands).most_common(1)[0][0]
    usual_wrapped = Counter(c[1] is not None for c in cands).most_common(1)[0][0]
    usual_len = statistics.median(len(_shown_text(c[2]).strip()) for c in cands)

    def score(c: tuple) -> tuple:
        s, wrapper, inner = c
        dur = (s.get("target_timerange") or {}).get("duration", 0)
        untouched = any(a.get("duration") == dur
                        for r in s.get("extra_material_refs") or []
                        for a in index.get(r, (None, {}))[1].get("animations") or [])
        try:
            one_style = len(json.loads(inner["content"]).get("styles") or []) == 1
        except (KeyError, TypeError, json.JSONDecodeError):
            one_style = False
        return ((wrapper is not None) == usual_wrapped, pos(s) == usual_pos, one_style, untouched,
                -abs(len(_shown_text(inner).strip()) - usual_len))

    seg, wrapper, inner = max(cands, key=score)
    # how the template spells its per-word timing: CapCut's own captions keep
    # the words lowercase and unpunctuated whatever the text on screen looks like
    wtexts = [t for c in cands for t in (c[2].get("words") or {}).get("text") or [] if t.strip()]
    plain = sum(t == t.lower() and t == t.strip(_SUB_PUNCT) for t in wtexts)
    return _CaptionProto(
        seg=seg, wrapper=wrapper, inner=inner, sizes=_size_model(index, track),
        render_indexes=sorted({s.get("render_index", 14000) for s in track["segments"]}),
        plain_words=bool(wtexts) and plain >= 0.9 * len(wtexts),
    )


def _fit_animation(anim: dict, old_us: int, new_us: int) -> None:
    """Re-time a cloned animation to the new subtitle's length."""
    start, dur = anim.get("start", 0) or 0, anim.get("duration", 0) or 0
    if anim.get("type") == "caption" or (start == 0 and dur >= old_us):
        anim["start"], anim["duration"] = 0, new_us         # runs for the whole subtitle
    elif anim.get("type") == "out":
        anim["duration"] = min(dur, new_us)
        anim["start"] = new_us - anim["duration"]
    else:
        anim["duration"] = min(dur, new_us)


def _set_text(inner: dict, text: str) -> None:
    """Make a `texts` material show `text`, in the sample's main style."""
    try:
        c = json.loads(inner["content"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return
    styles = [st for st in c.get("styles") or []
              if isinstance(st.get("range"), list) and len(st["range"]) == 2]
    if styles:
        # a sample with highlighted words has several ranges: keep the base one
        base = copy.deepcopy(max(styles, key=lambda st: st["range"][1] - st["range"][0]))
        base["range"] = [0, len(text.encode("utf-16-le")) // 2]
        c["styles"] = [base]
    c["text"] = text
    inner["content"] = json.dumps(c, ensure_ascii=False, separators=(",", ":"))


def _plain(word: str) -> str:
    return word.strip().strip(_SUB_PUNCT + "\"«»“”()").lower()


def _words_field(sub: SubtitleChunk, plain: bool) -> dict:
    """CapCut's per-word timing: milliseconds from the subtitle's start, with a
    zero-length " " entry between words. The caption animation reads it."""
    starts: list[int] = []
    ends: list[int] = []
    texts: list[str] = []
    for w in sub.words:
        text = _plain(w.text) if plain else w.text.strip()
        if not text:
            continue
        end = round((w.end - sub.timeline_start) * 1000)
        if texts and text[0] in "'’":           # second half of a split elision ("c" + "'è")
            texts[-1] += text
            ends[-1] = max(ends[-1], end)
            continue
        if texts:
            starts.append(ends[-1]); ends.append(ends[-1]); texts.append(" ")
        s = max(ends[-1] if ends else 0, round((w.start - sub.timeline_start) * 1000))
        starts.append(s); ends.append(max(s, end))
        texts.append(text)
    return {"start_time": starts, "end_time": ends, "text": texts}


def _clone_caption(info: dict, index: dict, proto: _CaptionProto, sub: SubtitleChunk,
                   n: int) -> dict:
    """Add subtitle number `n` to the draft's materials as a full clone of the
    sample bundle and return its segment."""
    mats = info["materials"]
    start_us = _us(sub.timeline_start)
    dur_us = max(1, _us(sub.timeline_end - sub.timeline_start))
    old_dur = (proto.seg.get("target_timerange") or {}).get("duration", 0) or 0
    new_ids: dict[str, str] = {}

    def clone_ref(ref: str) -> str:
        """Own copy of an animation/effect material (once, even if referenced twice)."""
        if ref not in new_ids:
            bucket, mat = index.get(ref, (None, None))
            if mat is None:
                return ref
            new = copy.deepcopy(mat)
            new["id"] = _uid()
            for anim in new.get("animations") or []:
                _fit_animation(anim, old_dur, dur_us)
            mats[bucket].append(new)
            new_ids[ref] = new["id"]
        return new_ids[ref]

    inner = copy.deepcopy(proto.inner)
    inner["id"] = _uid()
    spoken = " ".join(w.text.strip() for w in sub.words) or sub.text
    inner["recognize_text"] = (" ".join(filter(None, map(_plain, spoken.split())))
                               if proto.plain_words else spoken)
    _set_text(inner, sub.text)
    if isinstance(inner.get("words"), dict):
        inner["words"] = _words_field(sub, proto.plain_words)
    mats["texts"].append(inner)

    seg = copy.deepcopy(proto.seg)
    seg["id"] = _uid()
    seg["material_id"] = inner["id"]
    seg["target_timerange"] = {"start": start_us, "duration": dur_us}
    seg["extra_material_refs"] = [clone_ref(r) for r in proto.seg.get("extra_material_refs") or []]
    seg["render_index"] = proto.render_indexes[min(n, len(proto.render_indexes) - 1)]
    seg["keyframe_refs"], seg["common_keyframes"] = [], []

    if proto.wrapper is not None:
        wrapper = copy.deepcopy(proto.wrapper)
        wrapper["id"] = _uid()
        for i, tir in enumerate(wrapper.get("text_info_resources") or []):
            tir["id"] = _uid()
            if i == 0:
                tir["text_material_id"] = inner["id"]
            tir["extra_material_refs"] = [clone_ref(r) for r in tir.get("extra_material_refs") or []]
            att = tir.get("attach_info")
            if isinstance(att, dict):
                att["start_time"], att["duration"] = 0, dur_us
                if proto.sizes and "original_size_width" in att:
                    att["original_size_width"], att["original_size_height"] = proto.sizes.estimate(sub.text)
        font = (wrapper.get("aigc_config") or {}).get("font_item")
        if isinstance(font, dict) and font.get("id"):
            font["id"] = _uid()
        mats["text_templates"].append(wrapper)
        seg["material_id"] = wrapper["id"]
    return seg


def _caption_material_ids(index: dict, tracks: list[dict]) -> set[str]:
    """Every material the given subtitle tracks own (texts, wrappers, animations, effects)."""
    ids: set[str] = set()
    for tr in tracks:
        for s in tr["segments"]:
            ids.add(s.get("material_id"))
            ids.update(s.get("extra_material_refs") or [])
            wrapper, inner = _text_of(index, s)
            if inner is not None:
                ids.add(inner["id"])
            for tir in (wrapper or {}).get("text_info_resources") or []:
                ids.update(tir.get("extra_material_refs") or [])
    return ids


def _drop_unused(info: dict, candidates: set[str]) -> None:
    """Remove the `candidates` materials nothing points to any more (the old
    project's subtitles), so the new draft doesn't carry their words around."""
    used: set[str] = set()
    for tr in info["tracks"]:
        for s in tr["segments"]:
            used.add(s.get("material_id"))
            used.update(s.get("extra_material_refs") or [])
    for tt in info["materials"].get("text_templates", []):
        if tt.get("id") in used:
            for tir in tt.get("text_info_resources") or []:
                used.add(tir.get("text_material_id"))
                used.update(tir.get("extra_material_refs") or [])
    for bucket in ("text_templates", "texts", "material_animations", "effects"):
        items = info["materials"].get(bucket)
        if isinstance(items, list):
            info["materials"][bucket] = [m for m in items
                                         if m.get("id") not in candidates or m.get("id") in used]


@dataclass
class SubtitleLook:
    """How the template's subtitles are written, so the new ones read the same."""
    count: int
    max_chars: int
    typical_chars: int
    max_words: int
    max_duration: float
    case: str               # upper | lower | asis
    punctuation: str        # the punctuation marks the template's subtitles keep


def template_subtitle_look(template_name: str,
                           projects_dir: Path = CAPCUT_PROJECTS_DIR) -> SubtitleLook | None:
    """Measure the template's own subtitles (None when it has too few to learn from)."""
    info = json.loads((projects_dir / template_name / "draft_info.json").read_text())
    index = _materials_index(info)
    rows = []
    for tr in _caption_tracks(info, index):
        for s in tr["segments"]:
            _, inner = _text_of(index, s)
            text = _shown_text(inner).strip() if inner else ""
            if text:
                rows.append((text, _seg_seconds(s)))
    if len(rows) < 3:
        return None
    texts = [t for t, _ in rows]
    letters = [c for t in texts for c in t if c.isalpha()]
    upper = sum(c.isupper() for c in letters) / max(1, len(letters))
    # a mark counts as "kept" if the template uses it for real, not once by accident
    # (question and exclamation marks are rare by nature: once is enough)
    punct = "".join(
        p for p in _SUB_PUNCT
        if sum(p in t for t in texts) >= (1 if p in "?!" else max(2, round(len(texts) * 0.15))))
    return SubtitleLook(
        count=len(rows),
        max_chars=max(len(t) for t in texts),
        typical_chars=round(statistics.median(len(t) for t in texts)),
        max_words=max(len(t.split()) for t in texts),
        max_duration=min(6.0, max(1.0, max(d for _, d in rows))),
        case="upper" if upper >= 0.97 else "lower" if upper <= 0.03 else "asis",
        punctuation=punct,
    )


def template_blueprint(template_name: str, projects_dir: Path = CAPCUT_PROJECTS_DIR) -> dict:
    """What the template's own video is like, for the AI's brief: what was said
    (its subtitles), which texts appear on screen and when, how sounds are
    used, how fast it cuts."""
    info = json.loads((projects_dir / template_name / "draft_info.json").read_text())
    index = _materials_index(info)
    caps = _caption_tracks(info, index)
    cap_ids = {t["id"] for t in caps}

    def spans(tracks) -> list[tuple[float, float, str]]:
        out = []
        for tr in tracks:
            for s in tr["segments"]:
                _, inner = _text_of(index, s)
                text = " ".join(_shown_text(inner).split()) if inner else ""
                if text:
                    tt = s.get("target_timerange") or {}
                    start = (tt.get("start", 0) or 0) / 1e6
                    out.append((start, start + _seg_seconds(s), text))
        return sorted(out)

    sounds = []
    for tr in info["tracks"]:
        if tr["type"] != "audio":
            continue
        for s in tr["segments"]:
            bucket, mat = index.get(s.get("material_id"), (None, None))
            if bucket != "audios":
                continue
            cat = _classify_audio(mat, _seg_seconds(s))
            if cat != "leftover":
                start = ((s.get("target_timerange") or {}).get("start", 0) or 0) / 1e6
                sounds.append((start, _seg_seconds(s), mat.get("name") or "suono",
                               "musica" if cat == "music" else _CATEGORY_LABEL.get(cat, cat)))
    try:
        main = _find_main_video_track(info)
    except ValueError:
        main = {"id": None, "segments": []}
    overlays = sum(len(t["segments"]) for t in info["tracks"]
                   if t["type"] == "video" and t["id"] != main["id"])
    return {
        "name": template_name,
        "duration": (info.get("duration") or 0) / 1e6,
        "captions": spans(caps),
        "texts": spans(t for t in info["tracks"] if t["type"] == "text" and t["id"] not in cap_ids),
        "sounds": sorted(sounds),
        "shots": len(main["segments"]),
        "overlays": overlays,
    }


# ---------------------------------------------------------------------------

def build_from_template(
    template_name: str,
    out_name: str,
    timeline: list[TimelineSegment],
    subtitles: list[SubtitleChunk],
    emphasis: list[Emphasis] | None = None,
    intro_title: str | None = None,
    clear_template_texts_enabled: bool = False,
    sound_plan=None,
    covers: list[CoverShot] | None = None,
    projects_dir: Path = CAPCUT_PROJECTS_DIR,
    log=lambda _line: None,
) -> Path:
    src = projects_dir / template_name
    if not (src / "draft_info.json").exists():
        raise FileNotFoundError(f"Template draft not found: {src}")

    info = json.loads((src / "draft_info.json").read_text())

    main_video = _find_main_video_track(info)
    index = _materials_index(info)
    caption_tracks = _caption_tracks(info, index)
    caption_ids = {t["id"] for t in caption_tracks}
    sub_track = caption_tracks[0] if caption_tracks else None
    proto = _caption_proto(index, sub_track) if sub_track else None
    overlay_style = _find_overlay_style(info, exclude_track_ids=caption_ids)

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

    # ---- overlay video tracks ----
    # The template's own overlay *video* clips are footage of the project it was
    # copied from, so they go (photos/logos stay). Then the AI's cover shots.
    video_mats = {v["id"]: v for v in info["materials"]["videos"]}
    dropped: list[str] = []
    for tr in info["tracks"]:
        if tr["type"] != "video" or tr["id"] == main_video["id"]:
            continue
        kept_segs = []
        for s in tr["segments"]:
            mat = video_mats.get(s.get("material_id"), {})
            if mat.get("type") == "video":
                dropped.append(mat.get("material_name") or "clip")
            else:
                kept_segs.append(s)
        tr["segments"] = kept_segs
    if dropped:
        log(f"Tolte {len(dropped)} clip video sovrapposte del template (riprese del progetto "
            f"da cui è stato copiato): {', '.join(sorted(set(dropped))[:6])}")
    if covers:
        top = max((s.get("render_index", 0) for t in info["tracks"] if t["type"] == "video"
                   for s in t["segments"]), default=0)
        info["tracks"].append(cover_track(covers, info["materials"], src_to_mat,
                                          render_index=top + 1,
                                          track_index=len(info["tracks"])))

    # ---- subtitles ----
    # The template's own subtitles (on every caption track) belong to the video
    # it was made for: they all go, materials included. The new ones are cloned
    # from the most typical of them.
    old_caption_mats = _caption_material_ids(index, caption_tracks)
    old_count = sum(len(t["segments"]) for t in caption_tracks)
    for tr in caption_tracks:
        tr["segments"] = []
    if subtitles and proto is not None:
        for n, sub in enumerate(subtitles):
            sub_track["segments"].append(_clone_caption(info, index, proto, sub, n))
        log(f"Sottotitoli: {len(subtitles)} blocchi, ognuno clonato dal sottotitolo "
            f"«{_shown_text(proto.inner).strip()}» del template ("
            + ("modello di testo, " if proto.wrapper is not None else "")
            + "animazione, effetto, font e posizione identici); tolti i "
            f"{old_count} sottotitoli del vecchio video.")
    elif subtitles:
        # no subtitles in the template to copy the look from: plain default style,
        # on a track of their own so the template's titles stay where they are
        sub_track = {"id": _uid(), "type": "text", "attribute": 0, "flag": 0,
                     "segments": [], "is_default_name": True, "name": ""}
        for sub in subtitles:
            tm = _text_material(sub.text)
            anim = _default_anim()
            info["materials"]["texts"].append(tm)
            info["materials"]["material_animations"].append(anim)
            sub_track["segments"].append(_text_segment(
                _us(sub.timeline_start), max(1, _us(sub.timeline_end - sub.timeline_start)),
                tm["id"], [anim["id"]]))
        info["tracks"].append(sub_track)
        caption_ids.add(sub_track["id"])
        log(f"⚠ Il template non contiene sottotitoli da cui copiare lo stile: i "
            f"{len(subtitles)} sottotitoli usano lo stile base. Per averli come li vuoi, "
            f"usa come template un progetto che abbia già i sottotitoli.")
    elif old_count:
        log(f"Tolti i {old_count} sottotitoli del vecchio video (in questo reel non c'è parlato).")
    _drop_unused(info, old_caption_mats)

    # ---- emphasis text overlays (on a fresh text track, clean clip) ----
    emphasis_track_id: str | None = None
    if emphasis and overlay_style is not None:
        em_track = _emphasis_track()
        emphasis_track_id = em_track["id"]
        clean_clip = _emphasis_clip()
        for em in emphasis:
            start_us = _us(em.timeline_start)
            dur_us = max(1, _us(em.timeline_end - em.timeline_start))
            seg, new_tmpl, new_inner = _clone_overlay_text(overlay_style, em.text, start_us, dur_us)
            # Override the inherited (often tilted/off-center) overlay clip
            # with a clean upper-third position — emphasis should look like a
            # callout, not like a template intro sticker.
            seg["clip"] = copy.deepcopy(clean_clip)
            # Drop template-specific extra refs to avoid weird animations/effects
            # pulling the text off-position
            seg["extra_material_refs"] = []
            if new_tmpl is not None:
                info["materials"]["text_templates"].append(new_tmpl)
            info["materials"]["texts"].append(new_inner)
            em_track["segments"].append(seg)
        info["tracks"].append(em_track)
    elif emphasis:
        log(f"⚠ I {len(emphasis)} testi a schermo NON sono stati inseriti: il template non "
            f"ha testi (oltre ai sottotitoli) da cui copiare lo stile.")

    # ---- intro title ----
    # Written into the template's own opening title when it has one; otherwise
    # added as an on-screen text at the start, so it never silently disappears.
    own_track_ids = {main_video["id"]} | caption_ids
    if emphasis_track_id:
        own_track_ids.add(emphasis_track_id)
    if intro_title:
        slots = replace_intro_titles(info, intro_title, exclude_track_ids=own_track_ids)
        if slots:
            log(f"Titolo iniziale «{intro_title}» scritto al posto del titolo d'apertura "
                f"del template.")
        elif overlay_style is not None and timeline:
            total_us = _us(timeline[-1].timeline_end)
            first_em = min((_us(em.timeline_start) for em in emphasis or []), default=total_us)
            dur_us = max(1, min(_us(2.5), total_us, max(_us(1.0), first_em)))
            seg, _, new_inner = _clone_overlay_text(overlay_style, intro_title, 0, dur_us)
            seg["clip"] = copy.deepcopy(_emphasis_clip())
            seg["extra_material_refs"] = []
            title_track = _emphasis_track()
            title_track["segments"].append(seg)
            info["materials"]["texts"].append(new_inner)
            info["tracks"].append(title_track)
            own_track_ids.add(title_track["id"])
            log(f"Il template non ha un titolo nei primi 3 secondi: «{intro_title}» aggiunto "
                f"come testo a schermo all'inizio ({dur_us / 1e6:.1f}s, stile dei testi del template).")
        else:
            log(f"⚠ Titolo iniziale «{intro_title}» NON inserito: il template non ha né un "
                f"titolo d'apertura né altri testi da cui copiare lo stile.")

    # ---- optionally clear the template's residual text overlays
    # (intros that weren't replaced, "3 2 1 vai" countdowns, mid-video labels) ----
    if clear_template_texts_enabled:
        # protect the slots we used for the intro title — they're already correct
        protected_mat_ids: set[str] = set()
        if intro_title:
            for _start, _ti, mat in _intro_text_candidates(
                    info, exclude_track_ids=own_track_ids):
                protected_mat_ids.add(mat["id"])
        # Stash protected segments aside before clearing
        protected_segs: dict[str, list[dict]] = {}
        for tr in info["tracks"]:
            if tr["type"] != "text" or tr["id"] in own_track_ids:
                continue
            keep = []
            for s in tr["segments"]:
                if s.get("material_id") in protected_mat_ids:
                    keep.append(s)
            if keep:
                protected_segs[tr["id"]] = keep
        clear_template_texts(info, exclude_track_ids=own_track_ids)
        # restore protected (intro title) segments
        for tr in info["tracks"]:
            if tr["id"] in protected_segs:
                tr["segments"] = protected_segs[tr["id"]] + tr["segments"]

    # ---- sounds: rebuilt from the AI's plan (None = keep template audio as-is) ----
    if sound_plan is not None and timeline:
        apply_sound_plan(info, sound_plan, _us(timeline[-1].timeline_end), log)

    # ---- trim everything past the new total duration ----
    # The video timeline (T0) defines the *real* video length. Any template
    # overlay / audio / extra track segment that starts past the end is dropped;
    # anything straddling the end is truncated.
    new_total_us = _us(timeline[-1].timeline_end) if timeline else 0
    for tr in info["tracks"]:
        if tr["id"] in own_track_ids:
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
    sources = {ts.keep.source.path: ts.keep.source for ts in timeline}
    sources.update({c.source.path: c.source for c in covers or []})
    for path, _mid in src_to_mat.items():
        clip = sources[path]
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
