import {
  Activity,
  Blocks,
  Bot,
  CircleDot,
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
import { streamRun, type RunEvent, type RunStreamer } from "./api/run";
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

type InspectorTab = "plan" | "trace";
type RunMode = "chat" | "inspect";
type TraceEvent = TurnEvent | RunEvent;
type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error" };
type MessageStatus = "streaming" | "complete" | "incomplete" | "error" | "cancelled";

interface ConversationMessage {
  id: number;
  role: "user" | "assistant" | "progress";
  content: string;
  status: MessageStatus;
}

interface AppProps {
  healthLoader?: HealthLoader;
  modelStatusLoader?: ModelStatusLoader;
  chatStreamer?: ChatStreamer;
  runStreamer?: RunStreamer;
  sessionClient?: SessionClient;
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
}

function App({
  healthLoader = fetchHealth,
  modelStatusLoader = fetchModelStatus,
  chatStreamer = streamChat,
  runStreamer = streamRun,
  sessionClient = defaultSessionClient,
}: AppProps) {
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("plan");
  const [health, setHealth] = useState<LoadState<HealthResponse>>({ status: "loading" });
  const [modelStatus, setModelStatus] = useState<LoadState<ModelStatus>>({ status: "loading" });
  const [mode, setMode] = useState<RunMode>("chat");
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [trace, setTrace] = useState<Array<TraceEvent | SavedRunTrace | PersistedTraceEvent>>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("inspect");
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
    input.trim().length > 0 &&
    input.length <= 32_000;

  async function submitMessage() {
    if (!canSubmit) return;
    const message = input;
    const userId = nextMessageId.current++;
    const assistantId = mode === "chat" ? nextMessageId.current++ : null;
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
    setTrace([]);
    setInspectorTab("trace");
    setIsStreaming(true);

    try {
      if (mode === "chat" && assistantId !== null) {
        await chatStreamer(
          message,
          (event) => {
            setTrace((current) => [...current, event]);
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
            if (event.session_id && event.session_id !== resolvedSessionId) {
              resolvedSessionId = event.session_id;
              setSessionId(event.session_id);
              window.localStorage.setItem("leanharness.session", event.session_id);
            }
            if (event.type === "assistant.progress") {
              appendMessage("progress", event.summary, "complete");
            } else if (event.type === "run.completed") {
              appendMessage("assistant", event.answer, "complete");
            } else if (event.type === "run.incomplete") {
              appendMessage(
                "assistant",
                event.answer || event.summary || "运行预算已用完，任务尚未完成",
                "incomplete",
              );
            } else if (event.type === "run.failed") {
              appendMessage("assistant", event.error.message, "error");
            } else if (event.type === "run.cancelled") {
              appendMessage("assistant", "已停止运行", "cancelled");
            }
          },
          controller.signal,
          24,
          activeSessionId ?? undefined,
        );
      }
    } catch (error: unknown) {
      const content = isAbortError(error)
        ? mode === "chat"
          ? "已停止生成"
          : "已停止运行"
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
      setIsStreaming(false);
      await refreshSessionList(resolvedSessionId);
    }
  }

  function appendMessage(
    role: ConversationMessage["role"],
    content: string,
    status: MessageStatus,
  ) {
    const id = nextMessageId.current++;
    setMessages((current) => [...current, { id, role, content, status }]);
  }

  function selectMode(nextMode: RunMode) {
    if (isStreaming || nextMode === mode) return;
    setMode(nextMode);
    setTrace([]);
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
          role: message.role,
          content: message.content,
          status: (message.status as MessageStatus) || "complete",
        })),
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
      window.localStorage.setItem("leanharness.session", id);
      setSessionError(null);
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
            <div className="section-label"><span>项目</span><button className="icon-button compact" type="button" disabled aria-label="添加项目"><Plus size={14} /></button></div>
            <div className="empty-row" title={workspace}><FolderGit2 size={16} /><span>{workspace}</span></div>
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
          <div className="session-identity"><span className="session-title">{sessions.find((item) => item.id === sessionId)?.title || (mode === "chat" ? "新会话" : "只读检查")}</span><span className="session-subtitle">{workspace}</span></div>
          <div className="mode-select" role="group" aria-label="运行模式">
            <button type="button" className={`mode-button ${mode === "chat" ? "active" : ""}`} aria-pressed={mode === "chat"} disabled={isStreaming} onClick={() => selectMode("chat")}>单轮</button>
            <button type="button" className={`mode-button ${mode === "inspect" ? "active" : ""}`} aria-pressed={mode === "inspect"} disabled={isStreaming} onClick={() => selectMode("inspect")}>检查</button>
          </div>
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
                <article key={message.id} className={`message message-${message.role} is-${message.status}`}>
                  <div className="message-icon" aria-hidden="true">{message.role === "user" ? <UserRound size={16} /> : message.role === "progress" ? <Activity size={16} /> : <Bot size={16} />}</div>
                  <div className="message-body">
                    <strong>{message.role === "user" ? "你" : message.role === "progress" ? "行动" : modelName || "模型"}</strong>
                    <p>{message.content || "正在生成..."}</p>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <form className="composer" onSubmit={(event) => { event.preventDefault(); void submitMessage(); }}>
          <textarea aria-label="任务输入" placeholder={modelStatus.status === "ready" && !modelStatus.data.configured ? "请先配置模型环境变量" : mode === "chat" ? "输入一条消息" : "输入一个仓库分析任务"} rows={2} value={input} maxLength={32_000} disabled={health.status !== "ready" || modelStatus.status !== "ready" || !modelStatus.data.configured || isStreaming} onChange={(event) => setInput(event.target.value)} />
          <div className="composer-actions">
            <div className="composer-settings"><label htmlFor="permission-mode">权限</label><select id="permission-mode" value={permissionMode} onChange={(event) => void changePermission(event.target.value as PermissionMode)} disabled={isStreaming}><option value="inspect">只读</option><option value="approve">人类批准（当前仍只读）</option><option value="unrestricted">完全权限（当前仍只读）</option></select></div><span className="composer-state">{isStreaming ? mode === "chat" ? "模型正在生成" : "Agent 正在检查" : modelStatus.status === "ready" && modelStatus.data.configured ? mode === "chat" ? "单轮对话 · 本地保存" : "只读检查 · 本地保存" : `模型${modelCopy}`}</span>
            {isStreaming ? (
              <button className="send-button stop-button" type="button" aria-label={mode === "chat" ? "停止生成" : "停止运行"} title={mode === "chat" ? "停止生成" : "停止运行"} onClick={() => activeRequest.current?.abort()}><Square size={14} fill="currentColor" /></button>
            ) : (
              <button className="send-button" type="submit" aria-label="发送任务" disabled={!canSubmit}><Send size={17} /></button>
            )}
          </div>
        </form>
      </main>

      <aside className={`inspector ${rightOpen ? "is-open" : ""}`} aria-label="运行检查器">
        <div className="inspector-heading">
          <div className="inspector-tabs" role="tablist" aria-label="检查器视图">
            <button type="button" role="tab" aria-selected={inspectorTab === "plan"} className={inspectorTab === "plan" ? "active" : ""} onClick={() => setInspectorTab("plan")}>计划</button>
            <button type="button" role="tab" aria-selected={inspectorTab === "trace"} className={inspectorTab === "trace" ? "active" : ""} onClick={() => setInspectorTab("trace")}>轨迹</button>
          </div>
          <button className="icon-button mobile-only" type="button" title="关闭检查器" aria-label="关闭检查器" onClick={() => setRightOpen(false)}><X size={18} /></button>
        </div>
        {inspectorTab === "plan" ? (
          <div className="inspector-body" role="tabpanel"><div className="panel-empty"><CircleDot size={18} /><strong>没有活动计划</strong><span>单轮对话不创建计划</span></div></div>
        ) : (
          <div className="inspector-body" role="tabpanel">
            {trace.length === 0 ? <div className="panel-empty"><Activity size={18} /><strong>没有运行轨迹</strong><span>0 条事件</span></div> : <ol className="trace-list">{trace.map((event) => <li key={`${"run_id" in event ? event.run_id : "turn"}-${event.sequence}-${event.type}`}><span>{event.sequence}</span><strong title={traceLabel(event)}>{traceLabel(event)}</strong></li>)}</ol>}
          </div>
        )}
        <div className="inspector-summary"><div><span>模型</span><strong title={modelName ?? undefined}>{modelCopy}</strong></div><div><span>权限</span><strong>{mode === "inspect" ? "只读" : "未启用"}</strong></div></div>
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

export default App;
