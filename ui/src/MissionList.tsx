import { useState, useEffect } from "react";
import { api, type MissionSummary } from "./api";

interface Props {
  onSelect: (missionId: string) => void;
  selectedId: string | null;
  refreshKey: number;
}

export function MissionList({ onSelect, selectedId, refreshKey }: Props) {
  const [missions, setMissions] = useState<MissionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // React Compiler (v0.7.6) automatically memoizes this function — no
  // useCallback needed.
  const load = async () => {
    try {
      setLoading(true);
      const data = await api.listMissions();
      setMissions(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load missions");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [refreshKey]);

  if (loading && missions.length === 0) {
    return <div className="loading">Loading missions...</div>;
  }

  if (error) {
    return <div className="error">{error}</div>;
  }

  if (missions.length === 0) {
    return <div className="empty">No missions yet. Create one with `acp mission create`.</div>;
  }

  return (
    <div className="mission-list">
      <h3>Missions</h3>
      {missions.map((m) => (
        <div
          key={m.mission_id}
          className={`mission-item ${selectedId === m.mission_id ? "selected" : ""}`}
          onClick={() => onSelect(m.mission_id)}
        >
          <div className="mission-goal">{m.goal}</div>
          <div className="mission-meta">
            <span className={`status-badge status-${m.status}`}>{m.status}</span>
            <span className="mission-steps">
              {m.steps_completed}/{m.steps_total} steps
            </span>
            <span className="mission-repo">{m.repo_name}</span>
          </div>
          <div className="mission-id">{m.mission_id}</div>
        </div>
      ))}
    </div>
  );
}
