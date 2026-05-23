from dataclasses import dataclass, field
from pathlib import Path


US = 1_000_000  # microseconds per second — CapCut's time unit


@dataclass
class Word:
    start: float  # seconds, in source file
    end: float
    text: str


@dataclass
class SourceClip:
    """A video file with its transcription and probe metadata."""
    path: Path
    duration: float           # seconds (probed from ffprobe)
    width: int
    height: int
    has_audio: bool
    fps: float
    words: list[Word] = field(default_factory=list)


@dataclass
class KeepInterval:
    """A region of a source clip to keep in the final cut."""
    source: SourceClip
    src_start: float          # seconds in source file
    src_end: float
    words: list[Word] = field(default_factory=list)  # words inside this interval

    @property
    def duration(self) -> float:
        return self.src_end - self.src_start


@dataclass
class TimelineSegment:
    """A kept interval placed onto the final timeline."""
    keep: KeepInterval
    timeline_start: float     # seconds, position in final video
    timeline_end: float


@dataclass
class SubtitleChunk:
    text: str
    timeline_start: float     # seconds in final timeline
    timeline_end: float
