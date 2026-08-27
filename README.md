# LeanHarness

LeanHarness is a clean-room, local-first coding agent runtime written in
Python. The project is being developed from an empty repository for a software
engineering assessment.

## Current status

The repository is at the foundation milestone. It defines the product scope,
architecture, clean-room boundary, and security rules. Model calls, local
tools, the agent loop, sessions, and plugins are intentionally not implemented
yet.

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
