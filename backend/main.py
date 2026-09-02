"""FastAPI entry point for the Vercel container service."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Local development only. Vercel injects the same values as environment variables.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from backend.api import routes


app = FastAPI(
    title="Meeting Intelligence API",
    description=(
        "Whisper transcription, Gemini intelligence extraction, Qdrant meeting RAG, "
        "and MongoDB-backed resumable audio/video processing."
    ),
    version="2.1.0",
    docs_url="/api/v1/docs",
    redoc_url=None,
    openapi_url="/api/v1/openapi.json",
)

frontend_origins = [
    origin.strip()
    for origin in os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000").split(",")
    if origin.strip()
]
if frontend_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=frontend_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

app.include_router(routes.router, prefix="/api/v1")


@app.get("/")
def health_check() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "message": "Meeting Intelligence API is running.",
        "qdrant_configured": bool(
            os.environ.get("QDRANT_URL") and os.environ.get("QDRANT_API_KEY")
        ),
        "gemini_configured": bool(os.environ.get("GOOGLE_API_KEY")),
    }
