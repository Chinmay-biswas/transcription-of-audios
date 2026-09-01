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

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL || "/api/v1";

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiBaseUrl + path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.headers || {})
    }
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      body && typeof body.detail === "string"
        ? body.detail
        : "The request could not be completed.";
    throw new Error(detail);
  }
  return body as T;
}
