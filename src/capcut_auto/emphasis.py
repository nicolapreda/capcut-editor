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


def _phrase_around(words, idx: int, span: int = 2) -> str:
    """Pick ±span words around idx to form a short phrase."""
    lo = max(0, idx - span)
    hi = min(len(words), idx + span + 1)
    return " ".join(w.text.strip(_PUNCT_STRIP) for w in words[lo:hi]).strip()


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

            phrase = _phrase_around(words, i, span=1).upper()
            if not phrase:
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
