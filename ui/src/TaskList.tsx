// Task list component — shows all tasks with status badges.
// Uses SSE (Server-Sent Events) for real-time status updates, with
// a fallback to periodic polling if SSE is not available.

import { useEffect, useState } from "react";
import { api, type TaskSummary } from "./api";

interface TaskListProps {
  onSelect: (taskId: string) => void;
  selectedId: string | null;
  refreshKey: number;
}

const STATUS_COLORS: Record<string, string> = {
  passed: "#22c55e",
  needs_review: "#eab308",
  failed: "#ef4444",
  approved: "#3b82f6",
  rejected: "#6b7280",
  created: "#8b5cf6",
  reviewing: "#f59e0b",
  executing: "#06b6d4",
};

const POLL_FALLBACK_INTERVAL = 5000; // 5 seconds (fallback if SSE fails)

export function TaskList({ onSelect, selectedId, refreshKey }: TaskListProps) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // React Compiler (v0.7.6) automatically memoizes this function — no
  // useCallback needed.
  const fetchTasks = () => {
    api.listTasks()
      .then(setTasks)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  // Initial load + refresh on refreshKey change.
  useEffect(() => {
    setLoading(true);
    fetchTasks();
  }, [refreshKey]);

  // SSE stream for real-time updates — falls back to polling on error.
  useEffect(() => {
    let es: EventSource | null = null;
    let pollInterval: ReturnType<typeof setInterval> | null = null;
    let sseFailed = false;

    try {
      es = api.streamTasks();
      es.onmessage = () => {
        // SSE message received — refresh the task list.
        fetchTasks();
      };
      es.onerror = () => {
        // SSE failed — fall back to polling.
        if (!sseFailed) {
          sseFailed = true;
          es?.close();
          es = null;
          pollInterval = setInterval(fetchTasks, POLL_FALLBACK_INTERVAL);
        }
      };
    } catch {
      // EventSource not supported — fall back to polling.
      pollInterval = setInterval(fetchTasks, POLL_FALLBACK_INTERVAL);
    }

    return () => {
      es?.close();
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [refreshKey]);

  if (loading && tasks.length === 0) return <div className="loading">Loading tasks...</div>;
  if (error) return <div className="error">{error}</div>;
  if (tasks.length === 0) return <div className="empty">No tasks found. Run one with the form above.</div>;

  return (
    <div className="task-list">
      {tasks.map((task) => (
        <div
          key={task.task_id}
          className={`task-card ${selectedId === task.task_id ? "selected" : ""}`}
          onClick={() => onSelect(task.task_id)}
        >
          <div className="task-card-header">
            <span
              className="status-badge"
              style={{ background: STATUS_COLORS[task.status] || "#6b7280" }}
            >
              {task.status}
            </span>
            <span className="task-id">{task.task_id}</span>
          </div>
          <div className="task-request">{task.user_request}</div>
          <div className="task-meta">
            <span>{task.repo_name}</span>
            <span>{new Date(task.created_at).toLocaleString()}</span>
          </div>
        </div>
      ))}
    </div>
  );
}
