# Repository Guidance

## Product boundary

- Implement agent logic in this repository. Do not add an agent framework or
  agent SDK.
- Do not copy code, prompts, schemas, event names, or plugin APIs from existing
  coding agents.
- Keep the state machine, permission engine, and trace redaction in the fixed
  core; plugins cannot replace them.
- Do not advertise a capability before its implementation and tests exist.

## Engineering rules

- Target Python 3.12 and typed Python interfaces.
- Keep runtime code independent of FastAPI and frontend code.
- Resolve workspace paths before using them and fail closed on ambiguity.
- Bound model, command, and tool output before adding it to context.
- Never log credentials, authorization headers, or the complete environment.
- Add focused tests with every behavioral change.
- Use small descriptive commits and never rewrite pushed history.

## Verification

Run the backend and frontend test suites relevant to a change. Before a public
release, install from a clean checkout, build the frontend, run all tests, and
exercise the local web application on desktop and mobile viewports.
