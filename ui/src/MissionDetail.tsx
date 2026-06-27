// Mission detail view with step management and mission lifecycle controls.
// v0.7.5: Added step creation, mission complete/abort actions.
// v0.8.0: Added Run/Pause/Resume buttons, SSE event listeners, artifact chain viz.

import { useState, useEffect, useRef } from "react";
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
  const [runStatus, setRunStatus] = useState<string>("");
  const [sseEvents, setSseEvents] = useState<string[]>([]);
  const sseRef = useRef<EventSource | null>(null);

  // Load mission data
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

  // v0.8.0: SSE listener for mission events (step_started, step_completed, step_failed)
  useEffect(() => {
    if (!mission || (mission.status !== "running" && mission.status !== "in_progress")) {
      if (sseRef.current) {
        sseRef.current.close();
        sseRef.current = null;
      }
      return;
    }

    const es = api.streamMissionEvents(missionId);
    sseRef.current = es;

    es.addEventListener("mission.step_started", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setSseEvents((prev) => [...prev, `▶ Step ${data.step_index + 1} started: ${data.step_prompt?.slice(0, 60)}`]);
      // Reload mission to get updated step statuses
      api.getMission(missionId).then(setMission).catch(() => {});
    });

    es.addEventListener("mission.step_completed", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setSseEvents((prev) => [...prev, `✓ Step ${data.step_index + 1} completed (task: ${data.task_id})`]);
      api.getMission(missionId).then(setMission).catch(() => {});
    });

    es.addEventListener("mission.step_failed", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setSseEvents((prev) => [...prev, `✗ Step ${data.step_index + 1} failed: ${data.error}`]);
      api.getMission(missionId).then(setMission).catch(() => {});
    });

    es.onerror = () => {
      // SSE errors are expected when the mission finishes — just close.
    };

    return () => {
      es.close();
      sseRef.current = null;
    };
  }, [missionId, mission?.status]);

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

  // v0.8.0: Run/Pause/Resume handlers
  const handleRun = async () => {
    setActionLoading(true);
    setActionError("");
    setRunStatus("Running...");
    setSseEvents([]);
    try {
      const result = await api.runMission(missionId, ".repo.yaml");
      setRunStatus(
        `Complete: ${result.steps_run} run, ${result.steps_passed} passed, ${result.steps_failed} failed`,
      );
      const updated = await api.getMission(missionId);
      setMission(updated);
    } catch (e) {
      setActionError((e as Error).message);
      setRunStatus("Run failed");
    } finally {
      setActionLoading(false);
    }
  };

  const handlePause = async () => {
    setActionLoading(true);
    setActionError("");
    try {
      const updated = await api.pauseMission(missionId);
      setMission(updated);
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setActionLoading(false);
    }
  };

  const handleResume = async () => {
    setActionLoading(true);
    setActionError("");
    try {
      const updated = await api.resumeMission(missionId);
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

  const isActive = mission.status === "created" || mission.status === "in_progress" || mission.status === "running" || mission.status === "paused";
  const allStepsTerminal = mission.steps.every(
    (s) => s.status === "completed" || s.status === "failed"
  );
  const isRunning = mission.status === "running";
  const isPaused = mission.status === "paused";
  const canRun = mission.status === "created" || mission.status === "in_progress" || isPaused;

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

      {/* v0.8.0: Mission orchestration controls */}
      <div className="mission-orchestration">
        {canRun && (
          <button className="btn-run" onClick={handleRun} disabled={actionLoading}>
            {isPaused ? "Resume Run" : "Run Mission"}
          </button>
        )}
        {isRunning && (
          <button className="btn-pause" onClick={handlePause} disabled={actionLoading}>
            Pause
          </button>
        )}
        {isPaused && (
          <button className="btn-resume" onClick={handleResume} disabled={actionLoading}>
            Resume
          </button>
        )}
        {runStatus && <span className="run-status">{runStatus}</span>}
      </div>

      {/* v0.8.0: SSE event feed */}
      {sseEvents.length > 0 && (
        <div className="mission-sse-feed">
          <h4>Live Events</h4>
          <div className="sse-events">
            {sseEvents.map((evt, i) => (
              <div key={i} className="sse-event">{evt}</div>
            ))}
          </div>
        </div>
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
              {/* v0.8.0: Artifact chain visualization */}
              {idx > 0 && step.task_id && (
                <div className="artifact-chain-link">
                  <span className="chain-arrow">↑</span>
                  <span className="chain-label">chained from Step {idx}</span>
                </div>
              )}
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
