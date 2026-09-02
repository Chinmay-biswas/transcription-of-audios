"use client";

import { uploadPresigned } from "@vercel/blob/client";
import { ChangeEvent, useEffect, useState } from "react";

import { ApiHealthResponse, apiRequest, PipelineResult } from "@/lib/api";

const maxFileBytes = 100 * 1024 * 1024;
const acceptedExtensions = [".mp3", ".wav", ".m4a"];

type UploadedRecording = {
  fingerprint: string;
  url: string;
};

function isSupportedAudio(file: File): boolean {
  return acceptedExtensions.some((extension) => file.name.toLowerCase().endsWith(extension));
}

function safeFilename(filename: string): string {
  return filename.replace(/[^a-zA-Z0-9._-]/g, "-");
}

function readableFileSize(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function fileFingerprint(file: File): string {
  return [file.name, file.size, file.lastModified, file.type].join(":");
}

async function getBlobSetupError(): Promise<string | null> {
  try {
    const response = await fetch("/api/blob/upload/diagnostic", {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    });
    if (response.ok) {
      return null;
    }

    const payload = await response.json().catch(() => null) as { error?: unknown } | null;
    if (typeof payload?.error === "string") {
      return payload.error;
    }

    if (response.redirected || response.status === 401 || response.status === 403) {
      return "Vercel Deployment Protection blocked the upload endpoint. In Project Settings > Deployment Protection, turn off Vercel Authentication for Production, then redeploy.";
    }

    if (response.status === 404) {
      return "The current deployment does not include the Blob diagnostic route. Redeploy the latest GitHub commit, then refresh this page.";
    }

    return "The Blob upload check failed with HTTP " + response.status + ". Review the /api/blob/upload/diagnostic request in Vercel Runtime Logs.";
  } catch {
    return "The browser could not reach the Blob upload endpoint. Check Vercel Deployment Protection and redeploy the latest commit.";
  }
}

export default function UploadPage() {
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadedRecording, setUploadedRecording] = useState<UploadedRecording | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PipelineResult | null>(null);

  useEffect(() => {
    return () => {
      if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
      }
    };
  }, [previewUrl]);

  function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0] || null;
    setResult(null);
    setError(null);
    setUploadProgress(0);
    setUploadedRecording(null);

    if (!selected) {
      setFile(null);
      setPreviewUrl(null);
      return;
    }
    if (!isSupportedAudio(selected)) {
      setFile(null);
      setPreviewUrl(null);
      setError("Choose an MP3, WAV, or M4A audio file.");
      return;
    }
    if (selected.size > maxFileBytes) {
      setFile(null);
      setPreviewUrl(null);
      setError("The current deployment accepts recordings up to 100 MB.");
      return;
    }

    setFile(selected);
    setPreviewUrl(URL.createObjectURL(selected));
  }

  async function processRecording() {
    if (!file) {
      setError("Choose an audio recording before starting the pipeline.");
      return;
    }

    setIsProcessing(true);
    setError(null);
    setResult(null);
    setUploadProgress(1);

    try {
      const health = await apiRequest<ApiHealthResponse>("/health", { cache: "no-store" });
      if (!health.ready) {
        const problems = [
          health.missing_settings.length
            ? "Missing: " + health.missing_settings.join(", ") + "."
            : "",
          ...health.invalid_settings
        ].filter(Boolean);
        throw new Error("The processing service is not configured. " + problems.join(" "));
      }

      const setupError = await getBlobSetupError();
      if (setupError) {
        throw new Error(setupError);
      }

      const fingerprint = fileFingerprint(file);
      let blobUrl = uploadedRecording?.fingerprint === fingerprint
        ? uploadedRecording.url
        : null;

      if (!blobUrl) {
        const blob = await uploadPresigned("meetings/" + safeFilename(file.name), file, {
          access: "public",
          handleUploadUrl: "/api/blob/upload",
          multipart: true,
          onUploadProgress: ({ percentage }) => setUploadProgress(Math.round(percentage))
        });
        blobUrl = blob.url;
        setUploadedRecording({ fingerprint, url: blobUrl });
      }

      setUploadProgress(100);
      const pipelineResult = await apiRequest<PipelineResult>("/process-blob", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          blob_url: blobUrl,
          filename: file.name,
          content_type: file.type || null
        })
      });
      setResult(pipelineResult);
    } catch (caughtError) {
      const message = caughtError instanceof Error
        ? caughtError.message
        : "The meeting could not be processed.";
      const setupMessage = message.includes("Failed to retrieve the presigned URL")
        ? await getBlobSetupError()
        : null;
      setError(setupMessage || message);
    } finally {
      setIsProcessing(false);
    }
  }

  return (
    <section>
      <div className="page-heading">
        <p className="eyebrow">New meeting</p>
        <h1>Upload and process audio</h1>
        <p>Whisper creates the transcript; Gemini extracts decisions, tasks, and tone.</p>
      </div>

      <div className="upload-panel">
        <label className="dropzone" htmlFor="meeting-audio">
          <span className="dropzone-icon" aria-hidden="true">↑</span>
          <strong>{file ? file.name : "Choose a meeting recording"}</strong>
          <span>
            {file
              ? readableFileSize(file.size) + " selected"
              : "MP3, WAV, or M4A · maximum 100 MB"}
          </span>
          <input
            id="meeting-audio"
            type="file"
            accept="audio/mpeg,audio/wav,audio/x-wav,audio/mp4,audio/x-m4a,.mp3,.wav,.m4a"
            onChange={selectFile}
            disabled={isProcessing}
          />
        </label>

        {previewUrl ? (
          <audio className="audio-preview" controls src={previewUrl}>
            Your browser cannot preview this audio file.
          </audio>
        ) : null}

        {isProcessing ? (
          <div className="progress-region" aria-live="polite">
            <div className="progress-label">
              <span>{uploadProgress < 100 ? "Uploading securely" : "Transcribing and analyzing"}</span>
              <span>{uploadProgress < 100 ? uploadProgress + "%" : "In progress"}</span>
            </div>
            <div className="progress-track">
              <span style={{ width: uploadProgress + "%" }} />
            </div>
          </div>
        ) : null}

        {error ? <p className="alert alert-error">{error}</p> : null}

        <button
          type="button"
          className="button button-primary"
          disabled={!file || isProcessing}
          onClick={processRecording}
        >
          {isProcessing ? "Running AI pipeline…" : "Run AI pipeline"}
        </button>
      </div>

      {result ? <MeetingResult result={result} /> : null}
    </section>
  );
}

function MeetingResult({ result }: { result: PipelineResult }) {
  const { intelligence, transcription } = result;

  return (
    <section className="result-section">
      <div className="result-banner">
        <span className="status-dot" aria-hidden="true" />
        <div>
          <strong>Meeting processed</strong>
          <p>{transcription.filename} · {transcription.duration_seconds.toFixed(1)} seconds</p>
        </div>
      </div>

      <div className="result-grid">
        <article className="panel transcript-panel">
          <p className="panel-kicker">Transcript</p>
          <h2>What was said</h2>
          <p className="transcript-text">{transcription.transcript_text}</p>
        </article>

        <article className="panel">
          <div className="panel-heading">
            <div>
              <p className="panel-kicker">Meeting intelligence</p>
              <h2>What matters next</h2>
            </div>
            <span className="sentiment-badge">{intelligence.overall_sentiment}</span>
          </div>

          <h3>Executive summary</h3>
          <p>{intelligence.executive_summary}</p>

          <h3>Key decisions</h3>
          <ul className="decision-list">
            {intelligence.key_decisions.map((decision) => (
              <li key={decision}>{decision}</li>
            ))}
          </ul>

          <h3>Action items</h3>
          {intelligence.action_items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Task</th>
                    <th>Owner</th>
                    <th>Due</th>
                    <th>Priority</th>
                  </tr>
                </thead>
                <tbody>
                  {intelligence.action_items.map((item, index) => (
                    <tr key={item.task + index}>
                      <td>{item.task}</td>
                      <td>{item.assignee || "Unassigned"}</td>
                      <td>{item.due_date || "Not stated"}</td>
                      <td><span className={"priority priority-" + item.priority.toLowerCase()}>{item.priority}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="empty-inline">No explicit action items were found.</p>
          )}
        </article>
      </div>
    </section>
  );
}
