"""Decide which intervals of each source clip to keep on the final timeline.

Cuts are tight by default: every silence ≥ `min_silence` is removed entirely,
with a small `head_pad` before the first word of each kept group and a much
smaller `tail_pad` after the last word so words don't get clipped.

Three pacing presets exposed at the top of the module:
    PACING_NORMAL      — natural pauses preserved
    PACING_FAST        — punchy social video (default)
    PACING_AGGRESSIVE  — maximum density, no breathing room

Script-driven cuts: when a script is given, keep only the transcript regions
that fuzzy-match script lines, then apply the same tight-cut logic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from .models import KeepInterval, SourceClip, TimelineSegment, Word


@dataclass(frozen=True)
class Pacing:
    name: str
    min_silence: float   # gaps ≥ this are cut
    head_pad: float      # silence kept before first word of a group
    tail_pad: float      # silence kept after last word of a group
    min_keep_dur: float  # discard kept regions shorter than this (filler "uhm" etc.)


# Whisper marks word.start LATER than the perceptual start (adds silence before
# the word) and word.end EARLIER than the perceptual end (clips the tail of the
# word). So we keep head_pad small/zero (no extra silence pre-word) and a much
# bigger tail_pad (extends past Whisper's end so the last consonant survives).
PACING_NORMAL = Pacing("normal",         min_silence=0.40, head_pad=0.03, tail_pad=0.16, min_keep_dur=0.15)
PACING_FAST = Pacing("fast",             min_silence=0.22, head_pad=0.01, tail_pad=0.12, min_keep_dur=0.10)
PACING_AGGRESSIVE = Pacing("aggressive", min_silence=0.10, head_pad=0.0,  tail_pad=0.08, min_keep_dur=0.08)

PACING_BY_NAME = {p.name: p for p in (PACING_NORMAL, PACING_FAST, PACING_AGGRESSIVE)}


def _build_intervals(words: list[Word], clip_duration: float, pacing: Pacing) -> list[KeepInterval]:
    """Walk words; start a new group every time the gap to the previous word ≥ min_silence."""
    if not words:
        return []

    out: list[KeepInterval] = []
    cur: list[Word] = [words[0]]

    def _flush(group: list[Word]) -> None:
        if not group:
            return
        start = max(0.0, group[0].start - pacing.head_pad)
        end = min(clip_duration, group[-1].end + pacing.tail_pad)
        if end - start < pacing.min_keep_dur:
            return
        out.append(KeepInterval(source=cur_source, src_start=start, src_end=end, words=list(group)))

    cur_source = None  # set per call below

    for w in words[1:]:
        gap = w.start - cur[-1].end
        if gap >= pacing.min_silence:
            _flush(cur)
            cur = [w]
        else:
            cur.append(w)
    _flush(cur)
    return out


def cuts_silence(clip: SourceClip, pacing: Pacing = PACING_FAST) -> list[KeepInterval]:
    if not clip.words:
        return []
    out: list[KeepInterval] = []
    cur: list[Word] = [clip.words[0]]

    def _flush(group: list[Word]) -> None:
        if not group:
            return
        start = max(0.0, group[0].start - pacing.head_pad)
        end = min(clip.duration, group[-1].end + pacing.tail_pad)
        if end - start < pacing.min_keep_dur:
            return
        out.append(KeepInterval(source=clip, src_start=start, src_end=end, words=list(group)))

    for w in clip.words[1:]:
        gap = w.start - cur[-1].end
        if gap >= pacing.min_silence:
            _flush(cur)
            cur = [w]
        else:
            cur.append(w)
    _flush(cur)
    return out


# ---- script-driven cuts ----------------------------------------------------

_NORM = re.compile(r"[^a-zà-ÿ0-9 ]+", re.IGNORECASE)


def _norm(s: str) -> str:
    return _NORM.sub(" ", s.lower()).strip()


# Spoken lines in a "real" script are wrapped in quotes; everything else
# (headings like "HOOK (0-3 sec)", stage directions, metadata) is NOT spoken.
# Match straight ("..."), curly (“...”) and guillemet («...») double quotes.
_QUOTE_PATTERNS = [
    re.compile(r'"([^"]{2,})"'),
    re.compile(r'“([^”]{2,})”'),
    re.compile(r'«([^»]{2,})»'),
]


def _extract_quoted_lines(script: str) -> list[str]:
    """Pull out only the quoted (spoken) spans, in document order.

    Filters out incidental single quoted words (e.g. a direction like
    Entri nella "stanza") by requiring ≥ 2 words.
    """
    hits: list[tuple[int, str]] = []
    for pat in _QUOTE_PATTERNS:
        for m in pat.finditer(script):
            candidate = m.group(1).strip()
            if len(candidate.split()) >= 2:
                hits.append((m.start(), candidate))
    hits.sort(key=lambda x: x[0])
    return [t for _pos, t in hits]


def _script_lines(script: str) -> list[str]:
    """Turn a script into matchable lines.

    If the script contains quoted dialogue (a structured screenplay-style doc),
    use ONLY the quoted lines — headings, stage directions and metadata are
    ignored. Otherwise fall back to splitting every line on sentence boundaries.
    """
    quoted = _extract_quoted_lines(script)
    source_blocks = quoted if len(quoted) >= 2 else script.splitlines()

    lines: list[str] = []
    for raw in source_blocks:
        for part in re.split(r"(?<=[.!?])\s+", raw):
            t = part.strip().strip('"“”«»')
            if len(t) >= 3:
                lines.append(t)
    return lines


def match_script_to_transcript(
    clip: SourceClip, script: str, min_match: float = 60.0,
) -> tuple[list[tuple[int, int]], list[str]]:
    """Find which transcript word-spans correspond to which script lines.

    Returns:
        merged_spans: list of (start_idx, end_idx_inclusive) word-spans that matched
        unmatched_lines: script lines whose best fuzzy-match score was below min_match
    """
    if not clip.words:
        return [], _script_lines(script)
    lines = _script_lines(script)
    if not lines:
        return [], []

    words = clip.words
    n = len(words)
    norm_words = [_norm(w.text) for w in words]
    kept_word_spans: list[tuple[int, int]] = []
    unmatched: list[str] = []

    cursor = 0
    for line in lines:
        target = _norm(line)
        target_wc = max(1, len(target.split()))
        best = None
        max_len = min(n - cursor, target_wc * 3 + 4)
        min_len = max(1, target_wc // 2)
        for i in range(cursor, n):
            for L in range(min_len, max_len + 1):
                j = i + L
                if j > n:
                    break
                window = " ".join(norm_words[i:j])
                score = fuzz.token_set_ratio(target, window)
                if best is None or score > best[0]:
                    best = (score, i, j - 1)
                if score >= 95:
                    break
            if best and best[0] >= 95:
                break

        if best and best[0] >= min_match:
            _, si, ei = best
            kept_word_spans.append((si, ei))
            cursor = ei + 1
        else:
            unmatched.append(line)

    kept_word_spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in kept_word_spans:
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged, unmatched


def cuts_from_script(
    clip: SourceClip,
    script: str,
    pacing: Pacing = PACING_FAST,
    min_match: float = 60.0,
) -> list[KeepInterval]:
    """For each script line, find the best contiguous transcript window, then apply tight cuts."""
    if not clip.words:
        return []
    merged, _unmatched = match_script_to_transcript(clip, script, min_match=min_match)
    if not merged:
        return cuts_silence(clip, pacing)
    words = clip.words

    # Apply tight pacing within each matched span
    out: list[KeepInterval] = []
    for si, ei in merged:
        span_words = words[si : ei + 1]
        # synthesize a temporary clip-like-view; reuse cuts_silence by constructing
        # an interval list inline.
        if not span_words:
            continue
        cur = [span_words[0]]
        for w in span_words[1:]:
            gap = w.start - cur[-1].end
            if gap >= pacing.min_silence:
                start = max(0.0, cur[0].start - pacing.head_pad)
                end = min(clip.duration, cur[-1].end + pacing.tail_pad)
                if end - start >= pacing.min_keep_dur:
                    out.append(KeepInterval(source=clip, src_start=start, src_end=end, words=list(cur)))
                cur = [w]
            else:
                cur.append(w)
        if cur:
            start = max(0.0, cur[0].start - pacing.head_pad)
            end = min(clip.duration, cur[-1].end + pacing.tail_pad)
            if end - start >= pacing.min_keep_dur:
                out.append(KeepInterval(source=clip, src_start=start, src_end=end, words=list(cur)))
    return out


def place_on_timeline(kept: list[KeepInterval]) -> list[TimelineSegment]:
    out: list[TimelineSegment] = []
    cursor = 0.0
    for k in kept:
        out.append(TimelineSegment(keep=k, timeline_start=cursor, timeline_end=cursor + k.duration))
        cursor += k.duration
    return out
