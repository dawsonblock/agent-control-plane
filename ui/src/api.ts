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

export interface SkillSummary {
  name: string;
  purpose: string;
  rules: string[];
  has_hard_blocks: boolean;
  has_risk_elevators: boolean;
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

  listSkills: (skillsDir = "skills") =>
    fetchJSON<SkillSummary[]>(`/skills?skills_dir=${encodeURIComponent(skillsDir)}`),

  // SSE stream — returns an EventSource that emits task status changes.
  // Each event has a `data` field with a JSON object containing task_id,
  // status, repo_name, and user_request.
  streamTasks: (runsRoot = "data/runs"): EventSource =>
    new EventSource(`${API_BASE}/tasks/stream?runs_root=${encodeURIComponent(runsRoot)}`),
};
