// Task detail component — shows task info, report, events, approve/reject.

import { useEffect, useState } from "react";
import { api, type TaskDetail, type EventItem } from "./api";

interface TaskDetailProps {
  taskId: string;
  onAction: () => void;
}

export function TaskDetail({ taskId, onAction }: TaskDetailProps) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [report, setReport] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actor, setActor] = useState("");
  const [actionMsg, setActionMsg] = useState("");

  useEffect(() => {
    setLoading(true);
    setError("");
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

  const handleApprove = async () => {
    try {
      await api.approve(taskId, actor);
      setActionMsg("Approved successfully");
      onAction();
    } catch (e) {
      setActionMsg(`Error: ${(e as Error).message}`);
    }
  };

  const handleReject = async () => {
    try {
      await api.reject(taskId, actor);
      setActionMsg("Rejected successfully");
      onAction();
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
          {events.map((e) => (
            <div key={e.event_id} className="event-item">
              <div className="event-type">{e.type}</div>
              <div className="event-time">{new Date(e.timestamp).toLocaleString()}</div>
              <div className="event-hash">{e.hash.slice(0, 16)}...</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
