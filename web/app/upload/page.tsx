"use client";

import { uploadPresigned } from "@vercel/blob/client";
import { ChangeEvent, useEffect, useRef, useState } from "react";

import {
  ApiHealthResponse,
  apiRequest,
  createMediaJob,
  getMediaJobStatus,
  MediaChunk,
  MediaJob,
  MeetingIntelligence,
  runNextMediaJobChunk
} from "@/lib/api";

const defaultMaxMediaBytes = 2 * 1024 * 1024 * 1024;
const configuredMaxMediaBytes = Number(process.env.NEXT_PUBLIC_MAX_MEDIA_BYTES || defaultMaxMediaBytes);
const maxMediaBytes = Number.isSafeInteger(configuredMaxMediaBytes) && configuredMaxMediaBytes > 0
  ? configuredMaxMediaBytes
  : defaultMaxMediaBytes;
const acceptedExtensions = [".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm"];
const savedJobKey = "meeting-intelligence-resumable-job-v1";

type UploadedMedia = {
  fingerprint: string;
  url: string;
};

type SavedJob = {
  jobId: string;
  resumeToken: string;
};

type PipelinePhase = "idle" | "uploading" | "creating" | "processing" | "paused" | "completed";

function isSupportedMedia(file: File): boolean {
  return acceptedExtensions.some((extension) => file.name.toLowerCase().endsWith(extension));
}

function isVideo(file: File | null): boolean {
  if (!file) {
    return false;
  }
  return file.type.startsWith("video/") || [".mp4", ".mov", ".webm"].some((extension) =>
    file.name.toLowerCase().endsWith(extension)
  );
}

function safeFilename(filename: string): string {
  return filename.replace(/[^a-zA-Z0-9._-]/g, "-");
}

function readableFileSize(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) {
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
  }
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "0:00";
  }
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainingSeconds = total % 60;
  const minutePart = hours ? String(minutes).padStart(2, "0") : String(minutes);
  return (hours ? hours + ":" : "") + minutePart + ":" + String(remainingSeconds).padStart(2, "0");
}

function fileFingerprint(file: File): string {
  return [file.name, file.size, file.lastModified, file.type].join(":");
}

function mergeChunks(current: MediaChunk[], incoming: MediaChunk[]): MediaChunk[] {
  const byIndex = new Map<number, MediaChunk>();
  for (const chunk of [...current, ...incoming]) {
    byIndex.set(chunk.index, chunk);
  }
  return [...byIndex.values()].sort((left, right) => left.index - right.index);
}

function saveResumeJob(jobId: string, resumeToken: string): void {
  window.localStorage.setItem(savedJobKey, JSON.stringify({ jobId, resumeToken } satisfies SavedJob));
}

function clearResumeJob(): void {
  window.localStorage.removeItem(savedJobKey);
}

function getSavedJob(): SavedJob | null {
  try {
    const rawValue = window.localStorage.getItem(savedJobKey);
    if (!rawValue) {
      return null;
    }
    const parsed = JSON.parse(rawValue) as Partial<SavedJob>;
    if (typeof parsed.jobId === "string" && typeof parsed.resumeToken === "string") {
      return { jobId: parsed.jobId, resumeToken: parsed.resumeToken };
    }
  } catch {
    // A malformed browser cache should never stop a new upload.
  }
  clearResumeJob();
  return null;
}

async function getMediaDuration(file: File): Promise<number | null> {
  return new Promise((resolve) => {
    const element = document.createElement(isVideo(file) ? "video" : "audio");
    const objectUrl = URL.createObjectURL(file);
    const finish = (value: number | null) => {
      URL.revokeObjectURL(objectUrl);
      resolve(value);
    };
    element.preload = "metadata";
    element.onloadedmetadata = () => finish(Number.isFinite(element.duration) ? element.duration : null);
    element.onerror = () => finish(null);
    element.src = objectUrl;
  });
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

function phaseCopy(phase: PipelinePhase, job: MediaJob | null, uploadProgress: number): string {
  if (phase === "uploading") {
    return "Uploading original media " + uploadProgress + "%";
  }
  if (phase === "creating") {
    return "Checking media and saving the resumable job";
  }
  if (phase === "processing" && job) {
    if (job.status === "rolling_up" || job.status === "finalizing") {
      return "Combining completed segment summaries";
    }
    return "Processing segment " + Math.min(job.completed_chunks + 1, job.total_chunks) + " of " + job.total_chunks;
  }
  if (phase === "paused") {
    return "Processing is paused safely";
  }
  if (phase === "completed") {
    return "Meeting processing completed";
  }
  return "Ready";
}

export default function UploadPage() {
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [localDuration, setLocalDuration] = useState<number | null>(null);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadedMedia, setUploadedMedia] = useState<UploadedMedia | null>(null);
  const [job, setJob] = useState<MediaJob | null>(null);
  const [resumeToken, setResumeToken] = useState<string | null>(null);
  const [chunks, setChunks] = useState<MediaChunk[]>([]);
  const [phase, setPhase] = useState<PipelinePhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const continueRef = useRef(true);

  useEffect(() => {
    return () => {
      if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
      }
    };
  }, [previewUrl]);

  useEffect(() => {
    const saved = getSavedJob();
    if (!saved) {
      return;
    }
    let cancelled = false;
    void getMediaJobStatus(saved.jobId, saved.resumeToken)
      .then((response) => {
        if (cancelled) {
          return;
        }
        setJob(response.job);
        setResumeToken(saved.resumeToken);
        setChunks((current) => mergeChunks(current, response.job.recent_chunks || []));
        setPhase(response.job.status === "completed" ? "completed" : "paused");
        setNotice(
          response.job.status === "completed"
            ? "Restored the completed meeting from this browser."
            : "Restored a saved job. Resume starts from the first unfinished segment."
        );
      })
      .catch(() => {
        if (!cancelled) {
          clearResumeJob();
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function updateJob(nextJob: MediaJob, completedChunk?: MediaChunk | null): void {
    setJob(nextJob);
    setChunks((current) => mergeChunks(current, [
      ...(nextJob.recent_chunks || []),
      ...(completedChunk ? [completedChunk] : [])
    ]));
  }

  function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0] || null;
    continueRef.current = false;
    setError(null);
    setNotice(null);
    setUploadProgress(0);
    setUploadedMedia(null);
    setJob(null);
    setResumeToken(null);
    setChunks([]);
    setLocalDuration(null);
    clearResumeJob();

    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
    }
    if (!selected) {
      setFile(null);
      setPreviewUrl(null);
      setPhase("idle");
      return;
    }
    if (!isSupportedMedia(selected)) {
      setFile(null);
      setPreviewUrl(null);
      setPhase("idle");
      setError("Choose an MP3, WAV, M4A, MP4, MOV, or WebM file.");
      return;
    }
    if (selected.size > maxMediaBytes) {
      setFile(null);
      setPreviewUrl(null);
      setPhase("idle");
      setError("This deployment accepts media up to " + readableFileSize(maxMediaBytes) + ".");
      return;
    }

    setFile(selected);
    setPreviewUrl(URL.createObjectURL(selected));
    setPhase("idle");
    void getMediaDuration(selected).then(setLocalDuration);
  }

  async function ensureServiceReady(): Promise<void> {
    const health = await apiRequest<ApiHealthResponse>("/health", { cache: "no-store" });
    if (!health.ready) {
      const problems = [
        health.missing_settings.length ? "Missing: " + health.missing_settings.join(", ") + "." : "",
        ...health.invalid_settings
      ].filter(Boolean);
      throw new Error("The processing service is not configured. " + problems.join(" "));
    }
    if (!health.chunk_jobs_ready) {
      throw new Error(
        "Resumable audio/video processing needs MongoDB. Add " +
        (health.chunk_jobs_missing_settings || ["MONGODB_URI"]).join(", ") +
        " in Vercel Environment Variables, then redeploy."
      );
    }
    const setupError = await getBlobSetupError();
    if (setupError) {
      throw new Error(setupError);
    }
  }

  async function runSavedJob(jobId: string, token: string): Promise<void> {
    continueRef.current = true;
    setPhase("processing");
    setError(null);
    setNotice(null);

    try {
      // Each request owns exactly one durable unit. There is no hidden parallel
      // processing, so a failed interval can be retried from its checkpoint.
      while (continueRef.current) {
        const response = await runNextMediaJobChunk(jobId, token);
        updateJob(response.job, response.completed_chunk);

        if (response.action === "completed" || response.job.status === "completed") {
          setPhase("completed");
          clearResumeJob();
          setNotice("All segments and the final meeting summary are ready.");
          return;
        }
        if (response.action === "waiting") {
          setPhase("paused");
          setError(response.job.last_error || "This job is waiting for its current segment. Use Resume to continue safely.");
          return;
        }
      }
      setPhase("paused");
      setNotice("Paused after the last completed checkpoint. Resume whenever you are ready.");
    } catch (caughtError) {
      setPhase("paused");
      setError(caughtError instanceof Error ? caughtError.message : "The job could not continue.");
    }
  }

  async function startNewJob(): Promise<void> {
    if (!file) {
      setError("Choose an audio or video recording before starting the pipeline.");
      return;
    }

    setError(null);
    setNotice(null);
    try {
      await ensureServiceReady();
      const fingerprint = fileFingerprint(file);
      let blobUrl = uploadedMedia?.fingerprint === fingerprint ? uploadedMedia.url : null;
      if (!blobUrl) {
        setPhase("uploading");
        setUploadProgress(1);
        const uniqueName = typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID() + "-"
          : Date.now() + "-";
        const blob = await uploadPresigned("meetings/" + uniqueName + safeFilename(file.name), file, {
          access: "public",
          handleUploadUrl: "/api/blob/upload",
          multipart: true,
          onUploadProgress: ({ percentage }) => setUploadProgress(Math.round(percentage))
        });
        blobUrl = blob.url;
        setUploadedMedia({ fingerprint, url: blobUrl });
      }

      setUploadProgress(100);
      setPhase("creating");
      const created = await createMediaJob({
        blob_url: blobUrl,
        filename: file.name,
        content_type: file.type || null
      });
      updateJob(created.job);
      setResumeToken(created.resume_token);
      saveResumeJob(created.job.id, created.resume_token);
      await runSavedJob(created.job.id, created.resume_token);
    } catch (caughtError) {
      setPhase("paused");
      const message = caughtError instanceof Error ? caughtError.message : "The media job could not be started.";
      const setupMessage = message.includes("Failed to retrieve the presigned URL")
        ? await getBlobSetupError()
        : null;
      setError(setupMessage || message);
    }
  }

  function pauseAfterCheckpoint(): void {
    continueRef.current = false;
    setNotice("The current request will finish, then processing will pause at its saved checkpoint.");
  }

  const isWorking = phase === "uploading" || phase === "creating" || phase === "processing";
  const canResume = Boolean(job && resumeToken && job.status !== "completed" && !isWorking);
  const progressPercent = job
    ? Math.round((job.completed_chunks / Math.max(job.total_chunks, 1)) * 100)
    : uploadProgress;

  return (
    <section>
      <div className="page-heading">
        <p className="eyebrow">New meeting</p>
        <h1>Upload and process audio or video</h1>
        <p>
          Large recordings upload directly to Blob, then Whisper processes saved time segments.
          Gemini returns Roman Hinglish when needed and extracts decisions, tasks, and tone.
        </p>
      </div>

      <div className="upload-panel">
        <label className="dropzone" htmlFor="meeting-media">
          <span className="dropzone-icon" aria-hidden="true">↑</span>
          <strong>{file ? file.name : job ? job.filename : "Choose a meeting recording"}</strong>
          <span>
            {file
              ? readableFileSize(file.size) + " selected" + (localDuration ? " · " + formatTime(localDuration) : "")
              : job
                ? "Saved job · " + job.completed_chunks + "/" + job.total_chunks + " segments complete"
                : "MP3, WAV, M4A, MP4, MOV, or WebM · maximum " + readableFileSize(maxMediaBytes)}
          </span>
          <input
            id="meeting-media"
            type="file"
            accept="audio/mpeg,audio/mp3,audio/wav,audio/x-wav,audio/mp4,audio/x-m4a,audio/m4a,audio/webm,video/mp4,video/quicktime,video/webm,.mp3,.wav,.m4a,.mp4,.mov,.webm"
            onChange={selectFile}
            disabled={isWorking}
          />
        </label>

        {previewUrl ? (
          isVideo(file) ? (
            <video className="video-preview" controls src={previewUrl}>
              Your browser cannot preview this video file.
            </video>
          ) : (
            <audio className="audio-preview" controls src={previewUrl}>
              Your browser cannot preview this audio file.
            </audio>
          )
        ) : null}

        {(isWorking || job) ? (
          <div className="progress-region" aria-live="polite">
            <div className="progress-label">
              <span>{phaseCopy(phase, job, uploadProgress)}</span>
              <span>{job ? job.completed_chunks + "/" + job.total_chunks : uploadProgress + "%"}</span>
            </div>
            <div className="progress-track">
              <span style={{ width: (job ? progressPercent : uploadProgress) + "%" }} />
            </div>
            {job ? (
              <p className="field-hint">
                {job.media_kind === "video" ? "Video audio track" : "Audio"} · {formatTime(job.duration_seconds)} · {formatTime(job.chunk_duration_seconds)} batches
              </p>
            ) : null}
          </div>
        ) : null}

        {notice ? <p className="alert alert-notice">{notice}</p> : null}
        {error ? <p className="alert alert-error">{error}</p> : null}

        <div className="upload-actions">
          <button
            type="button"
            className="button button-primary"
            disabled={(!file && !canResume) || isWorking}
            onClick={() => {
              if (canResume && job && resumeToken) {
                void runSavedJob(job.id, resumeToken);
              } else {
                void startNewJob();
              }
            }}
          >
            {isWorking
              ? "Processing…"
              : canResume
                ? "Resume from saved checkpoint"
                : "Upload and start processing"}
          </button>
          {phase === "processing" ? (
            <button type="button" className="button button-secondary" onClick={pauseAfterCheckpoint}>
              Pause after this segment
            </button>
          ) : null}
        </div>
      </div>

      {job ? <JobProgress job={job} chunks={chunks} /> : null}
      {job?.status === "completed" && job.final_summary ? <MeetingResult result={job.final_summary} job={job} /> : null}
    </section>
  );
}

function JobProgress({ job, chunks }: { job: MediaJob; chunks: MediaChunk[] }) {
  return (
    <section className="result-section job-progress-section">
      <div className="result-banner">
        <span className="status-dot" aria-hidden="true" />
        <div>
          <strong>{job.status === "completed" ? "All processing checkpoints are saved" : "Durable processing checkpoints"}</strong>
          <p>{job.completed_chunks} of {job.total_chunks} segments are complete. A retry starts from the first unfinished segment.</p>
        </div>
      </div>
      <div className="segment-list" aria-live="polite">
        {chunks.length ? chunks.map((chunk) => (
          <article className="segment-card" key={chunk.index}>
            <div className="segment-card-heading">
              <strong>Segment {chunk.index + 1}</strong>
              <span>{formatTime(chunk.start_seconds)} – {formatTime(chunk.end_seconds)}</span>
            </div>
            <p lang="hi-Latn" dir="ltr">
              {chunk.transcript_text || "No clear speech was detected in this segment."}
            </p>
          </article>
        )) : (
          <p className="empty-inline">Completed segments will appear here one by one.</p>
        )}
      </div>
    </section>
  );
}

function MeetingResult({ result, job }: { result: MeetingIntelligence; job: MediaJob }) {
  return (
    <section className="result-section">
      <div className="result-banner">
        <span className="status-dot" aria-hidden="true" />
        <div>
          <strong>Meeting processed</strong>
          <p>{job.filename} · {formatTime(job.duration_seconds)} · {job.total_chunks} saved segments</p>
        </div>
      </div>

      <div className="result-grid">
        <article className="panel transcript-panel" lang="hi-Latn" dir="ltr">
          <p className="panel-kicker">Transcript</p>
          <h2>Segment transcript</h2>
          <p className="transcript-text">Every processed segment is displayed above and remains available after a retry.</p>
        </article>

        <article className="panel">
          <div className="panel-heading">
            <div>
              <p className="panel-kicker">Meeting intelligence</p>
              <h2>What matters next</h2>
            </div>
            <span className="sentiment-badge">{result.overall_sentiment}</span>
          </div>

          <h3>Executive summary</h3>
          <p lang="hi-Latn" dir="ltr">{result.executive_summary}</p>

          <h3>Key decisions</h3>
          {result.key_decisions.length ? (
            <ul className="decision-list">
              {result.key_decisions.map((decision, index) => (
                <li key={decision + index} lang="hi-Latn" dir="ltr">{decision}</li>
              ))}
            </ul>
          ) : <p className="empty-inline">No explicit decisions were found.</p>}

          <h3>Action items</h3>
          {result.action_items.length ? (
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
                  {result.action_items.map((item, index) => (
                    <tr key={item.task + index}>
                      <td lang="hi-Latn" dir="ltr">{item.task}</td>
                      <td lang="hi-Latn" dir="ltr">{item.assignee || "Unassigned"}</td>
                      <td lang="hi-Latn" dir="ltr">{item.due_date || "Not stated"}</td>
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
