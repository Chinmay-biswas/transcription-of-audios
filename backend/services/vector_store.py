"""Qdrant Cloud persistence for meeting transcripts and Gemini embeddings."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable

from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient, models


DEFAULT_COLLECTION = "meeting_transcripts_gemini"
DEFAULT_EMBEDDING_BATCH_SIZE = 24


def _optional_setting(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _required_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured.")
    return value


def _embedding_dimensions() -> int:
    raw_value = _optional_setting("GEMINI_EMBEDDING_DIMENSIONS", "768")
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError("GEMINI_EMBEDDING_DIMENSIONS must be a positive integer.") from error

    if value <= 0:
        raise ValueError("GEMINI_EMBEDDING_DIMENSIONS must be a positive integer.")
    return value


@lru_cache(maxsize=1)
def _client() -> QdrantClient:
    return QdrantClient(
        url=_required_setting("QDRANT_URL"),
        api_key=_required_setting("QDRANT_API_KEY"),
        prefer_grpc=False,
        timeout=30,
    )


@lru_cache(maxsize=1)
def _embeddings() -> GoogleGenerativeAIEmbeddings:
    return GoogleGenerativeAIEmbeddings(
        model=_optional_setting("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
        api_key=_required_setting("GOOGLE_API_KEY"),
        output_dimensionality=_embedding_dimensions(),
        request_options={"timeout": 60},
    )


def _collection_name() -> str:
    return _optional_setting("QDRANT_COLLECTION", DEFAULT_COLLECTION)


def _ensure_collection(vector_size: int) -> None:
    client = _client()
    name = _collection_name()
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(
                size=vector_size,
                distance=models.Distance.COSINE,
            ),
        )


def _embed_documents(chunks: list[str]) -> list[list[float]]:
    return _embeddings().embed_documents(
        chunks,
        task_type="RETRIEVAL_DOCUMENT",
        output_dimensionality=_embedding_dimensions(),
    )


def _embedding_batch_size() -> int:
    raw_value = _optional_setting("GEMINI_EMBEDDING_BATCH_SIZE", str(DEFAULT_EMBEDDING_BATCH_SIZE))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError("GEMINI_EMBEDDING_BATCH_SIZE must be a positive integer.") from error
    if value <= 0:
        raise ValueError("GEMINI_EMBEDDING_BATCH_SIZE must be a positive integer.")
    return value


def _embed_documents_in_batches(chunks: list[str]) -> list[list[float]]:
    """Avoid one oversized Gemini embeddings request for a long recording."""

    vectors: list[list[float]] = []
    batch_size = _embedding_batch_size()
    for start in range(0, len(chunks), batch_size):
        vectors.extend(_embed_documents(chunks[start : start + batch_size]))
    if len(vectors) != len(chunks):
        raise RuntimeError("Gemini returned an unexpected number of embeddings.")
    return vectors


def _embed_query(query: str) -> list[float]:
    return _embeddings().embed_query(
        query,
        task_type="RETRIEVAL_QUERY",
        output_dimensionality=_embedding_dimensions(),
    )


def save_meeting_to_db(
    *,
    meeting_id: str,
    filename: str,
    transcript: str,
    summary: dict[str, Any],
    blob_url: str | None = None,
) -> None:
    """Chunk, embed, and persist a processed meeting in Qdrant Cloud."""

    if not transcript.strip():
        raise ValueError("Transcript text is empty and cannot be indexed.")

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=150,
        length_function=len,
    )
    chunks = text_splitter.split_text(transcript)
    if not chunks:
        raise ValueError("No indexable transcript chunks were generated.")

    vectors = _embed_documents_in_batches(chunks)
    _ensure_collection(len(vectors[0]))
    created_at = datetime.now(timezone.utc).isoformat()

    points = [
        models.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{meeting_id}:{index}")),
            vector=vector,
            payload={
                "meeting_id": meeting_id,
                "filename": filename,
                "blob_url": blob_url or "",
                "document": chunk,
                "summary": summary,
                "meeting_status": "completed",
                "created_at": created_at,
            },
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
    ]

    _client().upsert(
        collection_name=_collection_name(),
        points=points,
        wait=True,
    )


def save_meeting_segment_to_db(
    *,
    meeting_id: str,
    filename: str,
    transcript: str,
    blob_url: str,
    segment_index: int,
    start_seconds: float,
    end_seconds: float,
    segment_summary: dict[str, Any],
) -> None:
    """Persist one completed media interval using stable, retry-safe point IDs.

    These points remain hidden from cross-meeting history while the job is in
    progress. ``finalize_meeting_in_db`` promotes them only after the complete
    meeting summary has been written successfully.
    """

    if not transcript.strip():
        return

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=150,
        length_function=len,
    )
    chunks = text_splitter.split_text(transcript)
    if not chunks:
        return

    vectors = _embed_documents_in_batches(chunks)
    _ensure_collection(len(vectors[0]))
    created_at = datetime.now(timezone.utc).isoformat()
    points = [
        models.PointStruct(
            id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{meeting_id}:segment:{segment_index}:part:{part_index}",
                )
            ),
            vector=vector,
            payload={
                "meeting_id": meeting_id,
                "filename": filename,
                "blob_url": blob_url,
                "document": chunk,
                "summary": segment_summary,
                "meeting_status": "processing",
                "segment_index": segment_index,
                "start_seconds": round(float(start_seconds), 3),
                "end_seconds": round(float(end_seconds), 3),
                "created_at": created_at,
            },
        )
        for part_index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
    ]
    _client().upsert(
        collection_name=_collection_name(),
        points=points,
        wait=True,
    )


def finalize_meeting_in_db(*, meeting_id: str, summary: dict[str, Any]) -> None:
    """Promote all committed intervals to one searchable completed meeting."""

    client = _client()
    name = _collection_name()
    if not client.collection_exists(name):
        # A silent recording has no transcript points to promote. The durable
        # Mongo job still contains its completion state and final analysis.
        return
    client.set_payload(
        collection_name=name,
        payload={
            "meeting_status": "completed",
            "summary": summary,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
        points=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="meeting_id",
                        match=models.MatchValue(value=meeting_id),
                    )
                ]
            )
        ),
        wait=True,
    )


def _query_points(
    query: str,
    *,
    n_results: int,
    query_filter: models.Filter | None = None,
) -> list[models.ScoredPoint]:
    vector = _embed_query(query)
    _ensure_collection(len(vector))
    # Segment points are written as checkpoints before the whole meeting is
    # finalized. Hide only explicit in-progress points, keeping older records
    # (which predate this field) searchable for backward compatibility.
    processing_filter = models.FieldCondition(
        key="meeting_status",
        match=models.MatchValue(value="processing"),
    )
    if query_filter is None:
        effective_filter = models.Filter(must_not=[processing_filter])
    else:
        effective_filter = models.Filter(
            must=list(query_filter.must or []),
            should=query_filter.should,
            must_not=[*(query_filter.must_not or []), processing_filter],
        )
    response = _client().query_points(
        collection_name=_collection_name(),
        query=vector,
        query_filter=effective_filter,
        limit=n_results,
        with_payload=True,
        with_vectors=False,
    )
    return list(response.points)


def _as_legacy_search_result(points: Iterable[models.ScoredPoint]) -> dict[str, list[list[Any]]]:
    """Keep the original API shape while returning JSON-native metadata."""

    point_list = list(points)
    payloads = [point.payload or {} for point in point_list]
    return {
        "ids": [[str(point.id) for point in point_list]],
        "documents": [[str(payload.get("document", "")) for payload in payloads]],
        "metadatas": [payloads],
        "distances": [[round(1 - float(point.score), 6) for point in point_list]],
    }


def search_meetings(query: str, n_results: int = 5) -> dict[str, list[list[Any]]]:
    """Search all indexed meetings using a Gemini query embedding."""

    return _as_legacy_search_result(_query_points(query, n_results=n_results))


def search_specific_meeting(
    query: str,
    meeting_id: str,
    n_results: int = 5,
) -> dict[str, list[list[Any]]]:
    """Search only transcript chunks associated with one meeting."""

    query_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="meeting_id",
                match=models.MatchValue(value=meeting_id),
            )
        ]
    )
    return _as_legacy_search_result(
        _query_points(query, n_results=n_results, query_filter=query_filter)
    )


def _all_meeting_records() -> list[dict[str, Any]]:
    """Return one metadata record per meeting from the remote Qdrant collection."""

    client = _client()
    name = _collection_name()
    if not client.collection_exists(name):
        return []

    offset: models.ExtendedPointId | None = None
    meetings: dict[str, dict[str, Any]] = {}
    while True:
        points, offset = client.scroll(
            collection_name=name,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            if payload.get("meeting_status") == "processing":
                continue
            meeting_id = payload.get("meeting_id")
            if not meeting_id or meeting_id in meetings:
                continue
            meetings[meeting_id] = {
                "id": str(meeting_id),
                "filename": str(payload.get("filename", "Untitled meeting")),
                "created_at": str(payload.get("created_at", "")),
                "summary": payload.get("summary", {}),
            }
        if offset is None:
            break

    return sorted(meetings.values(), key=lambda item: item["created_at"], reverse=True)


def get_all_meetings() -> list[dict[str, Any]]:
    """List meetings for the chat selector."""

    return _all_meeting_records()


def get_meeting_analytics() -> dict[str, Any]:
    """Build cross-meeting task and sentiment data for the analytics screen."""

    meetings = _all_meeting_records()
    action_items: list[dict[str, Any]] = []
    sentiment_counts: dict[str, int] = {}

    for meeting in meetings:
        summary = meeting.get("summary")
        if not isinstance(summary, dict):
            continue

        sentiment = str(summary.get("overall_sentiment", "Neutral"))
        sentiment_counts[sentiment] = sentiment_counts.get(sentiment, 0) + 1

        for item in summary.get("action_items", []):
            if not isinstance(item, dict):
                continue
            action_items.append(
                {
                    "task": str(item.get("task", "Untitled task")),
                    "assignee": item.get("assignee") or "Unassigned",
                    "due_date": item.get("due_date") or "Not stated",
                    "priority": item.get("priority") or "Normal",
                    "meeting_id": meeting["id"],
                    "meeting_filename": meeting["filename"],
                }
            )

    return {
        "total_meetings": len(meetings),
        "total_action_items": len(action_items),
        "active_assignees": len(
            {item["assignee"] for item in action_items if item["assignee"] != "Unassigned"}
        ),
        "sentiment_counts": sentiment_counts,
        "action_items": action_items,
    }
