# Clean-room Development Policy

## Purpose

LeanHarness is an independent implementation. Public coding-agent products may
inform product requirements and engineering questions, but their source code
is not an implementation input.

## Allowed references

- Public product documentation and user-visible behavior.
- Published protocol specifications such as HTTP, JSON Schema, and Git.
- Language and framework documentation.
- General software-engineering patterns such as state machines, dependency
  inversion, subprocess isolation, and structured logging.

## Prohibited inputs

- Copying or translating source from DeepSeek Harness, Codex, Claude Code,
  OpenCode, or another coding agent.
- Recreating Cordis or a third-party agent framework API.
- Copying internal type names, event vocabularies, package layouts, prompts, or
  plugin contracts from another agent.
- Using an agent SDK to provide the core loop, context, tools, or delegation.
- Presenting ordinary dependency code as original LeanHarness work.

## Independent design record

Important decisions must be explained in repository documentation and visible
in small Git commits. New agent behavior begins with a local requirement and a
testable contract. When public documentation inspires a product requirement,
the implementation must still be derived from LeanHarness interfaces and
constraints.

## Dependency policy

Ordinary infrastructure dependencies are allowed when they do not implement an
agent. Initial examples are FastAPI, Uvicorn, HTTPX, React, Vite, and test
libraries. Dependencies and licenses will be recorded by lock files and release
notices.
