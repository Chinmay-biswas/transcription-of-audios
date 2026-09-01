"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  apiRequest,
  MeetingChatResponse,
  MeetingListResponse,
  MeetingRecord
} from "@/lib/api";

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  context?: string[];
};

export default function MeetingsPage() {
  const [meetings, setMeetings] = useState<MeetingRecord[]>([]);
  const [selectedMeetingId, setSelectedMeetingId] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isAsking, setIsAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function loadMeetings() {
      try {
        const response = await apiRequest<MeetingListResponse>("/meetings");
        if (!active) {
          return;
        }
        setMeetings(response.meetings);
        setSelectedMeetingId(response.meetings[0]?.id || "");
      } catch (caughtError) {
        if (active) {
          setError(caughtError instanceof Error ? caughtError.message : "Could not load meetings.");
        }
      } finally {
        if (active) {
          setIsLoading(false);
        }
      }
    }
    void loadMeetings();
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    setMessages([]);
  }, [selectedMeetingId]);

  const selectedMeeting = useMemo(
    () => meetings.find((meeting) => meeting.id === selectedMeetingId),
    [meetings, selectedMeetingId]
  );

  async function submitQuestion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedQuestion = question.trim();
    if (!trimmedQuestion || !selectedMeetingId || isAsking) {
      return;
    }

    const userMessage: ChatMessage = { role: "user", content: trimmedQuestion };
    setMessages((current) => [...current, userMessage]);
    setQuestion("");
    setError(null);
    setIsAsking(true);

    try {
      const response = await apiRequest<MeetingChatResponse>("/meeting-chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: trimmedQuestion,
          meeting_id: selectedMeetingId
        })
      });
      setMessages((current) => [
        ...current,
        {
          role: "assistant",
          content: response.answer,
          context: response.context_used
        }
      ]);
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "The question could not be answered.");
    } finally {
      setIsAsking(false);
    }
  }

  return (
    <section className="chat-layout">
      <div className="page-heading">
        <p className="eyebrow">Meeting archive</p>
        <h1>Ask your meetings</h1>
        <p>Answers are generated only from the transcript chunks stored for the selected recording.</p>
      </div>

      <div className="panel meeting-selector">
        <label htmlFor="meeting-select">Meeting to analyze</label>
        <select
          id="meeting-select"
          value={selectedMeetingId}
          disabled={isLoading || !meetings.length}
          onChange={(event) => setSelectedMeetingId(event.target.value)}
        >
          {meetings.map((meeting) => (
            <option key={meeting.id} value={meeting.id}>
              {meeting.filename}
            </option>
          ))}
        </select>
        {selectedMeeting?.created_at ? (
          <p className="field-hint">Processed {new Date(selectedMeeting.created_at).toLocaleString()}</p>
        ) : null}
      </div>

      {isLoading ? <p className="loading-copy">Loading archived meetings…</p> : null}
      {error ? <p className="alert alert-error">{error}</p> : null}

      {!isLoading && !meetings.length && !error ? (
        <div className="empty-state">
          <h2>No meetings yet</h2>
          <p>Process an audio recording first, then return here to ask questions about it.</p>
        </div>
      ) : null}

      {selectedMeeting ? (
        <div className="panel chat-panel">
          <div className="chat-log" aria-live="polite">
            {!messages.length ? (
              <div className="chat-empty">
                <span aria-hidden="true">✦</span>
                <p>Try asking: “What deadline did the team agree on?”</p>
              </div>
            ) : null}
            {messages.map((message, index) => (
              <article className={"message message-" + message.role} key={message.role + index}>
                <p className="message-role">{message.role === "user" ? "You" : "Meeting assistant"}</p>
                <p>{message.content}</p>
                {message.context?.length ? (
                  <details className="source-context">
                    <summary>View source transcript context</summary>
                    {message.context.map((chunk, chunkIndex) => (
                      <p key={chunkIndex}>{chunk}</p>
                    ))}
                  </details>
                ) : null}
              </article>
            ))}
            {isAsking ? (
              <article className="message message-assistant">
                <p className="message-role">Meeting assistant</p>
                <p>Searching the selected transcript…</p>
              </article>
            ) : null}
          </div>

          <form className="chat-composer" onSubmit={submitQuestion}>
            <label className="sr-only" htmlFor="meeting-question">Ask a question</label>
            <input
              id="meeting-question"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder={"Ask a question about " + selectedMeeting.filename}
              disabled={isAsking}
            />
            <button className="button button-primary" disabled={isAsking || !question.trim()} type="submit">
              Ask
            </button>
          </form>
        </div>
      ) : null}
    </section>
  );
}
