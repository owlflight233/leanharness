# LeanHarness 代码阅读指南

这份指南按一次 Agent 请求实际经过的顺序解释代码。建议先顺着主路径读，
不要一开始逐行阅读所有适配器。

## 1. 先理解公共数据

从以下文件开始：

1. `models/contracts.py`：模型消息、工具调用、用量和模型响应。
2. `runtime/events.py`：Web、CLI 和存储共同使用的公开运行事件。
3. `runtime/state.py`：允许的状态和状态转换。
4. `tools/contracts.py`：工具成功与失败如何统一返回。

这些类型是模块之间的边界。模型适配器、Runtime、CLI 和 Web 不应通过
隐式字典共享私有状态。

## 2. 跟踪一次 Coding Run

入口位于 `application/agent_gateway.py:create_coding_run`。它只负责校验公共
输入、加载模型配置和组装 Runtime，不执行 Agent 循环。

核心流程位于 `runtime/loop.py:CodingAgent.run`：

```text
校验任务
  -> 组装系统约束、公开会话历史和当前任务
  -> 请求模型
  -> 解析工具调用
  -> 权限/审批
  -> 执行工具并登记证据
  -> 接受完成、继续或进入预算总结轮
```

为了避免把所有判断堆在循环里，相关规则分别在：

- `runtime/completion.py`：记录已发生的工具事实，并拒绝与事实冲突的完成声明。
- `runtime/outcome.py`：模型可调用的显式完成/未完成控制契约。
- `runtime/metrics.py`：模型次数、工具次数和 token 用量。
- `runtime/model_step.py`：一次模型请求的投影、压缩恢复和协议修正信号。
- `runtime/prompting.py`：语言和能力约束，不含隐藏思维提示。
- `runtime/recovery.py`：协议纠正、相同调用和等价工具失败的有界恢复。
- `runtime/tool_dispatch.py`：工具预览、受控执行及安全错误转换。
- `context/store.py`：活跃上下文超限时将旧工具结果替换为证据胶囊。

关键原则是：模型只能提出“完成候选”。是否完成由本地证据决定。

## 3. 理解工具和权限

`permissions/policy.py` 决定某个权限模式是否能看到工具，以及是否需要审批。
`tools/registry.py` 是统一分发器，负责将异常转换成安全的结构化错误。

- `tools/workspace.py`：列目录、读文本、字面量搜索和路径边界。
- `tools/controlled.py:WorkspaceWriteTool`：安全创建或完整替换单个文本文件。
- `tools/controlled.py:WorkspaceEditTool`：使用完整文件哈希进行有界行编辑。
- `tools/controlled.py:WorkspacePatchTool`：复杂多 hunk 变更的 unified diff 工具。
- `tools/controlled.py:WorkspaceCommandTool`：只运行命名命令配置。
- `tools/controlled.py:GitInspectTool`：只读 Git 操作。

模型不能直接访问文件系统或进程。即使是 `unrestricted`，它也只是不需要
逐次人工批准，路径、参数、超时和输出限制仍然有效。

## 4. 理解会话和审计

`application/session_gateway.py` 把 Runtime 事件转换为公开会话记录，并为下次
运行提供有界的公开历史。持久化实现按职责拆分：

- `storage/records.py`：不可变记录类型。
- `storage/migrations.py`：只向前执行的 SQLite 迁移。
- `storage/redaction.py`：SQLite 与 JSONL 共用的脱敏规则。
- `storage/store.py`：事务、查询和 trace 文件写入。

完整源码、diff、命令输出、密钥和隐藏思维都不进入公开 trace。会话历史用于
界面恢复，也会以有界的公开 user/assistant/plan 消息注入下一次模型请求。
当前消息始终原样追加，应用层不识别“继续”等短语，也不改写任务。

## 5. 理解两个界面

`cli/main.py` 和 `web/app.py` 都调用相同的 application service。它们只负责
输入输出、流式连接和生命周期清理，不拥有完成规则。

前端从 `frontend/src/App.tsx` 开始：

- `api/` 负责 HTTP 和 NDJSON 契约。
- `components/Markdown.tsx` 负责安全富文本。
- `components/RunProcess.tsx` 按 `tool_call_id` 聚合生命周期事件，并展示运行指标。

调试一次真实运行时，依次对照浏览器 NDJSON、SQLite 的 `run_events` 和对应
JSONL。三者应具有相同的公开事件顺序，但都不应包含原始工具内容。

## 6. 修改代码时的检查顺序

1. 先确定变化属于模型协议、Runtime 规则、工具、存储还是界面。
2. 在所属模块增加最小回归测试。
3. 检查失败是否会变成结构化错误，而不是异常泄漏或误报完成。
4. 检查新字段能否安全进入公开 trace。
5. 运行 Ruff、pytest、前端类型检查、前端测试和生产构建。

真实模型行为不要只靠手工点击判断。`evals/scenarios.py` 定义隔离任务，
`evals/runner.py` 在系统临时目录中运行真实 Runtime，并只输出完成状态、证据、
错误码、调用次数、延迟和 token 等有界指标。评测不会使用当前 Web 项目，且必须
显式选择场景，避免意外调用付费 API。

Plan Mode 已复用同一个 CodingAgent；插件、子 Agent 和 Budget Mode 仍是后续能力。
