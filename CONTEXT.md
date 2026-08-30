# Context Projection And Compaction

LeanHarness keeps task decisions inside the Agent Loop. Context management does
not classify intent, rewrite a user message, choose a tool, or decide whether a
run is complete. It only builds a bounded, protocol-valid factual view for one
model request.
Completion remains an evidence boundary: the runtime accepts a completed
outcome only after at least one successful workspace observation and after
mutation or verification failures have been resolved.

## Data flow

```text
SQLite public messages + redacted run evidence
                     |
                     v
             ContextSource records
                     |
current system/task -> ContextJournal
                     |
                     v
              ContextProjector
          deterministic tool capsules
          optional semantic JSON summary
                     |
                     v
             ContextProjection
                     |
                     v
                 ModelRequest
```

The persistent records and the projected model context have different jobs.
SQLite and JSONL retain public facts for display and recovery. A projection is
a disposable request view. Raw file reads, diffs, command output, credentials,
and hidden reasoning are never reconstructed from persistent storage.

## Projection rules

- The current system contract is first.
- Public session messages retain stable `message:<uuid>` source identifiers.
- Completed runs contribute bounded evidence with stable
  `run:<uuid>:evidence` identifiers.
- Persistent history is admitted as complete run groups: a user task, its
  redacted run evidence, and its public answer are retained or evicted
  together at the history budget boundary.
- The current run is excluded from its history seed and appended once through
  the live journal.
- The active user task remains verbatim in every projection and is never
  replaced by a semantic summary.
- Assistant tool calls and their tool results remain adjacent and paired.
- The current task and the two most recent assistant/tool steps are protected.
- A projection records only counts, generation, and a SHA-256 digest in traces;
  the request body is not persisted.

## Compaction levels

Deterministic compaction runs first. Old tool payloads become evidence capsules
containing safe metadata such as tool name, relative path, status, line or match
counts, content hash, and a re-read hint. It never cuts a message in the middle
or removes one side of a tool-call pair.

If the result still exceeds the hard character budget, the configured model may
summarize an old, complete prefix. The request has no tools, uses a separate
1,536-token output budget, and does not consume a run step. Its response must be
bounded JSON containing objective, constraints, decisions, observations,
changed files, verification, blockers, and pending actions. Invalid, unsafe, or
unavailable summaries fall back to deterministic capsules. Protected context
that still cannot fit fails with `CONTEXT_BUDGET_EXCEEDED`.

A valid semantic summary is cached by the stable IDs and hashes of the replaced
contiguous segment. Repeated model requests reuse it. If both historical turns
and older live steps must be reduced, one model request may compact them as
separate segments without crossing the active task boundary. At most three
semantic compactions can occur in one run.

## Provider overflow recovery

The OpenAI-compatible adapter classifies explicit provider context-window
errors separately from other protocol failures. The runtime then performs one
forced semantic projection and retries the task request once, but only when the
projection digest changed. Authentication, rate-limit, timeout, network, and
ordinary protocol errors are not retried by this path.

## Security boundary

The application adapter derives history only from records already processed by
`TraceRedactor`. Semantic summary fields are validated, bounded, sanitized, and
restricted to workspace-relative paths. Context trace events contain no source
text, prompt, tool output, diff, command output, credential, or environment.
