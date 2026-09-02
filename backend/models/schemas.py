from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ActionItem(BaseModel):
    """Represents a single task assigned during a meeting."""

    task: str = Field(..., description="The specific task that needs to be completed.")
    assignee: Optional[str] = Field(
        None,
        description="The name of the person assigned to the task. Null if unassigned.",
    )
    due_date: Optional[str] = Field(
        None,
        description="The deadline if mentioned, for example Next Friday.",
    )
    priority: str = Field(
        default="Normal",
        description="Priority level: High, Normal, or Low.",
    )


class MeetingSummary(BaseModel):
    """The structured intelligence generated after analyzing a meeting."""

    executive_summary: str = Field(
        ...,
        description="A concise two- or three-paragraph meeting summary.",
    )
    key_decisions: List[str] = Field(
        ...,
        description="Major decisions agreed during the call.",
    )
    action_items: List[ActionItem] = Field(
        ...,
        description="Action items extracted from the meeting.",
    )
    overall_sentiment: str = Field(
        ...,
        description="Overall emotional tone, for example Positive or Collaborative.",
    )


class MeetingAnalysis(MeetingSummary):
    """Internal Gemini result containing the user-facing Roman transcript."""

    romanized_transcript: str = Field(
        ...,
        description=(
            "A faithful transcript written in Latin characters. Hindi or Urdu speech "
            "must be transliterated as natural Roman Hinglish, not translated."
        ),
    )


class TranscriptionResponse(BaseModel):
    """The structured output from Whisper transcription."""

    filename: str
    transcript_text: str
    duration_seconds: float


class MediaJobCreateRequest(BaseModel):
    """A durable, Blob-backed audio or video processing request."""

    blob_url: str = Field(..., min_length=1)
    filename: str = Field(..., min_length=1, max_length=255)
    content_type: Optional[str] = Field(default=None, max_length=100)


class MediaJobResumeRequest(BaseModel):
    """Capability token required to inspect or continue a processing job."""

    resume_token: str = Field(..., min_length=1, max_length=512)


class MediaJobChunk(BaseModel):
    """One durably completed time segment shown in the upload UI."""

    index: int = Field(..., ge=0)
    start_seconds: float = Field(..., ge=0)
    end_seconds: float = Field(..., ge=0)
    transcript_text: str


class MediaJobStatus(BaseModel):
    """Secret-free durable progress returned to the browser."""

    id: str
    status: str
    filename: str
    media_kind: Literal["audio", "video"]
    total_chunks: int = Field(..., ge=0)
    completed_chunks: int = Field(..., ge=0)
    duration_seconds: float = Field(..., ge=0)
    chunk_duration_seconds: float = Field(..., gt=0)
    final_summary: Optional[MeetingSummary] = None
    recent_chunks: List[MediaJobChunk] = Field(default_factory=list)
    last_error: Optional[str] = None


class MediaJobResponse(BaseModel):
    """Response shared by job creation, status, and one-step processing calls."""

    status: Literal["success"] = "success"
    action: Literal["segment", "rollup", "completed", "waiting"]
    job: MediaJobStatus
    completed_chunk: Optional[MediaJobChunk] = None
    resume_token: Optional[str] = None
