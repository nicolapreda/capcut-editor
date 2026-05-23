"""ffprobe helpers — read duration, resolution, fps, audio presence."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .models import SourceClip


def probe(path: Path) -> SourceClip:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    info = json.loads(out)

    duration = float(info["format"]["duration"])
    width = height = 0
    fps = 30.0
    has_audio = False
    for s in info["streams"]:
        if s["codec_type"] == "video" and width == 0:
            width = int(s["width"])
            height = int(s["height"])
            # avg_frame_rate is "num/den"
            num, den = s.get("avg_frame_rate", "30/1").split("/")
            den = float(den) if float(den) else 1.0
            fps = float(num) / den if den else 30.0
        elif s["codec_type"] == "audio":
            has_audio = True

    return SourceClip(
        path=path, duration=duration, width=width, height=height,
        has_audio=has_audio, fps=fps,
    )


def extract_audio_wav(video: Path, out_wav: Path) -> None:
    """Extract mono 16kHz wav for Whisper."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-ac", "1", "-ar", "16000",
         "-vn", "-loglevel", "error", str(out_wav)],
        check=True,
    )
