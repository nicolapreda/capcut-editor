"""Word-level cleanup: drop fillers and re-take detection.

Two independent passes you can enable per run:

1. **Fillers** — drop transcript words like "ehm", "uhm", "cioè" that are usually
   noise in social-video edits.

2. **Retakes** — detect "false start, pause, restart with the corrected phrase"
   patterns and drop the false start. Heuristic: a pause ≥ `retake_gap`
   followed by a phrase that fuzzy-matches the K words before the pause means
   the speaker re-did the line; keep the second take, discard the first.

Both passes work on the raw word stream (before cuts) and operate purely on
timestamps + text — no need to re-process audio.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from rapidfuzz import fuzz

from .models import Word


# Italian fillers commonly produced by Whisper transcripts.
# "tipo" / "diciamo" / "praticamente" are aggressive — only enabled with
# drop_aggressive_fillers.
FILLERS_BASE = {"ehm", "uhm", "uh", "eh", "ah", "mh", "mmh", "boh", "ehh", "ehhh"}
FILLERS_AGGRESSIVE = FILLERS_BASE | {"cioè", "tipo", "diciamo", "praticamente",
                                     "insomma", "allora", "niente", "appunto"}

_PUNCT = ".,!?;:\"'()«»“”„‚‘’"


def _normalize(w: str) -> str:
    return w.lower().strip(_PUNCT).strip()


@dataclass
class CleanupReport:
    fillers_dropped: int = 0
    retakes_dropped: int = 0
    retake_examples: list[str] = None  # short snippets of what was dropped

    def __post_init__(self) -> None:
        if self.retake_examples is None:
            self.retake_examples = []


def drop_fillers(words: list[Word], aggressive: bool = False) -> tuple[list[Word], int]:
    bag = FILLERS_AGGRESSIVE if aggressive else FILLERS_BASE
    out = [w for w in words if _normalize(w.text) not in bag]
    return out, len(words) - len(out)


def _fuzzy_phrase_match(a: list[Word], b: list[Word]) -> float:
    """Token-set similarity 0..100 between two phrase candidates."""
    if not a or not b:
        return 0.0
    sa = " ".join(_normalize(w.text) for w in a)
    sb = " ".join(_normalize(w.text) for w in b)
    if not sa or not sb:
        return 0.0
    return fuzz.token_set_ratio(sa, sb)


def _split_sentences(words: list[Word], gap: float = 0.5) -> list[tuple[int, int]]:
    """Split the word stream into sentences on a pause ≥ `gap` OR terminal
    punctuation. Returns (start_idx, end_idx_inclusive) ranges."""
    if not words:
        return []
    sents: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(words)):
        g = words[i].start - words[i - 1].end
        prev_terminal = words[i - 1].text.strip().endswith((".", "!", "?"))
        if g >= gap or prev_terminal:
            sents.append((start, i - 1))
            start = i
    sents.append((start, len(words) - 1))
    return sents


def dedupe_sentences(
    words: list[Word],
    adjacent_sim: float = 80.0,     # false starts / immediate retakes
    repeat_sim: float = 86.0,       # the same line said again later
    lookahead: int = 8,
    min_words: int = 3,
    gap: float = 0.5,
) -> tuple[list[Word], int, list[str]]:
    """Remove sentences that are near-duplicates of a LATER sentence.

    Handles two real-world cases at once:
      * **false starts / retakes** — a short/partial sentence immediately re-said
        (adjacent, subset match via token_set_ratio);
      * **repeats** — the same line delivered again a few sentences later
        (order-independent match via token_sort_ratio within `lookahead`).

    Always keeps the LAST occurrence (usually the cleaner take).
    """
    sents = _split_sentences(words, gap=gap)
    if len(sents) < 2:
        return list(words), 0, []

    norm = [
        " ".join(_normalize(words[k].text) for k in range(s, e + 1)).strip()
        for (s, e) in sents
    ]
    drop = [False] * len(sents)

    for a in range(len(sents)):
        if drop[a]:
            continue
        na = norm[a]
        if len(na.split()) < min_words:
            continue
        ta = set(na.split())
        for b in range(a + 1, min(len(sents), a + 1 + lookahead)):
            if drop[b] or not norm[b]:
                continue
            hit = False
            if b == a + 1:
                # adjacent: a full-similarity retake, OR a false start whose words
                # are mostly contained in the following (longer) sentence.
                tb = set(norm[b].split())
                containment = len(ta & tb) / len(ta) if ta else 0.0
                if (fuzz.token_sort_ratio(na, norm[b]) >= adjacent_sim
                        or containment >= 0.75):
                    hit = True
            else:
                # a repeat delivered again a few sentences later
                if fuzz.token_sort_ratio(na, norm[b]) >= repeat_sim:
                    hit = True
            if hit:
                drop[a] = True   # keep the later occurrence (b)
                break

    kept: list[Word] = []
    dropped_examples: list[str] = []
    for idx, (s, e) in enumerate(sents):
        if drop[idx]:
            dropped_examples.append(" ".join(words[k].text for k in range(s, e + 1)))
        else:
            kept.extend(words[s : e + 1])
    return kept, len(words) - len(kept), dropped_examples


def detect_retakes(
    words: list[Word],
    retake_gap: float = 0.6,
    window: int = 5,
    min_similarity: float = 70.0,
) -> tuple[list[Word], int, list[str]]:
    """Drop false-start phrases that are immediately re-said after a pause.

    For every gap ≥ retake_gap, compare up to `window` words before vs after
    the gap. If they fuzzy-match (≥ min_similarity), drop the "before" block
    (up to the previous gap or sentence boundary) and keep the "after".
    """
    if len(words) < 2:
        return list(words), 0, []

    examples: list[str] = []
    keep_mask = [True] * len(words)
    # iterate left-to-right; for each gap, scan back to find the start of the
    # "before" phrase and decide if we drop it
    i = 1
    while i < len(words):
        gap = words[i].start - words[i - 1].end
        if gap < retake_gap:
            i += 1
            continue

        # Define the "after" window (up to `window` consecutive words from i)
        after_end = i
        while after_end < len(words) and after_end < i + window:
            if after_end > i:
                inner_gap = words[after_end].start - words[after_end - 1].end
                if inner_gap >= retake_gap:
                    break
            after_end += 1
        after = words[i:after_end]

        # Define the "before" window: look back up to `window` words or to the
        # previous gap
        before_start = i - 1
        while before_start > 0 and (i - before_start) < window:
            prev_gap = words[before_start].start - words[before_start - 1].end
            if prev_gap >= retake_gap:
                break
            before_start -= 1
        before = words[before_start:i]

        # only drop if they're plausibly similar AND the "before" block looks
        # like an incomplete sentence (ends without terminal punctuation)
        last_before = before[-1].text.strip() if before else ""
        terminal_punct = last_before.endswith((".", "!", "?"))
        sim = _fuzzy_phrase_match(before, after)
        if sim >= min_similarity and not terminal_punct:
            for j in range(before_start, i):
                keep_mask[j] = False
            examples.append(
                f"«{' '.join(w.text for w in before)}» → «{' '.join(w.text for w in after)}»"
            )
        i = after_end

    cleaned = [w for w, keep in zip(words, keep_mask) if keep]
    return cleaned, len(words) - len(cleaned), examples


def cleanup_words(
    words: list[Word],
    drop_fillers_enabled: bool = True,
    aggressive_fillers: bool = False,
    drop_retakes_enabled: bool = True,
    retake_gap: float = 0.6,
    log: Callable[[str], None] = print,
) -> tuple[list[Word], CleanupReport]:
    report = CleanupReport()
    out = list(words)

    if drop_fillers_enabled:
        out, n = drop_fillers(out, aggressive=aggressive_fillers)
        report.fillers_dropped = n

    if drop_retakes_enabled:
        # sentence-level dedup: catches false starts AND repeated lines anywhere
        out, n, examples = dedupe_sentences(out)
        report.retakes_dropped = n
        report.retake_examples = examples

    return out, report


# ---------------------------------------------------------------------------
# Script mismatch reporting (used with --script)
# ---------------------------------------------------------------------------

@dataclass
class ScriptMismatch:
    unmatched_script_lines: list[str]
    off_script_transcript: list[str]   # transcript runs that didn't match any script line


def script_mismatch_report(
    script_lines: list[str],
    transcript_words: list[Word],
    matched_word_spans: list[tuple[int, int]],
    min_run_words: int = 3,
) -> ScriptMismatch:
    """Given the matched spans produced by cuts_from_script, list what was
    dropped from the transcript and what script lines weren't found."""
    # script lines not found are tracked at call-site (we don't have access to
    # individual line scores here); this helper focuses on off-script runs.
    matched_idx: set[int] = set()
    for s, e in matched_word_spans:
        for j in range(s, e + 1):
            matched_idx.add(j)

    off_script_runs: list[list[Word]] = []
    cur: list[Word] = []
    for i, w in enumerate(transcript_words):
        if i not in matched_idx:
            cur.append(w)
        else:
            if len(cur) >= min_run_words:
                off_script_runs.append(cur)
            cur = []
    if len(cur) >= min_run_words:
        off_script_runs.append(cur)

    return ScriptMismatch(
        unmatched_script_lines=[],  # filled in by caller
        off_script_transcript=[" ".join(w.text for w in run) for run in off_script_runs],
    )
