// Task list component — shows all tasks with status badges.
// Auto-refreshes every 3 seconds to show status changes for running tasks.

import { useEffect, useState, useCallback } from "react";
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

const REFRESH_INTERVAL = 3000; // 3 seconds

export function TaskList({ onSelect, selectedId, refreshKey }: TaskListProps) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const fetchTasks = useCallback(() => {
    api.listTasks()
      .then(setTasks)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  // Initial load + refresh on refreshKey change.
  useEffect(() => {
    setLoading(true);
    fetchTasks();
  }, [refreshKey, fetchTasks]);

  // Auto-refresh polling — picks up status changes from background tasks.
  useEffect(() => {
    const interval = setInterval(fetchTasks, REFRESH_INTERVAL);
    return () => clearInterval(interval);
  }, [fetchTasks]);

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
