"""HTTP endpoints for the Vercel-hosted meeting intelligence service."""

from __future__ import annotations

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
from backend.services.llm_engine import (
    extract_relevant_info_from_chunk,
    generate_rag_answer,
    generate_summary_and_tasks,
)
from backend.services.transcription import transcribe_audio
from backend.services.vector_store import (
    get_all_meetings,
    get_meeting_analytics,
    save_meeting_to_db,
    search_meetings,
    search_specific_meeting,
)


router = APIRouter()


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


def _run_pipeline(file_path: str, filename: str, blob_url: str | None = None) -> dict[str, Any]:
    transcription = transcribe_audio(file_path, filename=filename)
    intelligence = generate_summary_and_tasks(transcription.transcript_text)
    meeting_id = str(uuid.uuid4())

    save_meeting_to_db(
        meeting_id=meeting_id,
        filename=filename,
        transcript=transcription.transcript_text,
        summary=intelligence.model_dump(),
        blob_url=blob_url,
    )

    return {
        "status": "success",
        "meeting_id": meeting_id,
        "transcription": transcription.model_dump(),
        "intelligence": intelligence.model_dump(),
    }


def _as_http_error(error: Exception) -> HTTPException:
    message = str(error)
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
        return {"status": "success", "results": search_meetings(query.query)}
    except Exception as error:
        raise _as_http_error(error) from error


@router.get("/meetings")
async def list_meetings() -> dict[str, Any]:
    try:
        return {"status": "success", "meetings": get_all_meetings()}
    except Exception as error:
        raise _as_http_error(error) from error


@router.get("/analytics")
async def analytics() -> dict[str, Any]:
    try:
        return {"status": "success", **get_meeting_analytics()}
    except Exception as error:
        raise _as_http_error(error) from error


@router.post("/meeting-chat")
async def chat_with_meeting(payload: MeetingChatQuery) -> dict[str, Any]:
    """Answer a question only from chunks indexed for the selected meeting."""

    try:
        search_results = search_specific_meeting(payload.query, payload.meeting_id)
        documents = search_results.get("documents", [[]])[0]
        if not documents:
            return {
                "status": "success",
                "answer": "I couldn't find relevant discussion in this meeting.",
                "context_used": [],
            }

        chunk_summaries = []
        for index, chunk in enumerate(documents):
            summary = extract_relevant_info_from_chunk(chunk, payload.query)
            if "No relevant information" not in summary:
                chunk_summaries.append(f"Source {index + 1}: {summary}")

        if not chunk_summaries:
            return {
                "status": "success",
                "answer": "I found transcript chunks, but none answered that question.",
                "context_used": documents,
            }

        answer = generate_rag_answer(payload.query, "\n\n".join(chunk_summaries))
        return {
            "status": "success",
            "answer": answer,
            "context_used": documents,
        }
    except Exception as error:
        raise _as_http_error(error) from error
