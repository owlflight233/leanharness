import {
  Activity,
  Blocks,
  Bot,
  FolderGit2,
  Menu,
  PanelRight,
  Pencil,
  Plus,
  Send,
  Settings,
  Square,
  TerminalSquare,
  Trash2,
  UserRound,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { streamChat, type ChatStreamer, type TurnEvent } from "./api/chat";
import { fetchHealth, type HealthLoader, type HealthResponse } from "./api/health";
import {
  fetchModelStatus,
  type ModelStatus,
  type ModelStatusLoader,
} from "./api/model";
import {
  resolveRunApproval,
  streamRun,
  type ApprovalResolver,
  type RunEvent,
  type RunStreamer,
} from "./api/run";
import {
  createSession,
  deleteSession,
  fetchSession,
  fetchSessions,
  updateSession,
  type PermissionMode,
  type SessionDetail,
  type SessionSummary,
} from "./api/sessions";
import { Markdown } from "./components/Markdown";
import { PlanCard } from "./components/PlanCard";
import { RunProcess } from "./components/RunProcess";
import { createWorkspace, listProjects, selectWorkspace, type ProjectSummary, type WorkspaceClient } from "./api/workspace";
import {
  fetchPlan,
  rejectPlan,
  streamPlanCreation,
  streamPlanAction,
  updatePlan,
  type Plan,
} from "./api/plans";

type InspectorTab = "trace";
type RunMode = "agent" | "plan";
type TraceEvent = TurnEvent | RunEvent;
type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error" };
type MessageStatus = "streaming" | "complete" | "incomplete" | "error" | "cancelled";

interface ConversationMessage {
  id: number;
  role: "user" | "assistant" | "progress" | "plan";
  content: string;
  status: MessageStatus;
  runId?: string;
  plan?: Plan;
}

interface AppProps {
  healthLoader?: HealthLoader;
  modelStatusLoader?: ModelStatusLoader;
  chatStreamer?: ChatStreamer;
  runStreamer?: RunStreamer;
  approvalResolver?: ApprovalResolver;
  sessionClient?: SessionClient;
  workspaceClient?: WorkspaceClient;
}

export interface SessionClient {
  list(signal?: AbortSignal): Promise<SessionSummary[]>;
  get(id: string, signal?: AbortSignal): Promise<SessionDetail>;
  create(permissionMode?: PermissionMode): Promise<SessionSummary>;
  update(
    id: string,
    patch: { title?: string; permission_mode?: PermissionMode },
  ): Promise<SessionSummary>;
  delete(id: string): Promise<{ deleted: boolean; session_id: string }>;
}

const defaultSessionClient: SessionClient = {
  list: fetchSessions,
  get: fetchSession,
  create: createSession,
  update: updateSession,
  delete: deleteSession,
};

interface SavedRunTrace {
  type: "run.saved";
  sequence: number;
  run_id: string;
  state: string;
  task: string;
}

interface PersistedTraceEvent {
  type: string;
  sequence: number;
  run_id: string;
  step?: number;
  tool?: string;
  summary?: string;
  error?: { code: string; message: string };
  metadata?: Record<string, unknown>;
}

interface PendingApproval {
  id: string;
  runId: string;
  tool: string;
  summary: string;
  parameters: Record<string, unknown>;
  preview?: string;
}

function App({
  healthLoader = fetchHealth,
  modelStatusLoader = fetchModelStatus,
  chatStreamer: legacyChatStreamer,
  runStreamer: providedRunStreamer,
  approvalResolver = resolveRunApproval,
  sessionClient = defaultSessionClient,
  workspaceClient = { select: selectWorkspace, create: createWorkspace },
}: AppProps) {
  const chatStreamer = legacyChatStreamer ?? streamChat;
  const runStreamer = providedRunStreamer ?? streamRun;
  // Keep the old single-turn transport injectable for existing integrations,
  // while the product always presents the coding-agent runtime by default.
  const useLegacyChatTransport = providedRunStreamer === undefined && legacyChatStreamer !== undefined;
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [, setInspectorTab] = useState<InspectorTab>("trace");
  const [health, setHealth] = useState<LoadState<HealthResponse>>({ status: "loading" });
  const [modelStatus, setModelStatus] = useState<LoadState<ModelStatus>>({ status: "loading" });
  const [mode, setMode] = useState<RunMode>("agent");
  const [composerMenuOpen, setComposerMenuOpen] = useState(false);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [trace, setTrace] = useState<Array<TraceEvent | SavedRunTrace | PersistedTraceEvent>>([]);
  const [openProcesses, setOpenProcesses] = useState<Record<string, boolean>>({});
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [approvalSubmitting, setApprovalSubmitting] = useState(false);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("inspect");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [planLoading, setPlanLoading] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const nextMessageId = useRef(1);
  const activeRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    healthLoader(controller.signal)
      .then((data) => setHealth({ status: "ready", data }))
      .catch((error: unknown) => {
        if (!isAbortError(error)) setHealth({ status: "error" });
      });
    return () => controller.abort();
  }, [healthLoader]);

  useEffect(() => {
    const controller = new AbortController();
    modelStatusLoader(controller.signal)
      .then((data) => setModelStatus({ status: "ready", data }))
      .catch((error: unknown) => {
        if (!isAbortError(error)) setModelStatus({ status: "error" });
      });
    return () => controller.abort();
  }, [modelStatusLoader]);

  useEffect(() => () => activeRequest.current?.abort(), []);

  useEffect(() => {
    if (health.status !== "ready") return;
    const controller = new AbortController();
    setSessionsLoading(true);
    const loadProjects = workspaceClient.list ?? listProjects;
    loadProjects(controller.signal)
      .then((data) => setProjects(data.projects))
      .catch((error: unknown) => {
        if (!isAbortError(error)) setSessionError(errorMessage(error));
      });
    sessionClient.list(controller.signal)
      .then((items) => {
        setSessions(items);
        const saved = window.localStorage.getItem("leanharness.session");
        const selected = items.find((item) => item.id === saved) || items[0];
        if (selected) void selectSession(selected.id);
      })
      .catch((error: unknown) => {
        if (!isAbortError(error)) setSessionError(errorMessage(error));
      })
      .finally(() => setSessionsLoading(false));
    return () => controller.abort();
  }, [health.status, sessionClient]);

  const workspace = health.status === "ready" ? health.data.workspace : "未选择";
  const version = health.status === "ready" ? health.data.version : "0.1.0.dev0";
  const connectionCopy = {
    loading: { title: "正在连接本地服务", status: "服务连接中", dot: "pending" },
    ready: { title: "工作区已连接", status: "服务在线", dot: "connected" },
    error: { title: "本地服务不可用", status: "服务离线", dot: "error" },
  }[health.status];
  const modelName = modelStatus.status === "ready" ? modelStatus.data.model : null;
  const modelCopy =
    modelStatus.status === "loading"
      ? "检查中"
      : modelStatus.status === "error"
        ? "状态未知"
        : modelStatus.data.configured
          ? (modelStatus.data.model ?? "已配置")
          : "未配置";
  const canSubmit =
    health.status === "ready" &&
    modelStatus.status === "ready" &&
    modelStatus.data.configured &&
    !isStreaming &&
    !planLoading &&
    input.trim().length > 0 &&
    input.length <= 32_000;

  async function submitMessage() {
    if (!canSubmit) return;
    const message = input;
    const userId = nextMessageId.current++;
    if (mode === "plan") {
      setInput("");
      setMessages((current) => [
        ...current,
        { id: userId, role: "user", content: message, status: "complete" },
      ]);
      setInspectorTab("trace");
      const activeSessionId = sessionId;
      let resolvedSessionId = activeSessionId;
      const controller = new AbortController();
      activeRequest.current = controller;
      setActiveRunId(null);
      setIsStreaming(true);
      setPlanLoading(true);
      try {
        await streamPlanCreation(
          message.trim(),
          (event) => {
            setTrace((current) => [...current, event as TraceEvent]);
            if (typeof event.run_id === "string") {
              const runId = event.run_id;
              setActiveRunId(runId);
              updateMessage(userId, (current) => ({ ...current, runId }));
            }
            if (typeof event.session_id === "string" && event.session_id !== resolvedSessionId) {
              resolvedSessionId = event.session_id;
              setSessionId(event.session_id);
              window.localStorage.setItem("leanharness.session", event.session_id);
            }
            if (event.type === "plan.created" && isRecord(event.plan)) {
              const created = event.plan as unknown as Plan;
              setPlan(created);
              appendMessage("plan", created.source_markdown, "complete", String(event.run_id), created);
              setProcessVisibility(String(event.run_id), false);
            }
            if (event.type === "assistant.progress" || event.type === "tool.requested" || event.type === "tool.started" || event.type === "approval.required") {
              if (typeof event.run_id === "string") setProcessVisibility(event.run_id, true);
            }
          },
          controller.signal,
          activeSessionId ?? undefined,
        );
      } catch (error: unknown) {
        setSessionError(errorMessage(error));
      } finally {
        if (activeRequest.current === controller) activeRequest.current = null;
        setActiveRunId(null);
        setIsStreaming(false);
        setPlanLoading(false);
        await refreshSessionList(resolvedSessionId);
      }
      return;
    }
    const assistantId = useLegacyChatTransport ? nextMessageId.current++ : null;
    const activeSessionId = sessionId;
    let resolvedSessionId = activeSessionId;
    const controller = new AbortController();
    activeRequest.current = controller;
    setMessages((current) => {
      const next: ConversationMessage[] = [
        ...current,
        { id: userId, role: "user", content: message, status: "complete" },
      ];
      if (assistantId !== null) {
        next.push({ id: assistantId, role: "assistant", content: "", status: "streaming" });
      }
      return next;
    });
    setInput("");
    setActiveRunId(null);
    setInspectorTab("trace");
    setIsStreaming(true);

    try {
      if (useLegacyChatTransport && assistantId !== null) {
        await chatStreamer(
          message,
          (event) => {
            setTrace((current) => [...current, event]);
            if (event.run_id) {
              setActiveRunId(event.run_id);
              updateMessage(userId, (current) => ({ ...current, runId: event.run_id }));
              updateMessage(assistantId, (current) => ({ ...current, runId: event.run_id }));
            }
            if (event.session_id && event.session_id !== resolvedSessionId) {
              resolvedSessionId = event.session_id;
              setSessionId(event.session_id);
              window.localStorage.setItem("leanharness.session", event.session_id);
            }
            if (event.type === "content.delta") {
              updateMessage(assistantId, (current) => ({
                ...current,
                content: current.content + event.content,
              }));
            } else if (event.type === "turn.completed") {
              updateMessage(assistantId, (current) => ({ ...current, status: "complete" }));
            } else if (event.type === "turn.failed") {
              updateMessage(assistantId, (current) => ({
                ...current,
                content: current.content || event.error.message,
                status: "error",
              }));
            }
          },
          controller.signal,
          activeSessionId ?? undefined,
        );
      } else {
        await runStreamer(
          message,
          (event) => {
            setTrace((current) => [...current, event]);
            setActiveRunId(event.run_id);
            updateMessage(userId, (current) => ({ ...current, runId: event.run_id }));
            if (event.session_id && event.session_id !== resolvedSessionId) {
              resolvedSessionId = event.session_id;
              setSessionId(event.session_id);
              window.localStorage.setItem("leanharness.session", event.session_id);
            }
            if (event.type === "assistant.progress") {
              setProcessVisibility(event.run_id, true);
            } else if (event.type === "approval.required") {
              const metadata = event.metadata ?? {};
              setPendingApproval({
                id: String(metadata.approval_id),
                runId: event.run_id,
                tool: event.tool,
                summary: event.summary ?? "此操作需要批准",
                parameters: isRecord(metadata.parameters) ? metadata.parameters : {},
                preview: typeof metadata.preview === "string" ? metadata.preview : undefined,
              });
              setProcessVisibility(event.run_id, true);
            } else if (event.type === "approval.resolved") {
              setPendingApproval(null);
              setApprovalSubmitting(false);
            } else if (event.type === "run.completed") {
              appendMessage("assistant", event.answer, "complete", event.run_id);
              setProcessVisibility(event.run_id, false);
            } else if (event.type === "run.incomplete") {
              appendMessage(
                "assistant",
                event.answer || event.summary || "运行预算已用完，任务尚未完成",
                "incomplete",
                event.run_id,
              );
              setProcessVisibility(event.run_id, false);
            } else if (event.type === "run.failed") {
              appendMessage("assistant", event.error.message, "error", event.run_id);
              setProcessVisibility(event.run_id, false);
            } else if (event.type === "run.cancelled") {
              appendMessage("assistant", "已停止运行", "cancelled", event.run_id);
              setProcessVisibility(event.run_id, false);
            }
          },
          controller.signal,
          24,
          activeSessionId ?? undefined,
        );
      }
    } catch (error: unknown) {
      const content = isAbortError(error)
        ? useLegacyChatTransport ? "已停止生成" : "已停止运行"
        : errorMessage(error);
      if (assistantId !== null) {
        updateMessage(assistantId, (current) => ({
          ...current,
          content: current.content || content,
          status: isAbortError(error) ? "cancelled" : "error",
        }));
      } else {
        appendMessage("assistant", content, isAbortError(error) ? "cancelled" : "error");
      }
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
      setPendingApproval(null);
      setApprovalSubmitting(false);
      setIsStreaming(false);
      await refreshSessionList(resolvedSessionId);
    }
  }

  function appendMessage(
    role: ConversationMessage["role"],
    content: string,
    status: MessageStatus,
    runId?: string,
    planRecord?: Plan,
  ) {
    const id = nextMessageId.current++;
    setMessages((current) => [...current, { id, role, content, status, runId, plan: planRecord }]);
  }

  function selectMode(nextMode: RunMode) {
    if (isStreaming || nextMode === mode) return;
    setMode(nextMode);
    setComposerMenuOpen(false);
  }

  async function selectSession(id: string) {
    if (isStreaming) return;
    try {
      const detail = await sessionClient.get(id);
      setSessionId(id);
      setPermissionMode(detail.session.permission_mode);
      setMessages(
        detail.messages.map((message, index) => ({
          id: -(index + 1),
          role: message.kind === "plan" ? "plan" : message.role,
          content: message.content,
          status: (message.status as MessageStatus) || "complete",
          runId: message.run_id ?? undefined,
          plan: message.plan_id
            ? detail.plans?.find((item) => item.id === message.plan_id)
            : undefined,
        })),
      );
      setPlan(
        detail.plans?.find((item) => ["AWAITING_CONFIRMATION", "RUNNING", "PAUSED"].includes(item.state))
          ?? null,
      );
      setTrace(
        detail.runs.flatMap((run) =>
          run.trace?.map((event) => ({
            ...event,
            run_id: run.id,
          })) ?? [
            {
              type: "run.saved" as const,
              sequence: 0,
              run_id: run.id,
              state: run.state,
              task: run.task,
            },
          ],
        ),
      );
      setOpenProcesses({});
      setActiveRunId(null);
      setPendingApproval(null);
      window.localStorage.setItem("leanharness.session", id);
      setSessionError(null);
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function changeWorkspace() {
    if (isStreaming || health.status !== "ready") return;
    const nextPath = window.prompt("输入工作区目录", health.data.workspace);
    if (!nextPath || nextPath.trim() === health.data.workspace) return;
    try {
      await workspaceClient.select(nextPath.trim());
      setHealth({ status: "loading" });
      const refreshed = await healthLoader();
      setHealth({ status: "ready", data: refreshed });
      setMessages([]);
      setTrace([]);
      setPlan(null);
      setSessionId(null);
      window.localStorage.removeItem("leanharness.session");
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function selectProject(project: ProjectSummary) {
    if (isStreaming || health.status !== "ready" || project.root_path === health.data.workspace) return;
    try {
      await workspaceClient.select(project.root_path);
      setHealth({ status: "loading" });
      const refreshed = await healthLoader();
      setHealth({ status: "ready", data: refreshed });
      setMessages([]);
      setTrace([]);
      setPlan(null);
      setSessionId(null);
      window.localStorage.removeItem("leanharness.session");
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function createProject() {
    if (isStreaming || health.status !== "ready") return;
    const nextPath = window.prompt("输入新项目目录", `${health.data.workspace}\\新项目`);
    if (!nextPath?.trim()) return;
    try {
      const created = await (workspaceClient.create ?? createWorkspace)(nextPath.trim());
      setHealth({ status: "loading" });
      const refreshed = await healthLoader();
      setHealth({ status: "ready", data: { ...refreshed, workspace: created.workspace } });
      setMessages([]);
      setTrace([]);
      setPlan(null);
      setSessionId(null);
      setSessions([]);
      window.localStorage.removeItem("leanharness.session");
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function handleNewSession() {
    if (isStreaming) return;
    try {
      const created = await sessionClient.create(permissionMode);
      setSessions((current) => [created, ...current]);
      await selectSession(created.id);
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function handleRename(session: SessionSummary) {
    const title = window.prompt("会话名称", session.title);
    if (!title || title === session.title) return;
    try {
      const updated = await sessionClient.update(session.id, { title });
      setSessions((current) => current.map((item) => (item.id === updated.id ? updated : item)));
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function handleDelete(session: SessionSummary) {
    if (!window.confirm(`删除会话“${session.title}”？`)) return;
    try {
      await sessionClient.delete(session.id);
      const remaining = sessions.filter((item) => item.id !== session.id);
      setSessions(remaining);
      if (sessionId === session.id) {
        setSessionId(null);
        setMessages([]);
        window.localStorage.removeItem("leanharness.session");
        if (remaining[0]) await selectSession(remaining[0].id);
      }
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function changePermission(next: PermissionMode) {
    setPermissionMode(next);
    if (sessionId) {
      try {
        const updated = await sessionClient.update(sessionId, { permission_mode: next });
        setSessions((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      } catch (error: unknown) {
        setSessionError(errorMessage(error));
      }
    }
  }

  async function refreshSessionList(preferredId: string | null) {
    try {
      const items = await sessionClient.list();
      setSessions(items);
      if (preferredId) {
        setSessionId(preferredId);
        window.localStorage.setItem("leanharness.session", preferredId);
      }
      setSessionError(null);
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function decideApproval(decision: "approve" | "reject") {
    if (!pendingApproval || approvalSubmitting) return;
    setApprovalSubmitting(true);
    try {
      await approvalResolver(pendingApproval.runId, pendingApproval.id, decision);
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
      setApprovalSubmitting(false);
    }
  }

  async function runPlanAction(
    action: "confirm" | "resume",
    targetPlan: Plan | undefined = plan ?? undefined,
  ) {
    if (!targetPlan || isStreaming) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    setIsStreaming(true);
    setInspectorTab("trace");
    try {
      for await (const event of streamPlanAction(targetPlan.id, action, controller.signal)) {
        setTrace((current) => [...current, event as TraceEvent]);
        if (typeof event.run_id === "string") {
          setActiveRunId(event.run_id);
          if (
            event.type !== "run.completed" &&
            event.type !== "run.incomplete" &&
            event.type !== "run.failed" &&
            event.type !== "run.cancelled"
          ) {
            setProcessVisibility(event.run_id, true);
          }
        }
        if (event.type === "run.completed" || event.type === "run.incomplete" || event.type === "run.failed" || event.type === "run.cancelled") {
          setPlan((current) => current?.id === targetPlan.id
            ? { ...current, state: event.type === "run.completed" ? "COMPLETED" : event.type === "run.incomplete" ? "PAUSED" : event.type === "run.cancelled" ? "CANCELLED" : "FAILED" }
            : current);
          if (typeof event.run_id === "string") setProcessVisibility(event.run_id, false);
          if (event.type === "run.completed" || event.type === "run.incomplete") {
            const answer = typeof event.answer === "string"
              ? event.answer
              : typeof event.summary === "string" ? event.summary : "运行结束";
            appendMessage(
              "assistant",
              answer,
              event.type === "run.completed" ? "complete" : "incomplete",
              typeof event.run_id === "string" ? event.run_id : undefined,
            );
          }
        }
      }
      const refreshed = await fetchPlan(targetPlan.id);
      setPlan((current) => current?.id === refreshed.id ? refreshed : current);
      setMessages((current) => current.map((message) => (
        message.plan?.id === refreshed.id
          ? { ...message, plan: refreshed, content: refreshed.source_markdown }
          : message
      )));
    } catch (error: unknown) {
      if (!isAbortError(error)) setSessionError(errorMessage(error));
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
      setIsStreaming(false);
    }
  }

  async function editPlanStep(
    stepId: string,
    field: "title" | "instruction",
    value: string,
    targetPlan: Plan | undefined = plan ?? undefined,
  ) {
    if (!targetPlan || targetPlan.state !== "AWAITING_CONFIRMATION") return;
    const steps = targetPlan.steps.map((step) => step.id === stepId ? { ...step, [field]: value } : step);
    try {
      const updated = await updatePlan(targetPlan, targetPlan.title, steps);
      setPlan(updated);
      setMessages((current) => current.map((message) => (
        message.plan?.id === updated.id
          ? { ...message, plan: updated, content: updated.source_markdown }
          : message
      )));
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function rejectCurrentPlan(targetPlan: Plan | undefined = plan ?? undefined) {
    if (!targetPlan || isStreaming) return;
    try {
      const updated = await rejectPlan(targetPlan.id);
      setPlan(updated);
      setMessages((current) => current.map((message) => (
        message.plan?.id === updated.id
          ? { ...message, plan: updated, content: updated.source_markdown }
          : message
      )));
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  function setProcessVisibility(runId: string, open: boolean) {
    setOpenProcesses((current) => ({ ...current, [runId]: open }));
  }

  function updateMessage(
    id: number,
    update: (message: ConversationMessage) => ConversationMessage,
  ) {
    setMessages((current) => current.map((message) => (message.id === id ? update(message) : message)));
  }

  return (
    <div className="app-shell">
      <aside className={`project-rail ${leftOpen ? "is-open" : ""}`} aria-label="项目导航">
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true"><TerminalSquare size={19} /></div>
          <div className="brand-copy"><strong>LeanHarness</strong><span>本地工作台</span></div>
          <button className="icon-button mobile-only" type="button" title="关闭项目导航" aria-label="关闭项目导航" onClick={() => setLeftOpen(false)}><X size={18} /></button>
        </div>
        <button className="primary-action" type="button" disabled={isStreaming || health.status !== "ready"} onClick={() => void handleNewSession()}><Plus size={17} /><span>新建会话</span></button>
        <nav className="rail-content" aria-label="项目与会话">
          <section className="rail-section">
            <div className="section-label"><span>项目</span><button className="icon-button compact" type="button" disabled={isStreaming || health.status !== "ready"} aria-label="添加项目" title="新建项目" onClick={() => void createProject()}><Plus size={14} /></button></div>
            {projects.length > 0 ? projects.map((project) => (
              <button key={project.id} className={`empty-row workspace-picker ${project.root_path === workspace ? "active" : ""}`} type="button" title={project.root_path} onClick={() => void selectProject(project)} disabled={isStreaming || health.status !== "ready"}>
                <FolderGit2 size={16} /><span>{project.root_path}</span>{project.root_path === workspace && <span className="project-current">当前</span>}
              </button>
            )) : <button className="empty-row workspace-picker" type="button" title="切换工作区" onClick={() => void changeWorkspace()} disabled={isStreaming || health.status !== "ready"}><FolderGit2 size={16} /><span>{workspace}</span><Pencil size={12} /></button>}
          </section>
          <section className="rail-section sessions-section">
            <div className="section-label"><span>当前运行</span></div>
            {sessionError && <div className="session-error">{sessionError}</div>}
            {sessionsLoading ? <div className="empty-row muted"><Bot size={16} /><span>正在加载会话</span></div> : sessions.length === 0 ? <div className="empty-row muted"><Bot size={16} /><span>暂无会话</span></div> : sessions.map((session) => (
              <div className={`session-row ${session.id === sessionId ? "active" : ""}`} key={session.id}>
                <button type="button" className="session-select" onClick={() => void selectSession(session.id)} disabled={isStreaming} title={session.title}><Bot size={16} /><span>{session.title}</span></button>
                <button type="button" className="session-menu" aria-label={`重命名会话 ${session.title}`} title="重命名会话" onClick={() => void handleRename(session)} disabled={isStreaming}><Pencil size={13} /></button>
                <button type="button" className="session-menu danger" aria-label={`删除会话 ${session.title}`} title="删除会话" onClick={() => void handleDelete(session)} disabled={isStreaming}><Trash2 size={14} /></button>
              </div>
            ))}
          </section>
        </nav>
        <div className="rail-footer">
          <button className="nav-button" type="button" disabled><Blocks size={17} /><span>扩展</span></button>
          <button className="nav-button" type="button" disabled><Settings size={17} /><span>设置</span></button>
        </div>
      </aside>

      <main className="workspace">
        <header className="workspace-header">
          <button className="icon-button mobile-only" type="button" title="打开项目导航" aria-label="打开项目导航" onClick={() => setLeftOpen(true)}><Menu size={19} /></button>
          <div className="session-identity"><span className="session-title">{sessions.find((item) => item.id === sessionId)?.title || "新会话"}</span><span className="session-subtitle">{workspace}</span></div>
          <button className="icon-button mobile-only" type="button" title="打开检查器" aria-label="打开检查器" onClick={() => setRightOpen(true)}><PanelRight size={19} /></button>
        </header>

        <section className="conversation" aria-label="会话内容">
          {messages.length === 0 ? (
            <div className="conversation-empty">
              <div className="empty-glyph"><Bot size={22} /></div>
              <h1>{connectionCopy.title}</h1>
              <p>{modelStatus.status === "ready" && !modelStatus.data.configured ? "请在环境变量中配置模型" : `LeanHarness ${version}`}</p>
            </div>
          ) : (
            <div className="message-list">
              {messages.map((message) => (
                <div key={message.id}>
                {!isStreaming && (message.role === "assistant" || message.role === "plan") && message.runId && trace.some((event) => event.run_id === message.runId) && (
                  <RunProcess
                    trace={trace.filter((event) => event.run_id === message.runId)}
                    open={openProcesses[message.runId] ?? false}
                    onToggle={() => setProcessVisibility(message.runId!, !(openProcesses[message.runId!] ?? false))}
                    running={false}
                  />
                )}
                <article className={`message message-${message.role} is-${message.status}`}>
                  <div className="message-icon" aria-hidden="true">{message.role === "user" ? <UserRound size={16} /> : message.role === "progress" ? <Activity size={16} /> : <Bot size={16} />}</div>
                  <div className="message-body">
                    <strong>{message.role === "user" ? "你" : message.role === "progress" ? "行动" : message.role === "plan" ? "计划" : modelName || "模型"}</strong>
                    <div className="message-content">
                      {message.role === "plan" && message.plan ? (
                        <PlanCard
                          plan={message.plan}
                          onConfirm={() => void runPlanAction("confirm", message.plan)}
                          onResume={() => void runPlanAction("resume", message.plan)}
                          onReject={() => void rejectCurrentPlan(message.plan)}
                          onEdit={(stepId, field, value) => void editPlanStep(stepId, field, value, message.plan)}
                        />
                      ) : message.content ? (message.role === "assistant" ? <Markdown content={message.content} /> : message.content) : "正在生成..."}
                    </div>
                  </div>
                </article>
                </div>
              ))}
              {isStreaming && activeRunId && trace.some((event) => event.run_id === activeRunId && (event.type === "assistant.progress" || event.type.startsWith("tool.") || event.type.startsWith("approval."))) && (
                <RunProcess
                  trace={trace.filter((event) => event.run_id === activeRunId)}
                  open={openProcesses[activeRunId] ?? true}
                  onToggle={() => setProcessVisibility(activeRunId, !(openProcesses[activeRunId] ?? true))}
                  running
                />
              )}
            </div>
          )}
        </section>

        <form className="composer" onSubmit={(event) => { event.preventDefault(); void submitMessage(); }}>
          {pendingApproval && (
            <div className="approval-panel" role="alert">
              <div className="approval-heading"><strong>{pendingApproval.summary}</strong><span>{pendingApproval.tool}</span></div>
              {pendingApproval.preview ? <pre>{pendingApproval.preview}</pre> : <code>{JSON.stringify(pendingApproval.parameters)}</code>}
              <div className="approval-actions">
                <button type="button" disabled={approvalSubmitting} onClick={() => void decideApproval("reject")}>拒绝</button>
                <button type="button" className="approve-button" disabled={approvalSubmitting} onClick={() => void decideApproval("approve")}>批准一次</button>
              </div>
            </div>
          )}
              <textarea aria-label="任务输入" placeholder={modelStatus.status === "ready" && !modelStatus.data.configured ? "请先配置模型环境变量" : mode === "plan" ? "描述需要完成的工作" : "输入一个仓库任务"} rows={2} value={input} maxLength={32_000} disabled={health.status !== "ready" || modelStatus.status !== "ready" || !modelStatus.data.configured || isStreaming || planLoading} onChange={(event) => setInput(event.target.value)} />
          <div className="composer-actions">
            <div className="composer-menu-wrap">
              <button
                className="icon-button composer-plus"
                type="button"
                aria-label="添加模式、文件或插件"
                aria-expanded={composerMenuOpen}
                title="添加模式、文件或插件"
                disabled={isStreaming || planLoading}
                onClick={() => setComposerMenuOpen((open) => !open)}
              >
                <Plus size={18} />
              </button>
              {composerMenuOpen && (
                <div className="composer-menu" role="menu" aria-label="添加到当前任务">
                  <div className="composer-menu-label">运行模式</div>
                  <button type="button" role="menuitem" className={mode === "plan" ? "selected" : ""} aria-pressed={mode === "plan"} onClick={() => selectMode(mode === "plan" ? "agent" : "plan")}>
                    <Blocks size={15} /><span>计划</span><small>先规划，再执行</small>
                  </button>
                  <div className="composer-menu-divider" />
                  <div className="composer-menu-label">附件与扩展</div>
                  <button type="button" role="menuitem" disabled><Plus size={15} /><span>上传文件</span><small>即将支持</small></button>
                  <button type="button" role="menuitem" disabled><Blocks size={15} /><span>选择插件</span><small>即将支持</small></button>
                </div>
              )}
            </div>
            <div className="composer-settings"><label htmlFor="permission-mode">权限</label><select id="permission-mode" value={permissionMode} onChange={(event) => void changePermission(event.target.value as PermissionMode)} disabled={isStreaming}><option value="inspect">只读检查</option><option value="approve">逐次批准</option><option value="unrestricted">受控直接执行</option></select></div><span className="composer-state">{isStreaming ? mode === "plan" ? "正在生成计划" : "Agent 正在执行" : modelStatus.status === "ready" && modelStatus.data.configured ? mode === "plan" ? "计划模式 · 本地保存" : "Agent · 本地保存" : `模型${modelCopy}`}</span>
            {isStreaming ? (
              <button className="send-button stop-button" type="button" aria-label={useLegacyChatTransport ? "停止生成" : "停止运行"} title={useLegacyChatTransport ? "停止生成" : "停止运行"} onClick={() => activeRequest.current?.abort()}><Square size={14} fill="currentColor" /></button>
            ) : (
              <button className="send-button" type="submit" aria-label="发送任务" disabled={!canSubmit}><Send size={17} /></button>
            )}
          </div>
        </form>
      </main>

      <aside className={`inspector ${rightOpen ? "is-open" : ""}`} aria-label="运行检查器">
        <div className="inspector-heading">
          <div className="inspector-tabs" role="tablist" aria-label="检查器视图">
            <button type="button" role="tab" aria-selected="true" className="active">轨迹</button>
          </div>
          <button className="icon-button mobile-only" type="button" title="关闭检查器" aria-label="关闭检查器" onClick={() => setRightOpen(false)}><X size={18} /></button>
        </div>
        <div className="inspector-body" role="tabpanel">
          {trace.filter((event) => event.type !== "assistant.progress").length === 0 ? <div className="panel-empty"><Activity size={18} /><strong>没有运行轨迹</strong><span>0 条事件</span></div> : <ol className="trace-list">{trace.filter((event) => event.type !== "assistant.progress").map((event) => <li key={`${"run_id" in event ? event.run_id : "turn"}-${event.sequence}-${event.type}`}><span>{event.sequence}</span><strong title={traceLabel(event)}>{traceLabel(event)}</strong></li>)}</ol>}
        </div>
        <div className="inspector-summary"><div><span>模型</span><strong title={modelName ?? undefined}>{modelCopy}</strong></div><div><span>权限</span><strong>{permissionMode === "inspect" ? "只读检查" : permissionMode === "approve" ? "逐次批准" : "受控直接执行"}</strong></div></div>
      </aside>

      <footer className="status-bar" aria-label="运行状态">
        <span className="status-item"><span className={`status-dot ${connectionCopy.dot}`} />{connectionCopy.status}</span><span className="status-divider" /><span className="status-item" title={workspace}>工作区：{workspace}</span><span className="status-spacer" /><span className="status-item">v{version}</span>
      </footer>
      {(leftOpen || rightOpen) && <button className="scrim mobile-only" type="button" aria-label="关闭面板" onClick={() => { setLeftOpen(false); setRightOpen(false); }} />}
    </div>
  );
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "模型请求失败";
}

function traceLabel(event: TraceEvent | SavedRunTrace | PersistedTraceEvent): string {
  if (event.type === "run.saved" && "state" in event && "task" in event) {
    return `${event.state}: ${event.task}`;
  }
  if ("tool" in event && event.tool) return `${event.type}: ${event.tool}`;
  if ("summary" in event && event.summary) return `${event.type}: ${event.summary}`;
  return event.type;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export default App;
