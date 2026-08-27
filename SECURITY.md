# Security Policy

## Current status

LeanHarness is under active development and is not yet a complete coding agent.
Do not use the development version on an untrusted repository or expose its
local server to a network.

## Security invariants

- The server binds to `127.0.0.1` unless the user explicitly overrides it.
- Workspace access is confined to a resolved project root.
- Inspect mode cannot mutate the workspace. Plan mode is not yet exposed.
- Missing approval channels and unknown permission states deny an action.
- Subprocesses have time, output, and cancellation bounds.
- External plugins run in child processes with explicit permissions.
- Credentials never enter Git, model-visible traces, or frontend responses.

## Credentials

Credentials may come from environment variables or ignored local
configuration. Example files contain placeholders only. If a credential is
committed, revoke it immediately; deleting a later commit is not sufficient.

## Logging and traces

Log structured event names and safe metadata. Do not log request headers,
cookies, access tokens, API keys, or the entire process environment. Paths and
tool output will be bounded and redacted before persistence.

## Reporting

Until a private reporting channel is published, open a GitHub issue containing
only non-sensitive reproduction details. Never include a credential or private
repository content in an issue.
