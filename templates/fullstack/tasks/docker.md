---
title: Dockerfile / docker-compose for one-command run
status: todo
owner:
deps: [repo-scaffold]
touches: [Dockerfile, docker-compose.yml, frontend/Dockerfile, backend/Dockerfile]
prio: 2
---
## Goal
Dockerfile / docker-compose for one-command run.

## Acceptance criteria
- [ ] `docker compose build` and `docker compose up` actually ran: db, backend, frontend start and the app is usable on one URL (record the commands and what you opened)
- [ ] Images build with the same runtime versions as CI, installing from the committed lockfiles (no `--force`/`--legacy-peer-deps`)
- [ ] Config through env vars; no dev-only paths

## Notes
