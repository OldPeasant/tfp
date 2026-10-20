---
title: Write the API contract (openapi.yaml)
status: todo
owner:
deps: [spec, stack]
touches: [openapi.yaml]
prio: 1
---
## Goal
Contract-first so frontend and backend agents can work in parallel. This is the most important shared artifact.

## Acceptance criteria
- [ ] openapi.yaml (or equivalent) covers every endpoint the must-have stories need: paths, methods, request/response schemas, error shape, auth scheme
- [ ] Includes example payloads for each endpoint (used later for mocks and tests)
- [ ] Breaking changes after this task require a note in docs/ARCHITECTURE.md

## Notes
