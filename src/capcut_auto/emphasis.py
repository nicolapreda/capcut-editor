"""Detect emphatic moments in the transcript and produce text-overlay specs.

Heuristic: numbers/percentages/prices, exclamations, and a small Italian keyword
list ("mai", "sempre", "ricorda"...) score points. Top-N scoring moments win.

We return moments expressed in **timeline time** (post-cut), so the caller can
drop them straight into a CapCut text track without further mapping.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import TimelineSegment


_KEYWORDS = {
    # impact words common in Italian short-form selling/teaching
    "mai", "sempre", "tutti", "nessuno", "zero",
    "ricorda", "ricordati", "attenzione", "importante", "fondamentale",
    "segreto", "trucco", "errore", "sbagliato", "giusto",
    "gratis", "subito", "ora", "veloce", "facile",
    "boom", "incredibile", "pazzesco",
}

_NUM_RE = re.compile(r"\d")
_PUNCT_STRIP = ".,!?;:\"'()«»“”„‚‘’"


@dataclass
class Emphasis:
    text: str            # display string (uppercase)
    timeline_start: float
    timeline_end: float
    score: int


def _clause_bounds(words, idx: int, silence_thresh: float = 0.35) -> tuple[int, int]:
    """Find the indices [lo, hi] (inclusive) of the smallest clause that
    contains words[idx], where a clause is delimited by silences ≥ silence_thresh
    or by terminal punctuation on a preceding/following word."""
    lo = idx
    while lo > 0:
        gap = words[lo].start - words[lo - 1].end
        if gap >= silence_thresh:
            break
        prev_text = words[lo - 1].text.strip()
        if prev_text.endswith((".", "!", "?")):
            break
        lo -= 1
    hi = idx
    while hi < len(words) - 1:
        gap = words[hi + 1].start - words[hi].end
        if gap >= silence_thresh:
            break
        cur_text = words[hi].text.strip()
        if cur_text.endswith((".", "!", "?")):
            break
        hi += 1
    return lo, hi


def _meaningful_phrase(words, idx: int, max_words: int = 4,
                       silence_thresh: float = 0.35) -> str:
    """Extract a short meaningful phrase around the emphasis word at `idx`.

    Stays inside the surrounding clause (silence-bounded), caps at max_words,
    and tries to center on the trigger word."""
    lo, hi = _clause_bounds(words, idx, silence_thresh)
    if hi - lo + 1 <= max_words:
        chosen = words[lo : hi + 1]
    else:
        # center on idx, prefer extending forward (subject usually before number)
        forward = max_words - 1
        back = 0
        s = max(lo, idx - back)
        e = min(hi, s + max_words - 1)
        if e - s + 1 < max_words:
            s = max(lo, e - max_words + 1)
        chosen = words[s : e + 1]
    return " ".join(w.text.strip(_PUNCT_STRIP) for w in chosen).strip()


def detect_emphasis(timeline: list[TimelineSegment], max_count: int = 4,
                    min_gap: float = 2.5) -> list[Emphasis]:
    """Pick up to `max_count` emphatic moments, spaced ≥ `min_gap` seconds apart."""
    candidates: list[Emphasis] = []

    for ts in timeline:
        offset = ts.timeline_start - ts.keep.src_start
        words = ts.keep.words
        for i, w in enumerate(words):
            raw = w.text.strip(_PUNCT_STRIP)
            lower = raw.lower()
            score = 0
            if _NUM_RE.search(raw):
                score += 5
            if lower in _KEYWORDS:
                score += 3
            if "!" in w.text:
                score += 2
            if raw.isupper() and len(raw) >= 2:
                score += 2
            if len(lower) >= 8:
                score += 1
            if score < 3:
                continue

            phrase = _meaningful_phrase(words, i, max_words=4).upper()
            if not phrase or len(phrase) < 2:
                continue
            # display time: from the emphasis word, hold for ~1.5–2s
            start = max(0.0, w.start + offset - 0.1)
            end = min(ts.timeline_end, max(start + 1.8, w.end + offset + 0.4))
            candidates.append(Emphasis(text=phrase, timeline_start=start, timeline_end=end, score=score))

    # sort by score desc, then dedup overlapping/too-close picks
    candidates.sort(key=lambda e: -e.score)
    picks: list[Emphasis] = []
    for c in candidates:
        if any(abs(p.timeline_start - c.timeline_start) < min_gap for p in picks):
            continue
        picks.append(c)
        if len(picks) >= max_count:
            break

    picks.sort(key=lambda e: e.timeline_start)
    return picks
