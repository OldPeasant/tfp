---
title: Frontend: login/registration and session handling
status: todo
owner:
deps: [fe-shell, auth-backend]
touches: [frontend/src/app/auth]
prio: 2
---
## Goal
Skip if there is no auth: `verify fe-auth <n> --skip "no login per SPEC.md"` for each criterion, then done.

## Acceptance criteria
- [ ] Login/register forms with validation and error display; token stored and sent; protected routes redirect
- [ ] Logout works; expired session handled: a failed token refresh or logout in another tab leads to the login page, and none of the previous user's data stays visible or cached

## Notes
