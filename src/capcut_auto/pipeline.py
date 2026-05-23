"""Reusable end-to-end pipeline, callable from both CLI and GUI."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .cuts import PACING_BY_NAME, PACING_FAST, Pacing, cuts_from_script, cuts_silence
from .draft import CAPCUT_PROJECTS_DIR, build_draft
from .emphasis import detect_emphasis
from .models import TimelineSegment
from .probe import probe
from .stabilize import stabilize
from .subtitles import build_subtitles
from .template import build_from_template
from .transcribe import transcribe


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def _is_video(p: Path) -> bool:
    # Skip macOS AppleDouble sidecars (e.g. "._C0867.MP4") that share a video
    # suffix but hold no media — ffprobe exits non-zero on them.
    if p.name.startswith("._"):
        return False
    return p.suffix.lower() in VIDEO_EXTS


def gather_inputs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        files = sorted(p for p in input_path.iterdir() if _is_video(p))
        if not files:
            raise ValueError(f"Nessun video trovato in {input_path}")
        return files
    raise ValueError(f"Percorso inesistente: {input_path}")


def _resolve_pacing(pacing: str | Pacing) -> Pacing:
    if isinstance(pacing, Pacing):
        return pacing
    return PACING_BY_NAME.get(pacing, PACING_FAST)


def run_pipeline(
    input_path: Path,
    name: str,
    template: str | None = None,
    script: str | None = None,
    model: str = "small",
    language: str = "it",
    pacing: str | Pacing = "fast",
    stabilize_clips: bool = False,
    redistribute_sfx_enabled: bool = True,
    add_emphasis: bool = True,
    emphasis_count: int = 4,
    subtitle_max_words: int = 4,
    subtitle_max_duration: float = 1.4,
    fmt: str = "vertical",
    projects_dir: Path | None = None,
    log: Callable[[str], None] = print,
) -> Path:
    """Run the whole pipeline. Returns path to generated draft folder."""
    projects_dir = projects_dir or CAPCUT_PROJECTS_DIR
    pacing_obj = _resolve_pacing(pacing)
    inputs = gather_inputs(input_path)
    log(f"→ {len(inputs)} input · modello={model} · lingua={language} · pacing={pacing_obj.name}")
    if template:
        log(f"→ template: {template}")
    if stabilize_clips:
        log("→ stabilizzazione: ON")

    canvas = (1080, 1920) if fmt == "vertical" else (1920, 1080)

    # 1. (optional) stabilize originals, then probe + transcribe
    clips = []
    for p in inputs:
        path = stabilize(p, log=log) if stabilize_clips else p
        log(f"  • probing  {path.name}")
        c = probe(path)
        log(f"  • trascrivo  {path.name} ({c.duration:.1f}s)…")
        transcribe(c, model_name=model, language=language)
        log(f"    → {len(c.words)} parole")
        clips.append(c)

    # 2. cut intervals + place on timeline
    timeline: list[TimelineSegment] = []
    cursor = 0.0
    for clip in clips:
        kept = (
            cuts_from_script(clip, script, pacing=pacing_obj)
            if script else
            cuts_silence(clip, pacing=pacing_obj)
        )
        log(f"  • {clip.path.name}: tenuti {len(kept)} segmenti, totale {sum(k.duration for k in kept):.1f}s")
        for k in kept:
            timeline.append(TimelineSegment(keep=k, timeline_start=cursor, timeline_end=cursor + k.duration))
            cursor += k.duration

    if not timeline:
        raise ValueError("Niente da mettere in timeline — abbassa il pacing o controlla che ci sia parlato.")

    # 3. subtitles
    subs = build_subtitles(timeline, max_words=subtitle_max_words, max_duration=subtitle_max_duration)
    log(f"  • sottotitoli: {len(subs)} blocchi")

    # 4. emphasis text overlays (only meaningful with a template that has overlay styles)
    emphasis = []
    if add_emphasis and template:
        emphasis = detect_emphasis(timeline, max_count=emphasis_count)
        if emphasis:
            log(f"  • enfasi rilevate: {len(emphasis)} → {', '.join(e.text for e in emphasis)}")

    # 5. write draft
    if template:
        out = build_from_template(
            template_name=template, out_name=name,
            timeline=timeline, subtitles=subs,
            emphasis=emphasis or None,
            redistribute_sfx_enabled=redistribute_sfx_enabled,
            projects_dir=projects_dir,
        )
    else:
        out = build_draft(name=name, timeline=timeline, subtitles=subs,
                          canvas_w=canvas[0], canvas_h=canvas[1],
                          projects_dir=projects_dir)
    log(f"✓ Draft scritto: {out}")
    return out
