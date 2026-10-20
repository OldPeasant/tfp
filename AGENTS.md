# Task protocol (read first)

Work is tracked in `tasks/*.md`, one file per task. Git is the only source of truth. Never edit the
`status`/`owner` fields by hand; use `python tasks.py`.

1. `python tasks.py next` - ready tasks, most-blocking first. Prefer the top one.
2. `python tasks.py claim <id>` - atomic. If it prints `LOST RACE`, go back to step 1.
   Do not start work before you see `CLAIMED`. If it says earlier unfinished work exists on
   `origin/wip/<id>` (its previous owner died or released it), merge that first and continue from it.
   If it prints `Heads-up: <other> ... touches the same files`, another agent is changing the same code
   right now: read its task, don't redo what it does, keep your changes there small, and pull often.
3. Implement only what the task's acceptance criteria say. Commit code often (the agent runner backs up
   your commits to `wip/<id>`) and run `git pull --rebase` every few commits, so conflicts with other
   agents show up early and small. Never `git push` to main yourself: `done` pushes your work once it
   passes the check, so main only ever holds finished tasks (it is published to the original repos).
4. Verify every acceptance criterion and record how (see "Evidence" below):
   `python tasks.py verify <id> <n> "<what you ran> -> <result>"` (`verify <id>` lists them, numbered).
5. `python tasks.py done <id>` when all criteria are met and verified. It refuses while a criterion is
   unverified, then merges the latest main, runs the project check (`.taskcheck`) on the merged result,
   and only then marks the task done and pushes. If the check fails, fix it and run `done` again. If it
   times out, see "Evidence". Then go to step 1.
6. Stuck or abandoning? Commit your work, then `python tasks.py release <id> --note "<what is done, what blocks>"`.
   Your unfinished commits go to `wip/<id>` for the next owner, not to main.
7. Missing task discovered? `python tasks.py new <id> "<title>" --deps a,b --touches <folders/files>`,
   commit, push. `--touches` names what the task will change, so agents avoid working on two tasks that
   touch the same files at the same time; `python tasks.py check` warns about such tasks without a dep.
8. If any command prints `REFUSED` or `LOST`, the task was taken over by someone else (your lease
   expired) or a human changed the plan (dropped it or put it on hold): stop working on it immediately.
9. If `next` or `claim` prints `FROZEN`, humans are changing the plan: finish a task you already own,
   but claim nothing new. Stop.
## Evidence
A criterion is met only when you saw it work. `verify` records what you ran and what came out, so a
human can trust a done task without redoing it:
- **Run what the criterion names.** "E2E tests pass" needs the browser tests run against the stack the
  criterion names (e.g. the real login, not a dev shortcut), "the image builds" needs the actual build
  path that CI or deployment uses (same runtime versions), "works on Windows" needs Windows. Tests that
  were found but not run, a passing unit suite for a browser criterion, a build with a different tool
  or runtime, or "should work" is not evidence.
- **Write the evidence so it can be repeated:** command, configuration, result
  (`npx playwright test --project=chromium against docker compose up -> 8 passed`).
- **Can't verify it here** (no browser, no Docker, a service you can't reach)? Don't tick it:
  `python tasks.py verify <id> <n> --skip "<why>"`, plus `python tasks.py assume <id> "..."` for what
  you assumed instead. If the task is pointless without it, `hold` it.
- **The check must finish.** `done` gives `.taskcheck` 15 min (`TASK_CHECK_TIMEOUT`) and warns above
  2 min. A check that times out verified nothing: if your change made it hang (a server left running, a
  test waiting for input), fix that; if the check itself is too slow, hold the task and say so.

## Quality gate before completing implementation tasks

For implementation tasks, the parent TFP worker owns the task from claim through completion.

Before running `tasks.py done`, the parent worker must complete this quality workflow:

1. Finish the implementation for the claimed task.
2. Invoke the `test-writer` sub-agent to:
    - inspect the task acceptance criteria and changed behavior;
    - add or improve relevant tests;
    - run focused tests and broader validation where practical;
    - report any production bugs it finds.
3. Invoke the `code-reviewer` sub-agent to:
    - review the implementation against the current task acceptance criteria;
    - check applicable project architecture, integration, security, tests, and maintainability;
    - report only validated, evidence-based findings.
4. If the test-writer or code-reviewer reports valid issues, invoke the `fixer` sub-agent to resolve them.
5. Re-run relevant tests after fixes.
6. If material fixes were made, re-run `code-reviewer` when useful to confirm the issues are resolved.
7. Record the evidence for each acceptance criterion with `tasks.py verify` (see "Evidence"), using
   what you and the helpers actually ran.
8. Run `.taskcheck`.
9. Only when required behavior is implemented, important findings are resolved, every criterion is
   verified (or skipped with a reason) and `.taskcheck` passes may the parent worker run `tasks.py done`.

### Quality sub-agent boundaries

`test-writer`, `code-reviewer`, and `fixer` are helper sub-agents within the current claimed task.

They are not TFP workers and must not:

- claim, release, hold, unhold, drop, or complete TFP tasks;
- run `tasks.py done`;
- push;
- change branches;
- merge or rebase;
- launch additional TFP workers;
- wait for human input.

If a quality sub-agent cannot proceed, it must return the reason to the parent worker.

The parent TFP worker remains responsible for:

- TFP task state;
- assumptions;
- holds;
- releases;
- deciding whether a finding is in scope;
- recording evidence (`tasks.py verify`);
- running final validation;
- completing the task.

#### Frontend implementation

For tasks that create or materially change a user-facing frontend or interface, use the available `frontend-design` plugin/skill when applicable.

Use it to establish and implement the visual direction of the interface before or during frontend implementation.

Do not invoke it for:

- backend-only tasks;
- infrastructure tasks;
- database-only tasks;
- documentation-only tasks;
- non-visual refactoring;
- tasks where the current design must remain unchanged.

The current task acceptance criteria, `SPEC.md`, and `docs/ARCHITECTURE.md` remain authoritative. Frontend Design may improve presentation and implementation quality, but it must not invent product requirements or expand the task scope.

## Assumptions
Humans may be asleep: don't wait for answers and don't stop because something is unclear. Choose the most
reasonable option that fits the spec, then record it right away:
`python tasks.py assume <id> "<what you assumed, and why>"`. One line per decision a human might want
to revisit (a guessed requirement, a library choice, a behavior the spec doesn't define, a shortcut you
took). Humans review them with `python tasks.py assumptions` and confirm or reject them; agents never do. Keep
working as if the assumption were true.

## Merge conflicts
Other agents change main while you work, so `git pull --rebase` (or `done`) can stop with CONFLICT.
Your work is never lost. Take the first step that fits:

a. **Obvious fix** (imports, both sides added lines, formatting, a rename): resolve, `git add`,
   `git rebase --continue`. Being sure isn't enough: the resolution only counts if the check passes,
   which `done` verifies.
b. **Many conflicts** (the same ones again for each commit): `git rebase --abort`, then
   `git merge origin/main`: you resolve once for the whole task instead of once per commit.
c. **Not obvious** (you'd have to guess what the other change meant, or the check keeps failing after
   merging): `python tasks.py redo <id>`. It saves your attempt as `wip/<id>-attemptN`, resets your
   clone to the latest main, and you keep the task. Re-implement it on top of the new code, using the
   old attempt as a reference (`git diff HEAD...origin/wip/<id>-attemptN`): reuse its design and
   tests, but don't merge it.
d. **The tasks disagree on design** (e.g. both changed the API contract differently): that is a spec
   problem, not a git problem. Run `redo` (c), then `python tasks.py release <id> --note "conflicts with
   <other task>: <what>"`, and add a task to reconcile them if one is missing.

A clean merge can still break things (another task renamed what you call). That's why `done` runs
the check after merging.

## Too big or stuck
A task should take 30-90 min. The runner gives each agent 2 sessions per task, then asks for a review.
You can also decide this earlier, as soon as you see it:
- **Nearly done:** `python tasks.py extend <id> --note "<what is left>"` (one more session, once).
- **Too big:** write the remaining work as 2-6 subtasks into a file, then
  `python tasks.py split <id> --from <file> --note "<what is done so far>"`. File format:
  ```
  ## auth-login: Login endpoint
  deps: auth-model
  Goal: POST /login returns a JWT for valid credentials.
  - [ ] endpoint + tests
  - [ ] wrong password -> 401
  ## auth-model: User model and password hashing
  - [ ] ...
  ```
  Ids: a-z, 0-9, `-`. The optional `deps:` line may name other subtasks or existing tasks. Your
  committed work goes to `wip/<id>` for the subtasks. The original task then waits for them; whoever
  claims it afterwards only integrates and verifies its acceptance criteria. Subtasks can't be split
  again: hand them off instead.
- **Hand off** (out of budget, but the task is still valid and resumable, e.g. only final review, the
  check or `done` is left, and splitting would be artificial): commit, then
  `python tasks.py release <id> --note "<what is done, exactly what is left>"`. Another agent continues
  from your commits. A review that decides nothing does this for you.
- **Stuck**, only when a person is really needed (unclear requirement, missing dependency, a decision is
  needed, the check fails for reasons outside your task): `python tasks.py hold <id> "<what a human must
  decide or do>"`. Agents won't pick it up until a human runs `python tasks.py unhold <id>`. Taking
  longer than planned is not a reason to hold.

## Several repos
If `repos.txt` exists, folders such as `backend/` and `frontend/` are other repos imported into this
one. Just work in the folders: a change touching several of them is one commit, and `tasks.py repos
sync` publishes each folder to its repo. Don't edit `repos.txt`, don't run `git subtree`, don't push
to those repos directly.

Humans change the plan with `drop`/`undrop` (a task is no longer wanted) and `freeze`/`unfreeze` (no new
claims while they replan). Agents don't use these.

Other commands: `list`, `status`, `check` (validates deps/cycles), `graph` (writes BOARD.md + board.html),
`heartbeat` (renews your leases; the agent runner does this for you), `redo` (see Merge conflicts),
`extend` / `split` / `release` / `hold` (see Too big or stuck).
