import {
  Activity,
  Blocks,
  Bot,
  ChevronDown,
  CircleDot,
  FolderGit2,
  Menu,
  PanelRight,
  Plus,
  Send,
  Settings,
  Square,
  TerminalSquare,
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

type InspectorTab = "plan" | "trace";
type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error" };
type MessageStatus = "streaming" | "complete" | "error" | "cancelled";

interface ConversationMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  status: MessageStatus;
}

interface AppProps {
  healthLoader?: HealthLoader;
  modelStatusLoader?: ModelStatusLoader;
  chatStreamer?: ChatStreamer;
}

function App({
  healthLoader = fetchHealth,
  modelStatusLoader = fetchModelStatus,
  chatStreamer = streamChat,
}: AppProps) {
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("plan");
  const [health, setHealth] = useState<LoadState<HealthResponse>>({ status: "loading" });
  const [modelStatus, setModelStatus] = useState<LoadState<ModelStatus>>({ status: "loading" });
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [trace, setTrace] = useState<TurnEvent[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
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
    const assistantId = nextMessageId.current++;
    const controller = new AbortController();
    activeRequest.current = controller;
    setMessages((current) => [
      ...current,
      { id: userId, role: "user", content: message, status: "complete" },
      { id: assistantId, role: "assistant", content: "", status: "streaming" },
    ]);
    setInput("");
    setTrace([]);
    setInspectorTab("trace");
    setIsStreaming(true);

    try {
      await chatStreamer(
        message,
        (event) => {
          setTrace((current) => [...current, event]);
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
      );
    } catch (error: unknown) {
      updateMessage(assistantId, (current) => ({
        ...current,
        content: current.content || (isAbortError(error) ? "已停止生成" : errorMessage(error)),
        status: isAbortError(error) ? "cancelled" : "error",
      }));
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
      setIsStreaming(false);
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
        <button className="primary-action" type="button" disabled title="持久会话尚未启用"><Plus size={17} /><span>新建会话</span></button>
        <nav className="rail-content" aria-label="项目与会话">
          <section className="rail-section">
            <div className="section-label"><span>项目</span><button className="icon-button compact" type="button" disabled aria-label="添加项目"><Plus size={14} /></button></div>
            <div className="empty-row" title={workspace}><FolderGit2 size={16} /><span>{workspace}</span></div>
          </section>
          <section className="rail-section sessions-section">
            <div className="section-label"><span>当前运行</span></div>
            <div className="empty-row muted"><Bot size={16} /><span>临时单轮对话</span></div>
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
          <div className="session-identity"><span className="session-title">临时对话</span><span className="session-subtitle">{workspace}</span></div>
          <div className="mode-select" aria-label="运行模式"><button type="button" className="mode-button active" disabled>单轮<ChevronDown size={14} /></button></div>
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
                  <div className="message-icon" aria-hidden="true">{message.role === "user" ? <UserRound size={16} /> : <Bot size={16} />}</div>
                  <div className="message-body">
                    <strong>{message.role === "user" ? "你" : modelName || "模型"}</strong>
                    <p>{message.content || "正在生成..."}</p>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <form className="composer" onSubmit={(event) => { event.preventDefault(); void submitMessage(); }}>
          <textarea aria-label="任务输入" placeholder={modelStatus.status === "ready" && !modelStatus.data.configured ? "请先配置模型环境变量" : "输入一条消息"} rows={2} value={input} maxLength={32_000} disabled={health.status !== "ready" || modelStatus.status !== "ready" || !modelStatus.data.configured || isStreaming} onChange={(event) => setInput(event.target.value)} />
          <div className="composer-actions">
            <span className="composer-state">{isStreaming ? "模型正在生成" : modelStatus.status === "ready" && modelStatus.data.configured ? "单轮对话 · 不保存历史" : `模型${modelCopy}`}</span>
            {isStreaming ? (
              <button className="send-button stop-button" type="button" aria-label="停止生成" title="停止生成" onClick={() => activeRequest.current?.abort()}><Square size={14} fill="currentColor" /></button>
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
            {trace.length === 0 ? <div className="panel-empty"><Activity size={18} /><strong>没有运行轨迹</strong><span>0 条事件</span></div> : <ol className="trace-list">{trace.map((event) => <li key={`${event.sequence}-${event.type}`}><span>{event.sequence}</span><strong>{event.type}</strong></li>)}</ol>}
          </div>
        )}
        <div className="inspector-summary"><div><span>模型</span><strong title={modelName ?? undefined}>{modelCopy}</strong></div><div><span>权限</span><strong>未启用</strong></div></div>
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

export default App;
