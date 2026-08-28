import type { TurnError, Usage } from "./chat";

interface RunEventBase {
  sequence: number;
  run_id: string;
  session_id?: string;
  step?: number;
}

export type RunEvent =
  | (RunEventBase & { type: "run.started"; summary?: string })
  | (RunEventBase & { type: "step.started" | "step.completed"; summary?: string })
  | (RunEventBase & { type: "assistant.progress"; summary: string })
  | (RunEventBase & {
      type: "tool.requested" | "tool.started" | "tool.completed";
      tool: string;
      metadata?: Record<string, unknown>;
    })
  | (RunEventBase & {
      type: "approval.required" | "approval.resolved";
      tool: string;
      summary?: string;
      metadata?: Record<string, unknown>;
    })
  | (RunEventBase & { type: "usage.reported"; usage: Usage })
  | (RunEventBase & { type: "run.completed"; answer: string; summary?: string })
  | (RunEventBase & { type: "run.incomplete"; answer?: string; summary?: string })
  | (RunEventBase & { type: "run.failed"; error: TurnError })
  | (RunEventBase & { type: "run.cancelled"; summary?: string });

export type RunStreamer = (
  task: string,
  onEvent: (event: RunEvent) => void,
  signal: AbortSignal,
  maxSteps?: number,
  sessionId?: string,
) => Promise<void>;

export type ApprovalResolver = (
  runId: string,
  approvalId: string,
  decision: "approve" | "reject",
) => Promise<void>;

export async function resolveRunApproval(
  runId: string,
  approvalId: string,
  decision: "approve" | "reject",
): Promise<void> {
  const response = await fetch(
    `/api/v1/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    },
  );
  if (!response.ok) throw new Error(await readApiError(response));
}

export async function streamRun(
  task: string,
  onEvent: (event: RunEvent) => void,
  signal: AbortSignal,
  maxSteps = 24,
  sessionId?: string,
): Promise<void> {
  const response = await fetch("/api/v1/runs", {
    method: "POST",
    headers: { Accept: "application/x-ndjson", "Content-Type": "application/json" },
    body: JSON.stringify({ task, max_steps: maxSteps, ...(sessionId ? { session_id: sessionId } : {}) }),
    signal,
  });
  if (!response.ok) throw new Error(await readApiError(response));
  if (!response.body) throw new Error("本地服务没有返回可读取的数据流");

  let terminalEventSeen = false;
  for await (const event of parseRunStream(response.body)) {
    onEvent(event);
    if (["run.completed", "run.incomplete", "run.failed", "run.cancelled"].includes(event.type)) {
      terminalEventSeen = true;
    }
  }
  if (!terminalEventSeen) throw new Error("Agent 事件流意外中断");
}

export async function* parseRunStream(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<RunEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) if (line.trim()) yield parseRunEvent(line);
      if (done) break;
    }
    if (buffer.trim()) yield parseRunEvent(buffer);
  } finally {
    reader.releaseLock();
  }
}

function parseRunEvent(line: string): RunEvent {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    throw new Error("本地服务返回了无效的 Agent 事件流");
  }
  if (!isRunEvent(value)) throw new Error("本地服务返回了未知的 Agent 事件");
  return value;
}

function isRunEvent(value: unknown): value is RunEvent {
  if (!isRecord(value)) return false;
  if (
    typeof value.type !== "string" ||
    typeof value.sequence !== "number" ||
    typeof value.run_id !== "string"
  ) {
    return false;
  }
  if (value.type === "assistant.progress") return typeof value.summary === "string";
  if (["tool.requested", "tool.started", "tool.completed", "approval.required", "approval.resolved"].includes(value.type)) {
    return typeof value.tool === "string";
  }
  if (value.type === "usage.reported") return isRecord(value.usage);
  if (value.type === "run.completed") return typeof value.answer === "string";
  if (value.type === "run.failed") {
    return (
      isRecord(value.error) &&
      typeof value.error.code === "string" &&
      typeof value.error.message === "string"
    );
  }
  return [
    "run.started",
    "step.started",
    "step.completed",
    "run.incomplete",
    "run.cancelled",
  ].includes(value.type);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

async function readApiError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { error?: { message?: string } };
    return payload.error?.message || `请求失败 (${response.status})`;
  } catch {
    return `请求失败 (${response.status})`;
  }
}
