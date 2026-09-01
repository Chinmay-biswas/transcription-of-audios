"use client";

import { useEffect, useMemo, useState } from "react";

import { AnalyticsResponse, apiRequest } from "@/lib/api";

type CountEntry = {
  label: string;
  count: number;
};

function countBy(items: string[]): CountEntry[] {
  const counts = new Map<string, number>();
  for (const item of items) {
    counts.set(item, (counts.get(item) || 0) + 1);
  }
  return Array.from(counts, ([label, count]) => ({ label, count })).sort(
    (left, right) => right.count - left.count
  );
}

function priorityClass(priority: string): string {
  return "priority priority-" + priority.toLowerCase();
}

export default function AnalyticsPage() {
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function loadAnalytics() {
      try {
        const response = await apiRequest<AnalyticsResponse>("/analytics");
        if (active) {
          setData(response);
        }
      } catch (caughtError) {
        if (active) {
          setError(caughtError instanceof Error ? caughtError.message : "Could not load analytics.");
        }
      }
    }
    void loadAnalytics();
    return () => {
      active = false;
    };
  }, []);

  const assigneeCounts = useMemo(
    () => countBy(data?.action_items.map((item) => item.assignee) || []),
    [data]
  );
  const priorityCounts = useMemo(
    () => countBy(data?.action_items.map((item) => item.priority) || []),
    [data]
  );
  const maxAssigneeCount = Math.max(...assigneeCounts.map((item) => item.count), 1);

  return (
    <section>
      <div className="page-heading">
        <p className="eyebrow">Operational visibility</p>
        <h1>Task analytics</h1>
        <p>See assignments, urgency, and extracted work across your indexed meetings.</p>
      </div>

      {error ? <p className="alert alert-error">{error}</p> : null}
      {!data && !error ? <p className="loading-copy">Compiling meeting analytics…</p> : null}

      {data ? (
        <>
          <div className="metric-grid">
            <MetricCard label="Tasks extracted" value={data.total_action_items} />
            <MetricCard label="Active assignees" value={data.active_assignees} />
            <MetricCard label="Meetings indexed" value={data.total_meetings} />
          </div>

          {!data.action_items.length ? (
            <div className="empty-state">
              <h2>No action items yet</h2>
              <p>Process a meeting with explicit tasks to populate this view.</p>
            </div>
          ) : (
            <>
              <div className="analytics-grid">
                <article className="panel">
                  <p className="panel-kicker">Work distribution</p>
                  <h2>Tasks by assignee</h2>
                  <div className="bar-list">
                    {assigneeCounts.map((item) => (
                      <div className="bar-row" key={item.label}>
                        <div className="bar-label">
                          <span>{item.label}</span>
                          <strong>{item.count}</strong>
                        </div>
                        <div className="bar-track">
                          <span style={{ width: (item.count / maxAssigneeCount) * 100 + "%" }} />
                        </div>
                      </div>
                    ))}
                  </div>
                </article>

                <article className="panel">
                  <p className="panel-kicker">Urgency profile</p>
                  <h2>Tasks by priority</h2>
                  <div className="priority-breakdown">
                    {priorityCounts.map((item) => (
                      <div className="priority-count" key={item.label}>
                        <span className={priorityClass(item.label)}>{item.label}</span>
                        <strong>{item.count}</strong>
                      </div>
                    ))}
                  </div>
                  <div className="sentiment-summary">
                    <h3>Meeting sentiment</h3>
                    {Object.entries(data.sentiment_counts).map(([sentiment, count]) => (
                      <span key={sentiment}>{sentiment}: {count}</span>
                    ))}
                  </div>
                </article>
              </div>

              <article className="panel ledger">
                <div className="panel-heading">
                  <div>
                    <p className="panel-kicker">Master ledger</p>
                    <h2>All extracted action items</h2>
                  </div>
                </div>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Task</th>
                        <th>Owner</th>
                        <th>Due</th>
                        <th>Priority</th>
                        <th>Meeting</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.action_items.map((item, index) => (
                        <tr key={item.meeting_id + item.task + index}>
                          <td>{item.task}</td>
                          <td>{item.assignee}</td>
                          <td>{item.due_date}</td>
                          <td><span className={priorityClass(item.priority)}>{item.priority}</span></td>
                          <td>{item.meeting_filename}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </article>
            </>
          )}
        </>
      ) : null}
    </section>
  );
}

function MetricCard({ label, value }: { label: string; value: number }) {
  return (
    <article className="metric-card">
      <p>{label}</p>
      <strong>{value}</strong>
    </article>
  );
}
