import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App, { type SessionClient } from "./App";
import type { ChatStreamer } from "./api/chat";
import type { HealthLoader, HealthResponse } from "./api/health";
import type { ModelStatusLoader } from "./api/model";
import type { RunStreamer } from "./api/run";
import type { PermissionMode, SessionDetail, SessionSummary } from "./api/sessions";

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

const baseSession: SessionSummary = {
  id: "session-1",
  project_id: "project-1",
  title: "分析仓库结构",
  permission_mode: "inspect",
  created_at: "2026-08-27T10:00:00+00:00",
  updated_at: "2026-08-27T10:00:00+00:00",
  last_run_state: "COMPLETED",
};

function sessionDetail(session = baseSession): SessionDetail {
  return {
    session,
    messages: [
      {
        id: "message-1",
        sequence: 0,
        role: "user",
        content: "分析仓库结构",
        status: "complete",
        created_at: session.created_at,
      },
      {
        id: "message-2",
        sequence: 1,
        role: "assistant",
        content: "已保存的结论",
        status: "complete",
        created_at: session.updated_at,
      },
    ],
    runs: [
      {
        id: "run-1",
        session_id: session.id,
        mode: "inspect",
        task: "分析仓库结构",
        state: "COMPLETED",
        max_steps: 24,
        answer: "已保存的结论",
        error_code: null,
        started_at: session.created_at,
        finished_at: session.updated_at,
      },
    ],
  };
}

function mockSessionClient(initial = baseSession): SessionClient {
  let current = initial;
  return {
    list: vi.fn(async () => [current]),
    get: vi.fn(async () => sessionDetail(current)),
    create: vi.fn(async (permissionMode: PermissionMode = "inspect") => {
      current = { ...baseSession, id: "session-new", title: "新会话", permission_mode: permissionMode };
      return current;
    }),
    update: vi.fn(async (_id, patch) => {
      current = { ...current, ...patch };
      return current;
    }),
    delete: vi.fn(async (id) => ({ deleted: true, session_id: id })),
  };
}

beforeEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
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

  it("restores the most recent persisted session and its run summary", async () => {
    const client = mockSessionClient();
    window.localStorage.setItem("leanharness.session", baseSession.id);

    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        sessionClient={client}
      />,
    );

    expect(await screen.findByText("已保存的结论")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "轨迹" }));
    expect(screen.getByText("COMPLETED: 分析仓库结构")).toBeInTheDocument();
    expect(client.get).toHaveBeenCalledWith(baseSession.id);
  });

  it("creates a new session using the selected permission", async () => {
    const user = userEvent.setup();
    const client = mockSessionClient();
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        sessionClient={client}
      />,
    );
    await screen.findByText("已保存的结论");
    await user.selectOptions(screen.getByLabelText("权限"), "approve");
    await user.click(screen.getByRole("button", { name: "新建会话" }));

    await waitFor(() => expect(client.create).toHaveBeenCalledWith("approve"));
    expect(window.localStorage.getItem("leanharness.session")).toBe("session-new");
  });

  it("renames a session and saves permission changes", async () => {
    const user = userEvent.setup();
    const client = mockSessionClient();
    vi.spyOn(window, "prompt").mockReturnValue("新的名称");
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        sessionClient={client}
      />,
    );
    await screen.findByText("已保存的结论");

    await user.click(screen.getByRole("button", { name: `重命名会话 ${baseSession.title}` }));
    expect((await screen.findAllByText("新的名称")).length).toBeGreaterThanOrEqual(1);
    await user.selectOptions(screen.getByLabelText("权限"), "unrestricted");
    await waitFor(() =>
      expect(client.update).toHaveBeenCalledWith(baseSession.id, {
        permission_mode: "unrestricted",
      }),
    );
  });

  it("requires confirmation before deleting one session", async () => {
    const user = userEvent.setup();
    const client = mockSessionClient();
    const confirmation = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        sessionClient={client}
      />,
    );
    await screen.findByText("已保存的结论");
    const deleteButton = screen.getByRole("button", { name: `删除会话 ${baseSession.title}` });

    await user.click(deleteButton);
    expect(client.delete).not.toHaveBeenCalled();
    await user.click(deleteButton);
    expect(confirmation).toHaveBeenCalledTimes(2);
    expect(client.delete).toHaveBeenCalledWith(baseSession.id);
  });

  it("binds the active session to chat requests", async () => {
    const user = userEvent.setup();
    const client = mockSessionClient();
    const chat = vi.fn<ChatStreamer>(async (_message, onEvent) => {
      onEvent({ type: "turn.started", sequence: 0, session_id: baseSession.id, run_id: "run-2" });
      onEvent({ type: "turn.completed", sequence: 1, session_id: baseSession.id, run_id: "run-2" });
    });
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        chatStreamer={chat}
        sessionClient={client}
      />,
    );
    await screen.findByText("已保存的结论");
    await user.type(screen.getByLabelText("任务输入"), "继续分析");
    await user.click(screen.getByRole("button", { name: "发送任务" }));

    await waitFor(() => expect(chat).toHaveBeenCalled());
    expect(chat.mock.calls[0]?.[3]).toBe(baseSession.id);
  });
});
