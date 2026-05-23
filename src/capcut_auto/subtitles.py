"""Group words into short subtitle chunks suited for vertical social video."""
from __future__ import annotations

from .models import SubtitleChunk, TimelineSegment, Word


def build_subtitles(
    segments: list[TimelineSegment],
    max_words: int = 4,
    max_chars: int = 28,
    max_duration: float = 1.6,
    max_gap_within_chunk: float = 0.35,
) -> list[SubtitleChunk]:
    """For each timeline segment, walk its words and emit chunks in timeline time.

    Word timestamps are in source-clip coordinates; convert to timeline by
    subtracting src_start and adding timeline_start.
    """
    chunks: list[SubtitleChunk] = []
    for ts in segments:
        offset = ts.timeline_start - ts.keep.src_start
        cur: list[Word] = []
        cur_start = 0.0
        cur_end = 0.0
        char_count = 0
        for w in ts.keep.words:
            w_start = w.start + offset
            w_end = w.end + offset
            text = w.text
            would_overflow = (
                len(cur) >= max_words
                or char_count + len(text) + 1 > max_chars
                or (cur and (w_end - cur_start) > max_duration)
                or (cur and (w_start - cur_end) > max_gap_within_chunk)
            )
            if would_overflow and cur:
                chunks.append(SubtitleChunk(
                    text=" ".join(x.text for x in cur),
                    timeline_start=cur[0].start + offset,
                    timeline_end=cur[-1].end + offset,
                ))
                cur = []
                char_count = 0
            if not cur:
                cur_start = w_start
            cur.append(w)
            cur_end = w_end
            char_count += len(text) + (1 if char_count else 0)
        if cur:
            chunks.append(SubtitleChunk(
                text=" ".join(x.text for x in cur),
                timeline_start=cur[0].start + offset,
                timeline_end=cur[-1].end + offset,
            ))
    return chunks
