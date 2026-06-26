// Run task form — submit a new coding task asynchronously.

import { useState } from "react";
import { api } from "./api";

interface RunFormProps {
  onSubmitted: (taskId: string) => void;
}

export function RunForm({ onSubmitted }: RunFormProps) {
  const [task, setTask] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState("");
  const [error, setError] = useState("");

  const handleSubmit = async () => {
    if (!task.trim()) return;
    setLoading(true);
    setError("");
    setResult("");
    try {
      const resp = await api.runTaskAsync(task);
      setResult(`Task ${resp.task_id} started — polling for status...`);
      setTask("");
      onSubmitted(resp.task_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="run-form">
      <h3>Run New Task</h3>
      <div className="form-row">
        <input
          type="text"
          placeholder="Describe the coding task..."
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
          className="task-input"
          disabled={loading}
        />
        <button onClick={handleSubmit} disabled={loading || !task.trim()}>
          {loading ? "Starting..." : "Run"}
        </button>
      </div>
      {error && <div className="error">{error}</div>}
      {result && <div className="success">{result}</div>}
    </div>
  );
}
