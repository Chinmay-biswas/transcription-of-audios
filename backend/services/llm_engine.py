"""Gemini-powered meeting analysis and retrieval-augmented answers."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from backend.models.schemas import MeetingAnalysis, MeetingSummary


GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.6-flash"

# Arabic ranges cover Urdu's Perso-Arabic characters; Devanagari is rejected too
# because the product promises Roman-script Hinglish for Hindi/Urdu recordings.
_NON_ROMAN_HINDI_SCRIPT = re.compile(
    r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff"
    r"\ufb50-\ufdff\ufe70-\ufeff\u0900-\u097f\ua8e0-\ua8ff]"
)


def contains_non_roman_hindi_script(value: Any) -> bool:
    """Return whether nested output contains Urdu/Arabic or Devanagari text."""

    if isinstance(value, str):
        return bool(_NON_ROMAN_HINDI_SCRIPT.search(value))
    if isinstance(value, dict):
        return any(contains_non_roman_hindi_script(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(contains_non_roman_hindi_script(item) for item in value)
    return False


def create_gemini_llm() -> ChatGoogleGenerativeAI:
    """Create the shared Gemini chat model from the server-side environment."""

    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not configured.")

    return ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        temperature=0,
        api_key=api_key,
        retries=2,
        request_timeout=60,
    )


def generate_summary_and_tasks(transcript_text: str) -> MeetingAnalysis:
    """Convert a transcript into Roman-script text and meeting intelligence."""

    if not transcript_text.strip():
        raise ValueError("Transcript text is empty. Cannot generate summary.")

    parser = PydanticOutputParser(pydantic_object=MeetingAnalysis)
    prompt = PromptTemplate(
        template="""
You are an expert enterprise business analyst. Review this meeting transcript
and extract the key intelligence accurately.

LANGUAGE AND SCRIPT REQUIREMENTS:
- `romanized_transcript` must be a faithful cleaned transcript using Latin
  characters only.
- When the recording is Hindi, Urdu, or mixed Hindi-English, transliterate its
  spoken words into natural Roman Hinglish. Do not translate the transcript into
  English. Example: "yeh tune kya kiya", not Urdu or Devanagari script.
- For a mostly Hindi/Urdu/Hinglish recording, write the executive summary, key
  decisions, action items, and sentiment in concise natural Roman Hinglish too.
- For an English recording, keep the transcript and intelligence in English.
- Never emit Perso-Arabic/Urdu or Devanagari characters anywhere in the output.
- Preserve names, numbers, meaning, and code-switched English terms.

{format_instructions}

--- MEETING TRANSCRIPT ---
{transcript}
""",
        input_variables=["transcript"],
        partial_variables={"format_instructions": parser.get_format_instructions()},
    )

    chain = prompt | create_gemini_llm() | parser
    analysis = chain.invoke({"transcript": transcript_text})
    if not contains_non_roman_hindi_script(analysis.model_dump()):
        return analysis

    # Gemini normally follows the script rule on the first pass. If it does not,
    # repair the structured result once rather than exposing Urdu/Devanagari text.
    repair_prompt = PromptTemplate(
        template="""
Rewrite this structured meeting analysis using Latin characters only.

For Hindi, Urdu, or mixed Hindi-English speech, use natural Roman Hinglish.
Transliterate the original spoken words faithfully; do not translate the
transcript into English. Preserve the facts and the exact structured schema.
Never output Perso-Arabic/Urdu or Devanagari characters.

{format_instructions}

--- ORIGINAL TRANSCRIPT ---
{transcript}

--- DRAFT ANALYSIS TO REPAIR ---
{analysis_json}
""",
        input_variables=["transcript", "analysis_json"],
        partial_variables={"format_instructions": parser.get_format_instructions()},
    )
    repaired = (repair_prompt | create_gemini_llm() | parser).invoke(
        {
            "transcript": transcript_text,
            "analysis_json": analysis.model_dump_json(),
        }
    )
    if contains_non_roman_hindi_script(repaired.model_dump()):
        raise ValueError("Gemini could not produce a Roman-script transcript.")
    return repaired


def generate_rollup_summary(segment_summaries: list[dict[str, Any]]) -> MeetingSummary:
    """Reduce a bounded group of segment summaries into one meeting summary.

    Long recordings are reduced in a small tree instead of asking Gemini to read
    an unbounded transcript in one call. The caller persists each completed
    reduction, so an interrupted finalization resumes from its saved level.
    """

    if not segment_summaries:
        raise ValueError("There are no segment summaries to combine.")

    parser = PydanticOutputParser(pydantic_object=MeetingSummary)
    source_json = json.dumps(segment_summaries, ensure_ascii=False)
    prompt = PromptTemplate(
        template="""
You are an expert enterprise meeting analyst. Combine these chronological
partial meeting analyses into one accurate, concise analysis. Deduplicate
repeated points, preserve uncertainty, and never invent decisions, owners, or
due dates that are absent from the source.

LANGUAGE AND SCRIPT REQUIREMENTS:
- If the source describes Hindi, Urdu, or mixed Hindi-English speech, write the
  result in natural Roman Hinglish using Latin characters only.
- For an English meeting, write English.
- Never output Perso-Arabic/Urdu or Devanagari characters.

{format_instructions}

--- CHRONOLOGICAL SEGMENT ANALYSES ---
{segment_summaries}
""",
        input_variables=["segment_summaries"],
        partial_variables={"format_instructions": parser.get_format_instructions()},
    )
    summary = (prompt | create_gemini_llm() | parser).invoke(
        {"segment_summaries": source_json}
    )
    if not contains_non_roman_hindi_script(summary.model_dump()):
        return summary

    repair_prompt = PromptTemplate(
        template="""
Rewrite this structured meeting summary using Latin characters only. For Hindi,
Urdu, or Hinglish use natural Roman Hinglish; for English use English. Preserve
facts and the exact schema. Never emit Perso-Arabic/Urdu or Devanagari script.

{format_instructions}

--- SOURCE ANALYSES ---
{segment_summaries}

--- DRAFT TO REPAIR ---
{draft}
""",
        input_variables=["segment_summaries", "draft"],
        partial_variables={"format_instructions": parser.get_format_instructions()},
    )
    repaired = (repair_prompt | create_gemini_llm() | parser).invoke(
        {"segment_summaries": source_json, "draft": summary.model_dump_json()}
    )
    if contains_non_roman_hindi_script(repaired.model_dump()):
        raise ValueError("Gemini could not produce a Roman-script meeting summary.")
    return repaired


def extract_relevant_info_from_chunk(chunk: str, question: str) -> str:
    """Map step: retain only context that can answer the user's question."""

    prompt = PromptTemplate(
        template="""
You are a meticulous meeting-data extractor. Review one transcript chunk.
Extract and summarize only information relevant to the user's question.
If the chunk contains no relevant information, output exactly:
No relevant information.

When the meeting is Hindi, Urdu, or Hinglish, write relevant information in
natural Roman Hinglish using Latin characters only. Never use Perso-Arabic/Urdu
or Devanagari script. For an English meeting, use English.

TEXT CHUNK:
{chunk}

USER QUESTION:
{question}
""",
        input_variables=["chunk", "question"],
    )

    response = (prompt | create_gemini_llm()).invoke(
        {"chunk": chunk, "question": question}
    )
    return response.content.strip()


def generate_rag_answer(question: str, context: str) -> str:
    """Generate a final answer strictly from condensed transcript context."""

    prompt = PromptTemplate(
        template="""
You are a helpful meeting assistant. Answer the user's question using only the
provided meeting transcript context. If the answer is not contained in the
context, say: I cannot find the answer to this in the meeting transcript.

When the meeting or question is Hindi, Urdu, or Hinglish, answer in natural
Roman Hinglish using Latin characters only. Never use Perso-Arabic/Urdu or
Devanagari script. For an English meeting and English question, use English.

CONTEXT:
{context}

QUESTION:
{question}
""",
        input_variables=["context", "question"],
    )

    response = (prompt | create_gemini_llm()).invoke(
        {"context": context, "question": question}
    )
    return response.content.strip()
