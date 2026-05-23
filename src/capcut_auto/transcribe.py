"""Whisper transcription with word-level timestamps."""
from __future__ import annotations

import tempfile
from pathlib import Path

from .models import SourceClip, Word
from .probe import extract_audio_wav


_MODEL_CACHE: dict[str, object] = {}


def _get_model(name: str):
    if name not in _MODEL_CACHE:
        from faster_whisper import WhisperModel
        # int8 on CPU — fastest reasonable default on Mac without GPU
        _MODEL_CACHE[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _MODEL_CACHE[name]


def transcribe(clip: SourceClip, model_name: str = "small", language: str | None = "it") -> None:
    """Populate clip.words with word-level timestamps."""
    model = _get_model(model_name)

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        extract_audio_wav(clip.path, wav)
        segments, _ = model.transcribe(
            str(wav),
            language=language,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
        )
        words: list[Word] = []
        for seg in segments:
            if not seg.words:
                continue
            for w in seg.words:
                txt = w.word.strip()
                if not txt:
                    continue
                words.append(Word(start=float(w.start), end=float(w.end), text=txt))
        clip.words = words
