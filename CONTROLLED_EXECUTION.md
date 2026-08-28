# Controlled Coding Execution

LeanHarness keeps the runtime, permission policy, and trace redaction in the
fixed Python core. The model can request a tool, but it cannot bypass the
workspace boundary or approval policy.

## Permission modes

- `inspect` registers workspace listing, bounded reads, literal search, and
  read-only Git inspection.
- `approve` also registers patch and verification commands. Each mutating call
  pauses in `WAITING_APPROVAL` and must be approved individually. Rejection is
  returned to the model as a recoverable tool result.
- `unrestricted` runs the registered guarded tools directly. It still enforces
  path checks, UTF-8 and size limits, command profiles, timeouts, and output
  bounds. It is not an OS-level sandbox.

## Patch and command boundaries

`workspace_patch` accepts a single unified diff. All hunks are parsed and
validated in memory before temporary files and atomic replacement are used.
Target hashes are captured for approval and checked again before writing.

`workspace_command` accepts only named verification profiles and an argument
array. It invokes processes with `shell=False`, a minimal environment, bounded
stdout/stderr, cancellation-aware process-tree termination, and a finite
timeout. Install, download, arbitrary interpreters, and shell control operators
are not accepted. These controls reduce the command surface but do not provide
OS-level network or process isolation.

`git_inspect` supports only `status`, `diff`, `log`, and `show`; it never writes
Git state or contacts a remote.

## Public trace

The API emits `approval.required` and `approval.resolved` events. Persistent
SQLite/JSONL records contain tool names, normalized metadata, result status,
hashes, and error codes, but never raw patches, source contents, command
output, credentials, or hidden reasoning.

## Completion evidence

A model answer is a completion candidate, not the final authority. The runtime
requires a successful observation for inspection work, a successful patch for
requested changes, and a successful command profile when the task explicitly
requests verification. If required evidence is missing, the runtime continues
or emits `run.incomplete` on the reserved summary round.

Terminal events include aggregate model-call, tool-call, and token counters.
The web client groups request, approval, start, and completion lifecycle events
for one `tool_call_id` into one displayed action.
