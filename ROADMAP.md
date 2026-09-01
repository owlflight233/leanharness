# Roadmap

## 0.1 - Base coding agent

- OpenAI-compatible model gateway
- Workspace tools and permission enforcement
- SQLite sessions and JSONL traces
- Explicit agent state machine
- Standard and Plan modes
- CLI, web, and headless interfaces
- Local controlled plugin protocol and bundled DOCX artifact example

## 0.2 - Fixed parallel analysis

- One parent CodingAgent can delegate up to five independent read-only analyses
- Workers reuse the core loop with isolated context and no nested delegation
- Structured, redacted evidence returns to the parent in request order
- No automatic routing, model profiles, or user-tuned delegation parameters

Future model routing and adaptive delegation remain deliberate follow-up work,
not part of the fixed first implementation.

## 0.3 - Budget Mode

- Per-run token, cost, and request budgets
- Context deduplication and diff-first file updates
- Cost-aware selection between deterministic and semantic context compaction
- Adaptive model escalation
- Delegation benefit estimation
- Standard-versus-budget evaluation reports

## 1.0 - Stable local runtime

- Hardened plugin protocol compatibility and security review
- Security review and release installation tests
- Stable configuration and migration policy
- Published benchmark and token-efficiency results
