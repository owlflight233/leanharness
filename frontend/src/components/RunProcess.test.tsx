import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { aggregateActions, RunProcess } from "./RunProcess";

const lifecycle = [
  { type: "assistant.progress", sequence: 1, step: 1, summary: "读取项目文件" },
  {
    type: "tool.requested",
    sequence: 2,
    step: 1,
    tool: "workspace_read",
    metadata: { tool_call_id: "call-1" },
  },
  {
    type: "tool.started",
    sequence: 3,
    step: 1,
    tool: "workspace_read",
    metadata: { tool_call_id: "call-1" },
  },
  {
    type: "tool.completed",
    sequence: 4,
    step: 1,
    tool: "workspace_read",
    metadata: { tool_call_id: "call-1", ok: true },
  },
] as const;

describe("RunProcess", () => {
  it("groups one tool lifecycle into one semantic action", () => {
    expect(aggregateActions([...lifecycle])).toHaveLength(1);
  });

  it("shows terminal efficiency metrics inside the expanded process", async () => {
    const onToggle = vi.fn();
    render(
      <RunProcess
        trace={[
          ...lifecycle,
          {
            type: "run.completed",
            sequence: 5,
            metadata: {
              metrics: { model_calls: 2, tool_calls: 1, total_tokens: 320 },
            },
          },
        ]}
        open
        onToggle={onToggle}
        running={false}
      />,
    );

    expect(screen.getByText("1 个动作")).toBeInTheDocument();
    expect(screen.getByText("2 次模型 · 1 次工具 · 320 tokens")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /执行过程已结束/ }));
    expect(onToggle).toHaveBeenCalledOnce();
  });
});

