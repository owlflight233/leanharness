import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import App from "./App";
import type { ChatStreamer } from "./api/chat";
import type { HealthLoader, HealthResponse } from "./api/health";
import type { ModelStatusLoader } from "./api/model";
import type { RunStreamer } from "./api/run";

const healthy: HealthResponse = {
  status: "ok",
  name: "LeanHarness",
  version: "0.1.0.dev0",
  workspace: "C:\\projects\\demo",
  capabilities: ["model.chat", "model.streaming", "agent.inspect", "agent.streaming"],
};

const successfulHealth: HealthLoader = async () => healthy;
const configuredModel: ModelStatusLoader = async () => ({
  configured: true,
  protocol: "openai-compatible",
  model: "example-model",
});
const unconfiguredModel: ModelStatusLoader = async () => ({
  configured: false,
  protocol: "openai-compatible",
  model: null,
});

describe("application shell", () => {
  it("renders a loading state without claiming agent capabilities", () => {
    const pendingHealth: HealthLoader = () => new Promise(() => undefined);
    render(<App healthLoader={pendingHealth} modelStatusLoader={unconfiguredModel} />);

    expect(screen.getByText("LeanHarness")).toBeInTheDocument();
    expect(screen.getByText("正在连接本地服务")).toBeInTheDocument();
    expect(screen.getByText("服务连接中")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送任务" })).toBeDisabled();
  });

  it("renders backend health and workspace", async () => {
    render(<App healthLoader={successfulHealth} modelStatusLoader={configuredModel} />);

    expect(await screen.findByText("工作区已连接")).toBeInTheDocument();
    expect(screen.getByText("服务在线")).toBeInTheDocument();
    expect(screen.getByText("工作区：C:\\projects\\demo")).toBeInTheDocument();
  });

  it("renders a failed connection", async () => {
    const failedHealth: HealthLoader = async () => {
      throw new Error("offline");
    };
    render(<App healthLoader={failedHealth} modelStatusLoader={unconfiguredModel} />);

    await waitFor(() => expect(screen.getByText("本地服务不可用")).toBeInTheDocument());
    expect(screen.getByText("服务离线")).toBeInTheDocument();
  });

  it("switches inspector projections", async () => {
    const user = userEvent.setup();
    render(<App healthLoader={successfulHealth} modelStatusLoader={configuredModel} />);

    await user.click(screen.getByRole("tab", { name: "轨迹" }));

    expect(screen.getByText("没有运行轨迹")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "轨迹" })).toHaveAttribute("aria-selected", "true");
  });

  it("opens and closes the project drawer", async () => {
    const user = userEvent.setup();
    render(<App healthLoader={successfulHealth} modelStatusLoader={configuredModel} />);

    await user.click(screen.getByRole("button", { name: "打开项目导航" }));
    expect(screen.getByLabelText("项目导航")).toHaveClass("is-open");

    await user.click(screen.getByRole("button", { name: "关闭项目导航" }));
    expect(screen.getByLabelText("项目导航")).not.toHaveClass("is-open");
  });

  it("keeps chat disabled when the model is not configured", async () => {
    render(<App healthLoader={successfulHealth} modelStatusLoader={unconfiguredModel} />);

    expect(await screen.findByText("请在环境变量中配置模型")).toBeInTheDocument();
    expect(screen.getByLabelText("任务输入")).toBeDisabled();
    expect(screen.getByText("模型未配置")).toBeInTheDocument();
  });

  it("renders streamed content and trace events", async () => {
    const user = userEvent.setup();
    const successfulChat: ChatStreamer = async (_message, onEvent) => {
      onEvent({ type: "turn.started", sequence: 0 });
      onEvent({ type: "content.delta", sequence: 1, content: "流式回复" });
      onEvent({ type: "turn.completed", sequence: 2, finish_reason: "stop" });
    };
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        chatStreamer={successfulChat}
      />,
    );

    const input = await screen.findByLabelText("任务输入");
    await user.type(input, "你好");
    await user.click(screen.getByRole("button", { name: "发送任务" }));

    expect(await screen.findByText("流式回复")).toBeInTheDocument();
    expect(screen.getByText("turn.started")).toBeInTheDocument();
    expect(screen.getByText("turn.completed")).toBeInTheDocument();
  });

  it("cancels the active stream", async () => {
    const user = userEvent.setup();
    const blockingChat: ChatStreamer = (_message, onEvent, signal) =>
      new Promise((_resolve, reject) => {
        onEvent({ type: "turn.started", sequence: 0 });
        signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      });
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        chatStreamer={blockingChat}
      />,
    );

    const input = await screen.findByLabelText("任务输入");
    await user.type(input, "停止测试");
    await user.click(screen.getByRole("button", { name: "发送任务" }));
    await user.click(await screen.findByRole("button", { name: "停止生成" }));

    expect(await screen.findByText("已停止生成")).toBeInTheDocument();
  });

  it("runs a read-only inspection and separates progress from the final answer", async () => {
    const user = userEvent.setup();
    const inspection: RunStreamer = async (_task, onEvent) => {
      onEvent({ type: "run.started", sequence: 0, run_id: "r1" });
      onEvent({
        type: "assistant.progress",
        sequence: 1,
        run_id: "r1",
        step: 1,
        summary: "读取 README",
      });
      onEvent({
        type: "tool.completed",
        sequence: 2,
        run_id: "r1",
        step: 1,
        tool: "workspace_read",
        metadata: { ok: true },
      });
      onEvent({
        type: "run.completed",
        sequence: 3,
        run_id: "r1",
        answer: "仓库包含 README。",
      });
    };
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        runStreamer={inspection}
      />,
    );
    await user.click(screen.getByRole("button", { name: "检查" }));
    await user.type(await screen.findByLabelText("任务输入"), "分析仓库");
    await user.click(screen.getByRole("button", { name: "发送任务" }));

    expect(await screen.findByText("仓库包含 README。")).toBeInTheDocument();
    expect(screen.getByText("读取 README")).toBeInTheDocument();
    expect(screen.getByText(/tool.completed: workspace_read/)).toBeInTheDocument();
  });
});
