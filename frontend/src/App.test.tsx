import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App, { type SessionClient } from "./App";
import type { Attachment } from "./api/attachments";
import type { HealthLoader, HealthResponse } from "./api/health";
import type { ModelStatusLoader } from "./api/model";
import type { PluginSummary } from "./api/plugins";
import type { ApprovalResolver, RunStreamer, UserInputResolver } from "./api/run";
import type { WorkspaceClient } from "./api/workspace";
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
        permission_mode: "inspect",
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

  it("switches to a selected workspace and clears the old session view", async () => {
    const user = userEvent.setup();
    const workspaceClient: WorkspaceClient = { select: vi.fn(async () => ({ workspace: "C:\\projects\\next" })) };
    vi.spyOn(window, "prompt").mockReturnValue("C:\\projects\\next");
    render(<App healthLoader={successfulHealth} modelStatusLoader={configuredModel} workspaceClient={workspaceClient} />);

    await user.click(await screen.findByRole("button", { name: /C:\\projects\\demo/ }));
    expect(workspaceClient.select).toHaveBeenCalledWith("C:\\projects\\next");
  });

  it("keeps project order stable and restores a separate session per project", async () => {
    const user = userEvent.setup();
    const firstProject = {
      id: "project-1",
      root_path: "C:\\projects\\one",
      permission_mode: "inspect" as const,
      created_at: "2026-08-27T10:00:00+00:00",
      updated_at: "2026-08-27T10:00:00+00:00",
    };
    const secondProject = {
      ...firstProject,
      id: "project-2",
      root_path: "C:\\projects\\two",
      created_at: "2026-08-27T10:01:00+00:00",
      updated_at: "2026-08-27T10:01:00+00:00",
    };
    const firstSession = { ...baseSession, id: "session-one", project_id: firstProject.id, title: "项目一会话" };
    const secondSession = { ...baseSession, id: "session-two", project_id: secondProject.id, title: "项目二会话" };
    let currentWorkspace = firstProject.root_path;
    const healthLoader: HealthLoader = async () => ({ ...healthy, workspace: currentWorkspace });
    const workspaceClient: WorkspaceClient = {
      select: vi.fn(async (path) => {
        currentWorkspace = path;
        return { workspace: path };
      }),
      list: vi.fn(async () => ({
        current_workspace: currentWorkspace,
        projects: [firstProject, secondProject],
      })),
    };
    const client: SessionClient = {
      list: vi.fn(async () => [
        currentWorkspace === firstProject.root_path ? firstSession : secondSession,
      ]),
      get: vi.fn(async (id) => sessionDetail(id === firstSession.id ? firstSession : secondSession)),
      create: vi.fn(),
      update: vi.fn(),
      delete: vi.fn(),
    };
    window.localStorage.setItem(`leanharness.session.${firstProject.id}`, firstSession.id);
    window.localStorage.setItem(`leanharness.session.${secondProject.id}`, secondSession.id);

    render(
      <App
        healthLoader={healthLoader}
        modelStatusLoader={configuredModel}
        sessionClient={client}
        workspaceClient={workspaceClient}
      />,
    );

    expect((await screen.findAllByText(firstSession.title)).length).toBeGreaterThan(0);
    const firstButton = screen.getAllByTitle(firstProject.root_path).find(
      (element) => element.tagName === "BUTTON",
    )!;
    const secondButton = screen.getAllByTitle(secondProject.root_path).find(
      (element) => element.tagName === "BUTTON",
    )!;
    expect(firstButton.compareDocumentPosition(secondButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(firstButton).getByText("one")).toBeInTheDocument();
    expect(within(firstButton).getByText("C:\\projects")).toBeInTheDocument();

    await user.click(secondButton);
    expect((await screen.findAllByText(secondSession.title)).length).toBeGreaterThan(0);
    expect(client.get).toHaveBeenLastCalledWith(secondSession.id);
    expect(firstButton.compareDocumentPosition(secondButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await user.click(firstButton);
    expect((await screen.findAllByText(firstSession.title)).length).toBeGreaterThan(0);
    expect(client.get).toHaveBeenLastCalledWith(firstSession.id);
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

  it("uses Agent by default and exposes Plan Mode only from the add menu", async () => {
    const user = userEvent.setup();
    render(<App healthLoader={successfulHealth} modelStatusLoader={configuredModel} />);

    expect(await screen.findByText("Agent · 本地保存")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Agent" })).not.toBeInTheDocument();
    expect(screen.queryByText("单轮对话")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "添加模式、文件或插件" }));
    expect(screen.getByRole("menuitem", { name: /计划/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Agent" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "单轮对话" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("menuitem", { name: /计划/ }));
    expect(screen.getByText("计划模式 · 本地保存")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "添加模式、文件或插件" }));
    await user.click(screen.getByRole("menuitem", { name: /计划/ }));
    expect(screen.getByText("Agent · 本地保存")).toBeInTheDocument();
  });

  it("enables parallel analysis from the add menu for this run", async () => {
    const user = userEvent.setup();
    const run = vi.fn<RunStreamer>(async (_task, onEvent) => {
      onEvent({ type: "run.started", sequence: 0, run_id: "delegated-run" });
      onEvent({ type: "run.completed", sequence: 1, run_id: "delegated-run", answer: "完成" });
    });
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        runStreamer={run}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "添加模式、文件或插件" }));
    const delegation = screen.getByRole("menuitemcheckbox", { name: /子任务协作/ });
    expect(delegation).toHaveAttribute("aria-checked", "false");
    await user.click(delegation);
    expect(delegation).toHaveAttribute("aria-checked", "true");
    await user.type(screen.getByLabelText("任务输入"), "分析项目");
    await user.click(screen.getByRole("button", { name: "发送任务" }));

    await waitFor(() => expect(run).toHaveBeenCalled());
    expect(run.mock.calls[0]?.[7]).toBe(true);
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

    expect(await screen.findByText("模型尚未配置")).toBeInTheDocument();
    expect(screen.getByLabelText("任务输入")).toBeDisabled();
    expect(screen.getByText("模型未配置")).toBeInTheDocument();
  });

  it("restores the latest run permission even when old trace metadata is absent", async () => {
    const detail = sessionDetail();
    detail.runs[0].permission_mode = "approve";
    const client = mockSessionClient();
    client.get = vi.fn(async () => detail);

    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        sessionClient={client}
      />,
    );

    const inspector = await screen.findByLabelText("运行检查器");
    await waitFor(() => {
      expect(within(inspector).getByText("逐次批准")).toBeInTheDocument();
      expect(within(inspector).queryByText("尚未运行")).not.toBeInTheDocument();
    });
  });

  it("renders agent content and trace events", async () => {
    const user = userEvent.setup();
    const successfulRun: RunStreamer = async (_task, onEvent) => {
      onEvent({ type: "run.started", sequence: 0, run_id: "run-1" });
      onEvent({ type: "assistant.progress", sequence: 1, run_id: "run-1", summary: "检查项目" });
      onEvent({ type: "tool.completed", sequence: 2, run_id: "run-1", tool: "workspace_list", metadata: { ok: true } });
      onEvent({ type: "run.completed", sequence: 3, run_id: "run-1", answer: "流式回复" });
    };
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        runStreamer={successfulRun}
      />,
    );

    const input = await screen.findByLabelText("任务输入");
    await user.type(input, "你好");
    await user.click(screen.getByRole("button", { name: "发送任务" }));

    expect(await screen.findByText("流式回复")).toBeInTheDocument();
    expect(screen.getByText("run.started")).toBeInTheDocument();
    expect(screen.getByText("run.completed")).toBeInTheDocument();
  });

  it("cancels the active stream", async () => {
    const user = userEvent.setup();
    const blockingRun: RunStreamer = (_task, onEvent, signal) =>
      new Promise((_resolve, reject) => {
        onEvent({ type: "run.started", sequence: 0, run_id: "blocking-run" });
        signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      });
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        runStreamer={blockingRun}
      />,
    );

    const input = await screen.findByLabelText("任务输入");
    await user.type(input, "停止测试");
    await user.click(screen.getByRole("button", { name: "发送任务" }));
    await user.click(await screen.findByRole("button", { name: "停止运行" }));

    expect(await screen.findByText("已停止运行")).toBeInTheDocument();
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
    await user.type(await screen.findByLabelText("任务输入"), "分析仓库");
    await user.click(screen.getByRole("button", { name: "发送任务" }));

    expect(await screen.findByText("仓库包含 README。")).toBeInTheDocument();
    const process = screen.getByRole("button", { name: /执行过程已结束/ });
    expect(process).toHaveAttribute("aria-expanded", "false");
    await user.click(process);
    expect(screen.getAllByText(/读取 README/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/tool.completed: workspace_read/).length).toBeGreaterThan(0);
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

  it("rolls back the permission selector when persistence fails", async () => {
    const user = userEvent.setup();
    const client = mockSessionClient();
    client.update = vi.fn(async () => {
      throw new Error("保存失败");
    });
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        sessionClient={client}
      />,
    );
    await screen.findByText("已保存的结论");

    await user.selectOptions(screen.getByLabelText("权限"), "unrestricted");

    await waitFor(() => expect(screen.getByLabelText("权限")).toHaveValue("inspect"));
    expect(screen.getByText("保存失败")).toBeInTheDocument();
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
    const run = vi.fn<RunStreamer>(async (_task, onEvent) => {
      onEvent({ type: "run.started", sequence: 0, session_id: baseSession.id, run_id: "run-2" });
      onEvent({ type: "run.completed", sequence: 1, session_id: baseSession.id, run_id: "run-2", answer: "完成" });
    });
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        runStreamer={run}
        sessionClient={client}
      />,
    );
    await screen.findByText("已保存的结论");
    await user.type(screen.getByLabelText("任务输入"), "继续分析");
    await user.click(screen.getByRole("button", { name: "发送任务" }));

    await waitFor(() => expect(run).toHaveBeenCalled());
    expect(run.mock.calls[0]?.[4]).toBe(baseSession.id);
  });

  it("uploads a text attachment and sends only its id to the current run", async () => {
    const user = userEvent.setup();
    const client = mockSessionClient();
    const attachment: Attachment = {
      id: "attachment-1",
      session_id: baseSession.id,
      message_id: null,
      filename: "notes.txt",
      media_type: "text/plain",
      kind: "text",
      byte_size: 5,
      sha256: "a".repeat(64),
      created_at: baseSession.created_at,
    };
    const attachmentClient = {
      upload: vi.fn(async () => attachment),
      delete: vi.fn(async () => undefined),
    };
    const run = vi.fn<RunStreamer>(async (_task, onEvent) => {
      onEvent({ type: "run.started", sequence: 0, run_id: "attachment-run" });
      onEvent({
        type: "run.completed",
        sequence: 1,
        run_id: "attachment-run",
        answer: "已读取附件。",
      });
    });
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        sessionClient={client}
        attachmentClient={attachmentClient}
        runStreamer={run}
      />,
    );
    await screen.findByText("已保存的结论");

    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    await user.upload(screen.getByLabelText("选择文本或代码附件"), file);
    expect(await screen.findByText("notes.txt")).toBeInTheDocument();
    await user.type(screen.getByLabelText("任务输入"), "读取附件");
    await user.click(screen.getByRole("button", { name: "发送任务" }));

    await waitFor(() => expect(run).toHaveBeenCalled());
    expect(attachmentClient.upload).toHaveBeenCalledWith(baseSession.id, file);
    expect(run.mock.calls[0]?.[5]).toEqual([attachment.id]);
    expect(run.mock.calls[0]?.[6]).toEqual([]);
  });

  it("retries a failed attachment upload and removes the stored attachment", async () => {
    const user = userEvent.setup();
    const attachment: Attachment = {
      id: "attachment-retry",
      session_id: baseSession.id,
      message_id: null,
      filename: "retry.txt",
      media_type: "text/plain",
      kind: "text",
      byte_size: 5,
      sha256: "b".repeat(64),
      created_at: baseSession.created_at,
    };
    const attachmentClient = {
      upload: vi.fn()
        .mockRejectedValueOnce(new Error("上传暂时失败"))
        .mockResolvedValueOnce(attachment),
      delete: vi.fn(async () => undefined),
    };
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        sessionClient={mockSessionClient()}
        attachmentClient={attachmentClient}
      />,
    );
    await screen.findByText("已保存的结论");

    const file = new File(["retry"], "retry.txt", { type: "text/plain" });
    await user.upload(screen.getByLabelText("选择文本或代码附件"), file);
    expect(await screen.findByRole("alert")).toHaveTextContent("上传暂时失败");
    await user.click(screen.getByRole("button", { name: "重试上传 retry.txt" }));
    await waitFor(() => expect(attachmentClient.upload).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("button", { name: "移除附件 retry.txt" }));
    await waitFor(() => expect(attachmentClient.delete).toHaveBeenCalledWith(attachment.id));
    expect(screen.queryByText("retry.txt")).not.toBeInTheDocument();
  });

  it("shows only enabled plugins and passes an explicit selection to the run", async () => {
    const user = userEvent.setup();
    const plugins: PluginSummary[] = [
      {
        id: "leanharness-docx",
        name: "LeanHarness DOCX",
        version: "0.1.0",
        description: "Generate DOCX",
        protocol_version: "leanharness.plugin.v1",
        enabled: true,
        tools: [{ name: "docx_generate", description: "Generate", mutation: true }],
        installed_at: baseSession.created_at,
        updated_at: baseSession.updated_at,
      },
      {
        id: "disabled-plugin",
        name: "Disabled Plugin",
        version: "0.1.0",
        description: "Disabled",
        protocol_version: "leanharness.plugin.v1",
        enabled: false,
        tools: [{ name: "disabled_tool", description: "Disabled", mutation: false }],
        installed_at: baseSession.created_at,
        updated_at: baseSession.updated_at,
      },
    ];
    const run = vi.fn<RunStreamer>(async (_task, onEvent) => {
      onEvent({ type: "run.started", sequence: 0, run_id: "plugin-run" });
      onEvent({ type: "run.completed", sequence: 1, run_id: "plugin-run", answer: "完成" });
    });
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        runStreamer={run}
        pluginLoader={async () => plugins}
      />,
    );
    await screen.findByText("工作区已连接");
    await user.click(screen.getByRole("button", { name: "添加模式、文件或插件" }));
    const docx = await screen.findByRole("menuitemcheckbox", { name: /LeanHarness DOCX/ });
    expect(screen.queryByText("Disabled Plugin")).not.toBeInTheDocument();
    await user.click(docx);
    await user.type(screen.getByLabelText("任务输入"), "生成报告");
    await user.click(screen.getByRole("button", { name: "发送任务" }));

    await waitFor(() => expect(run).toHaveBeenCalled());
    expect(run.mock.calls[0]?.[6]).toEqual(["leanharness-docx"]);
  });

  it("renders safe Markdown without raw HTML or dangerous links", async () => {
    const user = userEvent.setup();
    const markdownRun: RunStreamer = async (_task, onEvent) => {
      onEvent({ type: "run.started", sequence: 0, run_id: "markdown-run" });
      onEvent({
        type: "run.completed",
        sequence: 1,
        run_id: "markdown-run",
        answer: "# 标题\n\n| 列 | 值 |\n|---|---|\n| A | 1 |\n\n```ts\nconst value = 1\n```\n\n[危险链接](javascript:alert(1))\n\n<strong>原始 HTML</strong>",
      });
    };
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        runStreamer={markdownRun}
      />,
    );

    await user.type(await screen.findByLabelText("任务输入"), "渲染 Markdown");
    await user.click(screen.getByRole("button", { name: "发送任务" }));

    expect(await screen.findByRole("heading", { name: "标题" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("const")).toHaveClass("hljs-keyword");
    expect(screen.getByText("危险链接")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "危险链接" })).not.toBeInTheDocument();
    expect(screen.getByText("原始 HTML")).toBeInTheDocument();
    expect(screen.getByText("原始 HTML").tagName).toBe("P");
  });

  it("keeps persisted run actions independently collapsed", async () => {
    const detail = sessionDetail();
    detail.messages[0]!.run_id = "run-1";
    detail.messages[1]!.run_id = "run-1";
    detail.messages.push(
      {
        id: "message-3",
        sequence: 2,
        role: "user",
        content: "第二个任务",
        status: "complete",
        created_at: baseSession.updated_at,
        run_id: "run-2",
      },
      {
        id: "message-4",
        sequence: 3,
        role: "assistant",
        content: "第二个结论",
        status: "complete",
        created_at: baseSession.updated_at,
        run_id: "run-2",
      },
    );
    detail.runs[0]!.trace = [
      { type: "assistant.progress", sequence: 1, summary: "第一个动作" },
    ];
    detail.runs.push({
      ...detail.runs[0]!,
      id: "run-2",
      task: "第二个任务",
      answer: "第二个结论",
      trace: [{ type: "assistant.progress", sequence: 1, summary: "第二个动作" }],
    });
    const client = mockSessionClient();
    client.get = vi.fn(async () => detail);

    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        sessionClient={client}
      />,
    );

    const processes = await screen.findAllByRole("button", { name: /执行过程已结束/ });
    expect(processes).toHaveLength(2);
    expect(processes[0]).toHaveAttribute("aria-expanded", "false");
    expect(processes[1]).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(processes[0]!);
    expect(processes[0]).toHaveAttribute("aria-expanded", "true");
    expect(processes[1]).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText(/第一个动作/)).toBeInTheDocument();
    expect(screen.queryByText(/第二个动作/)).not.toBeInTheDocument();
  });

  it("resolves an interactive approval and resumes the run", async () => {
    const user = userEvent.setup();
    let resume: (() => void) | undefined;
    const approvalResolved = new Promise<void>((resolve) => { resume = resolve; });
    const approvalResolver = vi.fn<ApprovalResolver>(async () => { resume?.(); });
    const codingRun: RunStreamer = async (_task, onEvent) => {
      onEvent({ type: "run.started", sequence: 0, run_id: "approval-run" });
      onEvent({
        type: "approval.required",
        sequence: 1,
        run_id: "approval-run",
        tool: "workspace_patch",
        summary: "批准修改 value.txt",
        metadata: {
          approval_id: "approval-1",
          parameters: { files: ["value.txt"] },
          preview: "--- a/value.txt\n+++ b/value.txt",
        },
      });
      await approvalResolved;
      onEvent({
        type: "approval.resolved",
        sequence: 2,
        run_id: "approval-run",
        tool: "workspace_patch",
        metadata: { approval_id: "approval-1", decision: "approve" },
      });
      onEvent({
        type: "run.completed",
        sequence: 3,
        run_id: "approval-run",
        answer: "修改已完成。",
      });
    };
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        runStreamer={codingRun}
        approvalResolver={approvalResolver}
      />,
    );

    await user.type(await screen.findByLabelText("任务输入"), "修改 value.txt");
    await user.click(screen.getByRole("button", { name: "发送任务" }));
    await user.click(await screen.findByRole("button", { name: "批准一次" }));

    await waitFor(() =>
      expect(approvalResolver).toHaveBeenCalledWith(
        "approval-run",
        "approval-1",
        "approve",
      ),
    );
    expect(await screen.findByText("修改已完成。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批准一次" })).not.toBeInTheDocument();
  });

  it("answers a model-requested question and resumes the run", async () => {
    const user = userEvent.setup();
    let resume: (() => void) | undefined;
    const inputResolved = new Promise<void>((resolve) => { resume = resolve; });
    const userInputResolver = vi.fn<UserInputResolver>(async () => { resume?.(); });
    const questioningRun: RunStreamer = async (_task, onEvent) => {
      onEvent({ type: "run.started", sequence: 0, run_id: "question-run" });
      onEvent({
        type: "input.required",
        sequence: 1,
        run_id: "question-run",
        tool: "request_user_input",
        metadata: {
          input_id: "input-1",
          question: "选择目标",
          options: [
            { label: "API", description: "修改后端" },
            { label: "Web", description: "修改前端" },
          ],
        },
      });
      await inputResolved;
      onEvent({
        type: "input.resolved",
        sequence: 2,
        run_id: "question-run",
        tool: "request_user_input",
        metadata: { input_id: "input-1" },
      });
      onEvent({
        type: "run.completed",
        sequence: 3,
        run_id: "question-run",
        answer: "已选择 API。",
      });
    };
    render(
      <App
        healthLoader={successfulHealth}
        modelStatusLoader={configuredModel}
        runStreamer={questioningRun}
        userInputResolver={userInputResolver}
      />,
    );

    await user.type(await screen.findByLabelText("任务输入"), "选择修改目标");
    await user.click(screen.getByRole("button", { name: "发送任务" }));
    expect(await screen.findByText("选择目标")).toBeInTheDocument();
    expect(screen.getByText("修改后端")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /API/ }));

    await waitFor(() =>
      expect(userInputResolver).toHaveBeenCalledWith("question-run", "input-1", "API"),
    );
    expect(await screen.findByText("已选择 API。")).toBeInTheDocument();
    expect(screen.queryByLabelText("Agent 提问")).not.toBeInTheDocument();
  });
});
