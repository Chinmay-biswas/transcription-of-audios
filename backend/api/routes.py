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

from backend.models.schemas import (
    MediaJobCreateRequest,
    MediaJobResponse,
    MediaJobResumeRequest,
)
from backend.services.blob_storage import (
    download_blob_to_tempfile,
    ensure_supported_filename,
)
from backend.services.media_processing import (
    extract_audio_segment,
    media_chunk_duration_seconds,
    media_kind_for_filename,
    probe_blob_media,
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


def _save_meeting_segment_to_db(**kwargs: Any) -> None:
    from backend.services.vector_store import save_meeting_segment_to_db

    save_meeting_segment_to_db(**kwargs)


def _finalize_meeting_in_db(**kwargs: Any) -> None:
    from backend.services.vector_store import finalize_meeting_in_db

    finalize_meeting_in_db(**kwargs)


def _generate_rollup_summary(segment_summaries: list[dict[str, Any]]):
    from backend.services.llm_engine import generate_rollup_summary

    return generate_rollup_summary(segment_summaries)


def _get_job_store():
    # Keep PyMongo out of FastAPI import/startup. The existing small-file
    # pipeline can still start when Mongo is intentionally not configured.
    from backend.services.job_store import get_job_store

    return get_job_store()


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


def _no_speech_summary() -> dict[str, Any]:
    """Keep a silent segment checkpointable instead of retrying it forever."""

    return {
        "executive_summary": "Is segment mein koi saaf speech detect nahi hui.",
        "key_decisions": [],
        "action_items": [],
        "overall_sentiment": "Neutral",
    }


def _safe_segment_error(error: Exception) -> str:
    """Return a retry-oriented message without exposing provider internals."""

    message = str(error).lower()
    if "not configured" in message:
        return "A required AI or storage integration is not configured. Check the server settings, then resume."
    if "ffmpeg" in message or "media" in message or "audio track" in message:
        return "This time segment could not be decoded. Use Resume to retry it."
    if "gemini" in message or "google" in message or "quota" in message:
        return "Gemini could not analyze this segment. Check its quota, then use Resume."
    if "qdrant" in message or "embedding" in message:
        return "The segment could not be indexed. Check Qdrant, then use Resume."
    return "This segment could not be processed. Use Resume to retry it without restarting earlier segments."


def _job_response(
    store: Any,
    job_id: str,
    *,
    action: str,
    completed_chunk: dict[str, Any] | None = None,
    resume_token: str | None = None,
) -> dict[str, Any]:
    return {
        "status": "success",
        "action": action,
        "job": store.status_payload(job_id),
        "completed_chunk": completed_chunk,
        "resume_token": resume_token,
    }


def _create_media_job(payload: MediaJobCreateRequest) -> dict[str, Any]:
    store = _get_job_store()
    # Probe happens after the Mongo readiness check so a missing durable store
    # does not spend time fetching a source that cannot be resumed.
    media_kind = media_kind_for_filename(payload.filename)
    duration_seconds = probe_blob_media(payload.blob_url)
    job, resume_token = store.create_job(
        blob_url=payload.blob_url,
        filename=payload.filename,
        content_type=payload.content_type,
        media_kind=media_kind,
        duration_seconds=duration_seconds,
        chunk_duration_seconds=media_chunk_duration_seconds(),
    )
    return _job_response(
        store,
        str(job["id"]),
        action="waiting",
        resume_token=resume_token,
    )


def _process_claimed_media_chunk(
    store: Any,
    job: dict[str, Any],
    chunk: dict[str, Any],
) -> dict[str, Any] | None:
    """Run one leased interval and commit it only after every stage succeeds."""

    temporary_path: str | None = None
    source = job.get("source") or {}
    job_id = str(job["id"])
    chunk_index = int(chunk["index"])
    try:
        temporary_path = extract_audio_segment(
            blob_url=str(source["blob_url"]),
            start_seconds=float(chunk["start_seconds"]),
            end_seconds=float(chunk["end_seconds"]),
        )
        transcription = _transcribe_audio(
            temporary_path,
            f"{source.get('filename', 'meeting')}-segment-{chunk_index + 1}.wav",
        )
        raw_transcript = transcription.transcript_text.strip()
        if raw_transcript:
            analysis = _generate_summary_and_tasks(raw_transcript)
            romanized_transcript = analysis.romanized_transcript.strip()
            if not romanized_transcript:
                raise ValueError("Gemini did not return the Roman Hinglish transcript for this segment.")
            segment_summary = analysis.model_dump(exclude={"romanized_transcript"})
            _save_meeting_segment_to_db(
                meeting_id=job_id,
                filename=str(source.get("filename") or "Untitled recording"),
                transcript=romanized_transcript,
                blob_url=str(source.get("blob_url") or ""),
                segment_index=chunk_index,
                start_seconds=float(chunk["start_seconds"]),
                end_seconds=float(chunk["end_seconds"]),
                segment_summary=segment_summary,
            )
        else:
            romanized_transcript = ""
            segment_summary = _no_speech_summary()

        completed = store.complete_chunk(
            job_id=job_id,
            index=chunk_index,
            lease_id=str(chunk["lease_id"]),
            transcript_text=romanized_transcript,
            raw_transcript_text=raw_transcript,
            actual_duration_seconds=float(transcription.duration_seconds),
            segment_summary=segment_summary,
        )
        return {
            "index": int(completed["index"]),
            "start_seconds": float(completed["start_seconds"]),
            "end_seconds": float(completed["end_seconds"]),
            "transcript_text": str(completed.get("transcript_text") or ""),
        }
    except Exception as error:
        logger.exception("Media job %s segment %s failed", job_id, chunk_index)
        try:
            store.fail_chunk(
                job_id=job_id,
                index=chunk_index,
                lease_id=str(chunk["lease_id"]),
                public_error=_safe_segment_error(error),
            )
        except Exception:
            logger.exception("Could not persist media job %s segment failure", job_id)
            raise
        return None
    finally:
        _remove_temporary_file(temporary_path)


def _process_next_media_job(job_id: str, resume_token: str) -> dict[str, Any]:
    """Advance exactly one durable unit: a segment, rollup, or finalization."""

    store = _get_job_store()
    job = store.get_authorized_job(job_id, resume_token)
    # A deliberate Resume reclaims only a failed/uncommitted interval or an
    # expired lease. Completed intervals cannot be claimed again.
    job = store.reconcile_for_resume(job_id)
    if job.get("status") == "completed":
        return _job_response(store, job_id, action="completed")

    chunk = store.claim_next_chunk(job_id)
    if chunk:
        completed_chunk = _process_claimed_media_chunk(store, job, chunk)
        return _job_response(
            store,
            job_id,
            action="segment" if completed_chunk is not None else "waiting",
            completed_chunk=completed_chunk,
        )

    if store.has_failed_chunks(job_id):
        return _job_response(store, job_id, action="waiting")
    if not store.all_chunks_completed(job_id):
        # Another request still owns the next valid work unit. The UI can poll
        # status and resume after its lease completes or expires.
        return _job_response(store, job_id, action="waiting")

    work = store.claim_next_rollup_or_final(job_id)
    work_kind = work.get("kind")
    if work_kind == "rollup":
        task = work["task"]
        try:
            summary = _generate_rollup_summary(list(task.get("inputs") or []))
            store.complete_rollup(
                job_id=job_id,
                level=int(task["level"]),
                index=int(task["index"]),
                lease_id=str(task["lease_id"]),
                summary=summary.model_dump(),
            )
            return _job_response(store, job_id, action="rollup")
        except Exception as error:
            logger.exception("Media job %s rollup failed", job_id)
            store.fail_rollup(
                job_id=job_id,
                level=int(task["level"]),
                index=int(task["index"]),
                lease_id=str(task["lease_id"]),
                public_error=_safe_segment_error(error),
            )
            return _job_response(store, job_id, action="waiting")

    if work_kind == "final":
        final_summary = dict(work["summary"])
        try:
            _finalize_meeting_in_db(meeting_id=job_id, summary=final_summary)
            store.complete_finalization(
                job_id=job_id,
                lease_id=str(work["lease_id"]),
                final_summary=final_summary,
            )
            return _job_response(store, job_id, action="completed")
        except Exception as error:
            logger.exception("Media job %s finalization failed", job_id)
            store.fail_finalization(
                job_id=job_id,
                lease_id=str(work["lease_id"]),
                public_error=_safe_segment_error(error),
            )
            return _job_response(store, job_id, action="waiting")

    return _job_response(store, job_id, action="completed" if work_kind == "completed" else "waiting")


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
    error_type = type(error).__name__
    if error_type == "JobNotFoundError":
        return HTTPException(status_code=404, detail="The processing job was not found.")
    if error_type == "InvalidResumeTokenError":
        return HTTPException(status_code=403, detail="The resume token is invalid for this job.")
    if error_type == "MediaProcessingError":
        return HTTPException(status_code=422, detail=message)
    if "MONGODB_URI" in message and "not configured" in message:
        return HTTPException(
            status_code=503,
            detail="Resumable audio/video processing requires MONGODB_URI in the server environment.",
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
    chunk_jobs_missing_settings = [
        name for name in ("MONGODB_URI",) if not os.environ.get(name, "").strip()
    ]
    return {
        "status": "ok" if ready else "configuration_required",
        "ready": ready,
        "missing_settings": missing,
        "invalid_settings": invalid,
        # Mongo is deliberately optional for the legacy small-file endpoints,
        # but mandatory for the durable audio/video workflow.
        "chunk_jobs_ready": not chunk_jobs_missing_settings,
        "chunk_jobs_missing_settings": chunk_jobs_missing_settings,
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


@router.post("/jobs", response_model=MediaJobResponse, status_code=201)
async def create_media_job(payload: MediaJobCreateRequest) -> dict[str, Any]:
    """Create a Mongo-backed processing manifest for one Blob audio/video file."""

    try:
        return await run_in_threadpool(_create_media_job, payload)
    except Exception as error:
        raise _as_http_error(error) from error


@router.post("/jobs/{job_id}/status", response_model=MediaJobResponse)
async def media_job_status(
    job_id: str,
    payload: MediaJobResumeRequest,
) -> dict[str, Any]:
    """Return secret-free saved progress for a browser that was refreshed."""

    try:
        def read_status() -> dict[str, Any]:
            store = _get_job_store()
            store.get_authorized_job(job_id, payload.resume_token)
            return _job_response(store, job_id, action="waiting")

        return await run_in_threadpool(read_status)
    except Exception as error:
        raise _as_http_error(error) from error


@router.post("/jobs/{job_id}/run-next", response_model=MediaJobResponse)
async def run_next_media_job(
    job_id: str,
    payload: MediaJobResumeRequest,
) -> dict[str, Any]:
    """Process one saved media segment or one bounded summary-reduction step."""

    try:
        return await run_in_threadpool(_process_next_media_job, job_id, payload.resume_token)
    except Exception as error:
        raise _as_http_error(error) from error


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
