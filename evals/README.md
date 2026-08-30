# LeanHarness evaluations

This directory contains end-to-end scenarios for the real fixed-core runtime.
Every run creates a system temporary directory with separate `workspace/` and
`data/` children and removes it after verification. It never uses the repository,
the selected Web project, or the normal LeanHarness data directory as an evaluation
target.

List scenarios without calling a model:

```powershell
uv run python -m evals.runner --list
```

Run one explicitly selected scenario:

```powershell
uv run python -m evals.runner `
  --scenario create_tested_project `
  --output eval-results/create-tested-project.json
```

No scenario runs by default, which prevents accidental API charges. Reports contain
terminal states, evidence counts, error codes, latency, model/tool calls, approvals,
token usage, and a final-answer hash. They do not contain generated source, tool
output, credentials, or the final answer text.
