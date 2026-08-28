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
                      <li key={tool.id}>
                        <span>{tool.sequence}</span>
                        <strong>{tool.label}</strong>
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
        </>
      )}
    </section>
  );
}

export function aggregateActions(trace: RunTraceItem[]): SemanticAction[] {
  const progress = new Map<number, RunTraceItem>();
  const tools = new Map<string, RunTraceItem[]>();
  let fallbackIndex = 0;
  for (const event of trace) {
    if (event.type === "assistant.progress") {
      progress.set(event.step ?? event.sequence, event);
      continue;
    }
    if (!event.type.startsWith("tool.") && !event.type.startsWith("approval.")) continue;
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
    const ok = terminal?.metadata?.ok;
    const status = terminal ? (ok === false ? "失败" : "完成") : approval ? "等待批准" : "执行中";
    toolActions.push({
      id,
      sequence: first.sequence,
      step: first.step,
      label: `${first.tool ?? "工具"} · ${status}`,
    });
  }
  const actions: SemanticAction[] = [];
  const groupedByStep = new Map<number, Array<SemanticTool & { step?: number }>>();
  for (const tool of toolActions) {
    if (tool.step === undefined) {
      actions.push({ id: tool.id, sequence: tool.sequence, label: tool.label, tools: [tool] });
    } else {
      groupedByStep.set(tool.step, [...(groupedByStep.get(tool.step) ?? []), tool]);
    }
  }
  for (const [step, stepTools] of groupedByStep) {
    const first = stepTools[0]!;
    actions.push({
      id: `step-${step}`,
      sequence: Math.min(first.sequence, progress.get(step)?.sequence ?? first.sequence),
      label: progress.get(step)?.summary || `第 ${step} 步`,
      tools: stepTools.sort((left, right) => left.sequence - right.sequence),
    });
  }
  for (const [step, event] of progress) {
    if (!groupedByStep.has(step)) {
      actions.push({
        id: `progress-${event.sequence}`,
        sequence: event.sequence,
        label: event.summary || "Agent 行动",
        tools: [],
      });
    }
  }
  return actions.sort((left, right) => left.sequence - right.sequence);
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

function numberValue(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
