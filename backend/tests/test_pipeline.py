from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.language_models.fake import FakeListLLM

from backend.api import routes
from backend.main import app
from backend.models.schemas import (
    MeetingAnalysis,
    TranscriptionResponse,
)
from backend.services.blob_storage import DEFAULT_MAX_AUDIO_BYTES, _max_audio_bytes
from backend.services.llm_engine import (
    contains_non_roman_hindi_script,
    generate_summary_and_tasks,
)
from backend.services.vector_store import (
    DEFAULT_COLLECTION,
    _collection_name,
    _embedding_dimensions,
)


class ConfigurationTests(unittest.TestCase):
    def test_blank_optional_settings_use_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MAX_AUDIO_BYTES": "",
                "GEMINI_EMBEDDING_DIMENSIONS": "",
                "QDRANT_COLLECTION": "",
            },
            clear=False,
        ):
            self.assertEqual(_max_audio_bytes(), DEFAULT_MAX_AUDIO_BYTES)
            self.assertEqual(_embedding_dimensions(), 768)
            self.assertEqual(_collection_name(), DEFAULT_COLLECTION)

    def test_health_reports_missing_required_settings(self) -> None:
        with patch.dict(
            os.environ,
            {"GOOGLE_API_KEY": "", "QDRANT_URL": "", "QDRANT_API_KEY": ""},
            clear=False,
        ):
            health = routes.service_health()

        self.assertFalse(health["ready"])
        self.assertEqual(
            health["missing_settings"],
            ["GOOGLE_API_KEY", "QDRANT_URL", "QDRANT_API_KEY"],
        )

    def test_health_accepts_blank_optional_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GOOGLE_API_KEY": "configured",
                "QDRANT_URL": "https://example.qdrant.io",
                "QDRANT_API_KEY": "configured",
                "MAX_AUDIO_BYTES": "",
                "GEMINI_EMBEDDING_DIMENSIONS": "",
            },
            clear=False,
        ):
            health = routes.service_health()

        self.assertTrue(health["ready"])
        self.assertEqual(health["invalid_settings"], [])


class PipelineErrorTests(unittest.TestCase):
    @patch("backend.api.routes._transcribe_audio", side_effect=RuntimeError("decoder failed"))
    def test_transcription_failure_has_safe_stage_error(self, _mock_transcribe) -> None:
        with self.assertLogs(routes.logger, level="ERROR"):
            with self.assertRaises(routes.PipelineStageError) as raised:
                routes._run_pipeline("missing.mp3", "meeting.mp3")

        self.assertEqual(raised.exception.stage, "transcription")
        self.assertEqual(raised.exception.code, "transcription_failed")
        response = routes._as_http_error(raised.exception)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.detail["stage"], "transcription")

    def test_roman_script_policy_detects_urdu_and_devanagari(self) -> None:
        self.assertTrue(contains_non_roman_hindi_script("یہ تو نے کیا کیا"))
        self.assertTrue(contains_non_roman_hindi_script("यह तूने क्या किया"))
        self.assertFalse(contains_non_roman_hindi_script("yeh tune kya kiya"))

    @patch("backend.api.routes._save_meeting_to_db")
    @patch("backend.api.routes._generate_summary_and_tasks")
    @patch("backend.api.routes._transcribe_audio")
    def test_pipeline_returns_and_stores_roman_hinglish_transcript(
        self,
        mock_transcribe,
        mock_analyze,
        mock_save,
    ) -> None:
        mock_transcribe.return_value = TranscriptionResponse(
            filename="song.mp3",
            transcript_text="یہ تو نے کیا کیا",
            duration_seconds=58,
        )
        mock_analyze.return_value = MeetingAnalysis(
            romanized_transcript="yeh tune kya kiya",
            executive_summary="Yeh recording jazbaati geet par based hai.",
            key_decisions=[],
            action_items=[],
            overall_sentiment="Udaas",
        )

        result = routes._run_pipeline("song.mp3", "song.mp3")

        self.assertEqual(
            result["transcription"]["transcript_text"],
            "yeh tune kya kiya",
        )
        self.assertNotIn("romanized_transcript", result["intelligence"])
        self.assertEqual(
            mock_save.call_args.kwargs["transcript"],
            "yeh tune kya kiya",
        )

    @patch("backend.services.llm_engine.create_gemini_llm")
    def test_gemini_repairs_forbidden_script_once(self, mock_create_llm) -> None:
        invalid = json.dumps(
            {
                "executive_summary": "یہ ایک گانا ہے",
                "key_decisions": [],
                "action_items": [],
                "overall_sentiment": "Udaas",
                "romanized_transcript": "یہ تو نے کیا کیا",
            },
            ensure_ascii=False,
        )
        repaired = json.dumps(
            {
                "executive_summary": "Yeh ek jazbaati gaana hai.",
                "key_decisions": [],
                "action_items": [],
                "overall_sentiment": "Udaas",
                "romanized_transcript": "yeh tune kya kiya",
            }
        )
        mock_create_llm.return_value = FakeListLLM(responses=[invalid, repaired])

        result = generate_summary_and_tasks("یہ تو نے کیا کیا")

        self.assertEqual(result.romanized_transcript, "yeh tune kya kiya")
        self.assertEqual(mock_create_llm.call_count, 2)

    @patch("backend.services.llm_engine.create_gemini_llm")
    def test_gemini_never_returns_forbidden_script(self, mock_create_llm) -> None:
        invalid = json.dumps(
            {
                "executive_summary": "यह एक गाना है",
                "key_decisions": [],
                "action_items": [],
                "overall_sentiment": "Udaas",
                "romanized_transcript": "यह तूने क्या किया",
            },
            ensure_ascii=False,
        )
        mock_create_llm.return_value = FakeListLLM(responses=[invalid, invalid])

        with self.assertRaisesRegex(
            ValueError,
            "Gemini could not produce a Roman-script transcript",
        ):
            generate_summary_and_tasks("यह तूने क्या किया")


class ApiRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_route_returns_json(self) -> None:
        response = self.client.get("/api/v1/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertIn("ready", response.json())

    @patch("backend.api.routes._remove_temporary_file")
    @patch(
        "backend.api.routes._run_pipeline",
        return_value={"status": "success", "meeting_id": "meeting-1"},
    )
    @patch("backend.api.routes.download_blob_to_tempfile", return_value="temporary.mp3")
    def test_process_blob_orchestrates_pipeline(
        self,
        mock_download,
        mock_pipeline,
        mock_remove,
    ) -> None:
        response = self.client.post(
            "/api/v1/process-blob",
            json={
                "blob_url": "https://example.public.blob.vercel-storage.com/meeting.mp3",
                "filename": "meeting.mp3",
                "content_type": "audio/mpeg",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["meeting_id"], "meeting-1")
        mock_download.assert_called_once()
        mock_pipeline.assert_called_once()
        mock_remove.assert_called_once_with("temporary.mp3")

    @patch("backend.api.routes._get_all_meetings", return_value=[])
    def test_meetings_route(self, _mock_meetings) -> None:
        response = self.client.get("/api/v1/meetings")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success", "meetings": []})

    @patch(
        "backend.api.routes._get_meeting_analytics",
        return_value={
            "total_meetings": 0,
            "total_action_items": 0,
            "active_assignees": 0,
            "sentiment_counts": {},
            "action_items": [],
        },
    )
    def test_analytics_route(self, _mock_analytics) -> None:
        response = self.client.get("/api/v1/analytics")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_meetings"], 0)

    @patch(
        "backend.api.routes._search_specific_meeting",
        return_value={"documents": [[]]},
    )
    def test_meeting_chat_handles_no_matching_context(self, _mock_search) -> None:
        response = self.client.post(
            "/api/v1/meeting-chat",
            json={"query": "What was decided?", "meeting_id": "meeting-1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["context_used"], [])


if __name__ == "__main__":
    unittest.main()
