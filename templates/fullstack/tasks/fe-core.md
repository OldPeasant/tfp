---
title: Frontend: main screens for the must-have stories
status: todo
owner:
deps: [fe-shell, core-api]
touches: [frontend/src/app/pages, frontend/src/app/state]
prio: 1
---
## Goal
Build each must-have screen against the real API. Follow the state rules in docs/ARCHITECTURE.md.

## Acceptance criteria
- [ ] Every must-have story from SPEC.md is completable in the UI
- [ ] Loading, empty and error states on each screen; a failed load ends the spinner, shows a message and can be retried
- [ ] A slower, older request (search, filter, paging, selection) never overwrites a newer result; tested by answering in reverse order
- [ ] Shared data, loading and error state live in one place (store/service) per the architecture, not in each component
- [ ] Saved preferences/caches read back safely: invalid or unavailable storage is ignored, the screen still works; double submits don't create duplicates
- [ ] Component tests for the key interactions

## Notes
