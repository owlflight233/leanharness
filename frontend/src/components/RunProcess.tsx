import { Activity } from "lucide-react";

export interface RunTraceItem {
  type: string;
  sequence: number;
  run_id?: string;
  step?: number;
  tool?: string;
  summary?: string;
  metadata?: Record<string, unknown>;
}

interface SemanticAction {
  id: string;
  sequence: number;
  label: string;
  tools: SemanticTool[];
}

interface SemanticTool {
  id: string;
  sequence: number;
  label: string;
  groupKey: string;
  planStep?: number;
  kind?: "tool" | "subtask";
  detail?: string;
}

export function RunProcess({
  trace,
  open,
  onToggle,
  running,
}: {
  trace: RunTraceItem[];
  open: boolean;
  onToggle: () => void;
  running: boolean;
}) {
  const actions = aggregateActions(trace);
  if (!actions.length) return null;
  const metrics = terminalMetrics(trace);
  const permission = runtimePermission(trace);
  return (
    <section className={`run-process ${open ? "is-open" : "is-closed"}`}>
      <button type="button" className="run-process-header" onClick={onToggle} aria-expanded={open}>
        <Activity size={15} />
        <span>{running ? "执行过程" : "执行过程已结束"}</span>
        <span className="run-process-count">
          {actions.length} 个步骤 · {actions.reduce((count, action) => count + action.tools.length, 0)} 个工具
        </span>
        <span className="run-process-chevron" aria-hidden="true">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <>
          <ol className="run-process-list">
            {actions.map((action) => (
              <li key={action.id} className="run-process-step">
                <div className="run-process-step-heading">
                  <span>{action.sequence}</span>
                  <strong>{action.label}</strong>
                </div>
                {action.tools.length > 0 && (
                  <ol className="run-process-tools">
                    {action.tools.map((tool) => (
                      <li key={tool.id} className={tool.kind === "subtask" ? "run-process-subtask" : undefined}>
                        <span>{tool.sequence}</span>
                        <div>
                          <strong>{tool.label}</strong>
                          {tool.detail && <small>{tool.detail}</small>}
                        </div>
                      </li>
                    ))}
                  </ol>
                )}
              </li>
            ))}
          </ol>
          {metrics && (
            <div className="run-process-metrics">
              {metrics.modelCalls} 次模型 · {metrics.toolCalls} 次工具 · {metrics.totalTokens} tokens
            </div>
          )}
          {permission && (
            <div className="run-process-metrics">本次运行权限：{permissionLabel(permission)}</div>
          )}
        </>
      )}
    </section>
  );
}

export function aggregateActions(trace: RunTraceItem[]): SemanticAction[] {
  const progress = new Map<string, RunTraceItem>();
  const tools = new Map<string, RunTraceItem[]>();
  const subtasks = new Map<string, RunTraceItem[]>();
  let fallbackIndex = 0;
  for (const event of trace) {
    if (event.type === "assistant.progress") {
      progress.set(groupKeyFor(event), event);
      continue;
    }
    if (event.type.startsWith("subtask.")) {
      const subtaskId = event.metadata?.subtask_id;
      if (typeof subtaskId === "string") {
        subtasks.set(subtaskId, [...(subtasks.get(subtaskId) ?? []), event]);
      }
      continue;
    }
    if (!event.type.startsWith("tool.") && !event.type.startsWith("approval.") && !event.type.startsWith("input.")) continue;
    const callId = event.metadata?.tool_call_id;
    let key = typeof callId === "string" ? callId : "";
    if (!key && event.type === "tool.requested") key = `legacy-${fallbackIndex++}`;
    if (!key) key = [...tools.keys()].at(-1) ?? `legacy-${fallbackIndex++}`;
    tools.set(key, [...(tools.get(key) ?? []), event]);
  }

  const toolActions: Array<SemanticTool & { step?: number }> = [];
  for (const [id, events] of tools) {
    const first = events[0]!;
    const terminal = [...events].reverse().find((event) => event.type === "tool.completed");
    const approval = [...events].reverse().find((event) => event.type === "approval.required");
    const input = [...events].reverse().find((event) => event.type === "input.required");
    const ok = terminal?.metadata?.ok;
    const status = terminal
      ? (ok === false ? "失败" : "完成")
      : approval
        ? "等待批准"
        : input
          ? "等待回答"
          : "执行中";
    toolActions.push({
      id,
      sequence: first.sequence,
      step: first.step,
      label: `${first.tool ?? "工具"} · ${status}`,
      groupKey: groupKeyFor(first),
      planStep: planStepFor(first),
      kind: "tool",
    });
  }
  for (const [id, events] of subtasks) {
    const first = events[0]!;
    const terminal = [...events].reverse().find((event) =>
      ["subtask.completed", "subtask.failed", "subtask.cancelled"].includes(event.type)
    );
    const state = terminal?.metadata?.status;
    const status = terminal?.type === "subtask.completed"
      ? "完成"
      : terminal?.type === "subtask.cancelled"
        ? "已取消"
        : terminal?.type === "subtask.failed" && state === "incomplete"
          ? "未完成"
          : terminal
            ? "失败"
            : "分析中";
    toolActions.push({
      id: `subtask-${id}`,
      sequence: first.sequence,
      step: first.step,
      label: `子任务 · ${first.summary || "并行分析"} · ${status}`,
      detail: subtaskDetail(terminal),
      groupKey: groupKeyFor(first),
      planStep: planStepFor(first),
      kind: "subtask",
    });
  }
  const actions: SemanticAction[] = [];
  const groupedByStep = new Map<string, Array<SemanticTool & { step?: number }>>();
  for (const tool of toolActions) {
    if (tool.step === undefined) {
      actions.push({ id: tool.id, sequence: tool.sequence, label: tool.label, tools: [tool] });
    } else {
      groupedByStep.set(tool.groupKey, [...(groupedByStep.get(tool.groupKey) ?? []), tool]);
    }
  }
  for (const [groupKey, stepTools] of groupedByStep) {
    const first = stepTools[0]!;
    const progressEvent = progress.get(groupKey);
    const planStep = first.planStep;
    const baseLabel = progressEvent?.summary || `第 ${first.step} 步`;
    actions.push({
      id: `step-${groupKey}`,
      sequence: Math.min(first.sequence, progressEvent?.sequence ?? first.sequence),
      label: planStep === undefined ? baseLabel : `计划第 ${planStep} 步 · ${baseLabel}`,
      tools: stepTools.sort((left, right) => left.sequence - right.sequence),
    });
  }
  for (const [groupKey, event] of progress) {
    if (!groupedByStep.has(groupKey)) {
      actions.push({
        id: `progress-${event.sequence}`,
        sequence: event.sequence,
        label: planStepFor(event) === undefined
          ? event.summary || "Agent 行动"
          : `计划第 ${planStepFor(event)} 步 · ${event.summary || "Agent 行动"}`,
        tools: [],
      });
    }
  }
  return actions.sort((left, right) => left.sequence - right.sequence);
}

function groupKeyFor(event: RunTraceItem): string {
  const planStep = planStepFor(event);
  return planStep === undefined
    ? `run:${event.step ?? event.sequence}`
    : `plan:${planStep}:${event.step ?? event.sequence}`;
}

function planStepFor(event: RunTraceItem): number | undefined {
  const value = event.metadata?.plan_step;
  return typeof value === "number" ? value : undefined;
}

function subtaskDetail(event: RunTraceItem | undefined): string | undefined {
  if (!event) return undefined;
  const parts: string[] = [];
  if (event.summary) parts.push(event.summary);
  const scope = event.metadata?.scope;
  if (Array.isArray(scope) && scope.every((item) => typeof item === "string")) {
    parts.push(`范围：${scope.join("、")}`);
  }
  const usage = event.metadata?.usage;
  if (isRecord(usage)) {
    const total = numberValue(usage.input_tokens) + numberValue(usage.output_tokens);
    parts.push(`${total} tokens`);
  }
  const duration = event.metadata?.duration_ms;
  if (typeof duration === "number") parts.push(`${duration} ms`);
  return parts.join(" · ") || undefined;
}

function terminalMetrics(trace: RunTraceItem[]) {
  for (const event of [...trace].reverse()) {
    const metrics = event.metadata?.metrics;
    if (!isRecord(metrics)) continue;
    return {
      modelCalls: numberValue(metrics.model_calls),
      toolCalls: numberValue(metrics.tool_calls),
      totalTokens: numberValue(metrics.total_tokens),
    };
  }
  return null;
}

function runtimePermission(trace: RunTraceItem[]): string | null {
  const started = [...trace].reverse().find((event) =>
    ["run.started", "run.permission.updated"].includes(event.type)
  );
  const permission = started?.metadata?.permission_mode;
  return typeof permission === "string" ? permission : null;
}

function permissionLabel(permission: string): string {
  if (permission === "inspect") return "只读检查";
  if (permission === "approve") return "逐次批准";
  return permission === "unrestricted" ? "受控直接执行" : permission;
}

function numberValue(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
