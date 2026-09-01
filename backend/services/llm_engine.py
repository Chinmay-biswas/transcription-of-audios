"""Gemini-powered meeting analysis and retrieval-augmented answers."""

from __future__ import annotations

import os

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from backend.models.schemas import MeetingSummary


GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")


def create_gemini_llm() -> ChatGoogleGenerativeAI:
    """Create the shared Gemini chat model from the server-side environment."""

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not configured.")

    return ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        temperature=0,
        api_key=api_key,
    )


def generate_summary_and_tasks(transcript_text: str) -> MeetingSummary:
    """Convert a transcript into the strict meeting intelligence schema."""

    if not transcript_text.strip():
        raise ValueError("Transcript text is empty. Cannot generate summary.")

    parser = PydanticOutputParser(pydantic_object=MeetingSummary)
    prompt = PromptTemplate(
        template="""
You are an expert enterprise business analyst. Review this meeting transcript
and extract the key intelligence accurately.

{format_instructions}

--- MEETING TRANSCRIPT ---
{transcript}
""",
        input_variables=["transcript"],
        partial_variables={"format_instructions": parser.get_format_instructions()},
    )

    chain = prompt | create_gemini_llm() | parser
    return chain.invoke({"transcript": transcript_text})


def extract_relevant_info_from_chunk(chunk: str, question: str) -> str:
    """Map step: retain only context that can answer the user's question."""

    prompt = PromptTemplate(
        template="""
You are a meticulous meeting-data extractor. Review one transcript chunk.
Extract and summarize only information relevant to the user's question.
If the chunk contains no relevant information, output exactly:
No relevant information.

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
