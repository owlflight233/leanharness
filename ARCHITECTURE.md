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
        -> EXECUTING_TOOL -> PREPARING
        -> COMPLETED | EXHAUSTED | FAILED | CANCELLED
```

Every transition will be deterministic from the current run state and a typed
input. Model calls and tools are effects requested by transitions rather than
hidden side effects inside state objects.

The current implementation enables only the inspect permission mode and three
workspace-scoped tools: `workspace_list`, `workspace_read`, and
`workspace_search`. Tool failures are returned to the model as structured
results; repeated calls, context pressure, and step budgets are runtime
decisions rather than model instructions.

## Data strategy

SQLite is the source of local business state for projects, sessions, messages,
runs, and ordered run events. A separate append-only JSONL trace is written for
each run under the application data directory. Both sinks receive the same
`TraceRedactor` output: credentials, hidden reasoning, and raw tool/file
content are excluded. Replaying traces can render what happened but does not
replace relational recovery. Session history is presentation-only and is not
automatically added to a subsequent model request.

The storage adapter applies numbered migrations, enables WAL and foreign keys,
and uses UUID identifiers with UTC ISO-8601 timestamps. Deleting a session
cascades its relational records and removes its run trace directory.

## Interface deployment

During development, Vite proxies `/api` to FastAPI. Production builds are
served by the same FastAPI process, so the browser uses same-origin requests
without broad CORS rules. The server listens only on loopback by default.
