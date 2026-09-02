"""Safe FFmpeg helpers for bounded, Blob-backed audio/video processing.

The browser uploads one original media file to Vercel Blob.  This module never
uses raw byte slices as processing chunks: an MP3/MP4 byte range is usually not
a valid standalone media file.  Instead, FFmpeg reads one time range from the
source and writes a temporary WAV that Whisper can reliably decode.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from backend.services.blob_storage import _is_vercel_blob_url


ALLOWED_MEDIA_SUFFIXES = {".m4a", ".mov", ".mp3", ".mp4", ".wav", ".webm"}
MEDIA_KIND_BY_SUFFIX: dict[str, Literal["audio", "video"]] = {
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".mp4": "video",
    ".mov": "video",
    ".webm": "video",
}
DEFAULT_CHUNK_DURATION_SECONDS = 60
DEFAULT_FFMPEG_TIMEOUT_SECONDS = 240


class MediaProcessingError(RuntimeError):
    """A media probe or extraction failed before Whisper could run."""


def _positive_int_setting(name: str, default: int, *, minimum: int = 1) -> int:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive whole number.") from error
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


def media_chunk_duration_seconds() -> int:
    """Return a safe per-request processing interval (60 seconds by default)."""

    # A shorter bounded request is deliberate: a long meeting is processed as
    # durable intervals, not as one Vercel HTTP invocation.
    return _positive_int_setting("MEDIA_CHUNK_DURATION_SECONDS", DEFAULT_CHUNK_DURATION_SECONDS)


def ffmpeg_timeout_seconds() -> int:
    return _positive_int_setting(
        "MEDIA_FFMPEG_TIMEOUT_SECONDS", DEFAULT_FFMPEG_TIMEOUT_SECONDS, minimum=30
    )


def ensure_supported_media_filename(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_MEDIA_SUFFIXES:
        supported = ", ".join(sorted(ALLOWED_MEDIA_SUFFIXES))
        raise ValueError(f"Unsupported media format. Use one of: {supported}.")
    return suffix


def media_kind_for_filename(filename: str) -> Literal["audio", "video"]:
    return MEDIA_KIND_BY_SUFFIX[ensure_supported_media_filename(filename)]


def _validated_blob_url(blob_url: str) -> str:
    if not _is_vercel_blob_url(blob_url):
        raise ValueError("The media URL must be an HTTPS URL from Vercel Blob.")
    return blob_url


def _command_error(command: str, result: subprocess.CompletedProcess[str]) -> MediaProcessingError:
    detail = (result.stderr or result.stdout or "").strip()
    # Do not leak a Blob URL or arbitrary FFmpeg output into the UI.
    if detail:
        detail = detail.splitlines()[-1][:300]
    return MediaProcessingError(
        f"{command} could not read this media file" + (f": {detail}" if detail else ".")
    )


def probe_blob_media(blob_url: str) -> float:
    """Verify a public Blob source has an audio stream and return its duration."""

    safe_url = _validated_blob_url(blob_url)
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                safe_url,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=ffmpeg_timeout_seconds(),
        )
    except FileNotFoundError as error:
        raise MediaProcessingError("FFmpeg is not available in this processing service.") from error
    except subprocess.TimeoutExpired as error:
        raise MediaProcessingError("Media inspection timed out. Retry the upload or use a smaller file.") from error

    if result.returncode != 0:
        raise _command_error("FFprobe", result)

    try:
        payload = json.loads(result.stdout)
        duration = float((payload.get("format") or {}).get("duration"))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise MediaProcessingError("The media duration could not be determined.") from error

    streams = payload.get("streams") or []
    if not any(stream.get("codec_type") == "audio" for stream in streams if isinstance(stream, dict)):
        raise MediaProcessingError("This video has no audio track to transcribe.")
    if duration <= 0:
        raise MediaProcessingError("The media duration must be greater than zero.")
    return round(duration, 3)


def extract_audio_segment(
    *,
    blob_url: str,
    start_seconds: float,
    end_seconds: float,
) -> str:
    """Extract one valid, mono 16 kHz WAV segment to temporary storage.

    The caller owns the returned path and must remove it after Whisper finishes.
    """

    safe_url = _validated_blob_url(blob_url)
    duration = round(float(end_seconds) - float(start_seconds), 3)
    if start_seconds < 0 or duration <= 0:
        raise ValueError("The requested media segment has an invalid time range.")

    with tempfile.NamedTemporaryFile(prefix="meeting-segment-", suffix=".wav", delete=False) as output:
        output_path = output.name

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-ss",
                f"{float(start_seconds):.3f}",
                "-t",
                f"{duration:.3f}",
                "-i",
                safe_url,
                "-vn",
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-y",
                output_path,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=ffmpeg_timeout_seconds(),
        )
        if result.returncode != 0:
            raise _command_error("FFmpeg", result)
        if not os.path.exists(output_path) or os.path.getsize(output_path) <= 44:
            raise MediaProcessingError("FFmpeg did not produce a readable audio segment.")
        return output_path
    except FileNotFoundError as error:
        raise MediaProcessingError("FFmpeg is not available in this processing service.") from error
    except subprocess.TimeoutExpired as error:
        raise MediaProcessingError("This media segment took too long to decode. Resume to retry it.") from error
    except Exception:
        if os.path.exists(output_path):
            os.unlink(output_path)
        raise
