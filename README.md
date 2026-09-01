# LeanHarness

LeanHarness is a local-first Python coding-agent runtime with a same-origin
web client. The current release supports persistent sessions, a bounded model
gateway, a read-only inspection loop, and controlled coding execution.

For the permission model and execution limits, see
[`CONTROLLED_EXECUTION.md`](CONTROLLED_EXECUTION.md).
To understand the implementation in request-flow order, see
[`READING_GUIDE.md`](READING_GUIDE.md).

## Current status

The current milestone provides an installable Python package, environment
diagnostics, a provider-independent model gateway, a bounded coding loop,
persistent local sessions, guarded workspace edits, verification command
profiles, read-only Git inspection, persistent Plan Mode, bounded image/text
attachments, and a local controlled plugin protocol through both the CLI and
responsive web interface. Arbitrary shell execution, Git writes, plugin
downloads, and plugin-owned agent loops remain out of scope.

## Prerequisites

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 or newer
- pnpm 11 or newer
- Git

## Run from a source checkout

PowerShell:

```powershell
git clone https://github.com/owlflight233/leanharness.git
Set-Location leanharness
uv sync --dev
Set-Location frontend
pnpm install --frozen-lockfile
pnpm build
Set-Location ..
uv run leanharness doctor
uv run leanharness serve
```

macOS or Linux:

```sh
git clone https://github.com/owlflight233/leanharness.git
cd leanharness
uv sync --dev
cd frontend
pnpm install --frozen-lockfile
pnpm build
cd ..
uv run leanharness doctor
uv run leanharness serve
```

Open `http://127.0.0.1:4318`. Use another workspace with:

```sh
uv run leanharness serve --workspace /path/to/project
```

## Configure a model

LeanHarness currently targets the OpenAI-compatible chat completions protocol.
Non-secret settings can be saved once in the local application data directory;
CLI commands and the Web server then reuse them after restart. Credentials
remain process environment variables and are never written to `model.json`.

PowerShell:

```powershell
uv run leanharness model configure `
  --base-url "https://api.deepseek.com" `
  --name "deepseek-v4-flash-vision-exp" `
  --thinking enabled `
  --reasoning-effort high
$env:DEEPSEEK_API_KEY = "your-api-key"
uv run leanharness model status
uv run leanharness model check
uv run leanharness serve
```

macOS or Linux:

```sh
uv run leanharness model configure \
  --base-url "https://api.deepseek.com" \
  --name "deepseek-v4-flash-vision-exp" \
  --thinking enabled \
  --reasoning-effort high
export DEEPSEEK_API_KEY="your-api-key"
uv run leanharness model status
uv run leanharness model check
uv run leanharness serve
```

Environment variables can override the saved non-secret settings for one
process. Never place a real key in `.env.example` or another tracked file.

PowerShell:

```powershell
$env:LEANHARNESS_MODEL_BASE_URL = "https://api.deepseek.com"
$env:LEANHARNESS_MODEL_NAME = "deepseek-v4-flash-vision-exp"
$env:LEANHARNESS_MODEL_THINKING = "enabled"
$env:LEANHARNESS_MODEL_REASONING_EFFORT = "high"
$env:LEANHARNESS_MODEL_API_KEY = "your-api-key"
uv run leanharness model check
uv run leanharness run "Inspect this repository and explain its structure." --permission inspect
uv run leanharness run "Fix the failing test." --permission approve
uv run leanharness plan "Refactor the authentication module"
uv run leanharness serve
```

macOS or Linux:

```sh
export LEANHARNESS_MODEL_BASE_URL="https://api.deepseek.com"
export LEANHARNESS_MODEL_NAME="deepseek-v4-flash-vision-exp"
export LEANHARNESS_MODEL_THINKING="enabled"
export LEANHARNESS_MODEL_REASONING_EFFORT="high"
export LEANHARNESS_MODEL_API_KEY="your-api-key"
uv run leanharness model check
uv run leanharness run "Fix the failing test." --permission approve
uv run leanharness plan "Refactor the authentication module"
uv run leanharness serve
```

`LEANHARNESS_MODEL_API_KEY` may be omitted for a local endpoint that does not
require authentication. Plain HTTP is accepted only for `localhost`,
`127.0.0.1`, or `::1`. Coding runs persist public messages, summaries, and
redacted event traces locally. A new run in the same session receives a bounded
history of recent public user, assistant, and plan messages (up to 24 messages
and 32,000 characters). The current message is sent verbatim; the model uses
the complete public history to resolve references. Tool results, progress
events, and hidden reasoning are excluded.
Workspace changes are disabled in
`inspect`, require per-call confirmation in `approve`, and execute directly in
`unrestricted` while still enforcing tool-level path, command, timeout, and
output limits.

## Local data

Sessions and audit records use SQLite plus append-only JSONL traces. The default
location is `%LOCALAPPDATA%\\LeanHarness` on Windows,
`~/Library/Application Support/LeanHarness` on macOS, and
`${XDG_DATA_HOME:-~/.local/share}/leanharness` on Linux. Set
`LEANHARNESS_DATA_DIR` or pass `--data-dir` for a portable/test location.

Plan Mode first generates a limited Markdown draft using only read-only tools.
Review or edit the steps in the web client, then confirm execution. Confirmed
plans are projected into one continuous `CodingAgent` loop with a permission
snapshot taken at confirmation time. Markdown steps are visible context and
audit records; they do not create separate model loops or per-step budgets.
Plan Mode has no model-request ceiling by default. It ends through the model's
outcome action, cancellation, a safety failure, or a runtime invariant such as
repeated identical actions. A paused plan can be explicitly resumed with the
newly selected permission; the transition is recorded in its audit trace. The
final report is rendered from step state and public evidence, rather than
concatenated model-written step reports:

```sh
uv run leanharness plan "Refactor the authentication module"
uv run leanharness plan show PLAN_ID
uv run leanharness plan confirm PLAN_ID
uv run leanharness plan resume PLAN_ID
```

Session commands are available from the CLI:

```sh
uv run leanharness session list
uv run leanharness session new --title "Repository review"
uv run leanharness session rename SESSION_ID "New title"
uv run leanharness session delete SESSION_ID
```

Local plugins are installed only from an explicit local directory. The bundled
`plugins/leanharness-docx` example can generate a validated DOCX artifact through
the existing permission and approval runtime:

```sh
uv run leanharness plugin install plugins/leanharness-docx
uv run leanharness plugin enable leanharness-docx
uv run leanharness plugin list
uv run leanharness plugin disable leanharness-docx
uv run leanharness plugin remove leanharness-docx
```

The plugin protocol is LeanHarness-owned and versioned; it is not MCP-compatible.
Plugins run in a bounded child process and cannot access model credentials,
session storage, traces, or arbitrary workspace paths.

For frontend development, keep the Python server on port 4318 and run
`pnpm dev` from `frontend/`; Vite proxies `/api` to the local server.

## Verify

```sh
uv run ruff check src tests evals
uv run pytest
uv build
cd frontend
pnpm typecheck
pnpm test
pnpm build
```

Real-model evaluations always use disposable system temporary directories and
never target the selected Web project or normal local data directory. Scenarios
must be selected explicitly to avoid accidental API charges:

```sh
uv run python -m evals.runner --list
uv run python -m evals.runner --scenario create_tested_project
```

Evaluation reports contain outcome, evidence, latency, calls, tool failures, and
token metrics. Generated source, command output, credentials, and final answer
text are excluded.

## Direction

- A fixed, auditable agent runtime rather than an agent framework wrapper.
- One application service shared by CLI, web, and headless interfaces.
- Local workspace tools guarded by explicit permission checks.
- Inspectable execution traces without storing credentials.
- Persistent Plan Mode built on the same evidence and permission runtime.

See [SPEC.md](SPEC.md), [ARCHITECTURE.md](ARCHITECTURE.md),
[CONTEXT.md](CONTEXT.md), and [CLEAN_ROOM.md](CLEAN_ROOM.md) before
contributing.

## License

LeanHarness is available under the [MIT License](LICENSE).
