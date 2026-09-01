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
          {
            type: "run.started",
            sequence: 0,
            metadata: { permission_mode: "unrestricted" },
          },
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

    expect(screen.getByText("1 个步骤 · 1 个工具")).toBeInTheDocument();
    expect(screen.getByText("2 次模型 · 1 次工具 · 320 tokens")).toBeInTheDocument();
    expect(screen.getByText("本次运行权限：受控直接执行")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /执行过程已结束/ }));
    expect(onToggle).toHaveBeenCalledOnce();
  });

  it("shows one step with multiple tools instead of repeating the action", () => {
    const actions = aggregateActions([
      { type: "assistant.progress", sequence: 1, step: 1, summary: "检查工作区" },
      { type: "tool.requested", sequence: 2, step: 1, tool: "git_inspect", metadata: { tool_call_id: "a" } },
      { type: "tool.completed", sequence: 3, step: 1, tool: "git_inspect", metadata: { tool_call_id: "a", ok: true } },
      { type: "tool.requested", sequence: 4, step: 1, tool: "workspace_list", metadata: { tool_call_id: "b" } },
      { type: "tool.completed", sequence: 5, step: 1, tool: "workspace_list", metadata: { tool_call_id: "b", ok: true } },
    ]);

    expect(actions).toHaveLength(1);
    expect(actions[0]?.label).toBe("检查工作区");
    expect(actions[0]?.tools).toHaveLength(2);
  });

  it("keeps runtime steps separate when a plan restarts the inner step counter", () => {
    const actions = aggregateActions([
      { type: "assistant.progress", sequence: 1, step: 1, summary: "读取计划文件", metadata: { plan_step: 1 } },
      { type: "tool.requested", sequence: 2, step: 1, tool: "workspace_read", metadata: { plan_step: 1, tool_call_id: "a" } },
      { type: "tool.completed", sequence: 3, step: 1, tool: "workspace_read", metadata: { plan_step: 1, tool_call_id: "a", ok: true } },
      { type: "assistant.progress", sequence: 4, step: 1, summary: "读取测试文件", metadata: { plan_step: 2 } },
      { type: "tool.requested", sequence: 5, step: 1, tool: "workspace_read", metadata: { plan_step: 2, tool_call_id: "b" } },
      { type: "tool.completed", sequence: 6, step: 1, tool: "workspace_read", metadata: { plan_step: 2, tool_call_id: "b", ok: true } },
    ]);

    expect(actions).toHaveLength(2);
    expect(actions.map((action) => action.label)).toEqual([
      "计划第 1 步 · 读取计划文件",
      "计划第 2 步 · 读取测试文件",
    ]);
  });

  it("renders delegated workers inside the same execution process", () => {
    const actions = aggregateActions([
      { type: "assistant.progress", sequence: 1, step: 1, summary: "并行检查项目" },
      {
        type: "subtask.requested",
        sequence: 2,
        step: 1,
        summary: "检查运行入口",
        metadata: { subtask_id: "s1", scope: ["src"] },
      },
      {
        type: "subtask.started",
        sequence: 3,
        step: 1,
        summary: "检查运行入口",
        metadata: { subtask_id: "s1", scope: ["src"] },
      },
      {
        type: "subtask.completed",
        sequence: 4,
        step: 1,
        summary: "发现入口缺少错误转换",
        metadata: {
          subtask_id: "s1",
          scope: ["src"],
          status: "completed",
          usage: { input_tokens: 4, output_tokens: 3 },
          duration_ms: 12,
        },
      },
    ]);

    expect(actions).toHaveLength(1);
    expect(actions[0]?.label).toBe("并行检查项目");
    expect(actions[0]?.tools[0]?.label).toContain("子任务 · 检查运行入口 · 完成");
    expect(actions[0]?.tools[0]?.detail).toContain("发现入口缺少错误转换");
  });
});
