import {
  Activity,
  Blocks,
  Bot,
  FileCode2,
  FolderGit2,
  Image as ImageIcon,
  LoaderCircle,
  Menu,
  PanelRight,
  Pencil,
  Plus,
  RotateCcw,
  Send,
  Settings,
  Square,
  TerminalSquare,
  Trash2,
  UserRound,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { fetchHealth, type HealthLoader, type HealthResponse } from "./api/health";
import {
  deleteAttachment,
  uploadAttachment,
  type Attachment,
} from "./api/attachments";
import { fetchPlugins, type PluginSummary } from "./api/plugins";
import {
  fetchModelStatus,
  type ModelStatus,
  type ModelStatusLoader,
} from "./api/model";
import {
  resolveRunApproval,
  resolveRunInput,
  streamRun,
  type ApprovalResolver,
  type RunEvent,
  type RunStreamer,
  type UserInputResolver,
} from "./api/run";
import {
  createSession,
  deleteSession,
  fetchSession,
  fetchSessions,
  updateSession,
  type PermissionMode,
  type MessageAttachment,
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
type TraceEvent = RunEvent;
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
  attachments?: AttachmentView[];
}

interface AttachmentView extends MessageAttachment {
  previewUrl?: string;
}

interface PendingAttachment {
  localId: string;
  file: File;
  status: "uploading" | "ready" | "error";
  attachment?: Attachment;
  previewUrl?: string;
  error?: string;
}

interface AppProps {
  healthLoader?: HealthLoader;
  modelStatusLoader?: ModelStatusLoader;
  runStreamer?: RunStreamer;
  approvalResolver?: ApprovalResolver;
  userInputResolver?: UserInputResolver;
  sessionClient?: SessionClient;
  workspaceClient?: WorkspaceClient;
  attachmentClient?: AttachmentClient;
  pluginLoader?: (signal?: AbortSignal) => Promise<PluginSummary[]>;
}

export interface AttachmentClient {
  upload(sessionId: string, file: File, signal?: AbortSignal): Promise<Attachment>;
  delete(id: string): Promise<void>;
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

const defaultAttachmentClient: AttachmentClient = {
  upload: uploadAttachment,
  delete: deleteAttachment,
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

interface PendingQuestion {
  id: string;
  runId: string;
  question: string;
  options: Array<{ label: string; description: string }>;
}

function App({
  healthLoader = fetchHealth,
  modelStatusLoader = fetchModelStatus,
  runStreamer: providedRunStreamer,
  approvalResolver = resolveRunApproval,
  userInputResolver = resolveRunInput,
  sessionClient = defaultSessionClient,
  workspaceClient = { select: selectWorkspace, create: createWorkspace },
  attachmentClient = defaultAttachmentClient,
  pluginLoader = fetchPlugins,
}: AppProps) {
  const runStreamer = providedRunStreamer ?? streamRun;
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [, setInspectorTab] = useState<InspectorTab>("trace");
  const [health, setHealth] = useState<LoadState<HealthResponse>>({ status: "loading" });
  const [modelStatus, setModelStatus] = useState<LoadState<ModelStatus>>({ status: "loading" });
  const [mode, setMode] = useState<RunMode>("agent");
  const [composerMenuOpen, setComposerMenuOpen] = useState(false);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [trace, setTrace] = useState<Array<TraceEvent | SavedRunTrace | PersistedTraceEvent>>([]);
  const [restoredRunPermission, setRestoredRunPermission] = useState<PermissionMode | null>(null);
  const [openProcesses, setOpenProcesses] = useState<Record<string, boolean>>({});
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [approvalSubmitting, setApprovalSubmitting] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState<PendingQuestion | null>(null);
  const [questionSubmitting, setQuestionSubmitting] = useState(false);
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
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [plugins, setPlugins] = useState<PluginSummary[]>([]);
  const [selectedPluginIds, setSelectedPluginIds] = useState<string[]>([]);
  const [pluginError, setPluginError] = useState<string | null>(null);
  const nextMessageId = useRef(1);
  const nextAttachmentId = useRef(1);
  const activeRequest = useRef<AbortController | null>(null);
  const sessionSelection = useRef(0);
  const imageInput = useRef<HTMLInputElement | null>(null);
  const textInput = useRef<HTMLInputElement | null>(null);
  const objectUrls = useRef(new Set<string>());

  function projectSessionKey(projectId: string | undefined): string {
    return projectId ? `leanharness.session.${projectId}` : "leanharness.session";
  }

  function currentProjectId(projectList: ProjectSummary[] = projects, currentWorkspace = workspace): string | undefined {
    return projectList.find((project) => project.root_path === currentWorkspace)?.id;
  }

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

  useEffect(() => {
    const controller = new AbortController();
    pluginLoader(controller.signal)
      .then((items) => {
        setPlugins(items);
        setSelectedPluginIds((current) =>
          current.filter((id) => items.some((item) => item.id === id && item.enabled)),
        );
        setPluginError(null);
      })
      .catch((error: unknown) => {
        if (!isAbortError(error)) setPluginError(errorMessage(error));
      });
    return () => controller.abort();
  }, [pluginLoader]);

  useEffect(() => () => {
    activeRequest.current?.abort();
    for (const url of objectUrls.current) URL.revokeObjectURL(url);
  }, []);

  useEffect(() => {
    if (health.status !== "ready") return;
    const controller = new AbortController();
    setSessionsLoading(true);
    const loadProjects = workspaceClient.list ?? listProjects;
    const projectRequest = loadProjects(controller.signal).catch((error: unknown) => {
      if (!isAbortError(error)) setSessionError(errorMessage(error));
      return null;
    });
    const sessionRequest = sessionClient.list(controller.signal).catch((error: unknown) => {
      if (!isAbortError(error)) setSessionError(errorMessage(error));
      return null;
    });
    Promise.all([projectRequest, sessionRequest])
      .then(([projectData, items]) => {
        if (controller.signal.aborted) return;
        const projectList = projectData?.projects ?? projects;
        if (projectData) setProjects(projectData.projects);
        if (!items) return;
        setSessions(items);
        const projectId = projectData
          ? currentProjectId(projectData.projects, projectData.current_workspace)
          : currentProjectId(projectList);
        const scopedKey = projectSessionKey(projectId);
        const scopedSaved = window.localStorage.getItem(scopedKey);
        const legacySaved = projectId ? window.localStorage.getItem("leanharness.session") : null;
        const saved = scopedSaved || legacySaved;
        const selected = items.find((item) => item.id === saved) || items[0];
        if (projectId && legacySaved && selected?.id === legacySaved) {
          window.localStorage.removeItem("leanharness.session");
        }
        if (selected) void selectSession(selected.id, projectId);
      })
      .finally(() => setSessionsLoading(false));
    return () => controller.abort();
  }, [
    health.status,
    health.status === "ready" ? health.data.workspace : null,
    sessionClient,
  ]);

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
    !pendingAttachments.some((attachment) => attachment.status !== "ready") &&
    input.trim().length > 0 &&
    input.length <= 32_000;
  const runPermissionMode = latestRunPermission(trace) ?? restoredRunPermission;

  async function submitMessage() {
    if (!canSubmit) return;
    const message = input;
    const userId = nextMessageId.current++;
    const submittedAttachments = pendingAttachments;
    const attachmentIds = submittedAttachments.flatMap((item) =>
      item.attachment ? [item.attachment.id] : [],
    );
    const messageAttachments = submittedAttachments.flatMap((item) =>
      item.attachment
        ? [{ ...item.attachment, previewUrl: item.previewUrl }]
        : [],
    );
    setPendingAttachments([]);
    setAttachmentError(null);
    if (mode === "plan") {
      setInput("");
      setMessages((current) => [
        ...current,
        {
          id: userId,
          role: "user",
          content: message,
          status: "complete",
          attachments: messageAttachments,
        },
      ]);
      setInspectorTab("trace");
      const activeSessionId = sessionId;
      let resolvedSessionId = activeSessionId;
      const controller = new AbortController();
      activeRequest.current = controller;
      setActiveRunId(null);
      setIsStreaming(true);
      setPlanLoading(true);
      let requestStarted = false;
      try {
        await streamPlanCreation(
          message.trim(),
          (event) => {
            requestStarted = true;
            setTrace((current) => [...current, event as unknown as TraceEvent]);
            if (typeof event.run_id === "string") {
              const runId = event.run_id;
              setActiveRunId(runId);
              updateMessage(userId, (current) => ({ ...current, runId }));
            }
            if (typeof event.session_id === "string" && event.session_id !== resolvedSessionId) {
              resolvedSessionId = event.session_id;
              setSessionId(event.session_id);
              window.localStorage.setItem(projectSessionKey(currentProjectId()), event.session_id);
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
          attachmentIds,
        );
      } catch (error: unknown) {
        if (!requestStarted) setPendingAttachments(submittedAttachments);
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
    const activeSessionId = sessionId;
    let resolvedSessionId = activeSessionId;
    const controller = new AbortController();
    activeRequest.current = controller;
    setMessages((current) => {
      const next: ConversationMessage[] = [
        ...current,
        {
          id: userId,
          role: "user",
          content: message,
          status: "complete",
          attachments: messageAttachments,
        },
      ];
      return next;
    });
    setInput("");
    setActiveRunId(null);
    setInspectorTab("trace");
    setIsStreaming(true);
    let requestStarted = false;

    try {
      await runStreamer(
          message,
          (event) => {
            requestStarted = true;
            setTrace((current) => [...current, event]);
            setActiveRunId(event.run_id);
            updateMessage(userId, (current) => ({ ...current, runId: event.run_id }));
            if (event.session_id && event.session_id !== resolvedSessionId) {
              resolvedSessionId = event.session_id;
              setSessionId(event.session_id);
              window.localStorage.setItem(projectSessionKey(currentProjectId()), event.session_id);
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
            } else if (event.type === "input.required") {
              const metadata = event.metadata ?? {};
              const options = Array.isArray(metadata.options)
                ? metadata.options.filter(isRecord).map((option) => ({
                    label: String(option.label ?? ""),
                    description: String(option.description ?? ""),
                  }))
                : [];
              setPendingQuestion({
                id: String(metadata.input_id),
                runId: event.run_id,
                question: String(metadata.question ?? "Agent 需要补充信息"),
                options,
              });
              setProcessVisibility(event.run_id, true);
            } else if (event.type === "input.resolved") {
              setPendingQuestion(null);
              setQuestionSubmitting(false);
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
          attachmentIds,
          selectedPluginIds,
      );
    } catch (error: unknown) {
      if (!requestStarted) setPendingAttachments(submittedAttachments);
      const content = isAbortError(error) ? "已停止运行" : errorMessage(error);
      appendMessage("assistant", content, isAbortError(error) ? "cancelled" : "error");
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
      setPendingApproval(null);
      setApprovalSubmitting(false);
      setPendingQuestion(null);
      setQuestionSubmitting(false);
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

  function togglePlugin(pluginId: string) {
    if (isStreaming) return;
    setSelectedPluginIds((current) =>
      current.includes(pluginId)
        ? current.filter((id) => id !== pluginId)
        : [...current, pluginId],
    );
  }

  async function ensureAttachmentSession(): Promise<string> {
    if (sessionId) return sessionId;
    const created = await sessionClient.create(permissionMode);
    setSessions((current) => [created, ...current]);
    setSessionId(created.id);
    setPermissionMode(created.permission_mode);
    window.localStorage.setItem(projectSessionKey(currentProjectId()), created.id);
    return created.id;
  }

  async function queueAttachments(files: FileList | null) {
    if (!files?.length || isStreaming) return;
    const incoming = Array.from(files);
    if (pendingAttachments.length + incoming.length > 8) {
      setAttachmentError("每条消息最多添加 8 个附件");
      return;
    }
    const totalBytes = [
      ...pendingAttachments.map((item) => item.file.size),
      ...incoming.map((file) => file.size),
    ].reduce((total, size) => total + size, 0);
    if (totalBytes > 20 * 1024 * 1024) {
      setAttachmentError("每条消息的附件总量不能超过 20 MiB");
      return;
    }
    const queued = incoming.map((file): PendingAttachment => {
      const previewUrl = file.type.startsWith("image/") && "createObjectURL" in URL
        ? URL.createObjectURL(file)
        : undefined;
      if (previewUrl) objectUrls.current.add(previewUrl);
      return {
        localId: `attachment-${nextAttachmentId.current++}`,
        file,
        status: "uploading",
        previewUrl,
      };
    });
    setPendingAttachments((current) => [...current, ...queued]);
    setAttachmentError(null);
    let targetSession: string;
    try {
      targetSession = await ensureAttachmentSession();
    } catch (error: unknown) {
      const message = errorMessage(error);
      setPendingAttachments((current) => current.map((item) =>
        queued.some((queuedItem) => queuedItem.localId === item.localId)
          ? { ...item, status: "error", error: message }
          : item,
      ));
      setAttachmentError(message);
      return;
    }
    await Promise.all(queued.map((item) => uploadQueuedAttachment(item, targetSession)));
  }

  async function uploadQueuedAttachment(item: PendingAttachment, targetSession: string) {
    try {
      const attachment = await attachmentClient.upload(targetSession, item.file);
      setPendingAttachments((current) => current.map((candidate) =>
        candidate.localId === item.localId
          ? { ...candidate, status: "ready", attachment, error: undefined }
          : candidate,
      ));
    } catch (error: unknown) {
      const message = errorMessage(error);
      setPendingAttachments((current) => current.map((candidate) =>
        candidate.localId === item.localId
          ? { ...candidate, status: "error", error: message }
          : candidate,
      ));
      setAttachmentError(message);
    }
  }

  async function retryAttachment(item: PendingAttachment) {
    if (item.status !== "error") return;
    setPendingAttachments((current) => current.map((candidate) =>
      candidate.localId === item.localId
        ? { ...candidate, status: "uploading", error: undefined }
        : candidate,
    ));
    setAttachmentError(null);
    try {
      await uploadQueuedAttachment(item, await ensureAttachmentSession());
    } catch (error: unknown) {
      setAttachmentError(errorMessage(error));
    }
  }

  async function removePendingAttachment(item: PendingAttachment) {
    try {
      if (item.attachment) await attachmentClient.delete(item.attachment.id);
      if (item.previewUrl) {
        URL.revokeObjectURL(item.previewUrl);
        objectUrls.current.delete(item.previewUrl);
      }
      setPendingAttachments((current) =>
        current.filter((candidate) => candidate.localId !== item.localId),
      );
      setAttachmentError(null);
    } catch (error: unknown) {
      setAttachmentError(errorMessage(error));
    }
  }

  async function discardPendingAttachments() {
    const current = pendingAttachments;
    for (const item of current) {
      if (item.attachment) await attachmentClient.delete(item.attachment.id);
      if (item.previewUrl) {
        URL.revokeObjectURL(item.previewUrl);
        objectUrls.current.delete(item.previewUrl);
      }
    }
    setPendingAttachments([]);
    setAttachmentError(null);
  }

  async function selectSession(id: string, projectId = currentProjectId()) {
    if (isStreaming) return;
    if (id !== sessionId && pendingAttachments.length) {
      try {
        await discardPendingAttachments();
      } catch (error: unknown) {
        setAttachmentError(errorMessage(error));
        return;
      }
    }
    const requestId = ++sessionSelection.current;
    try {
      const detail = await sessionClient.get(id);
      if (requestId !== sessionSelection.current) return;
      setSessionId(id);
      if (id !== sessionId) setSelectedPluginIds([]);
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
          attachments: message.attachments ?? [],
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
      setRestoredRunPermission(detail.runs.at(-1)?.permission_mode ?? null);
      setOpenProcesses({});
      setActiveRunId(null);
      setPendingApproval(null);
      setPendingQuestion(null);
      window.localStorage.setItem(projectSessionKey(projectId), id);
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
      await discardPendingAttachments();
      sessionSelection.current += 1;
      await workspaceClient.select(nextPath.trim());
      setHealth({ status: "loading" });
      const refreshed = await healthLoader();
      setHealth({ status: "ready", data: refreshed });
      setMessages([]);
      setTrace([]);
      setRestoredRunPermission(null);
      setPlan(null);
      setSessionId(null);
      setSessions([]);
      setSelectedPluginIds([]);
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function selectProject(project: ProjectSummary) {
    if (isStreaming || health.status !== "ready" || project.root_path === health.data.workspace) return;
    try {
      await discardPendingAttachments();
      sessionSelection.current += 1;
      await workspaceClient.select(project.root_path);
      setHealth({ status: "loading" });
      const refreshed = await healthLoader();
      setHealth({ status: "ready", data: refreshed });
      setMessages([]);
      setTrace([]);
      setRestoredRunPermission(null);
      setPlan(null);
      setSessionId(null);
      setSessions([]);
      setSelectedPluginIds([]);
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function createProject() {
    if (isStreaming || health.status !== "ready") return;
    const nextPath = window.prompt("输入新项目目录", `${health.data.workspace}\\新项目`);
    if (!nextPath?.trim()) return;
    try {
      await discardPendingAttachments();
      sessionSelection.current += 1;
      const created = await (workspaceClient.create ?? createWorkspace)(nextPath.trim());
      setHealth({ status: "loading" });
      const refreshed = await healthLoader();
      setHealth({ status: "ready", data: { ...refreshed, workspace: created.workspace } });
      setMessages([]);
      setTrace([]);
      setRestoredRunPermission(null);
      setPlan(null);
      setSessionId(null);
      setSessions([]);
      setSelectedPluginIds([]);
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function handleNewSession() {
    if (isStreaming) return;
    try {
      await discardPendingAttachments();
      const created = await sessionClient.create(permissionMode);
      setSessions((current) => [created, ...current]);
      setSelectedPluginIds([]);
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
        window.localStorage.removeItem(projectSessionKey(currentProjectId()));
        if (remaining[0]) await selectSession(remaining[0].id);
      }
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
    }
  }

  async function changePermission(next: PermissionMode) {
    const previousPermission = permissionMode;
    const previousPlan = plan;
    const previousMessages = messages;
    setPermissionMode(next);
    setPlan((current) => current?.state === "AWAITING_CONFIRMATION"
      ? { ...current, execution_permission_mode: next }
      : current);
    setMessages((current) => current.map((message) => (
      message.plan?.state === "AWAITING_CONFIRMATION"
        ? {
            ...message,
            plan: { ...message.plan, execution_permission_mode: next },
          }
        : message
    )));
    if (sessionId) {
      try {
        const updated = await sessionClient.update(sessionId, { permission_mode: next });
        setSessions((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      } catch (error: unknown) {
        setPermissionMode(previousPermission);
        setPlan(previousPlan);
        setMessages(previousMessages);
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
        window.localStorage.setItem(projectSessionKey(currentProjectId()), preferredId);
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

  async function answerQuestion(answer: string) {
    if (!pendingQuestion || questionSubmitting) return;
    setQuestionSubmitting(true);
    try {
      await userInputResolver(pendingQuestion.runId, pendingQuestion.id, answer);
    } catch (error: unknown) {
      setSessionError(errorMessage(error));
      setQuestionSubmitting(false);
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
      for await (const event of streamPlanAction(
        targetPlan.id,
        action,
        controller.signal,
        selectedPluginIds,
      )) {
        setTrace((current) => [...current, event as unknown as TraceEvent]);
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
                <FolderGit2 size={16} />
                <span className="project-label">
                  <strong>{projectPathParts(project.root_path).name}</strong>
                  <small>{projectPathParts(project.root_path).parent}</small>
                </span>
                {project.root_path === workspace && <span className="project-current">当前</span>}
              </button>
            )) : <button className="empty-row workspace-picker" type="button" title="切换工作区" onClick={() => void changeWorkspace()} disabled={isStreaming || health.status !== "ready"}><FolderGit2 size={16} /><span>{workspace}</span><Pencil size={12} /></button>}
          </section>
          <section className="rail-section sessions-section">
            <div className="section-label"><span>会话</span></div>
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
              <p>{modelStatus.status === "ready" && !modelStatus.data.configured ? "模型尚未配置" : `LeanHarness ${version}`}</p>
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
                          currentPermission={permissionMode}
                        />
                      ) : message.content ? (message.role === "assistant" ? <Markdown content={message.content} /> : message.content) : "正在生成..."}
                    </div>
                    {message.attachments && message.attachments.length > 0 && (
                      <div className="message-attachments" aria-label="消息附件">
                        {message.attachments.map((attachment) => (
                          <div className="message-attachment" key={attachment.id}>
                            {attachment.kind === "image" && attachment.previewUrl
                              ? <img src={attachment.previewUrl} alt={attachment.filename} />
                              : attachment.kind === "image"
                                ? <ImageIcon size={16} />
                                : <FileCode2 size={16} />}
                            <span><strong>{attachment.filename}</strong><small>{formatBytes(attachment.byte_size)}</small></span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </article>
                </div>
              ))}
              {isStreaming && activeRunId && trace.some((event) => event.run_id === activeRunId && (event.type === "assistant.progress" || event.type.startsWith("tool.") || event.type.startsWith("approval.") || event.type.startsWith("input."))) && (
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
          <input
            ref={imageInput}
            className="visually-hidden"
            type="file"
            accept="image/png,image/jpeg,image/webp"
            multiple
            aria-label="选择图片附件"
            onChange={(event) => {
              void queueAttachments(event.target.files);
              event.target.value = "";
            }}
          />
          <input
            ref={textInput}
            className="visually-hidden"
            type="file"
            accept=".txt,.py,.ts,.tsx,.js,.jsx,.json,.yaml,.yml,.md,.css,.html,.htm,.sql,.java,.go,.rs,.toml,.xml,.sh,.bat,.ps1,.c,.h,.cpp,.hpp,.cs,text/plain,application/json"
            multiple
            aria-label="选择文本或代码附件"
            onChange={(event) => {
              void queueAttachments(event.target.files);
              event.target.value = "";
            }}
          />
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
          {pendingQuestion && (
            <div className="question-panel" role="group" aria-label="Agent 提问">
              <strong>{pendingQuestion.question}</strong>
              <div className="question-options">
                {pendingQuestion.options.map((option) => (
                  <button
                    type="button"
                    key={option.label}
                    disabled={questionSubmitting}
                    onClick={() => void answerQuestion(option.label)}
                  >
                    <span>{option.label}</span>
                    <small>{option.description}</small>
                  </button>
                ))}
              </div>
            </div>
          )}
          {pendingAttachments.length > 0 && (
            <div className="pending-attachments" aria-label="待发送附件">
              {pendingAttachments.map((item) => (
                <div className={`pending-attachment is-${item.status}`} key={item.localId}>
                  {item.previewUrl
                    ? <img src={item.previewUrl} alt="" />
                    : <FileCode2 size={17} />}
                  <span>
                    <strong title={item.file.name}>{item.file.name}</strong>
                    <small>{item.status === "uploading" ? "正在上传" : item.status === "error" ? item.error : formatBytes(item.file.size)}</small>
                  </span>
                  {item.status === "uploading" && <LoaderCircle className="attachment-spinner" size={15} aria-label="正在上传" />}
                  {item.status === "error" && (
                    <button type="button" className="icon-button compact" aria-label={`重试上传 ${item.file.name}`} title="重试" onClick={() => void retryAttachment(item)}><RotateCcw size={14} /></button>
                  )}
                  <button type="button" className="icon-button compact" aria-label={`移除附件 ${item.file.name}`} title="移除" disabled={item.status === "uploading"} onClick={() => void removePendingAttachment(item)}><X size={14} /></button>
                </div>
              ))}
            </div>
          )}
          {attachmentError && <div className="attachment-error" role="alert">{attachmentError}</div>}
              <textarea aria-label="任务输入" placeholder={modelStatus.status === "ready" && !modelStatus.data.configured ? "模型尚未配置" : mode === "plan" ? "描述需要完成的工作" : "输入一个仓库任务"} rows={2} value={input} maxLength={32_000} disabled={health.status !== "ready" || modelStatus.status !== "ready" || !modelStatus.data.configured || isStreaming || planLoading} onChange={(event) => setInput(event.target.value)} />
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
                  <button type="button" role="menuitem" onClick={() => { setComposerMenuOpen(false); imageInput.current?.click(); }}><ImageIcon size={15} /><span>上传图片</span><small>PNG、JPEG、WebP</small></button>
                  <button type="button" role="menuitem" onClick={() => { setComposerMenuOpen(false); textInput.current?.click(); }}><FileCode2 size={15} /><span>上传文本或代码</span><small>UTF-8 文件</small></button>
                  {plugins.filter((plugin) => plugin.enabled).length === 0 ? (
                    <button type="button" role="menuitem" disabled><Blocks size={15} /><span>没有已启用插件</span><small>使用 CLI 或 API 安装</small></button>
                  ) : plugins.filter((plugin) => plugin.enabled).map((plugin) => (
                    <button
                      type="button"
                      role="menuitemcheckbox"
                      aria-checked={selectedPluginIds.includes(plugin.id)}
                      className={selectedPluginIds.includes(plugin.id) ? "selected" : ""}
                      key={plugin.id}
                      title={`${plugin.description}\n工具：${plugin.tools.map((tool) => tool.name).join("、")}`}
                      onClick={() => togglePlugin(plugin.id)}
                    >
                      <Blocks size={15} /><span>{plugin.name}</span><small>v{plugin.version}</small>
                    </button>
                  ))}
                </div>
              )}
            </div>
            {selectedPluginIds.length > 0 && <span className="plugin-selection" title={selectedPluginIds.join(", ")}><Blocks size={12} />{selectedPluginIds.length} 个插件</span>}
            {pluginError && <span className="plugin-error" title={pluginError}>插件状态不可用</span>}
            <div className="composer-settings"><label htmlFor="permission-mode">权限</label><select id="permission-mode" value={permissionMode} onChange={(event) => void changePermission(event.target.value as PermissionMode)} disabled={isStreaming}><option value="inspect">只读检查</option><option value="approve">逐次批准</option><option value="unrestricted">受控直接执行</option></select></div><span className="composer-state">{isStreaming ? mode === "plan" ? "正在生成计划" : "Agent 正在执行" : modelStatus.status === "ready" && modelStatus.data.configured ? mode === "plan" ? "计划模式 · 本地保存" : "Agent · 本地保存" : `模型${modelCopy}`}</span>
            {isStreaming ? (
              <button className="send-button stop-button" type="button" aria-label="停止运行" title="停止运行" onClick={() => activeRequest.current?.abort()}><Square size={14} fill="currentColor" /></button>
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
        <div className="inspector-summary"><div><span>模型</span><strong title={modelName ?? undefined}>{modelCopy}</strong></div><div><span>本次运行权限</span><strong>{runPermissionMode ? permissionLabel(runPermissionMode) : "尚未运行"}</strong></div><div><span>会话默认权限</span><strong>{permissionLabel(permissionMode)}</strong></div></div>
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

function latestRunPermission(
  trace: Array<TraceEvent | SavedRunTrace | PersistedTraceEvent>,
): PermissionMode | null {
  for (const event of [...trace].reverse()) {
    if (
      !["run.started", "run.permission.updated"].includes(event.type)
      || !("metadata" in event)
      || !event.metadata
    ) continue;
    const value = event.metadata.permission_mode;
    if (value === "inspect" || value === "approve" || value === "unrestricted") return value;
  }
  return null;
}

function projectPathParts(path: string): { name: string; parent: string } {
  const normalized = path.replace(/[\\/]+$/, "");
  const separator = normalized.includes("\\") ? "\\" : "/";
  const parts = normalized.split(/[\\/]+/).filter(Boolean);
  const name = parts.at(-1) ?? normalized;
  const parent = parts.slice(-3, -1).join(separator) || normalized;
  return { name, parent };
}

function permissionLabel(permission: PermissionMode): string {
  if (permission === "inspect") return "只读检查";
  return permission === "approve" ? "逐次批准" : "受控直接执行";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export default App;
