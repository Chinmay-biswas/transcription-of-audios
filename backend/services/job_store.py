"""Lazy MongoDB persistence for resumable media-transcription jobs.

The Vercel container has no durable local filesystem.  This module therefore
stores every completed time segment in MongoDB and treats Blob as the immutable
source file.  All claim/complete transitions are atomic so duplicate browser
requests or overlapping Vercel instances cannot redo a committed segment.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable


DEFAULT_DATABASE = "meeting_intelligence"
DEFAULT_CHUNK_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 15 * 60
DEFAULT_ROLLUP_BATCH_SIZE = 10


class JobStoreError(RuntimeError):
    """Base error for durable media-job persistence."""


class JobNotFoundError(JobStoreError):
    """Raised when the requested job no longer exists."""


class InvalidResumeTokenError(JobStoreError):
    """Raised when a caller does not hold the job capability token."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _positive_int_setting(name: str, default: int, *, minimum: int = 1) -> int:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise JobStoreError(f"{name} must be a whole number.") from error
    if value < minimum:
        raise JobStoreError(f"{name} must be at least {minimum}.")
    return value


def _resume_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _serialize_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": int(chunk["index"]),
        "start_seconds": round(float(chunk["start_seconds"]), 3),
        "end_seconds": round(float(chunk["end_seconds"]), 3),
        "transcript_text": str(chunk.get("transcript_text") or ""),
    }


class MongoJobStore:
    """A small synchronous repository; routes execute it in a thread pool."""

    def __init__(self, database: Any) -> None:
        self._database = database
        self._jobs = database["media_jobs"]
        self._chunks = database["media_job_chunks"]
        self._rollups = database["media_job_rollups"]
        self._indexes_ready = False

    def _ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        self._jobs.create_index("id", unique=True, name="media_jobs_id_unique")
        self._chunks.create_index(
            [("job_id", 1), ("index", 1)],
            unique=True,
            name="media_job_chunks_job_index_unique",
        )
        self._chunks.create_index(
            [("job_id", 1), ("status", 1), ("index", 1)],
            name="media_job_chunks_next_claim",
        )
        self._chunks.create_index(
            [("status", 1), ("lease_expires_at", 1)],
            name="media_job_chunks_expired_lease",
        )
        self._rollups.create_index(
            [("job_id", 1), ("level", 1), ("index", 1)],
            unique=True,
            name="media_job_rollups_job_level_index_unique",
        )
        self._rollups.create_index(
            [("job_id", 1), ("level", 1), ("status", 1), ("index", 1)],
            name="media_job_rollups_next_claim",
        )
        self._indexes_ready = True

    def create_job(
        self,
        *,
        blob_url: str,
        filename: str,
        content_type: str | None,
        media_kind: str,
        duration_seconds: float,
        chunk_duration_seconds: float,
    ) -> tuple[dict[str, Any], str]:
        """Persist a job and all deterministic segment boundaries."""

        if duration_seconds <= 0:
            raise JobStoreError("Media duration must be greater than zero.")
        if chunk_duration_seconds <= 0:
            raise JobStoreError("Chunk duration must be greater than zero.")

        self._ensure_indexes()
        now = _utcnow()
        job_id = str(uuid.uuid4())
        resume_token = secrets.token_urlsafe(32)
        total_chunks = int(math.ceil(duration_seconds / chunk_duration_seconds))
        total_chunks = max(1, total_chunks)
        max_attempts = _positive_int_setting(
            "MAX_MEDIA_CHUNK_ATTEMPTS", DEFAULT_CHUNK_ATTEMPTS
        )
        lease_seconds = _positive_int_setting(
            "MEDIA_JOB_LEASE_SECONDS", DEFAULT_LEASE_SECONDS, minimum=60
        )

        job = {
            "id": job_id,
            "status": "queued",
            "source": {
                "blob_url": blob_url,
                "filename": filename,
                "content_type": content_type or "",
                "media_kind": media_kind,
                "duration_seconds": round(duration_seconds, 3),
            },
            "chunk_duration_seconds": round(chunk_duration_seconds, 3),
            "total_chunks": total_chunks,
            "completed_chunks": 0,
            "resume_token_hash": _resume_token_hash(resume_token),
            "max_chunk_attempts": max_attempts,
            "lease_seconds": lease_seconds,
            "final_summary": None,
            "meeting_id": job_id,
            "last_error": None,
            "finalization_lease_id": None,
            "finalization_lease_expires_at": None,
            "finalization_error": False,
            "rollup_current_level": None,
            "created_at": now,
            "updated_at": now,
        }
        chunks = []
        for index in range(total_chunks):
            start_seconds = round(index * chunk_duration_seconds, 3)
            end_seconds = round(min(duration_seconds, start_seconds + chunk_duration_seconds), 3)
            chunks.append(
                {
                    "job_id": job_id,
                    "index": index,
                    "status": "pending",
                    "start_seconds": start_seconds,
                    "end_seconds": end_seconds,
                    "attempts": 0,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "transcript_text": "",
                    "raw_transcript_text": "",
                    "segment_summary": None,
                    "actual_duration_seconds": None,
                    "last_error": None,
                    "retryable": True,
                    "created_at": now,
                    "updated_at": now,
                }
            )

        self._jobs.insert_one(job)
        try:
            self._chunks.insert_many(chunks, ordered=True)
        except Exception:
            # The job id was not returned yet, so this narrowly-scoped cleanup is
            # safe and prevents an unusable half-created job from being exposed.
            self._jobs.delete_one({"id": job_id})
            raise
        return job, resume_token

    def get_authorized_job(self, job_id: str, resume_token: str) -> dict[str, Any]:
        self._ensure_indexes()
        job = self._jobs.find_one({"id": job_id})
        if not job:
            raise JobNotFoundError("The processing job was not found.")
        expected = str(job.get("resume_token_hash") or "")
        if not expected or not hmac.compare_digest(expected, _resume_token_hash(resume_token)):
            raise InvalidResumeTokenError("The resume token is invalid for this job.")
        return job

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.find_one({"id": job_id})
        if not job:
            raise JobNotFoundError("The processing job was not found.")
        return job

    def reconcile_for_resume(self, job_id: str) -> dict[str, Any]:
        """Release stale work and retry only failed, uncommitted segments."""

        now = _utcnow()
        self._chunks.update_many(
            {
                "job_id": job_id,
                "status": "processing",
                "lease_expires_at": {"$lte": now},
            },
            {
                "$set": {
                    "status": "pending",
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            },
        )
        retried = self._chunks.update_many(
            {
                "job_id": job_id,
                "status": "failed",
                "retryable": True,
            },
            {
                "$set": {
                    "status": "pending",
                    "lease_id": None,
                    "lease_expires_at": None,
                    "last_error": None,
                    "updated_at": now,
                }
            },
        ).modified_count

        self._rollups.update_many(
            {
                "job_id": job_id,
                "status": "processing",
                "lease_expires_at": {"$lte": now},
            },
            {
                "$set": {
                    "status": "pending",
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            },
        )
        retried_rollups = self._rollups.update_many(
            {
                "job_id": job_id,
                "status": "failed",
                "retryable": True,
            },
            {
                "$set": {
                    "status": "pending",
                    "lease_id": None,
                    "lease_expires_at": None,
                    "last_error": None,
                    "updated_at": now,
                }
            },
        ).modified_count

        job = self.get_job(job_id)
        if retried or retried_rollups:
            self._jobs.update_one(
                {"id": job_id},
                {
                    "$set": {
                        "status": "rolling_up" if retried_rollups else "processing",
                        "last_error": None,
                        "updated_at": now,
                    }
                },
            )
        elif job.get("finalization_error") and job.get("completed_chunks", 0) >= job.get("total_chunks", 0):
            self._jobs.update_one(
                {"id": job_id},
                {
                    "$set": {
                        "status": "rolling_up",
                        "last_error": None,
                        "finalization_error": False,
                        "updated_at": now,
                    }
                },
            )
        return self.get_job(job_id)

    def claim_next_chunk(self, job_id: str) -> dict[str, Any] | None:
        """Atomically lease the earliest unfinished segment for this job."""

        from pymongo import ReturnDocument

        now = _utcnow()
        job = self.get_job(job_id)
        if job.get("status") == "completed":
            return None
        # Preserve chronological processing even if two browser tabs or Vercel
        # instances call this endpoint at once. A later pending segment cannot
        # jump ahead of an active earlier lease.
        earliest_unfinished = self._chunks.find_one(
            {"job_id": job_id, "status": {"$ne": "completed"}},
            sort=[("index", 1)],
        )
        if not earliest_unfinished:
            return None
        if earliest_unfinished.get("status") == "failed":
            return None

        lease_id = secrets.token_urlsafe(24)
        lease_expires_at = now + timedelta(seconds=int(job.get("lease_seconds", DEFAULT_LEASE_SECONDS)))
        chunk = self._chunks.find_one_and_update(
            {
                "job_id": job_id,
                "index": int(earliest_unfinished["index"]),
                "$or": [
                    {"status": "pending"},
                    {"status": "processing", "lease_expires_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "status": "processing",
                    "lease_id": lease_id,
                    "lease_expires_at": lease_expires_at,
                    "last_error": None,
                    "updated_at": now,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("index", 1)],
            return_document=ReturnDocument.AFTER,
        )
        if chunk:
            self._jobs.update_one(
                {"id": job_id, "status": {"$ne": "completed"}},
                {"$set": {"status": "processing", "last_error": None, "updated_at": now}},
            )
        return chunk

    def complete_chunk(
        self,
        *,
        job_id: str,
        index: int,
        lease_id: str,
        transcript_text: str,
        raw_transcript_text: str,
        actual_duration_seconds: float,
        segment_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit one checkpoint exactly once after all external work succeeds."""

        from pymongo import ReturnDocument

        now = _utcnow()
        completed = self._chunks.find_one_and_update(
            {
                "job_id": job_id,
                "index": index,
                "status": "processing",
                "lease_id": lease_id,
            },
            {
                "$set": {
                    "status": "completed",
                    "transcript_text": transcript_text,
                    "raw_transcript_text": raw_transcript_text,
                    "segment_summary": segment_summary,
                    "actual_duration_seconds": round(actual_duration_seconds, 3),
                    "lease_id": None,
                    "lease_expires_at": None,
                    "last_error": None,
                    "retryable": False,
                    "updated_at": now,
                    "completed_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if completed:
            self._jobs.update_one(
                {"id": job_id},
                {
                    "$inc": {"completed_chunks": 1},
                    "$set": {"status": "processing", "last_error": None, "updated_at": now},
                },
            )
            return completed

        existing = self._chunks.find_one({"job_id": job_id, "index": index})
        if existing and existing.get("status") == "completed":
            return existing
        raise JobStoreError("This segment lease expired before its checkpoint could be saved.")

    def fail_chunk(
        self,
        *,
        job_id: str,
        index: int,
        lease_id: str,
        public_error: str,
    ) -> None:
        """Keep previous checkpoints and mark only the active segment failed."""

        now = _utcnow()
        job = self.get_job(job_id)
        current = self._chunks.find_one({"job_id": job_id, "index": index})
        attempts = int((current or {}).get("attempts", 1))
        max_attempts = int(job.get("max_chunk_attempts", DEFAULT_CHUNK_ATTEMPTS))
        retryable = attempts < max_attempts
        failed = self._chunks.update_one(
            {
                "job_id": job_id,
                "index": index,
                "status": "processing",
                "lease_id": lease_id,
            },
            {
                "$set": {
                    "status": "failed",
                    "lease_id": None,
                    "lease_expires_at": None,
                    "last_error": public_error[:500],
                    "retryable": retryable,
                    "updated_at": now,
                }
            },
        )
        if failed.modified_count:
            self._jobs.update_one(
                {"id": job_id},
                {
                    "$set": {
                        "status": "failed",
                        "last_error": public_error[:500],
                        "updated_at": now,
                    }
                },
            )

    def all_chunks_completed(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return int(job.get("completed_chunks", 0)) >= int(job.get("total_chunks", 0))

    def has_failed_chunks(self, job_id: str) -> bool:
        return bool(self._chunks.find_one({"job_id": job_id, "status": "failed"}))

    def _rollup_batch_size(self) -> int:
        return _positive_int_setting(
            "MEDIA_ROLLUP_BATCH_SIZE", DEFAULT_ROLLUP_BATCH_SIZE, minimum=2
        )

    def _create_rollup_level(
        self,
        *,
        job_id: str,
        level: int,
        inputs: list[dict[str, Any]],
    ) -> None:
        """Create idempotent, bounded LLM rollup tasks for one tree level."""

        if not inputs:
            raise JobStoreError("There is no completed transcript data to summarize.")

        batch_size = self._rollup_batch_size()
        expected_count = int(math.ceil(len(inputs) / batch_size))
        existing_count = self._rollups.count_documents({"job_id": job_id, "level": level})
        if existing_count >= expected_count:
            return

        now = _utcnow()
        documents = [
            {
                "job_id": job_id,
                "level": level,
                "index": index,
                "status": "pending",
                "inputs": inputs[index * batch_size : (index + 1) * batch_size],
                "result": None,
                "attempts": 0,
                "lease_id": None,
                "lease_expires_at": None,
                "last_error": None,
                "retryable": True,
                "created_at": now,
                "updated_at": now,
            }
            for index in range(expected_count)
        ]
        try:
            self._rollups.insert_many(documents, ordered=False)
        except Exception:
            # A simultaneous browser request may have created the same uniquely
            # keyed work units.  If every expected unit now exists, it is safe to
            # continue; otherwise preserve the real database error.
            current_count = self._rollups.count_documents({"job_id": job_id, "level": level})
            if current_count < expected_count:
                raise

    def _ensure_rollups_started(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job.get("rollup_current_level") is not None:
            return job

        summaries: list[dict[str, Any]] = []
        for chunk in self.iter_completed_chunks(job_id):
            summary = chunk.get("segment_summary")
            if not isinstance(summary, dict):
                raise JobStoreError("A completed segment is missing its analysis checkpoint.")
            summaries.append(summary)
        self._create_rollup_level(job_id=job_id, level=0, inputs=summaries)
        now = _utcnow()
        self._jobs.update_one(
            {"id": job_id, "rollup_current_level": None},
            {
                "$set": {
                    "rollup_current_level": 0,
                    "status": "rolling_up",
                    "last_error": None,
                    "updated_at": now,
                }
            },
        )
        return self.get_job(job_id)

    def claim_next_rollup_or_final(self, job_id: str) -> dict[str, Any]:
        """Claim one bounded rollup call, or a finalization lease.

        A long meeting never sends every chunk to Gemini in one request.  Level 0
        summarizes small groups of segment analyses; completed groups are reduced
        into the next level until one final ``MeetingSummary`` remains.
        """

        from pymongo import ReturnDocument

        job = self.get_job(job_id)
        if job.get("status") == "completed":
            return {"kind": "completed"}
        if not self.all_chunks_completed(job_id) or self.has_failed_chunks(job_id):
            return {"kind": "waiting"}

        job = self._ensure_rollups_started(job_id)
        # There can be many levels, but the number decreases by at least half
        # each time.  The guard only protects malformed/manual Mongo records.
        for _ in range(64):
            level = int(job.get("rollup_current_level") or 0)
            tasks = list(
                self._rollups.find(
                    {"job_id": job_id, "level": level}, sort=[("index", 1)]
                )
            )
            if not tasks:
                raise JobStoreError("The meeting summary work queue is missing.")

            failed_task = next((task for task in tasks if task.get("status") == "failed"), None)
            if failed_task:
                self._jobs.update_one(
                    {"id": job_id},
                    {
                        "$set": {
                            "status": "failed",
                            "last_error": str(failed_task.get("last_error") or "Meeting summary failed."),
                            "updated_at": _utcnow(),
                        }
                    },
                )
                return {"kind": "waiting"}

            now = _utcnow()
            lease_id = secrets.token_urlsafe(24)
            lease_expires_at = now + timedelta(
                seconds=int(job.get("lease_seconds", DEFAULT_LEASE_SECONDS))
            )
            task = self._rollups.find_one_and_update(
                {
                    "job_id": job_id,
                    "level": level,
                    "$or": [
                        {"status": "pending"},
                        {"status": "processing", "lease_expires_at": {"$lte": now}},
                    ],
                },
                {
                    "$set": {
                        "status": "processing",
                        "lease_id": lease_id,
                        "lease_expires_at": lease_expires_at,
                        "last_error": None,
                        "updated_at": now,
                    },
                    "$inc": {"attempts": 1},
                },
                sort=[("index", 1)],
                return_document=ReturnDocument.AFTER,
            )
            if task:
                self._jobs.update_one(
                    {"id": job_id, "status": {"$ne": "completed"}},
                    {"$set": {"status": "rolling_up", "last_error": None, "updated_at": now}},
                )
                return {"kind": "rollup", "task": task}

            if any(task.get("status") == "processing" for task in tasks):
                return {"kind": "waiting"}

            completed_results = [
                task.get("result") for task in tasks if task.get("status") == "completed"
            ]
            if len(completed_results) != len(tasks) or not all(
                isinstance(result, dict) for result in completed_results
            ):
                return {"kind": "waiting"}

            if len(completed_results) == 1:
                finalization_lease_id = self.claim_finalization(job_id)
                if finalization_lease_id:
                    return {
                        "kind": "final",
                        "lease_id": finalization_lease_id,
                        "summary": completed_results[0],
                    }
                return {"kind": "waiting"}

            next_level = level + 1
            self._create_rollup_level(
                job_id=job_id,
                level=next_level,
                inputs=[result for result in completed_results if isinstance(result, dict)],
            )
            self._jobs.update_one(
                {"id": job_id, "rollup_current_level": level},
                {
                    "$set": {
                        "rollup_current_level": next_level,
                        "status": "rolling_up",
                        "updated_at": _utcnow(),
                    }
                },
            )
            job = self.get_job(job_id)

        raise JobStoreError("The meeting summary reduction exceeded the supported depth.")

    def complete_rollup(
        self,
        *,
        job_id: str,
        level: int,
        index: int,
        lease_id: str,
        summary: dict[str, Any],
    ) -> None:
        now = _utcnow()
        completed = self._rollups.update_one(
            {
                "job_id": job_id,
                "level": level,
                "index": index,
                "status": "processing",
                "lease_id": lease_id,
            },
            {
                "$set": {
                    "status": "completed",
                    "result": summary,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "last_error": None,
                    "retryable": False,
                    "updated_at": now,
                    "completed_at": now,
                }
            },
        )
        if not completed.modified_count:
            raise JobStoreError("This meeting-summary lease expired before it could be saved.")

    def fail_rollup(
        self,
        *,
        job_id: str,
        level: int,
        index: int,
        lease_id: str,
        public_error: str,
    ) -> None:
        now = _utcnow()
        job = self.get_job(job_id)
        current = self._rollups.find_one({"job_id": job_id, "level": level, "index": index})
        attempts = int((current or {}).get("attempts", 1))
        retryable = attempts < int(job.get("max_chunk_attempts", DEFAULT_CHUNK_ATTEMPTS))
        failed = self._rollups.update_one(
            {
                "job_id": job_id,
                "level": level,
                "index": index,
                "status": "processing",
                "lease_id": lease_id,
            },
            {
                "$set": {
                    "status": "failed",
                    "lease_id": None,
                    "lease_expires_at": None,
                    "last_error": public_error[:500],
                    "retryable": retryable,
                    "updated_at": now,
                }
            },
        )
        if failed.modified_count:
            self._jobs.update_one(
                {"id": job_id},
                {
                    "$set": {
                        "status": "failed",
                        "last_error": public_error[:500],
                        "updated_at": now,
                    }
                },
            )

    def claim_finalization(self, job_id: str) -> str | None:
        """Claim the one final map-reduce step after every segment is committed."""

        from pymongo import ReturnDocument

        job = self.get_job(job_id)
        if job.get("status") == "completed" or not self.all_chunks_completed(job_id):
            return None
        if self.has_failed_chunks(job_id):
            return None

        now = _utcnow()
        lease_id = secrets.token_urlsafe(24)
        lease_expires_at = now + timedelta(seconds=int(job.get("lease_seconds", DEFAULT_LEASE_SECONDS)))
        claimed = self._jobs.find_one_and_update(
            {
                "id": job_id,
                "completed_chunks": {"$gte": int(job["total_chunks"])},
                "status": {
                    "$in": [
                        "queued",
                        "processing",
                        "ready_for_rollup",
                        "rolling_up",
                        "failed",
                    ]
                },
                "$or": [
                    {"finalization_lease_id": None},
                    {"finalization_lease_expires_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "status": "finalizing",
                    "finalization_lease_id": lease_id,
                    "finalization_lease_expires_at": lease_expires_at,
                    "last_error": None,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return lease_id if claimed else None

    def complete_finalization(
        self,
        *,
        job_id: str,
        lease_id: str,
        final_summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the final meeting intelligence once, guarded by its lease."""

        from pymongo import ReturnDocument

        now = _utcnow()
        completed = self._jobs.find_one_and_update(
            {
                "id": job_id,
                "status": "finalizing",
                "finalization_lease_id": lease_id,
            },
            {
                "$set": {
                    "status": "completed",
                    "final_summary": final_summary,
                    "finalization_lease_id": None,
                    "finalization_lease_expires_at": None,
                    "finalization_error": False,
                    "last_error": None,
                    "updated_at": now,
                    "completed_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if completed:
            return completed
        return self.get_job(job_id)

    def fail_finalization(self, *, job_id: str, lease_id: str, public_error: str) -> None:
        now = _utcnow()
        self._jobs.update_one(
            {
                "id": job_id,
                "status": "finalizing",
                "finalization_lease_id": lease_id,
            },
            {
                "$set": {
                    "status": "failed",
                    "finalization_lease_id": None,
                    "finalization_lease_expires_at": None,
                    "finalization_error": True,
                    "last_error": public_error[:500],
                    "updated_at": now,
                }
            },
        )

    def iter_completed_chunks(self, job_id: str) -> Iterable[dict[str, Any]]:
        return self._chunks.find(
            {"job_id": job_id, "status": "completed"},
            sort=[("index", 1)],
        )

    def status_payload(self, job_id: str, *, recent_limit: int = 8) -> dict[str, Any]:
        """Produce the safe, UI-facing job shape without leaking its capability."""

        job = self.get_job(job_id)
        recent = list(
            self._chunks.find(
                {"job_id": job_id, "status": "completed"},
                sort=[("index", -1)],
                limit=recent_limit,
            )
        )
        recent.reverse()
        source = job.get("source") or {}
        return {
            "id": str(job["id"]),
            "status": str(job.get("status") or "queued"),
            "filename": str(source.get("filename") or "Untitled recording"),
            "media_kind": str(source.get("media_kind") or "audio"),
            "total_chunks": int(job.get("total_chunks", 0)),
            "completed_chunks": int(job.get("completed_chunks", 0)),
            "duration_seconds": round(float(source.get("duration_seconds") or 0), 3),
            "chunk_duration_seconds": round(float(job.get("chunk_duration_seconds") or 0), 3),
            "final_summary": job.get("final_summary"),
            "recent_chunks": [_serialize_chunk(chunk) for chunk in recent],
            "last_error": job.get("last_error"),
        }


@lru_cache(maxsize=1)
def get_job_store() -> MongoJobStore:
    """Create a lazy PyMongo client only when a durable job endpoint is used."""

    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri:
        raise RuntimeError("MONGODB_URI is not configured.")

    from pymongo import MongoClient

    database_name = os.environ.get("MONGODB_DATABASE", "").strip() or DEFAULT_DATABASE
    client = MongoClient(
        uri,
        connect=False,
        serverSelectionTimeoutMS=5_000,
        connectTimeoutMS=10_000,
        socketTimeoutMS=30_000,
        appname="meeting-intelligence-pipeline",
    )
    return MongoJobStore(client[database_name])


def clear_job_store_cache() -> None:
    """Test helper; production code never needs to clear the Mongo client cache."""

    get_job_store.cache_clear()
