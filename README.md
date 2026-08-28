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
diagnostics, a provider-independent model gateway, single-turn streaming chat,
a bounded coding loop, persistent local sessions, guarded unified-diff edits,
verification command profiles, read-only Git inspection, and a persistent Plan
Mode through both the CLI and responsive local web interface. Arbitrary shell
execution, Git writes, and plugins remain intentionally out of scope.

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
For DeepSeek, set process environment variables before running the CLI or
server. Never place a real key in `.env.example` or another tracked file.

PowerShell:

```powershell
$env:LEANHARNESS_MODEL_BASE_URL = "https://api.deepseek.com"
$env:LEANHARNESS_MODEL_NAME = "deepseek-chat"
$env:LEANHARNESS_MODEL_API_KEY = "your-api-key"
uv run leanharness model check
uv run leanharness chat "Reply with one short sentence."
uv run leanharness run "Inspect this repository and explain its structure." --permission inspect
uv run leanharness run "Fix the failing test." --permission approve
uv run leanharness plan "Refactor the authentication module"
uv run leanharness serve
```

macOS or Linux:

```sh
export LEANHARNESS_MODEL_BASE_URL="https://api.deepseek.com"
export LEANHARNESS_MODEL_NAME="deepseek-chat"
export LEANHARNESS_MODEL_API_KEY="your-api-key"
uv run leanharness model check
uv run leanharness chat "Reply with one short sentence."
uv run leanharness run "Fix the failing test." --permission approve
uv run leanharness plan "Refactor the authentication module"
uv run leanharness serve
```

`LEANHARNESS_MODEL_API_KEY` may be omitted for a local endpoint that does not
require authentication. Plain HTTP is accepted only for `localhost`,
`127.0.0.1`, or `::1`. Chat and coding runs persist public messages,
summaries, and redacted event traces locally, but old messages are never
replayed into a later model request. A coding run may receive one bounded
public capsule from the immediately preceding run so short follow-ups such as
permission changes retain their task reference without importing full history.
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
plans run sequentially with the session permission and can be paused and
explicitly resumed after a service restart:

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

For frontend development, keep the Python server on port 4318 and run
`pnpm dev` from `frontend/`; Vite proxies `/api` to the local server.

## Verify

```sh
uv run ruff check src tests
uv run pytest
uv build
cd frontend
pnpm typecheck
pnpm test
pnpm build
```

## Direction

- A fixed, auditable agent runtime rather than an agent framework wrapper.
- One application service shared by CLI, web, and headless interfaces.
- Local workspace tools guarded by explicit permission checks.
- Inspectable execution traces without storing credentials.
- Persistent Plan Mode built on the same evidence and permission runtime.

See [SPEC.md](SPEC.md), [ARCHITECTURE.md](ARCHITECTURE.md), and
[CLEAN_ROOM.md](CLEAN_ROOM.md) before contributing.

## License

LeanHarness is available under the [MIT License](LICENSE).
