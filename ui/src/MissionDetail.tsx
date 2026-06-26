import { useState, useEffect } from "react";
import { api, type MissionDetail } from "./api";

interface Props {
  missionId: string;
}

export function MissionDetail({ missionId }: Props) {
  const [mission, setMission] = useState<MissionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        setLoading(true);
        const data = await api.getMission(missionId);
        if (!cancelled) {
          setMission(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load mission");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, [missionId]);

  if (loading) return <div className="loading">Loading mission...</div>;
  if (error) return <div className="error">{error}</div>;
  if (!mission) return <div className="empty">Mission not found</div>;

  return (
    <div className="mission-detail">
      <h2>{mission.goal}</h2>
      <div className="mission-header">
        <span className={`status-badge status-${mission.status}`}>{mission.status}</span>
        <span className="mission-repo">Repo: {mission.repo_name}</span>
        <span className="mission-created">Created: {mission.created_at}</span>
      </div>

      {mission.description && (
        <p className="mission-description">{mission.description}</p>
      )}

      <h3>Steps</h3>
      <div className="mission-steps-list">
        {mission.steps.length === 0 ? (
          <div className="empty">No steps defined yet.</div>
        ) : (
          mission.steps.map((step, idx) => (
            <div key={idx} className={`mission-step step-${step.status}`}>
              <div className="step-number">Step {idx + 1}</div>
              <div className="step-description">{step.description}</div>
              <div className="step-meta">
                <span className={`status-badge status-${step.status}`}>{step.status}</span>
                {step.task_id && <span className="step-task">{step.task_id}</span>}
              </div>
            </div>
          ))
        )}
      </div>

      {mission.completed_at && (
        <div className="mission-completed">Completed: {mission.completed_at}</div>
      )}
    </div>
  );
}
