// Task detail component — shows task info, report, events, approve/reject.
// Uses SSE for real-time updates while the task is in a non-terminal state,
// with polling fallback.

import { useEffect, useState, useCallback } from "react";
import { api, type TaskDetail, type EventItem } from "./api";

interface TaskDetailProps {
  taskId: string;
  onAction: () => void;
}

const POLL_FALLBACK_INTERVAL = 3000; // 3 seconds
const TERMINAL_STATES = new Set(["passed", "failed", "approved", "rejected", "needs_review"]);

export function TaskDetail({ taskId, onAction }: TaskDetailProps) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [report, setReport] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actor, setActor] = useState("");
  const [actionMsg, setActionMsg] = useState("");

  const fetchDetail = useCallback(() => {
    Promise.all([
      api.getTask(taskId),
      api.getEvents(taskId),
      api.getReport(taskId).catch(() => ({ task_id: taskId, report: "" })),
    ])
      .then(([t, e, r]) => {
        setTask(t);
        setEvents(e);
        setReport(r.report);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [taskId]);

  // Initial load.
  useEffect(() => {
    setLoading(true);
    setError("");
    setActionMsg("");
    fetchDetail();
  }, [taskId, fetchDetail]);

  // SSE + polling fallback for live updates while non-terminal.
  const isTerminal = task ? TERMINAL_STATES.has(task.status) : true;
  useEffect(() => {
    if (isTerminal) return;

    let es: EventSource | null = null;
    let pollInterval: ReturnType<typeof setInterval> | null = null;
    let sseFailed = false;

    try {
      es = api.streamTasks();
      es.onmessage = () => fetchDetail();
      es.onerror = () => {
        if (!sseFailed) {
          sseFailed = true;
          es?.close();
          es = null;
          pollInterval = setInterval(fetchDetail, POLL_FALLBACK_INTERVAL);
        }
      };
    } catch {
      pollInterval = setInterval(fetchDetail, POLL_FALLBACK_INTERVAL);
    }

    return () => {
      es?.close();
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [isTerminal, fetchDetail]);

  const handleApprove = async () => {
    try {
      await api.approve(taskId, actor);
      setActionMsg("Approved successfully");
      onAction();
      fetchDetail();
    } catch (e) {
      setActionMsg(`Error: ${(e as Error).message}`);
    }
  };

  const handleReject = async () => {
    try {
      await api.reject(taskId, actor);
      setActionMsg("Rejected successfully");
      onAction();
      fetchDetail();
    } catch (e) {
      setActionMsg(`Error: ${(e as Error).message}`);
    }
  };

  if (loading) return <div className="loading">Loading task...</div>;
  if (error) return <div className="error">{error}</div>;
  if (!task) return <div className="empty">Task not found</div>;

  const canApprove = task.status === "passed" || task.status === "needs_review";

  return (
    <div className="task-detail">
      <div className="detail-header">
        <h2>{task.task_id}</h2>
        <span className={`status-badge status-${task.status}`}>{task.status}</span>
        {!isTerminal && <span className="live-indicator">● live</span>}
      </div>

      <div className="detail-section">
        <strong>Request:</strong> {task.user_request}
      </div>
      <div className="detail-section">
        <strong>Repo:</strong> {task.repo_name} · <strong>Branch:</strong> {task.task_branch}
      </div>
      <div className="detail-section">
        <strong>Created:</strong> {new Date(task.created_at).toLocaleString()}
      </div>

      {canApprove && (
        <div className="action-bar">
          <input
            type="text"
            placeholder="Your name (optional)"
            value={actor}
            onChange={(e) => setActor(e.target.value)}
            className="actor-input"
          />
          <button className="btn-approve" onClick={handleApprove}>Approve</button>
          <button className="btn-reject" onClick={handleReject}>Reject</button>
          {actionMsg && <span className="action-msg">{actionMsg}</span>}
        </div>
      )}

      {report && (
        <div className="detail-section">
          <h3>Report</h3>
          <pre className="report-content">{report}</pre>
        </div>
      )}

      <div className="detail-section">
        <h3>Event Timeline ({events.length} events)</h3>
        <div className="event-timeline">
          {events.map((e) => {
            const isRefusal = e.type === "auto.merge.refused";
            const reason = (e.payload as Record<string, string>).reason;
            return (
              <div key={e.event_id} className={`event-item${isRefusal ? " event-refusal" : ""}`}>
                <div className="event-type">{e.type}</div>
                <div className="event-time">{new Date(e.timestamp).toLocaleString()}</div>
                <div className="event-hash">{e.hash.slice(0, 16)}...</div>
                {isRefusal && reason && (
                  <div className="event-payload">⚠ {reason}</div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
