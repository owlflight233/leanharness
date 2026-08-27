export interface Usage {
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
}

export interface TurnError {
  code: string;
  message: string;
}

export type TurnEvent =
  | { type: "turn.started"; sequence: number; session_id?: string; run_id?: string }
  | { type: "content.delta"; sequence: number; content: string; session_id?: string; run_id?: string }
  | { type: "usage.reported"; sequence: number; usage: Usage; session_id?: string; run_id?: string }
  | { type: "turn.completed"; sequence: number; finish_reason?: string; session_id?: string; run_id?: string }
  | { type: "turn.failed"; sequence: number; error: TurnError; session_id?: string; run_id?: string };

export type ChatStreamer = (
  message: string,
  onEvent: (event: TurnEvent) => void,
  signal: AbortSignal,
  sessionId?: string,
) => Promise<void>;

export async function streamChat(
  message: string,
  onEvent: (event: TurnEvent) => void,
  signal: AbortSignal,
  sessionId?: string,
): Promise<void> {
  const response = await fetch("/api/v1/chat", {
    method: "POST",
    headers: { Accept: "application/x-ndjson", "Content-Type": "application/json" },
    body: JSON.stringify({ message, ...(sessionId ? { session_id: sessionId } : {}) }),
    signal,
  });
  if (!response.ok) {
    throw new Error(await readApiError(response));
  }
  if (!response.body) {
    throw new Error("本地服务没有返回可读取的数据流");
  }

  let terminalEventSeen = false;
  for await (const event of parseNDJSONStream(response.body)) {
    onEvent(event);
    if (event.type === "turn.completed" || event.type === "turn.failed") {
      terminalEventSeen = true;
    }
  }
  if (!terminalEventSeen) throw new Error("模型事件流意外中断");
}

export async function* parseNDJSONStream(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<TurnEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (line.trim()) yield parseEvent(line);
      }
      if (done) break;
    }
    if (buffer.trim()) yield parseEvent(buffer);
  } finally {
    reader.releaseLock();
  }
}

function parseEvent(line: string): TurnEvent {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    throw new Error("本地服务返回了无效的事件流");
  }
  if (!isTurnEvent(value)) {
    throw new Error("本地服务返回了未知事件");
  }
  return value;
}

function isTurnEvent(value: unknown): value is TurnEvent {
  if (!isRecord(value)) return false;
  const event = value;
  if (typeof event.sequence !== "number" || typeof event.type !== "string") return false;
  if (event.type === "turn.started") return true;
  if (event.type === "turn.completed") {
    return event.finish_reason === undefined || typeof event.finish_reason === "string";
  }
  if (event.type === "content.delta") return typeof event.content === "string";
  if (event.type === "usage.reported") {
    if (!isRecord(event.usage)) return false;
    const usage = event.usage;
    return ["prompt_tokens", "completion_tokens", "total_tokens"].every((key) => {
      const count = usage[key];
      return count === null || (typeof count === "number" && Number.isInteger(count) && count >= 0);
    });
  }
  if (event.type === "turn.failed") {
    return (
      isRecord(event.error) &&
      typeof event.error.code === "string" &&
      typeof event.error.message === "string"
    );
  }
  return false;
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
