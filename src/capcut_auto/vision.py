"""Frame strips — what lets the AI *see* a clip.

For each clip we grab a few frames evenly spaced in time and join them side by
side into one small JPEG (left → right = start → end). Claude reads the strip to
tell a talking-head take from b-roll, describe what the shot shows, and pick the
best stretch of it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .models import SourceClip


def frame_times(duration: float) -> list[float]:
    """3–6 timestamps, roughly one every 3 seconds, centred in equal slices."""
    n = max(3, min(6, round(duration / 3)))
    return [round((i + 0.5) * duration / n, 2) for i in range(n)]


def make_strip(clip: SourceClip, out_dir: Path, name: str) -> tuple[Path | None, list[float]]:
    """Write `<name>.jpg` in out_dir; returns (path, frame times) or (None, [])
    when no frame could be extracted."""
    frames: list[Path] = []
    times: list[float] = []
    for j, t in enumerate(frame_times(clip.duration)):
        f = out_dir / f"{name}_f{j}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t}", "-i", str(clip.path),
             "-frames:v", "1", "-vf", "scale=-2:360", str(f)],
            capture_output=True,
        )
        if r.returncode == 0 and f.exists():
            frames.append(f)
            times.append(t)
    if not frames:
        return None, []

    strip = out_dir / f"{name}.jpg"
    if len(frames) == 1:
        frames[0].rename(strip)
        return strip, times
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for f in frames:
        args += ["-i", str(f)]
    args += ["-filter_complex", f"hstack=inputs={len(frames)}", "-q:v", "4", str(strip)]
    ok = subprocess.run(args, capture_output=True).returncode == 0
    for f in frames:
        f.unlink(missing_ok=True)
    return (strip, times) if ok and strip.exists() else (None, [])
