from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api import routes
from backend.main import app
from backend.models.schemas import MediaJobCreateRequest, MeetingAnalysis, TranscriptionResponse
from backend.services.job_store import clear_job_store_cache
from backend.services.media_processing import (
    DEFAULT_CHUNK_DURATION_SECONDS,
    ensure_supported_media_filename,
    media_chunk_duration_seconds,
    media_kind_for_filename,
)


def _status_payload(*, status: str = "queued", completed_chunks: int = 0) -> dict[str, object]:
    return {
        "id": "job-1",
        "status": status,
        "filename": "meeting.mp4",
        "media_kind": "video",
        "total_chunks": 3,
        "completed_chunks": completed_chunks,
        "duration_seconds": 125.2,
        "chunk_duration_seconds": 60,
        "final_summary": None,
        "recent_chunks": [],
        "last_error": None,
    }


class FakeCreateStore:
    def __init__(self) -> None:
        self.created: dict[str, object] | None = None

    def create_job(self, **kwargs):
        self.created = kwargs
        return {"id": "job-1"}, "resume-token"

    def status_payload(self, job_id: str) -> dict[str, object]:
        self.asserted_job_id = job_id
        return _status_payload()


class FakeChunkStore:
    def __init__(self) -> None:
        self.completed: dict[str, object] | None = None
        self.failed: dict[str, object] | None = None

    def complete_chunk(self, **kwargs):
        self.completed = kwargs
        return {
            "index": kwargs["index"],
            "start_seconds": 60,
            "end_seconds": 120,
            "transcript_text": kwargs["transcript_text"],
        }

    def fail_chunk(self, **kwargs):
        self.failed = kwargs


class FakeNextStore:
    def __init__(self) -> None:
        self.claimed = False

    def get_authorized_job(self, job_id: str, resume_token: str):
        self.authorized = (job_id, resume_token)
        return {"id": job_id}

    def reconcile_for_resume(self, job_id: str):
        return {"id": job_id, "status": "queued"}

    def claim_next_chunk(self, job_id: str):
        if self.claimed:
            return None
        self.claimed = True
        return {
            "index": 1,
            "lease_id": "lease-1",
            "start_seconds": 60,
            "end_seconds": 120,
        }

    def status_payload(self, job_id: str):
        return _status_payload(status="processing", completed_chunks=2)

    def has_failed_chunks(self, job_id: str):
        return False

    def all_chunks_completed(self, job_id: str):
        return False


class MediaConfigurationTests(unittest.TestCase):
    def test_media_extensions_and_kind(self) -> None:
        self.assertEqual(ensure_supported_media_filename("recording.MP4"), ".mp4")
        self.assertEqual(media_kind_for_filename("recording.mov"), "video")
        self.assertEqual(media_kind_for_filename("recording.m4a"), "audio")
        with self.assertRaisesRegex(ValueError, "Unsupported media format"):
            ensure_supported_media_filename("recording.avi")

    def test_chunk_duration_uses_default_and_validates_setting(self) -> None:
        with patch.dict(os.environ, {"MEDIA_CHUNK_DURATION_SECONDS": ""}, clear=False):
            self.assertEqual(media_chunk_duration_seconds(), DEFAULT_CHUNK_DURATION_SECONDS)
        with patch.dict(os.environ, {"MEDIA_CHUNK_DURATION_SECONDS": "0"}, clear=False):
            with self.assertRaisesRegex(ValueError, "at least 1"):
                media_chunk_duration_seconds()


class ChunkJobOrchestrationTests(unittest.TestCase):
    def test_job_api_explains_when_mongo_is_not_configured(self) -> None:
        clear_job_store_cache()
        with patch.dict(os.environ, {"MONGODB_URI": ""}, clear=False):
            response = TestClient(app).post(
                "/api/v1/jobs",
                json={
                    "blob_url": "https://example.public.blob.vercel-storage.com/meeting.mp4",
                    "filename": "meeting.mp4",
                    "content_type": "video/mp4",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("MONGODB_URI", response.json()["detail"])

    @patch("backend.api.routes.media_chunk_duration_seconds", return_value=60)
    @patch("backend.api.routes.probe_blob_media", return_value=125.2)
    @patch("backend.api.routes._get_job_store")
    def test_create_media_job_probes_and_persists_manifest(
        self,
        mock_store_factory,
        mock_probe,
        _mock_chunk_duration,
    ) -> None:
        store = FakeCreateStore()
        mock_store_factory.return_value = store

        result = routes._create_media_job(
            MediaJobCreateRequest(
                blob_url="https://example.public.blob.vercel-storage.com/meeting.mp4",
                filename="meeting.mp4",
                content_type="video/mp4",
            )
        )

        self.assertEqual(result["resume_token"], "resume-token")
        self.assertEqual(result["job"]["total_chunks"], 3)
        self.assertEqual(store.created["media_kind"], "video")
        self.assertEqual(store.created["duration_seconds"], 125.2)
        mock_probe.assert_called_once()

    @patch("backend.api.routes._remove_temporary_file")
    @patch("backend.api.routes._save_meeting_segment_to_db")
    @patch("backend.api.routes._generate_summary_and_tasks")
    @patch("backend.api.routes._transcribe_audio")
    @patch("backend.api.routes.extract_audio_segment", return_value="segment.wav")
    def test_completed_segment_is_checkpointed_once(
        self,
        _mock_extract,
        mock_transcribe,
        mock_analyze,
        mock_save_segment,
        mock_remove,
    ) -> None:
        store = FakeChunkStore()
        mock_transcribe.return_value = TranscriptionResponse(
            filename="segment.wav",
            transcript_text="یہ تو نے کیا کیا",
            duration_seconds=58.5,
        )
        mock_analyze.return_value = MeetingAnalysis(
            romanized_transcript="yeh tune kya kiya",
            executive_summary="Ek jazbaati segment hai.",
            key_decisions=[],
            action_items=[],
            overall_sentiment="Udaas",
        )
        job = {
            "id": "job-1",
            "source": {
                "blob_url": "https://example.public.blob.vercel-storage.com/meeting.mp4",
                "filename": "meeting.mp4",
            },
        }
        chunk = {"index": 1, "lease_id": "lease-1", "start_seconds": 60, "end_seconds": 120}

        result = routes._process_claimed_media_chunk(store, job, chunk)

        self.assertEqual(result["transcript_text"], "yeh tune kya kiya")
        self.assertEqual(store.completed["index"], 1)
        self.assertEqual(store.completed["segment_summary"]["overall_sentiment"], "Udaas")
        self.assertIsNone(store.failed)
        mock_save_segment.assert_called_once()
        mock_remove.assert_called_once_with("segment.wav")

    @patch("backend.api.routes._remove_temporary_file")
    @patch("backend.api.routes.extract_audio_segment", side_effect=RuntimeError("decoder failed"))
    def test_failed_segment_marks_only_current_checkpoint_retryable(
        self,
        _mock_extract,
        _mock_remove,
    ) -> None:
        store = FakeChunkStore()
        job = {"id": "job-1", "source": {"blob_url": "https://example.public.blob.vercel-storage.com/meeting.mp4"}}
        chunk = {"index": 2, "lease_id": "lease-2", "start_seconds": 120, "end_seconds": 125}

        with self.assertLogs(routes.logger, level="ERROR"):
            result = routes._process_claimed_media_chunk(store, job, chunk)

        self.assertIsNone(result)
        self.assertEqual(store.failed["index"], 2)
        self.assertEqual(store.failed["lease_id"], "lease-2")
        self.assertIn("Resume", store.failed["public_error"])

    @patch("backend.api.routes._process_claimed_media_chunk")
    @patch("backend.api.routes._get_job_store")
    def test_run_next_claims_only_one_segment_and_returns_saved_progress(
        self,
        mock_store_factory,
        mock_process,
    ) -> None:
        store = FakeNextStore()
        mock_store_factory.return_value = store
        mock_process.return_value = {
            "index": 1,
            "start_seconds": 60,
            "end_seconds": 120,
            "transcript_text": "second segment",
        }

        result = routes._process_next_media_job("job-1", "resume-token")

        self.assertEqual(result["action"], "segment")
        self.assertEqual(result["completed_chunk"]["index"], 1)
        self.assertEqual(store.authorized, ("job-1", "resume-token"))
        mock_process.assert_called_once()


if __name__ == "__main__":
    unittest.main()
