# Meeting Intelligence Pipeline - Vercel Edition

This is a deployment-ready copy of the original Meeting Intelligence Pipeline.
It replaces the local-only Streamlit, file-system, and ChromaDB pieces with:

- Next.js for the web interface
- Vercel Blob multipart upload for browser-to-storage audio and video files
- FastAPI + Docker + FFmpeg + Whisper for bounded media-segment transcription
- Gemini for Roman Hinglish conversion, structured meeting intelligence, and embeddings
- MongoDB for durable media-job checkpoints, retry, and browser-refresh resume
- Qdrant Cloud for durable meeting search and chat

The original project remains unchanged at:

    C:/Users/CJ/Desktop/dsProject/Meeting-Intelligence-Pipeline/Meeting-Intelligence-Pipeline

## Architecture

    Browser (Next.js)
      - multipart-uploads one original audio/video file directly to Vercel Blob
      - creates a durable media job and requests one saved work unit at a time
              |
              v
    FastAPI container service
      - FFprobes the Blob and uses FFmpeg to extract one valid time segment
      - transcribes the segment with Whisper and Romanizes it with Gemini
      - checkpoints the segment in MongoDB and indexes it in Qdrant
      - reduces saved segment summaries in bounded Gemini batches

    MongoDB keeps the manifest, segment state, retry lease, and final result.
    Qdrant exposes only completed meetings to search/history.

## Before you deploy

Create these external resources first:

1. A Gemini API key in Google AI Studio.
2. A Qdrant Cloud cluster and API key. The free tier is sufficient for a demo.
3. A MongoDB Atlas database (or another reachable MongoDB deployment).
4. A Vercel Blob store connected to this Vercel project.
5. A GitHub repository containing this copied project.

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
    MONGODB_URI
    MONGODB_DATABASE=meeting_intelligence
    WHISPER_MODEL=base
    MAX_AUDIO_BYTES=104857600
    MAX_MEDIA_BYTES=2147483648
    MEDIA_CHUNK_DURATION_SECONDS=60
    MEDIA_ROLLUP_BATCH_SIZE=10
    BLOB_STORE_ID
    BLOB_WEBHOOK_PUBLIC_KEY

Connect the Blob store through Vercel Storage rather than copying those two
Blob values manually. New Blob connections use short-lived OIDC credentials;
they do not need BLOB_READ_WRITE_TOKEN in Vercel. Keep GOOGLE_API_KEY and
QDRANT_API_KEY as Secrets. BLOB_READ_WRITE_TOKEN is only a local-development
fallback when the Next.js app runs outside Vercel.

## Local development

### 1. Backend

    cd "C:/Users/CJ/Desktop/dsProject/Meeting-Intelligence-Pipeline-vercel"
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r backend\requirements.txt
    Copy-Item .env.example .env
    python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

Fill in the real Gemini and Qdrant values in .env before using the API.
For resumable audio/video jobs, also fill in MONGODB_URI. Do not put that URI
in `web/.env.local` or commit it to Git.

### 2. Frontend

Open a second terminal:

    cd "C:/Users/CJ/Desktop/dsProject/Meeting-Intelligence-Pipeline-vercel/web"
    npm install
    Copy-Item .env.local.example .env.local
    npm run dev

Set BLOB_READ_WRITE_TOKEN in web/.env.local when running the Next.js upload
route outside vercel dev.

Set NEXT_PUBLIC_MAX_MEDIA_BYTES to the same value as MAX_MEDIA_BYTES when you
change the Blob upload limit.

Open http://localhost:3000.

## Deploy to Vercel

1. Push the contents of this project to GitHub.
2. In Vercel, choose Add New > Project, import that repository, and use the
   repository root as the Root Directory.
3. In the project's framework settings, choose Services.
4. Create and connect a public Vercel Blob store from the project's Storage tab.
5. Open Project Settings > Security, enable Secure Backend Access with OIDC
   Federation, and save. This lets the upload route authenticate to Blob with a
   short-lived credential.
6. Add the non-Blob environment variables listed above, including MONGODB_URI.
   The connected Blob
   store supplies BLOB_STORE_ID and BLOB_WEBHOOK_PUBLIC_KEY automatically.
7. Deploy (or redeploy after changing the OIDC setting).

vercel.json starts two services in one deployment:

- web: the Next.js frontend at /
- api: the Dockerized FastAPI backend behind /api/v1/*

The API service includes FFmpeg, FFprobe, and the Whisper base model. Vercel may require
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

## Resumable audio and video processing

The upload page accepts MP3, WAV, M4A, MP4, MOV, and WebM. Vercel Blob handles
the large-file transfer using multipart upload. After the source Blob is fully
uploaded, the app creates a MongoDB job with 60-second time segments by default.

Each `Run next` request processes exactly one segment: FFmpeg extracts valid
audio for that time range, Whisper transcribes it, Gemini produces Roman
Hinglish where needed, and MongoDB marks that segment complete. If a request,
browser tab, or deployment fails, press **Resume from saved checkpoint**. The
completed segments are retained and only the first unfinished segment retries.

There is no hard total meeting-duration limit imposed by the application. Long
recordings are handled as many bounded requests and the final intelligence is
combined as a saved summary tree. A browser must remain open to request the
next work unit; for unattended multi-hour processing, use a queue/worker or
Vercel Sandbox/Workflow on top of the same MongoDB manifest.
