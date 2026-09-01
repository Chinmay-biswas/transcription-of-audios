"""Safe, temporary retrieval of audio uploaded to Vercel Blob."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ALLOWED_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a"}
DEFAULT_MAX_AUDIO_BYTES = 100 * 1024 * 1024


def ensure_supported_filename(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_AUDIO_SUFFIXES:
        supported = ", ".join(sorted(ALLOWED_AUDIO_SUFFIXES))
        raise ValueError(f"Unsupported audio format. Use one of: {supported}.")
    return suffix


def _max_audio_bytes() -> int:
    return int(os.environ.get("MAX_AUDIO_BYTES", str(DEFAULT_MAX_AUDIO_BYTES)))


def _is_vercel_blob_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    return parsed.scheme == "https" and hostname.endswith(".blob.vercel-storage.com")


def download_blob_to_tempfile(blob_url: str, filename: str) -> str:
    """Download a public Vercel Blob object to temporary function storage."""

    suffix = ensure_supported_filename(filename)
    if not _is_vercel_blob_url(blob_url):
        raise ValueError("The audio URL must be an HTTPS URL from Vercel Blob.")

    max_bytes = _max_audio_bytes()
    temporary_path = ""
    request = Request(blob_url, headers={"User-Agent": "meeting-intelligence-api/1.0"})

    try:
        with urlopen(request, timeout=60) as response:
            final_hostname = urlparse(response.geturl()).hostname or ""
            if not final_hostname.endswith(".blob.vercel-storage.com"):
                raise ValueError("The audio download redirected outside Vercel Blob.")

            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError("The uploaded audio exceeds the configured size limit.")

            with tempfile.NamedTemporaryFile(
                prefix="meeting-",
                suffix=suffix,
                delete=False,
            ) as temporary_file:
                temporary_path = temporary_file.name
                bytes_written = 0
                while chunk := response.read(1024 * 1024):
                    bytes_written += len(chunk)
                    if bytes_written > max_bytes:
                        raise ValueError("The uploaded audio exceeds the configured size limit.")
                    temporary_file.write(chunk)
    except Exception:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise

    return temporary_path
