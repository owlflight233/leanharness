# LeanHarness Architecture

## Design position

LeanHarness is a modular monolith with a privileged runtime core. Capabilities
can be added at documented boundaries, but plugins cannot replace the state
machine, permission checks, or audit recorder.

This choice keeps the safety and control flow visible. It also distinguishes
the implementation from systems in which every runtime component is a plugin.

## Layers

```text
CLI / Web / Headless
        |
Application services
        |
Agent runtime
  - state machine
  - context assembler
  - action dispatcher
  - permission engine
  - plan controller
  - trace recorder
        |
Capabilities
  - model providers
  - built-in tools
  - storage
  - external tool processes
```

Interfaces translate user input and render state. They do not implement an
agent loop. Application services own projects, sessions, and runs. The runtime
owns transitions and is the only layer allowed to request model or tool work.

## Dependency rules

- Interfaces may depend on application services and public data contracts.
- Application services may depend on runtime interfaces and storage ports.
- Runtime modules must not depend on FastAPI, React, or interface code.
- Tools cannot bypass the permission engine.
- Model adapters cannot execute tools.
- Storage and tracing receive redacted domain data, never raw credentials.
- External plugins run out of process and receive only declared capabilities.

## Runtime flow target

The runtime uses explicit states:

```text
CREATED -> PREPARING -> REQUESTING_MODEL -> INTERPRETING
        -> WAITING_APPROVAL | EXECUTING_TOOL -> PREPARING
        -> COMPLETED | EXHAUSTED | FAILED | CANCELLED
```

Every transition will be deterministic from the current run state and a typed
input. Model calls and tools are effects requested by transitions rather than
hidden side effects inside state objects.

The runtime does not classify user language or infer task requirements. The
model receives the bounded public conversation and current tool definitions,
then chooses observations, mutations, verification, or the explicit
`report_run_outcome` control action. The fixed core only checks that a reported
completed outcome does not contradict observed tool facts; failed edits and
failed commands therefore cannot be presented as successful operations.

This is the central decision boundary: application services never rewrite a
user task, select a continuation target, or decide which kind of work the task
means. They create a run and supply its public history. The Agent Loop is the
only component that asks the model for the next action. Runtime policy remains
authoritative for safety, permissions, budgets, cancellation, and protocol
validity, but those controls do not infer the user's intent.

`inspect` registers bounded workspace and read-only Git tools. `approve` and
`unrestricted` additionally register guarded unified-diff patching and named
verification commands. Tool failures are returned to the model as structured
results; repeated calls, repeated equivalent failures, context pressure, and
step budgets are runtime decisions rather than model instructions.

Permission is evaluated at the start of each execution segment. The session
selector is a persistent default; an active segment keeps its captured mode.
Plan generation is always inspect-only. Confirming a plan captures the current
session permission, while resuming a paused plan explicitly captures the new
selection and appends a permission-transition event before any model or tool
work.

Runtime responsibilities are separated by purpose:

- `runtime/loop.py` coordinates effects and state transitions.
- `runtime/completion.py` owns observed evidence and terminal-outcome checks.
- `runtime/prompting.py` owns model-facing public constraints.
- `runtime/metrics.py` collects provider-neutral efficiency counters.
- `context/store.py` performs local evidence-preserving compaction.

## Data strategy

SQLite is the source of local business state for projects, sessions, messages,
runs, and ordered run events. A separate append-only JSONL trace is written for
each run under the application data directory. Both sinks receive the same
`TraceRedactor` output: credentials, hidden reasoning, and raw tool/file
content are excluded. Replaying traces can render what happened but does not
replace relational recovery. New runs in a session receive a bounded history of
recent public `user` and `assistant` messages, including plan messages (at most
24 messages and 32,000 characters). Tool results, progress events and hidden
reasoning are excluded. The current user message is appended after that
history. References
such as "continue" are resolved by the model from ordinary conversation
context, not by phrase allow-lists or an application-side task rewrite. The
model must still re-read the workspace before making claims.

Storage code separates immutable records, forward-only migrations, the shared
redaction policy, and SQLite operations into `storage/records.py`,
`storage/migrations.py`, `storage/redaction.py`, and `storage/store.py`.

The storage adapter applies numbered migrations, enables WAL and foreign keys,
and uses UUID identifiers with UTC ISO-8601 timestamps. Deleting a session
cascades its relational records and removes its run trace directory.

## Interface deployment

During development, Vite proxies `/api` to FastAPI. Production builds are
served by the same FastAPI process, so the browser uses same-origin requests
without broad CORS rules. The server listens only on loopback by default.

The React client renders safe Markdown separately from the run-process view.
Run-process lifecycle events are grouped by `tool_call_id`, so one request,
approval, start, and completion sequence is displayed as one semantic action.
Terminal metadata exposes model calls, tool calls, and token totals without
persisting prompts or tool output.
