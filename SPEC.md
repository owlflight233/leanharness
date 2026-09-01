# LeanHarness Product Specification

## Goal

LeanHarness will be a local coding agent that can use a language model to read
and edit workspace files, run local commands, and complete programming tasks.
The model supplies decisions; LeanHarness owns the execution environment,
context construction, permissions, state transitions, and error handling.

## Assessment constraints

- Do not wrap an existing coding-agent product.
- Do not use an agent framework or agent SDK.
- Model vendor clients, OpenAI-compatible endpoints, and native tool calling
  are allowed.
- Do not use hosted code execution or hosted file tools.
- Implement conversation and context management, local tools, output parsing,
  loop termination, and error handling in this repository.
- Read credentials only from environment variables or ignored local files.
- Preserve the public Git history and do not rewrite pushed commits.

## Version 0.1 target

Version 0.1 will provide:

- an OpenAI-compatible model adapter;
- workspace-scoped file, search, patch, shell, and Git tools;
- a deterministic agent state machine with cancellation and bounded runs;
- inspect, workspace-write, and explicitly unrestricted permissions;
- SQLite-backed projects, sessions, plans, and tool-call records;
- redacted JSONL execution traces;
- standard and plan operating modes;
- CLI, local web, and one-shot headless interfaces;
- bounded image/text attachments and a documented local plugin extension
  boundary with the bundled controlled DOCX example.

## Foundation milestone

The first milestone is complete when:

- the Python package installs on Python 3.12;
- the CLI exposes version, doctor, and local server commands;
- the React client builds and reports backend health;
- the local server defaults to `127.0.0.1:4318`;
- backend and frontend tests pass from a clean checkout;
- no model, file mutation, shell, or session API is falsely advertised.

## Non-goals for the foundation milestone

- No model request is sent.
- No workspace file is modified by the application.
- No shell command is exposed to a model.
- No plugin is downloaded or auto-discovered. Explicitly installed local
  plugins cannot replace the fixed runtime, permission, or trace core.
- No conversation or run is persisted.
- No multi-agent orchestration is implemented.

## Later research

After the base agent works, the project will investigate model routing and a
Budget Mode that reduces tokens per successful task. These features must be
measured against task success and test results, not token count alone.
