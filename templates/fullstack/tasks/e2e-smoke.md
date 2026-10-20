---
title: End-to-end smoke test of the demo scenario
status: todo
owner:
deps: [fe-core, fe-auth, core-logic]
touches: [e2e]
prio: 2
---
## Goal
Automate the demo path from SPEC.md with Playwright (or equivalent).

## Acceptance criteria
- [ ] One command runs a browser test walking the entire demo scenario against seeded data, using the login users actually see (not a dev-only shortcut, unless that is what the demo uses)
- [ ] The tests were executed, not just listed: record the command, the stack it ran against and the result
- [ ] The documented startup for e2e and the test's login helper agree, including what logout leads to
- [ ] Failures are reported clearly; test is added to CI/`make check` if it is fast (<60s)

## Notes
