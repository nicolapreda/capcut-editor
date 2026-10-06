"""Group the spoken words into subtitle blocks.

Two ways to size them: by hand (words / seconds per block), or the way the
template's own subtitles are written — phrase-shaped blocks, its letter case,
its punctuation — as measured by `template.template_subtitle_look`.
"""
from __future__ import annotations

import re

from .models import SubtitleChunk, TimelineSegment, Word

_PUNCT = ".,;:!?…"

# words a new block reads well starting from
_OPENERS = {
    "e", "ed", "ma", "o", "oppure", "che", "perché", "quindi", "però", "se", "quando",
    "mentre", "dove", "come", "con", "per", "anche", "poi", "così", "infatti", "cioè",
    "invece", "and", "but", "or", "so", "because", "that", "when", "with",
}


def style_text(text: str, case: str = "asis", punctuation: str | None = None) -> str:
    """Write a subtitle the way the template does: its letter case and only the
    punctuation marks it keeps (None leaves punctuation alone)."""
    if punctuation is not None:
        drop = "".join(p for p in _PUNCT if p not in punctuation)
        if drop:
            # only marks closing a word: "200.000" and "1,5" stay whole
            text = re.sub(rf"[{re.escape(drop)}]+(?=\s|$)", "", text)
    text = " ".join(text.split())
    # Whisper sometimes splits an elision in two words ("c" + "'è"): rejoin it
    text = re.sub(r"(?<=\w) (['’])(?=\w)", r"\1", text)
    return text.upper() if case == "upper" else text.lower() if case == "lower" else text


# words a block shouldn't end on: they belong with what follows
_DANGLING = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "a", "da", "in", "su",
    "tra", "fra", "del", "dello", "della", "dei", "degli", "delle", "al", "allo", "alla",
    "ai", "agli", "alle", "nel", "nella", "nei", "nelle", "sul", "sulla", "dal", "dalla",
    "non", "più", "the", "an", "of", "to", "in", "on", "at",
} | _OPENERS


def _chars(words: list[Word]) -> int:
    return sum(len(w.text.strip()) for w in words) + max(0, len(words) - 1)


def _split_phrase(words: list[Word], max_chars: int, max_duration: float) -> list[list[Word]]:
    """Cut one phrase into as few blocks as fit, of similar length, breaking
    where it reads best (after a comma, before a connective, never leaving an
    article or preposition hanging)."""
    def fits(a: int, b: int) -> bool:           # words[a:b]
        return (b - a == 1 or (_chars(words[a:b]) <= max_chars
                               and words[b - 1].end - words[a].start <= max_duration))

    n_words = len(words)
    if fits(0, n_words):
        return [words]

    def break_cost(j: int) -> float:            # cost of starting a block at word j
        before = words[j - 1].text.strip()
        if before[-1:] in ",;:":
            return 0.0
        if before.strip(_PUNCT).lower() in _DANGLING:
            return 0.6
        return 0.03 if words[j].text.strip(_PUNCT).lower() in _OPENERS else 0.12

    total = _chars(words)
    for n in range(2, n_words + 1):
        ideal = total / n
        # best[k][j] = cheapest way to cover words[:j] with k blocks
        best: list[dict[int, tuple[float, int]]] = [{0: (0.0, -1)}]
        for k in range(1, n + 1):
            row: dict[int, tuple[float, int]] = {}
            for j in range(k, n_words + 1):
                for i, (cost, _) in best[k - 1].items():
                    if i < j and fits(i, j):
                        c = (cost + ((_chars(words[i:j]) - ideal) / max_chars) ** 2
                             + (break_cost(i) if i else 0.0))
                        if j not in row or c < row[j][0]:
                            row[j] = (c, i)
            best.append(row)
        if n_words in best[n]:
            cuts, j = [], n_words
            for k in range(n, 0, -1):
                i = best[k][j][1]
                cuts.append((i, j))
                j = i
            return [words[a:b] for a, b in reversed(cuts)]
    return [[w] for w in words]


def build_subtitles(
    segments: list[TimelineSegment],
    max_words: int = 4,
    max_chars: int = 28,
    max_duration: float = 1.6,
    max_gap_within_chunk: float = 0.35,
    phrases: bool = False,
    case: str = "asis",
    punctuation: str | None = None,
) -> list[SubtitleChunk]:
    """Walk the reel's words and emit blocks in timeline time.

    By hand (`phrases` off) blocks hold up to `max_words` and never cross a cut.
    The template's way (`phrases` on) follows the speech like CapCut's own
    captions do: a block is a whole phrase — up to a full stop or a pause —
    and only phrases too long for `max_chars` / `max_duration` are split, into
    even parts. A jump cut inside a sentence doesn't break the block.
    """
    # word timestamps are in source-clip time: move them onto the timeline
    stream: list[tuple[Word, bool]] = []          # (word, opens a timeline segment)
    for ts in segments:
        offset = ts.timeline_start - ts.keep.src_start
        for i, w in enumerate(ts.keep.words):
            stream.append((Word(start=w.start + offset, end=w.end + offset, text=w.text), i == 0))

    blocks: list[list[Word]] = []
    cur: list[Word] = []
    for w, opens_segment in stream:
        text = w.text.strip()
        if cur:
            last = cur[-1].text.strip()
            pause = w.start - cur[-1].end > max_gap_within_chunk
            if phrases:
                close = pause or last[-1:] in ".!?…" or (opens_segment and last[-1:] in ",;:")
            else:
                close = (pause or opens_segment or len(cur) >= max_words
                         or _chars(cur) + len(text) + 1 > max_chars
                         or w.end - cur[0].start > max_duration)
            if close:
                blocks += _split_phrase(cur, max_chars, max_duration) if phrases else [cur]
                cur = []
        cur.append(w)
    if cur:
        blocks += _split_phrase(cur, max_chars, max_duration) if phrases else [cur]

    chunks = []
    for block in blocks:
        text = style_text(" ".join(w.text.strip() for w in block), case, punctuation)
        if text:
            chunks.append(SubtitleChunk(text=text, timeline_start=block[0].start,
                                        timeline_end=block[-1].end, words=block))
    return chunks
