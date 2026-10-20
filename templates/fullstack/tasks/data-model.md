---
title: Database schema and migrations
status: todo
owner:
deps: [api-contract, repo-scaffold]
touches: [backend/db]
prio: 1
---
## Goal
Model the entities from the contract.

## Acceptance criteria
- [ ] Schema/migrations create all tables needed by the contract; `make dev` applies them automatically
- [ ] Model layer/ORM classes exist with basic repository functions
- [ ] Test creates and reads back one row of each entity

## Notes
