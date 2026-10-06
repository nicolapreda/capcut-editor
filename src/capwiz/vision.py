"""Frame strips — what lets the AI *see* a clip.

For each clip we grab a few frames evenly spaced in time and join them into one
small JPEG (left → right, then row by row = start → end). Claude reads it to tell
a talking-head take from b-roll, say what happens in the shot and what it is for
in the script, and pick the stretch where that happens.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .models import SourceClip


def frame_times(duration: float) -> list[float]:
    """4–8 timestamps, roughly one every 1.5 seconds, centred in equal slices:
    close enough to see an action happen (a shutter going up, a light coming on),
    not just the state before and after."""
    n = max(4, min(8, round(duration / 1.5)))
    return [round((i + 0.5) * duration / n, 2) for i in range(n)]


def make_strip(clip: SourceClip, out_dir: Path, name: str) -> tuple[Path | None, list[float]]:
    """Write `<name>.jpg` in out_dir; returns (path, frame times) or (None, [])
    when no frame could be extracted. Frames run left to right, then row by row
    (rows keep the sheet narrow enough to stay readable for wide footage)."""
    frames: list[Path] = []
    times: list[float] = []
    for t in frame_times(clip.duration):
        f = out_dir / f"{name}_f{len(frames)}.jpg"
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
    width = _image_width(frames[0]) or 640
    cols = min(len(frames), max(2, 1700 // width))
    rows = -(-len(frames) // cols)
    ok = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-framerate", "1", "-start_number", "0",
         "-i", str(out_dir / f"{name}_f%d.jpg"), "-vf", f"tile={cols}x{rows}",
         "-frames:v", "1", "-q:v", "4", str(strip)],
        capture_output=True,
    ).returncode == 0
    for f in frames:
        f.unlink(missing_ok=True)
    return (strip, times) if ok and strip.exists() else (None, [])


def _image_width(path: Path) -> int:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return int(r.stdout.strip().split(",")[0])
    except ValueError:
        return 0


def light_changes(clip: SourceClip, max_duration: float = 45.0) -> str:
    """Measure how the brightness of a shot moves over time and say it in words:
    "sale di colpo a 2.5s", "sale gradualmente da 3.3s a 19.0s".

    Frames a couple of seconds apart show that a room got brighter, not when.
    A light being switched on or a shutter going up is often the whole point of
    a b-roll shot, and a half-second cut has to land on that instant. Long
    clips (talking takes) are skipped; returns "" when the light is steady.
    """
    if clip.duration > max_duration:
        return ""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(clip.path), "-an",
         "-vf", "fps=10,scale=64:-2,signalstats,"
                "metadata=print:key=lavfi.signalstats.YAVG:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    luma: list[float] = []
    for line in r.stdout.splitlines():
        if "YAVG=" in line:
            try:
                luma.append(float(line.split("YAVG=")[1]))
            except ValueError:
                pass
    if len(luma) < 5:
        return ""
    top, low = max(luma), min(luma)
    if top <= 0 or (top - low) / top < 0.08:
        return ""

    # sudden steps: more than 8% of the brightest level within two tenths of a second
    steps: list[tuple[float, float]] = []                   # (second, signed size)
    for i in range(len(luma) - 2):
        delta = luma[i + 2] - luma[i]
        t = (i + 1) / 10
        if t < 0.4 or t > clip.duration - 0.4:      # the camera being started / stopped
            continue
        if abs(delta) / top >= 0.08:
            if steps and t - steps[-1][0] <= 0.4 and (delta > 0) == (steps[-1][1] > 0):
                if abs(delta) > abs(steps[-1][1]):
                    steps[-1] = (t, delta)
            else:
                steps.append((t, delta))
    if steps:
        return "; ".join(
            f"{'sale' if d > 0 else 'scende'} di colpo a {t:.1f}s" for t, d in steps[:6])

    # no step: a slow drift (a shutter going up, a door opening onto daylight)
    if (top - low) / top >= 0.3:
        rising = luma.index(top) > luma.index(low)
        lo_t, hi_t = low + 0.1 * (top - low), low + 0.9 * (top - low)
        inside = [i for i, v in enumerate(luma) if lo_t <= v <= hi_t]
        if inside:
            return (f"{'sale' if rising else 'scende'} gradualmente da "
                    f"{inside[0] / 10:.1f}s a {inside[-1] / 10:.1f}s")
    return ""
