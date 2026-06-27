// Mission detail view with step management and mission lifecycle controls.
// v0.7.5: Added step creation, mission complete/abort actions.

import { useState, useEffect } from "react";
import { api, type MissionDetail } from "./api";

interface Props {
  missionId: string;
}

export function MissionDetail({ missionId }: Props) {
  const [mission, setMission] = useState<MissionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newStepDesc, setNewStepDesc] = useState("");
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState("");

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

  const handleAddStep = async () => {
    if (!newStepDesc.trim()) return;
    setActionLoading(true);
    setActionError("");
    try {
      const updated = await api.addMissionStep(missionId, newStepDesc.trim());
      setMission(updated);
      setNewStepDesc("");
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setActionLoading(false);
    }
  };

  const handleComplete = async () => {
    setActionLoading(true);
    setActionError("");
    try {
      const updated = await api.completeMission(missionId);
      setMission(updated);
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setActionLoading(false);
    }
  };

  const handleAbort = async () => {
    if (!confirm("Abort this mission? Pending/running steps will be marked as failed.")) return;
    setActionLoading(true);
    setActionError("");
    try {
      const updated = await api.abortMission(missionId);
      setMission(updated);
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setActionLoading(false);
    }
  };

  if (loading) return <div className="loading">Loading mission...</div>;
  if (error) return <div className="error">{error}</div>;
  if (!mission) return <div className="empty">Mission not found</div>;

  const isActive = mission.status === "created" || mission.status === "in_progress";
  const allStepsTerminal = mission.steps.every(
    (s) => s.status === "completed" || s.status === "failed"
  );

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

      <h3>Steps ({mission.steps.length})</h3>
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

      {isActive && (
        <div className="mission-step-add">
          <h4>Add Step</h4>
          <div className="form-row">
            <input
              type="text"
              placeholder="Step description..."
              value={newStepDesc}
              onChange={(e) => setNewStepDesc(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleAddStep()}
              disabled={actionLoading}
            />
            <button onClick={handleAddStep} disabled={actionLoading || !newStepDesc.trim()}>
              {actionLoading ? "Adding..." : "Add Step"}
            </button>
          </div>
        </div>
      )}

      {isActive && (
        <div className="mission-actions">
          <button
            className="btn-complete"
            onClick={handleComplete}
            disabled={actionLoading || !allStepsTerminal}
            title={allStepsTerminal ? "" : "All steps must be completed or failed first"}
          >
            Complete Mission
          </button>
          <button
            className="btn-abort"
            onClick={handleAbort}
            disabled={actionLoading}
          >
            Abort Mission
          </button>
        </div>
      )}

      {actionError && <div className="error">{actionError}</div>}

      {mission.completed_at && (
        <div className="mission-completed">
          {mission.status === "aborted" ? "Aborted" : "Completed"}: {mission.completed_at}
        </div>
      )}
    </div>
  );
}
