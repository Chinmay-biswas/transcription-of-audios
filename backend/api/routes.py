"""HTTP endpoints for the Vercel-hosted meeting intelligence service."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.services.blob_storage import (
    download_blob_to_tempfile,
    ensure_supported_filename,
)


router = APIRouter()
logger = logging.getLogger(__name__)


class PipelineStageError(RuntimeError):
    def __init__(self, stage: str, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.stage = stage
        self.code = code
        self.public_message = public_message


class SearchQuery(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)


class MeetingChatQuery(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    meeting_id: str = Field(min_length=1, max_length=200)


class BlobProcessRequest(BaseModel):
    blob_url: str = Field(min_length=1)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str | None = Field(default=None, max_length=100)


def _remove_temporary_file(path: str | None) -> None:
    if path and os.path.exists(path):
        os.unlink(path)


def _save_small_upload_to_tempfile(file: UploadFile) -> str:
    suffix = ensure_supported_filename(file.filename or "")
    with tempfile.NamedTemporaryFile(prefix="meeting-", suffix=suffix, delete=False) as output:
        shutil.copyfileobj(file.file, output)
        return output.name


def _transcribe_audio(file_path: str, filename: str):
    from backend.services.transcription import transcribe_audio

    return transcribe_audio(file_path, filename=filename)


def _generate_summary_and_tasks(transcript_text: str):
    from backend.services.llm_engine import generate_summary_and_tasks

    return generate_summary_and_tasks(transcript_text)


def _save_meeting_to_db(**kwargs: Any) -> None:
    from backend.services.vector_store import save_meeting_to_db

    save_meeting_to_db(**kwargs)


def _search_meetings(query: str):
    from backend.services.vector_store import search_meetings

    return search_meetings(query)


def _search_specific_meeting(query: str, meeting_id: str):
    from backend.services.vector_store import search_specific_meeting

    return search_specific_meeting(query, meeting_id)


def _get_all_meetings():
    from backend.services.vector_store import get_all_meetings

    return get_all_meetings()


def _get_meeting_analytics():
    from backend.services.vector_store import get_meeting_analytics

    return get_meeting_analytics()


def _extract_relevant_info_from_chunk(chunk: str, question: str) -> str:
    from backend.services.llm_engine import extract_relevant_info_from_chunk

    return extract_relevant_info_from_chunk(chunk, question)


def _generate_rag_answer(question: str, context: str) -> str:
    from backend.services.llm_engine import generate_rag_answer

    return generate_rag_answer(question, context)


def _run_pipeline(file_path: str, filename: str, blob_url: str | None = None) -> dict[str, Any]:
    try:
        transcription = _transcribe_audio(file_path, filename)
    except Exception as error:
        logger.exception("Meeting pipeline transcription stage failed")
        raise PipelineStageError(
            "transcription",
            "transcription_failed",
            "Whisper could not transcribe this recording. Confirm the audio is a valid MP3, WAV, or M4A file.",
        ) from error

    try:
        intelligence = _generate_summary_and_tasks(transcription.transcript_text)
    except Exception as error:
        logger.exception("Meeting pipeline Gemini analysis stage failed")
        raise PipelineStageError(
            "analysis",
            "gemini_analysis_failed",
            "Gemini could not analyze the transcript. Check GOOGLE_API_KEY, GEMINI_MODEL, and API quota.",
        ) from error

    romanized_transcript = intelligence.romanized_transcript.strip()
    if not romanized_transcript:
        raise PipelineStageError(
            "analysis",
            "romanized_transcript_missing",
            "Gemini did not return the Roman Hinglish transcript. Please retry the recording.",
        )
    transcription = transcription.model_copy(
        update={"transcript_text": romanized_transcript}
    )
    intelligence_payload = intelligence.model_dump(exclude={"romanized_transcript"})

    meeting_id = str(uuid.uuid4())

    try:
        _save_meeting_to_db(
            meeting_id=meeting_id,
            filename=filename,
            transcript=transcription.transcript_text,
            summary=intelligence_payload,
            blob_url=blob_url,
        )
    except Exception as error:
        logger.exception("Meeting pipeline Qdrant storage stage failed")
        raise PipelineStageError(
            "storage",
            "qdrant_storage_failed",
            "Qdrant could not store the processed meeting. Check QDRANT_URL, QDRANT_API_KEY, and the collection configuration.",
        ) from error

    return {
        "status": "success",
        "meeting_id": meeting_id,
        "transcription": transcription.model_dump(),
        "intelligence": intelligence_payload,
    }


def _as_http_error(error: Exception) -> HTTPException:
    message = str(error)
    if isinstance(error, PipelineStageError):
        status_code = 500 if error.stage == "transcription" else 502
        return HTTPException(
            status_code=status_code,
            detail={
                "message": error.public_message,
                "stage": error.stage,
                "code": error.code,
            },
        )
    if "not configured" in message:
        return HTTPException(
            status_code=503,
            detail="A required server integration is not configured. Check Vercel environment variables.",
        )
    if isinstance(error, ValueError):
        return HTTPException(status_code=400, detail=message)
    print(f"Pipeline error: {message}")
    return HTTPException(
        status_code=500,
        detail="Processing failed. Check the Vercel function logs for details.",
    )


@router.get("/health")
def service_health() -> dict[str, Any]:
    """Return a secret-free readiness check for the processing service."""

    required_settings = ("GOOGLE_API_KEY", "QDRANT_URL", "QDRANT_API_KEY")
    missing = [name for name in required_settings if not os.environ.get(name, "").strip()]
    invalid: list[str] = []

    try:
        from backend.services.blob_storage import _max_audio_bytes

        _max_audio_bytes()
    except ValueError as error:
        invalid.append(str(error))

    raw_dimensions = os.environ.get("GEMINI_EMBEDDING_DIMENSIONS", "").strip() or "768"
    try:
        embedding_dimensions = int(raw_dimensions)
        if embedding_dimensions <= 0:
            raise ValueError
    except ValueError:
        invalid.append("GEMINI_EMBEDDING_DIMENSIONS must be a positive integer.")

    ready = not missing and not invalid
    return {
        "status": "ok" if ready else "configuration_required",
        "ready": ready,
        "missing_settings": missing,
        "invalid_settings": invalid,
    }


@router.post("/process-meeting")
async def process_meeting(file: UploadFile = File(...)) -> dict[str, Any]:
    """Local and small-file compatibility endpoint.

    The Vercel frontend uses /process-blob so audio bypasses the Vercel Function
    request-body limit.
    """

    temporary_path: str | None = None
    try:
        temporary_path = await run_in_threadpool(_save_small_upload_to_tempfile, file)
        return await run_in_threadpool(
            _run_pipeline,
            temporary_path,
            file.filename or "meeting-audio",
        )
    except Exception as error:
        raise _as_http_error(error) from error
    finally:
        _remove_temporary_file(temporary_path)


@router.post("/process-blob")
async def process_blob(payload: BlobProcessRequest) -> dict[str, Any]:
    """Process an audio recording uploaded directly from the browser to Blob."""

    temporary_path: str | None = None
    try:
        temporary_path = await run_in_threadpool(
            download_blob_to_tempfile,
            payload.blob_url,
            payload.filename,
        )
        return await run_in_threadpool(
            _run_pipeline,
            temporary_path,
            payload.filename,
            payload.blob_url,
        )
    except Exception as error:
        raise _as_http_error(error) from error
    finally:
        _remove_temporary_file(temporary_path)


@router.post("/search")
async def search_history(query: SearchQuery) -> dict[str, Any]:
    try:
        return {"status": "success", "results": _search_meetings(query.query)}
    except Exception as error:
        raise _as_http_error(error) from error


@router.get("/meetings")
async def list_meetings() -> dict[str, Any]:
    try:
        return {"status": "success", "meetings": _get_all_meetings()}
    except Exception as error:
        raise _as_http_error(error) from error


@router.get("/analytics")
async def analytics() -> dict[str, Any]:
    try:
        return {"status": "success", **_get_meeting_analytics()}
    except Exception as error:
        raise _as_http_error(error) from error


@router.post("/meeting-chat")
async def chat_with_meeting(payload: MeetingChatQuery) -> dict[str, Any]:
    """Answer a question only from chunks indexed for the selected meeting."""

    try:
        search_results = _search_specific_meeting(payload.query, payload.meeting_id)
        documents = search_results.get("documents", [[]])[0]
        if not documents:
            return {
                "status": "success",
                "answer": "Mujhe is meeting mein relevant discussion nahi mili.",
                "context_used": [],
            }

        chunk_summaries = []
        for index, chunk in enumerate(documents):
            summary = _extract_relevant_info_from_chunk(chunk, payload.query)
            if "No relevant information" not in summary:
                chunk_summaries.append(f"Source {index + 1}: {summary}")

        if not chunk_summaries:
            return {
                "status": "success",
                "answer": "Transcript mila, lekin usmein is sawaal ka jawab nahi tha.",
                "context_used": documents,
            }

        answer = _generate_rag_answer(payload.query, "\n\n".join(chunk_summaries))
        return {
            "status": "success",
            "answer": answer,
            "context_used": documents,
        }
    except Exception as error:
        raise _as_http_error(error) from error
