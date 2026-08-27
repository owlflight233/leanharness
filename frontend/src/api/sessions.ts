export type PermissionMode = "inspect" | "approve" | "unrestricted";

export interface SessionSummary {
  id: string;
  project_id: string;
  title: string;
  permission_mode: PermissionMode;
  created_at: string;
  updated_at: string;
  last_run_state: string | null;
}

export interface SessionDetail {
  session: SessionSummary;
  messages: Array<{
    id: string;
    sequence: number;
    role: "user" | "assistant" | "progress";
    content: string;
    status: string;
    created_at: string;
  }>;
  runs: Array<{
    id: string;
    session_id: string;
    mode: "chat" | "inspect";
    task: string;
    state: string;
    max_steps: number;
    answer: string | null;
    error_code: string | null;
    started_at: string;
    finished_at: string | null;
    trace?: Array<{
      sequence: number;
      type: string;
      step?: number;
      tool?: string;
      summary?: string;
      error?: { code: string; message: string };
    }>;
  }>;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { Accept: "application/json", "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = (await response.json()) as { error?: { message?: string } };
      message = payload.error?.message || message;
    } catch { /* keep status message */ }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

export async function fetchSessions(signal?: AbortSignal): Promise<SessionSummary[]> {
  const response = await fetch("/api/v1/sessions", { headers: { Accept: "application/json" }, signal });
  if (!response.ok) throw new Error(`会话加载失败 (${response.status})`);
  return ((await response.json()) as { sessions: SessionSummary[] }).sessions;
}

export function fetchSession(id: string, signal?: AbortSignal): Promise<SessionDetail> {
  return request<SessionDetail>(`/api/v1/sessions/${encodeURIComponent(id)}`, { signal });
}

export function createSession(permission_mode: PermissionMode = "inspect"): Promise<SessionSummary> {
  return request<SessionSummary>("/api/v1/sessions", { method: "POST", body: JSON.stringify({ permission_mode }) });
}

export function updateSession(id: string, patch: { title?: string; permission_mode?: PermissionMode }): Promise<SessionSummary> {
  return request<SessionSummary>(`/api/v1/sessions/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(patch) });
}

export function deleteSession(id: string): Promise<{ deleted: boolean; session_id: string }> {
  return request(`/api/v1/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
}
