# Plan Mode

Plan Mode separates planning from execution. A request is inspected with the
read-only workspace tools and the model must return a deliberately small
Markdown format: an optional level-one heading followed by a consecutive
ordered list. LeanHarness parses this text immediately into structured steps;
free-form prose is never treated as an implicit plan.

Generated plans start in `AWAITING_CONFIRMATION`. They are editable until the
user confirms them. Confirmation captures the session permission mode and runs
all enabled steps through one `CodingAgent` and one persisted run. Each step
has an evidence requirement inferred from its instruction: analysis needs a
workspace observation, changes need a successful patch, and verification needs
an allowed command. A step cannot be marked complete without that evidence.

Plans are local records in the same SQLite database as sessions and runs.
Runtime events are appended to the run trace after redaction. A process restart
pauses plans that were running; resuming is always explicit and never invokes a
model automatically during startup. Plan history is display-only and is not
injected into ordinary future conversations.

The CLI supports `leanharness plan TASK`, `show`, `confirm`, `reject`, `resume`,
and `cancel`. The web client exposes the same lifecycle and optimistic version
checks for draft edits. Plan Mode does not provide plugins, subagents, remote
synchronization, or a separate budget policy.
