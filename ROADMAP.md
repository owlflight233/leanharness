# Roadmap

## 0.1 - Base coding agent

- OpenAI-compatible model gateway
- Workspace tools and permission enforcement
- SQLite sessions and JSONL traces
- Explicit agent state machine
- Standard and Plan modes
- CLI, web, and headless interfaces
- Out-of-process tool plugin protocol

## 0.2 - Delegated workers

- Focused subagents with independent context budgets
- Strong, balanced, and fast logical model routes
- Structured delegation results and one-level delegation limit
- Routing based on risk, complexity, and observed failures

## 0.3 - Budget Mode

- Per-run token, cost, and request budgets
- Context deduplication and diff-first file updates
- Cost-aware selection between deterministic and semantic context compaction
- Adaptive model escalation
- Delegation benefit estimation
- Standard-versus-budget evaluation reports

## 1.0 - Stable local runtime

- Versioned external plugin protocol
- Security review and release installation tests
- Stable configuration and migration policy
- Published benchmark and token-efficiency results
