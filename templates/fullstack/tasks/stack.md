---
title: Decide tech stack, write ARCHITECTURE.md
status: todo
owner:
deps: [spec]
touches: [docs/ARCHITECTURE.md]
prio: 1
---
## Goal
Pick the simplest stack the team already knows. Prefer boring: one language per side, SQLite/Postgres, no microservices.

## Acceptance criteria
- [ ] docs/ARCHITECTURE.md: chosen stack with runtime versions, folder layout (backend/, frontend/), how to run, ports, env vars, one paragraph per major component
- [ ] Where shared frontend state lives (one owner per kind of data) and how concurrent requests/writes are handled
- [ ] Decision favors speed of development and AI-agent familiarity over novelty

## Notes
