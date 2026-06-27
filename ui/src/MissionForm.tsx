// Mission creation form — create a new mission epic with steps.
// v0.7.5: Enables creating missions from the dashboard UI.

import { useState } from "react";
import { api, type CreateMissionRequest } from "./api";

interface MissionFormProps {
  onCreated: (missionId: string) => void;
}

interface StepInput {
  description: string;
}

export function MissionForm({ onCreated }: MissionFormProps) {
  const [goal, setGoal] = useState("");
  const [repoName, setRepoName] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [description, setDescription] = useState("");
  const [baseBranch, setBaseBranch] = useState("main");
  const [steps, setSteps] = useState<StepInput[]>([{ description: "" }]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const addStep = () => {
    setSteps([...steps, { description: "" }]);
  };

  const removeStep = (index: number) => {
    setSteps(steps.filter((_, i) => i !== index));
  };

  const updateStep = (index: number, value: string) => {
    setSteps(steps.map((s, i) => (i === index ? { description: value } : s)));
  };

  const handleSubmit = async () => {
    if (!goal.trim() || !repoName.trim() || !repoPath.trim()) return;
    setLoading(true);
    setError("");
    setSuccess("");

    const req: CreateMissionRequest = {
      goal: goal.trim(),
      repo_name: repoName.trim(),
      repo_path: repoPath.trim(),
      base_branch: baseBranch.trim() || "main",
      description: description.trim(),
      steps: steps.filter((s) => s.description.trim()).map((s) => ({ description: s.description.trim() })),
    };

    try {
      const resp = await api.createMission(req);
      setSuccess(`Mission ${resp.mission_id} created with ${resp.steps_total} steps.`);
      setGoal("");
      setRepoName("");
      setRepoPath("");
      setDescription("");
      setBaseBranch("main");
      setSteps([{ description: "" }]);
      onCreated(resp.mission_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mission-form">
      <h3>Create New Mission</h3>

      <div className="form-group">
        <label>Goal *</label>
        <input
          type="text"
          placeholder="e.g., Migrate authentication to OAuth 2.0"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          disabled={loading}
        />
      </div>

      <div className="form-row">
        <div className="form-group">
          <label>Repo Name *</label>
          <input
            type="text"
            placeholder="e.g., my-project"
            value={repoName}
            onChange={(e) => setRepoName(e.target.value)}
            disabled={loading}
          />
        </div>
        <div className="form-group">
          <label>Repo Path *</label>
          <input
            type="text"
            placeholder="/path/to/repo"
            value={repoPath}
            onChange={(e) => setRepoPath(e.target.value)}
            disabled={loading}
          />
        </div>
      </div>

      <div className="form-row">
        <div className="form-group">
          <label>Base Branch</label>
          <input
            type="text"
            placeholder="main"
            value={baseBranch}
            onChange={(e) => setBaseBranch(e.target.value)}
            disabled={loading}
          />
        </div>
      </div>

      <div className="form-group">
        <label>Description</label>
        <textarea
          placeholder="Optional longer description of the mission..."
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          disabled={loading}
        />
      </div>

      <div className="form-group">
        <label>Steps</label>
        {steps.map((step, i) => (
          <div key={i} className="step-row">
            <span className="step-number">{i + 1}</span>
            <input
              type="text"
              placeholder={`Step ${i + 1} description...`}
              value={step.description}
              onChange={(e) => updateStep(i, e.target.value)}
              disabled={loading}
            />
            {steps.length > 1 && (
              <button className="step-remove" onClick={() => removeStep(i)} disabled={loading}>
                ×
              </button>
            )}
          </div>
        ))}
        <button className="step-add" onClick={addStep} disabled={loading}>
          + Add Step
        </button>
      </div>

      <div className="form-actions">
        <button
          onClick={handleSubmit}
          disabled={loading || !goal.trim() || !repoName.trim() || !repoPath.trim()}
        >
          {loading ? "Creating..." : "Create Mission"}
        </button>
      </div>

      {error && <div className="error">{error}</div>}
      {success && <div className="success">{success}</div>}
    </div>
  );
}
