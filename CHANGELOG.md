# Changelog

All notable changes to LeanHarness are recorded here. The project is still in
development and the `0.1.0.dev0` version does not promise a stable plugin API.

## 0.1.0.dev0

### Added

- Local Python runtime with an explicit, inspectable Agent Loop.
- OpenAI-compatible model gateway with streaming tool calls.
- Workspace-scoped inspection, patch, command, and read-only Git tools.
- `inspect`, `approve`, and `unrestricted` permission snapshots with interactive
  approval for guarded mutations.
- Persistent projects, sessions, plans, runs, and redacted JSONL traces.
- Replayable context projection with deterministic and bounded semantic
  compaction.
- Plan Mode with Markdown parsing, confirmation, pause, resume, and step
  evidence.
- CLI and same-origin React web client with Markdown answers and collapsible
  execution actions.

### Deliberately deferred

- Third-party plugin loading and a stable plugin protocol.
- Sub-agents, remote synchronization, and Budget Mode.

### Verification

- Python 3.12 package installation and `leanharness doctor` from a clean clone.
- Backend Ruff and pytest, frontend typecheck/test/build, and package build.
- Browser checks on desktop and mobile viewports.
