export interface PlanStep {
  id: string;
  sequence: number;
  title: string;
  instruction: string;
  enabled: boolean;
  state: string;
  evidence?: Record<string, unknown> | null;
}

export interface Plan {
  id: string;
  session_id: string;
  title: string;
  task: string;
  state: string;
  version: number;
  source_markdown: string;
  run_id?: string | null;
  execution_permission_mode?: "inspect" | "approve" | "unrestricted" | null;
  steps: PlanStep[];
  generation_trace?: Array<Record<string, unknown>>;
}

export async function createPlan(
  task: string,
  sessionId?: string,
  attachmentIds: string[] = [],
): Promise<Plan> {
  const response = await fetch("/api/v1/plans", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      task,
      ...(sessionId ? { session_id: sessionId } : {}),
      ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}),
    }),
  });
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()) as Plan;
}

export async function streamPlanCreation(
  task: string,
  onEvent: (event: Record<string, unknown>) => void,
  signal: AbortSignal,
  sessionId?: string,
  attachmentIds: string[] = [],
): Promise<void> {
  const response = await fetch("/api/v1/plans/stream", {
    method: "POST",
    headers: { Accept: "application/x-ndjson", "Content-Type": "application/json" },
    body: JSON.stringify({
      task,
      ...(sessionId ? { session_id: sessionId } : {}),
      ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}),
    }),
    signal,
  });
  if (!response.ok) throw new Error(await readError(response));
  if (!response.body) throw new Error("计划生成没有返回事件流");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) if (line.trim()) onEvent(JSON.parse(line) as Record<string, unknown>);
      if (done) break;
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer) as Record<string, unknown>);
  } finally {
    reader.releaseLock();
  }
}

export async function fetchPlan(id: string): Promise<Plan> {
  const response = await fetch(`/api/v1/plans/${encodeURIComponent(id)}`);
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()) as Plan;
}

export async function updatePlan(
  plan: Plan,
  title: string,
  steps: PlanStep[],
): Promise<Plan> {
  const response = await fetch(`/api/v1/plans/${encodeURIComponent(plan.id)}`, {
    method: "PATCH",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      title,
      version: plan.version,
      steps: steps.map(({ id, title: stepTitle, instruction, enabled }) => ({
        id,
        title: stepTitle,
        instruction,
        enabled,
      })),
    }),
  });
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()) as Plan;
}

export async function* streamPlanAction(
  id: string,
  action: "confirm" | "resume",
  signal: AbortSignal,
  pluginIds: string[] = [],
): AsyncGenerator<Record<string, unknown>> {
  const response = await fetch(`/api/v1/plans/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    headers: { Accept: "application/x-ndjson", "Content-Type": "application/json" },
    body: JSON.stringify({ plugin_ids: pluginIds }),
    signal,
  });
  if (!response.ok) throw new Error(await readError(response));
  if (!response.body) throw new Error("计划运行没有返回事件流");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) if (line.trim()) yield JSON.parse(line) as Record<string, unknown>;
      if (done) break;
    }
    if (buffer.trim()) yield JSON.parse(buffer) as Record<string, unknown>;
  } finally {
    reader.releaseLock();
  }
}

export async function rejectPlan(id: string): Promise<Plan> {
  const response = await fetch(`/api/v1/plans/${encodeURIComponent(id)}/reject`, {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()) as Plan;
}

async function readError(response: Response): Promise<string> {
  try {
    const value = (await response.json()) as { error?: { message?: string } };
    return value.error?.message || `请求失败 (${response.status})`;
  } catch {
    return `请求失败 (${response.status})`;
  }
}
