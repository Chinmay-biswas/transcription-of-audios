# Meeting Intelligence Pipeline - Vercel Edition

This is a deployment-ready copy of the original Meeting Intelligence Pipeline.
It replaces the local-only Streamlit, file-system, and ChromaDB pieces with:

- Next.js for the web interface
- Vercel Blob for browser-to-storage audio uploads
- FastAPI + Docker + Whisper for audio transcription
- Gemini for structured meeting intelligence and embeddings
- Qdrant Cloud for durable meeting search and chat

The original project remains unchanged at:

    C:/Users/CJ/Desktop/dsProject/Meeting-Intelligence-Pipeline/Meeting-Intelligence-Pipeline

## Architecture

    Browser (Next.js)
      - uploads audio directly to Vercel Blob
      - calls /api/v1/process-blob
              |
              v
    FastAPI container service
      - downloads the Blob object temporarily
      - transcribes with Whisper
      - extracts summary, decisions, tasks with Gemini
      - stores embeddings and metadata in Qdrant Cloud

## Before you deploy

Create these external resources first:

1. A Gemini API key in Google AI Studio.
2. A Qdrant Cloud cluster and API key. The free tier is sufficient for a demo.
3. A Vercel Blob store connected to this Vercel project.
4. A GitHub repository containing this copied project.

> This version uses a public Blob store so the container can download uploaded
> recordings without exposing storage credentials to the browser. It is suitable
> for a portfolio/demo using non-sensitive audio. Before handling private meeting
> recordings, add authentication and switch to an authenticated private-storage
> retrieval flow.

## Environment variables

Copy .env.example to .env for local backend development. Never commit it.

In Vercel, add these values under Project Settings > Environment Variables for
both Production and Preview:

    GOOGLE_API_KEY
    GEMINI_MODEL=gemini-3.6-flash
    GEMINI_EMBEDDING_MODEL=gemini-embedding-001
    GEMINI_EMBEDDING_DIMENSIONS=768
    QDRANT_URL
    QDRANT_API_KEY
    QDRANT_COLLECTION=meeting_transcripts_gemini
    WHISPER_MODEL=base
    MAX_AUDIO_BYTES=104857600
    BLOB_READ_WRITE_TOKEN

Creating the Blob store in Vercel normally adds BLOB_READ_WRITE_TOKEN
automatically. Keep GOOGLE_API_KEY, QDRANT_API_KEY, and BLOB_READ_WRITE_TOKEN
as Secrets.

## Local development

### 1. Backend

    cd "C:/Users/CJ/Desktop/dsProject/Meeting-Intelligence-Pipeline-vercel"
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r backend\requirements.txt
    Copy-Item .env.example .env
    python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

Fill in the real Gemini and Qdrant values in .env before using the API.

### 2. Frontend

Open a second terminal:

    cd "C:/Users/CJ/Desktop/dsProject/Meeting-Intelligence-Pipeline-vercel/web"
    npm install
    Copy-Item .env.local.example .env.local
    npm run dev

Set BLOB_READ_WRITE_TOKEN in web/.env.local too when running the Next.js upload
route outside vercel dev.

Open http://localhost:3000.

## Deploy to Vercel

1. Push the contents of this project to GitHub.
2. In Vercel, choose Add New > Project, import that repository, and use the
   repository root as the Root Directory.
3. In the project's framework settings, choose Services.
4. Create a public Vercel Blob store from the project's Storage tab.
5. Add the environment variables listed above.
6. Deploy.

vercel.json starts two services in one deployment:

- web: the Next.js frontend at /
- api: the Dockerized FastAPI backend behind /api/v1/*

The API service includes FFmpeg and the Whisper base model. Vercel may require
Large Functions for this container because Whisper, PyTorch, FFmpeg, and model
weights can exceed the standard function package allowance. Enable Large
Functions in Vercel before deploying if the build reports a size-limit error.

For a local Vercel-style multi-service run:

    npm install -g vercel
    vercel login
    vercel link
    vercel dev -L

## What changed from the original

| Original component | Vercel replacement |
| --- | --- |
| Streamlit UI | Next.js app in web/ |
| data/uploads | Direct browser upload to Vercel Blob |
| Local persistent ChromaDB | Qdrant Cloud |
| App-import Whisper loading | Lazy Whisper loading in Docker |
| Python-literal analytics metadata | JSON-native analytics endpoint |

The frontend/ directory is retained only as a reference to the original
Streamlit interface. Vercel deploys the web/ Next.js application instead.
