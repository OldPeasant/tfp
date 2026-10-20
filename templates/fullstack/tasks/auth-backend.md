---
title: Authentication and authorization (backend)
status: todo
owner:
deps: [data-model, api-contract]
touches: [backend/auth]
prio: 2
---
## Goal
Skip if SPEC.md says no login is needed: then `python tasks.py verify auth-backend <n> --skip "no login per SPEC.md"` for each criterion, and done.

## Acceptance criteria
- [ ] Register/login/logout (or a demo-user shortcut) per the contract; protected endpoints reject anonymous calls with the contract's error shape
- [ ] Passwords hashed; secrets from env, never committed
- [ ] Tests: happy path, wrong password, missing token

## Notes
