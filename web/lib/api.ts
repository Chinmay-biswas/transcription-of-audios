export type ActionItem = {
  task: string;
  assignee: string | null;
  due_date: string | null;
  priority: string;
};

export type MeetingIntelligence = {
  executive_summary: string;
  key_decisions: string[];
  action_items: ActionItem[];
  overall_sentiment: string;
};

export type PipelineResult = {
  status: "success";
  meeting_id: string;
  transcription: {
    filename: string;
    transcript_text: string;
    duration_seconds: number;
  };
  intelligence: MeetingIntelligence;
};

export type MediaKind = "audio" | "video";

export type MediaChunk = {
  index: number;
  start_seconds: number;
  end_seconds: number;
  transcript_text: string;
};

export type MediaJobStatus =
  | "queued"
  | "processing"
  | "rolling_up"
  | "finalizing"
  | "ready_for_rollup"
  | "failed"
  | "completed";

export type MediaJob = {
  id: string;
  filename: string;
  media_kind: MediaKind;
  status: MediaJobStatus;
  total_chunks: number;
  completed_chunks: number;
  duration_seconds: number;
  chunk_duration_seconds: number;
  recent_chunks?: MediaChunk[];
  final_summary?: MeetingIntelligence | null;
  last_error?: string | null;
};

export type CreateMediaJobResponse = {
  job: MediaJob;
  resume_token: string;
};

export type MediaJobStatusResponse = {
  job: MediaJob;
};

export type RunMediaJobResponse = {
  job: MediaJob;
  action: "segment" | "rollup" | "completed" | "waiting";
  completed_chunk?: MediaChunk | null;
};

export type MeetingRecord = {
  id: string;
  filename: string;
  created_at: string;
  summary: MeetingIntelligence | Record<string, never>;
};

export type MeetingListResponse = {
  status: "success";
  meetings: MeetingRecord[];
};

export type MeetingChatResponse = {
  status: "success";
  answer: string;
  context_used: string[];
};

export type AnalyticsItem = {
  task: string;
  assignee: string;
  due_date: string;
  priority: string;
  meeting_id: string;
  meeting_filename: string;
};

export type AnalyticsResponse = {
  status: "success";
  total_meetings: number;
  total_action_items: number;
  active_assignees: number;
  sentiment_counts: Record<string, number>;
  action_items: AnalyticsItem[];
};

export type ApiHealthResponse = {
  status: "ok" | "configuration_required";
  ready: boolean;
  missing_settings: string[];
  invalid_settings: string[];
  chunk_jobs_ready?: boolean;
  chunk_jobs_missing_settings?: string[];
};

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL || "/api/v1";

function detailMessage(detail: unknown): string | null {
  if (typeof detail === "string" && detail.trim()) {
    return detail;
  }
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const message = (detail as { message?: unknown }).message;
    return typeof message === "string" && message.trim() ? message : null;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (!item || typeof item !== "object") {
          return null;
        }
        const message = (item as { msg?: unknown }).msg;
        return typeof message === "string" ? message : null;
      })
      .filter((message): message is string => Boolean(message));
    return messages.length ? messages.join(" ") : null;
  }
  return null;
}

function requestFailureMessage(response: Response, body: unknown, responseText: string): string {
  if (body && typeof body === "object") {
    const payload = body as { detail?: unknown; error?: unknown; message?: unknown };
    const message =
      detailMessage(payload.detail) ||
      detailMessage(payload.error) ||
      detailMessage(payload.message);
    if (message) {
      return message;
    }
  }

  const vercelError = response.headers.get("x-vercel-error");
  if (response.status === 401 || response.status === 403 || response.redirected) {
    return "Vercel Deployment Protection blocked the processing API. Disable Vercel Authentication for Production or open the authenticated deployment.";
  }
  if (response.status === 504 || vercelError === "FUNCTION_INVOCATION_TIMEOUT") {
    return "The processing service timed out before it could finish. Try a shorter recording and check the API service duration in Vercel Runtime Logs.";
  }
  if (response.status === 502) {
    return "The processing container stopped or could not answer. Check the API service Runtime Logs for a startup or memory error.";
  }
  if (response.status === 503) {
    return "The processing service or one of its required integrations is unavailable.";
  }
  if (response.status === 500) {
    const contentType = response.headers.get("content-type") || "";
    const plainText = responseText.trim();
    if (
      contentType.includes("text/plain") &&
      plainText &&
      plainText.length <= 500 &&
      !plainText.includes("<")
    ) {
      return plainText;
    }
    return "The processing container could not initialize or crashed while handling the request. Check the API service Runtime Logs.";
  }
  if (response.status === 404) {
    return "The processing API route was not found in this deployment. Redeploy the latest GitHub commit.";
  }

  const suffix = vercelError ? " (" + vercelError + ")" : "";
  return "The processing request failed with HTTP " + response.status + suffix + ".";
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(apiBaseUrl + path, {
      ...init,
      cache: init?.cache ?? "no-store",
      credentials: "same-origin",
      headers
    });
  } catch {
    throw new Error(
      "The browser could not reach the processing API. Check Vercel Deployment Protection and the API service deployment."
    );
  }

  const responseText = await response.text();
  let body: unknown = null;
  if (responseText) {
    try {
      body = JSON.parse(responseText) as unknown;
    } catch {
      body = null;
    }
  }

  if (response.redirected) {
    throw new Error(
      "Vercel Deployment Protection redirected the processing API. Open the authenticated deployment or adjust Production protection settings."
    );
  }

  if (!response.ok) {
    throw new Error(requestFailureMessage(response, body, responseText));
  }
  if (body === null) {
    throw new Error(
      "The processing API returned a non-JSON response. Check the API service routing and Runtime Logs."
    );
  }
  return body as T;
}

export function createMediaJob(payload: {
  blob_url: string;
  filename: string;
  content_type: string | null;
}): Promise<CreateMediaJobResponse> {
  return apiRequest<CreateMediaJobResponse>("/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export function getMediaJobStatus(
  jobId: string,
  resumeToken: string
): Promise<MediaJobStatusResponse> {
  return apiRequest<MediaJobStatusResponse>("/jobs/" + encodeURIComponent(jobId) + "/status", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resume_token: resumeToken })
  });
}

export function runNextMediaJobChunk(
  jobId: string,
  resumeToken: string
): Promise<RunMediaJobResponse> {
  return apiRequest<RunMediaJobResponse>("/jobs/" + encodeURIComponent(jobId) + "/run-next", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resume_token: resumeToken })
  });
}
