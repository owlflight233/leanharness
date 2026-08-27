import {
  Activity,
  Blocks,
  Bot,
  ChevronDown,
  CircleDot,
  FileCode2,
  FolderGit2,
  Menu,
  PanelRight,
  Plus,
  Send,
  Settings,
  TerminalSquare,
  X,
} from "lucide-react";
import { useState } from "react";

type InspectorTab = "plan" | "trace";

function App() {
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("plan");

  return (
    <div className="app-shell">
      <aside className={`project-rail ${leftOpen ? "is-open" : ""}`} aria-label="项目导航">
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true">
            <TerminalSquare size={19} strokeWidth={2} />
          </div>
          <div className="brand-copy">
            <strong>LeanHarness</strong>
            <span>本地工作台</span>
          </div>
          <button
            className="icon-button mobile-only"
            type="button"
            title="关闭项目导航"
            aria-label="关闭项目导航"
            onClick={() => setLeftOpen(false)}
          >
            <X size={18} />
          </button>
        </div>

        <button className="primary-action" type="button" disabled title="会话功能尚未启用">
          <Plus size={17} />
          <span>新建会话</span>
        </button>

        <nav className="rail-content" aria-label="项目与会话">
          <section className="rail-section">
            <div className="section-label">
              <span>项目</span>
              <button className="icon-button compact" type="button" disabled aria-label="添加项目">
                <Plus size={14} />
              </button>
            </div>
            <div className="empty-row">
              <FolderGit2 size={16} />
              <span>尚未选择工作区</span>
            </div>
          </section>

          <section className="rail-section sessions-section">
            <div className="section-label">
              <span>最近会话</span>
            </div>
            <div className="empty-row muted">
              <Bot size={16} />
              <span>尚无会话</span>
            </div>
          </section>
        </nav>

        <div className="rail-footer">
          <button className="nav-button" type="button" disabled>
            <Blocks size={17} />
            <span>扩展</span>
          </button>
          <button className="nav-button" type="button" disabled>
            <Settings size={17} />
            <span>设置</span>
          </button>
        </div>
      </aside>

      <main className="workspace">
        <header className="workspace-header">
          <button
            className="icon-button mobile-only"
            type="button"
            title="打开项目导航"
            aria-label="打开项目导航"
            onClick={() => setLeftOpen(true)}
          >
            <Menu size={19} />
          </button>
          <div className="session-identity">
            <span className="session-title">新会话</span>
            <span className="session-subtitle">未选择工作区</span>
          </div>
          <div className="mode-select" aria-label="运行模式">
            <button type="button" className="mode-button active" disabled>
              标准
              <ChevronDown size={14} />
            </button>
          </div>
          <button
            className="icon-button mobile-only"
            type="button"
            title="打开检查器"
            aria-label="打开检查器"
            onClick={() => setRightOpen(true)}
          >
            <PanelRight size={19} />
          </button>
        </header>

        <section className="conversation" aria-label="会话内容">
          <div className="conversation-empty">
            <div className="empty-glyph">
              <FileCode2 size={22} />
            </div>
            <h1>准备连接本地服务</h1>
            <p>LeanHarness 0.1.0.dev0</p>
          </div>
        </section>

        <form className="composer" onSubmit={(event) => event.preventDefault()}>
          <textarea aria-label="任务输入" placeholder="输入编程任务" rows={2} disabled />
          <div className="composer-actions">
            <span className="composer-state">基础架构阶段</span>
            <button className="send-button" type="submit" aria-label="发送任务" disabled>
              <Send size={17} />
            </button>
          </div>
        </form>
      </main>

      <aside className={`inspector ${rightOpen ? "is-open" : ""}`} aria-label="运行检查器">
        <div className="inspector-heading">
          <div className="inspector-tabs" role="tablist" aria-label="检查器视图">
            <button
              type="button"
              role="tab"
              aria-selected={inspectorTab === "plan"}
              className={inspectorTab === "plan" ? "active" : ""}
              onClick={() => setInspectorTab("plan")}
            >
              计划
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={inspectorTab === "trace"}
              className={inspectorTab === "trace" ? "active" : ""}
              onClick={() => setInspectorTab("trace")}
            >
              轨迹
            </button>
          </div>
          <button
            className="icon-button mobile-only"
            type="button"
            title="关闭检查器"
            aria-label="关闭检查器"
            onClick={() => setRightOpen(false)}
          >
            <X size={18} />
          </button>
        </div>

        {inspectorTab === "plan" ? (
          <div className="inspector-body" role="tabpanel">
            <div className="panel-empty">
              <CircleDot size={18} />
              <strong>没有活动计划</strong>
              <span>0 / 0</span>
            </div>
          </div>
        ) : (
          <div className="inspector-body" role="tabpanel">
            <div className="panel-empty">
              <Activity size={18} />
              <strong>没有运行轨迹</strong>
              <span>0 条事件</span>
            </div>
          </div>
        )}

        <div className="inspector-summary">
          <div>
            <span>模型</span>
            <strong>未配置</strong>
          </div>
          <div>
            <span>权限</span>
            <strong>未启用</strong>
          </div>
        </div>
      </aside>

      <footer className="status-bar" aria-label="运行状态">
        <span className="status-item">
          <span className="status-dot pending" />
          服务未连接
        </span>
        <span className="status-divider" />
        <span className="status-item">工作区：未选择</span>
        <span className="status-spacer" />
        <span className="status-item">v0.1.0.dev0</span>
      </footer>

      {(leftOpen || rightOpen) && (
        <button
          className="scrim mobile-only"
          type="button"
          aria-label="关闭面板"
          onClick={() => {
            setLeftOpen(false);
            setRightOpen(false);
          }}
        />
      )}
    </div>
  );
}

export default App;
