# LeanHarness

LeanHarness is a clean-room, local-first coding agent runtime written in
Python. The project is being developed from an empty repository for a software
engineering assessment.

## Current status

The foundation milestone provides an installable Python package, environment
diagnostics, a local FastAPI server, and a responsive React workspace shell.
Model calls, local tools, the agent loop, sessions, and plugins are
intentionally not implemented yet.

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
