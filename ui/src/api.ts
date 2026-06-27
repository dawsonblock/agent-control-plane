// ACP API client — typed wrappers for the FastAPI backend.

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

export interface TaskSummary {
  task_id: string;
  repo_name: string;
  status: string;
  user_request: string;
  created_at: string;
  updated_at: string;
}

export interface TaskDetail {
  task_id: string;
  repo_name: string;
  status: string;
  user_request: string;
  created_at: string;
  updated_at: string;
  base_branch: string;
  task_branch: string;
}

export interface EventItem {
  event_id: string;
  task_id: string;
  type: string;
  timestamp: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export interface RunResponse {
  task_id: string;
  status: string;
  report_path: string | null;
  vault_note_path: string | null;
  error: string | null;
}

export interface RunAsyncResponse {
  status: string;
  task_id: string;
  task: string;
}

export interface MemoryFact {
  fact: string;
  source_node: string;
  target_node: string;
  valid_at: string;
}

export interface MissionSummary {
  mission_id: string;
  goal: string;
  status: string;
  repo_name: string;
  steps_total: number;
  steps_completed: number;
  created_at: string;
}

export interface MissionStep {
  description: string;
  task_id: string;
  status: string;
}

export interface MissionDetail {
  mission_id: string;
  goal: string;
  description: string;
  repo_name: string;
  status: string;
  steps: MissionStep[];
  created_at: string;
  updated_at: string;
  completed_at: string;
}

export interface CreateMissionRequest {
  goal: string;
  repo_name: string;
  repo_path: string;
  base_branch?: string;
  description?: string;
  steps?: { description: string }[];
}

export interface CreateMissionResponse {
  mission_id: string;
  goal: string;
  status: string;
  repo_name: string;
  steps_total: number;
  created_at: string;
}

export interface SkillSummary {
  name: string;
  purpose: string;
  rules: string[];
  has_hard_blocks: boolean;
  has_risk_elevators: boolean;
}

// v0.9.0 (Step 6): machine-readable review (artifacts/review.json) for the
// diff viewer's risk-annotation overlay. Mirrors acp.models.ReviewResult.
export interface ReviewResult {
  risk: string; // "low" | "medium" | "high"
  recommendation: string; // "merge" | "revise" | "reject"
  changed_files: string[];
  concerns: string[];
  summary: string;
  hard_block: boolean;
}

async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${url}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(detail.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

export const api = {
  health: () => fetchJSON<{ status: string; version: string }>("/health"),

  listTasks: (runsRoot = "data/runs") =>
    fetchJSON<TaskSummary[]>(`/tasks?runs_root=${encodeURIComponent(runsRoot)}`),

  getTask: (taskId: string, runsRoot = "data/runs") =>
    fetchJSON<TaskDetail>(`/tasks/${taskId}?runs_root=${encodeURIComponent(runsRoot)}`),

  getEvents: (taskId: string, runsRoot = "data/runs") =>
    fetchJSON<EventItem[]>(`/tasks/${taskId}/events?runs_root=${encodeURIComponent(runsRoot)}`),

  getReport: (taskId: string, runsRoot = "data/runs") =>
    fetchJSON<{ task_id: string; report: string }>(`/tasks/${taskId}/report?runs_root=${encodeURIComponent(runsRoot)}`),

  // v0.9.0 (Step 6): diff patch + machine-readable review for the inline
  // diff viewer with risk annotations.
  getDiff: (taskId: string, runsRoot = "data/runs") =>
    fetchJSON<{ task_id: string; diff: string }>(`/tasks/${taskId}/diff?runs_root=${encodeURIComponent(runsRoot)}`),

  getReview: (taskId: string, runsRoot = "data/runs") =>
    fetchJSON<{ task_id: string; review: ReviewResult }>(
      `/tasks/${taskId}/review?runs_root=${encodeURIComponent(runsRoot)}`,
    ),

  approve: (taskId: string, approver = "", runsRoot = "data/runs", vaultRoot = "vault") =>
    fetchJSON<{ status: string }>(`/tasks/${taskId}/approve?runs_root=${encodeURIComponent(runsRoot)}&vault_root=${encodeURIComponent(vaultRoot)}`, {
      method: "POST",
      body: JSON.stringify({ approver }),
    }),

  reject: (taskId: string, rejecter = "", runsRoot = "data/runs", vaultRoot = "vault") =>
    fetchJSON<{ status: string }>(`/tasks/${taskId}/reject?runs_root=${encodeURIComponent(runsRoot)}&vault_root=${encodeURIComponent(vaultRoot)}`, {
      method: "POST",
      body: JSON.stringify({ rejecter }),
    }),

  runTask: (task: string, runsRoot = "data/runs", vaultRoot = "vault") =>
    fetchJSON<RunResponse>(`/tasks/run`, {
      method: "POST",
      body: JSON.stringify({ task, runs_root: runsRoot, vault_root: vaultRoot }),
    }),

  runTaskAsync: (task: string, runsRoot = "data/runs", vaultRoot = "vault") =>
    fetchJSON<RunAsyncResponse>(`/tasks/run/async`, {
      method: "POST",
      body: JSON.stringify({ task, runs_root: runsRoot, vault_root: vaultRoot }),
    }),

  searchMemory: (query: string, numResults = 10) =>
    fetchJSON<MemoryFact[]>(`/memory/search?query=${encodeURIComponent(query)}&num_results=${numResults}`),

  listMissions: (missionsRoot = "data/missions") =>
    fetchJSON<MissionSummary[]>(`/missions?missions_root=${encodeURIComponent(missionsRoot)}`),

  getMission: (missionId: string, missionsRoot = "data/missions") =>
    fetchJSON<MissionDetail>(`/missions/${missionId}?missions_root=${encodeURIComponent(missionsRoot)}`),

  createMission: (req: CreateMissionRequest, missionsRoot = "data/missions") =>
    fetchJSON<CreateMissionResponse>(`/missions?missions_root=${encodeURIComponent(missionsRoot)}`, {
      method: "POST",
      body: JSON.stringify(req),
    }),

  addMissionStep: (missionId: string, description: string, missionsRoot = "data/missions") =>
    fetchJSON<MissionDetail>(`/missions/${missionId}/steps?missions_root=${encodeURIComponent(missionsRoot)}`, {
      method: "POST",
      body: JSON.stringify({ description }),
    }),

  completeMission: (missionId: string, missionsRoot = "data/missions") =>
    fetchJSON<MissionDetail>(`/missions/${missionId}/complete?missions_root=${encodeURIComponent(missionsRoot)}`, {
      method: "POST",
    }),

  abortMission: (missionId: string, missionsRoot = "data/missions") =>
    fetchJSON<MissionDetail>(`/missions/${missionId}/abort?missions_root=${encodeURIComponent(missionsRoot)}`, {
      method: "POST",
    }),

  // M15: Run a single mission step by index.
  runMissionStep: (
    missionId: string,
    stepIndex: number,
    configPath = ".repo.yaml",
    missionsRoot = "data/missions",
    runsRoot = "data/runs",
    vaultRoot = "data/vault",
  ) =>
    fetchJSON<{ mission_id: string; step_index: number; task_id: string; status: string; error: string | null }>(
      `/missions/${missionId}/steps/${stepIndex}/run?missions_root=${encodeURIComponent(missionsRoot)}&config_path=${encodeURIComponent(configPath)}&runs_root=${encodeURIComponent(runsRoot)}&vault_root=${encodeURIComponent(vaultRoot)}`,
      { method: "POST" },
    ),

  // v0.8.0 (Phase 3.1): Mission orchestration — run, pause, resume.
  runMission: (missionId: string, configPath: string, missionsRoot = "data/missions") =>
    fetchJSON<{ steps_run: number; steps_passed: number; steps_failed: number; paused: boolean }>(
      `/missions/${missionId}/run?missions_root=${encodeURIComponent(missionsRoot)}&config_path=${encodeURIComponent(configPath)}`,
      { method: "POST" },
    ),

  pauseMission: (missionId: string, missionsRoot = "data/missions") =>
    fetchJSON<MissionDetail>(`/missions/${missionId}/pause?missions_root=${encodeURIComponent(missionsRoot)}`, {
      method: "POST",
    }),

  resumeMission: (missionId: string, missionsRoot = "data/missions") =>
    fetchJSON<MissionDetail>(`/missions/${missionId}/resume?missions_root=${encodeURIComponent(missionsRoot)}`, {
      method: "POST",
    }),

  listSkills: (skillsDir = "skills") =>
    fetchJSON<SkillSummary[]>(`/skills?skills_dir=${encodeURIComponent(skillsDir)}`),

  // SSE stream — returns an EventSource that emits task status changes.
  // Each event has a `data` field with a JSON object containing task_id,
  // status, repo_name, and user_request.
  streamTasks: (runsRoot = "data/runs"): EventSource =>
    new EventSource(`${API_BASE}/tasks/stream?runs_root=${encodeURIComponent(runsRoot)}`),

  // v0.8.0 (Phase 3.2): SSE stream for mission events (step_started,
  // step_completed, step_failed). Returns an EventSource.
  streamMissionEvents: (missionId: string, missionsRoot = "data/missions"): EventSource =>
    new EventSource(
      `${API_BASE}/missions/${missionId}/stream?missions_root=${encodeURIComponent(missionsRoot)}`,
    ),
};
