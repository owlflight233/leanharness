# LeanHarness

LeanHarness is a clean-room, local-first coding agent runtime written in
Python. The project is being developed from an empty repository for a software
engineering assessment.

## Current status

The current milestone provides an installable Python package, environment
diagnostics, a provider-independent model gateway, ephemeral single-turn
streaming chat, and a bounded read-only inspection loop through both the CLI
and responsive local web interface. File mutation, shell execution, persistent
sessions, and plugins remain intentionally out of scope.

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
uv run leanharness run "Inspect this repository and explain its structure."
uv run leanharness serve
```

macOS or Linux:

```sh
export LEANHARNESS_MODEL_BASE_URL="https://api.deepseek.com"
export LEANHARNESS_MODEL_NAME="deepseek-chat"
export LEANHARNESS_MODEL_API_KEY="your-api-key"
uv run leanharness model check
uv run leanharness chat "Reply with one short sentence."
uv run leanharness serve
```

`LEANHARNESS_MODEL_API_KEY` may be omitted for a local endpoint that does not
require authentication. Plain HTTP is accepted only for `localhost`,
`127.0.0.1`, or `::1`. Chat is currently stateless and does not read or modify
the selected workspace. The `run` command and the Web inspection mode perform
read-only workspace inspection with bounded steps and no persistence.

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
- Plan and budget-aware modes built after the core runtime is correct.

See [SPEC.md](SPEC.md), [ARCHITECTURE.md](ARCHITECTURE.md), and
[CLEAN_ROOM.md](CLEAN_ROOM.md) before contributing.

## License

LeanHarness is available under the [MIT License](LICENSE).
