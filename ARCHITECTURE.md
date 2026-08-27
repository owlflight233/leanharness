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

The future runtime will use explicit states:

```text
CREATED -> PREPARING -> REQUESTING_MODEL -> INTERPRETING
        -> WAITING_APPROVAL -> EXECUTING_TOOL -> PREPARING
        -> COMPLETED | FAILED | CANCELLED
```

Every transition will be deterministic from the current run state and a typed
input. Model calls and tools are effects requested by transitions rather than
hidden side effects inside state objects.

## Data strategy target

SQLite will be the source of business state. A separate append-only JSONL trace
will support inspection and demonstration. Replaying traces will render what
happened but will not replace relational recovery of sessions and plans.

## Interface deployment

During development, Vite proxies `/api` to FastAPI. Production builds are
served by the same FastAPI process, so the browser uses same-origin requests
without broad CORS rules. The server listens only on loopback by default.
