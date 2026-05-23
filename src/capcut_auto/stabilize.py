"""Video stabilization via ffmpeg vidstab. Results are cached on disk.

Two passes:
  1. vidstabdetect → produces a .trf transform file
  2. vidstabtransform → renders stabilized video

If ffmpeg wasn't built with vidstab the function returns the original path with
a warning logged.
"""
from __future__ import annotations

import hashlib
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable


CACHE_DIR = Path.home() / ".cache/capcut-auto/stabilized"


@lru_cache(maxsize=1)
def _has_vidstab() -> bool:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, check=True,
        ).stdout
        return "vidstabdetect" in out and "vidstabtransform" in out
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _cache_key(video: Path) -> str:
    st = video.stat()
    raw = f"{video.resolve()}:{st.st_size}:{int(st.st_mtime)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def stabilize(video: Path, smoothing: int = 30, log: Callable[[str], None] = print) -> Path:
    """Return the path to a stabilized copy of `video` (cached).

    Uses vidstab (2-pass, high quality) if available; falls back to ffmpeg's
    built-in deshake filter (single-pass, lower quality but always present).
    Returns the original path if both are unavailable or rendering fails.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(video)
    out = CACHE_DIR / f"{video.stem}-{key}.mp4"
    if out.exists():
        log(f"  • stabilizzato (cache) {video.name}")
        return out

    if _has_vidstab():
        return _stabilize_vidstab(video, out, key, smoothing, log)
    return _stabilize_deshake(video, out, log)


def _stabilize_vidstab(video: Path, out: Path, key: str, smoothing: int,
                       log: Callable[[str], None]) -> Path:
    trf = CACHE_DIR / f"{key}.trf"
    log(f"  • stabilizzazione vidstab {video.name} (1/2 analisi)…")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
             "-vf", f"vidstabdetect=output={trf}:shakiness=5:accuracy=15",
             "-f", "null", "-"],
            check=True,
        )
        log(f"  • stabilizzazione vidstab {video.name} (2/2 render)…")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
             "-vf",
             f"vidstabtransform=input={trf}:smoothing={smoothing}:crop=black,"
             "unsharp=5:5:0.8:3:3:0.4",
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-c:a", "copy",
             str(out)],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        log(f"  ⚠ vidstab fallito per {video.name} ({e}); uso l'originale")
        return video
    finally:
        try:
            trf.unlink(missing_ok=True)
        except OSError:
            pass
    return out


def _stabilize_deshake(video: Path, out: Path, log: Callable[[str], None]) -> Path:
    log(f"  • stabilizzazione deshake {video.name} (vidstab non disponibile, qualità inferiore)…")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
             "-vf", "deshake=x=-1:y=-1:w=-1:h=-1:rx=16:ry=16",
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-c:a", "copy", str(out)],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        log(f"  ⚠ deshake fallito per {video.name} ({e}); uso l'originale")
        return video
    return out
