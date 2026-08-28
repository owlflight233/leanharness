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

The runtime infers deterministic evidence requirements before the first model
request. Inspection tasks need a successful workspace observation, mutation
tasks need a successful `workspace_patch`, and explicit verification tasks
need a successful command profile. A non-empty model answer is only a
completion candidate; the fixed core accepts or rejects it from this evidence
ledger. Failed edits therefore cannot be persisted as completed runs.

`inspect` registers bounded workspace and read-only Git tools. `approve` and
`unrestricted` additionally register guarded unified-diff patching and named
verification commands. Tool failures are returned to the model as structured
results; repeated calls, repeated equivalent failures, context pressure, and
step budgets are runtime decisions rather than model instructions.

Runtime responsibilities are separated by purpose:

- `runtime/loop.py` coordinates effects and state transitions.
- `runtime/completion.py` owns task requirements and completion evidence.
- `runtime/continuation.py` defines the bounded cross-run capsule.
- `runtime/prompting.py` owns model-facing public constraints.
- `runtime/metrics.py` collects provider-neutral efficiency counters.
- `context/store.py` performs local evidence-preserving compaction.

## Data strategy

SQLite is the source of local business state for projects, sessions, messages,
runs, and ordered run events. A separate append-only JSONL trace is written for
each run under the application data directory. Both sinks receive the same
`TraceRedactor` output: credentials, hidden reasoning, and raw tool/file
content are excluded. Replaying traces can render what happened but does not
replace relational recovery. Session history is presentation-only and is not
automatically replayed into a subsequent model request. Coding runs receive at
most one 4 KiB continuation capsule from the immediately preceding terminal
run. It contains only the previous task, terminal state, changed file names,
public incomplete/error reason, and current permission mode. The model must
still re-read the workspace before making claims.

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
