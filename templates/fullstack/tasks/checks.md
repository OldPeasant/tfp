---
title: Add test runner, linter and `make check`
status: todo
owner:
deps: [repo-scaffold]
touches: [Makefile, .taskcheck, .gitlab-ci.yml]
prio: 2
---
## Goal
Give every agent one command to validate its work before marking done.

## Acceptance criteria
- [ ] `make check` runs backend tests, frontend tests/typecheck and linters and exits non-zero on failure
- [ ] At least one trivial passing test on each side
- [ ] `.taskcheck` contains `make check`, so `tasks.py done` runs it on every task merged with the latest main
- [ ] `make check` is fast enough to run on every `done` (aim for < 2 min; slow e2e tests go elsewhere): record its time
- [ ] One lint contract: the developer lint command, CI and `make check` run the same linters with the same rules, including the frontend framework's template linting; warnings are either errors or reported, not hidden by `--quiet`
- [ ] Runtime versions (Node, JDK, Python...) are pinned in one place and every CI job and build tool that downloads its own runtime uses a version the installed dependencies support

## Notes
