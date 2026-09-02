"""Whisper transcription helpers that work in a stateless container."""

from __future__ import annotations

import os
import time
from functools import lru_cache
from pathlib import Path

import whisper

from backend.models.schemas import TranscriptionResponse


@lru_cache(maxsize=1)
def get_model():
    """Load Whisper only when the first transcription is requested."""

    model_name = os.environ.get("WHISPER_MODEL", "").strip() or "base"
    configured_root = os.environ.get("WHISPER_MODEL_DIR", "").strip()
    baked_model_root = "/opt/whisper" if Path("/opt/whisper").is_dir() else None
    download_root = configured_root or baked_model_root
    print(f"Loading Whisper model '{model_name}' into memory...")
    return whisper.load_model(model_name, download_root=download_root)


def transcribe_audio(file_path: str, filename: str | None = None) -> TranscriptionResponse:
    """Transcribe one temporary audio file and return a validated response."""

    if not os.path.exists(file_path):
        raise FileNotFoundError("The uploaded audio file is no longer available.")

    start_time = time.time()
    # fp16 is unavailable on CPU-only function instances.
    result = get_model().transcribe(file_path, fp16=False)
    elapsed = round(time.time() - start_time, 2)
    print(f"Transcription finished in {elapsed} seconds.")

    segments = result.get("segments", [])
    duration = sum(segment["end"] - segment["start"] for segment in segments) if segments else 0.0

    return TranscriptionResponse(
        filename=filename or Path(file_path).name,
        transcript_text=result["text"].strip(),
        duration_seconds=round(duration, 2),
    )
