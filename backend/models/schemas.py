from typing import List, Optional

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


class TranscriptionResponse(BaseModel):
    """The structured output from Whisper transcription."""

    filename: str
    transcript_text: str
    duration_seconds: float
