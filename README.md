# TFP (Task Flow Protocol): git-native task board for human + AI-agent teams

Git is the source of truth. One file per task in `tasks/`, claimed atomically via `git push`.
Needs only Python 3 and git.

| File | Purpose |
|---|---|
| `tasks.py` | task CLI: `new list next claim verify done release redo extend split hold unhold drop undrop freeze unfreeze assume assumptions confirm reject heartbeat check status graph watch repos` |
| `board.py` | the dashboard behind `tasks.py watch` / `graph` |
| `repos.py` | `tasks.py repos`: work on several repos as folders of one repo, synced back to them |
| `templates/gitlab-ci-sync.yml` | GitLab CI job that runs the repo sync every 30 min |
| `agents.py` | launch/monitor N Claude agents, each in its own clone, looping over the board |
| `AGENTS.md` | protocol the agents read (copy into the project) |
| `templates/fullstack/tasks/` | 22-task starter set for a typical full-stack + AI app |
| `tests/test_publish.sh` | scenario tests: status changes never push unfinished code to main (`bash tests/test_publish.sh`) |
| `tests/test_*.py` | tests for evidence, check limits, `touches`, assumptions, over-budget tasks (`python3 tests/test_done_gate.py`, ...) |

## Contest quick start
The setup below is for work that spans several existing repos (e.g. frontend + backend on GitLab, maybe
a new Android app later): agents work in **one working repo** that contains the others as folders, and
the sync publishes each folder back to its original repo every 30 min. For a single repo, skip steps
3 and 6.

**0. Every laptop:** Python 3, git, Claude Code logged in (`claude`), and `ANTHROPIC_API_KEY` *not* set
(otherwise the agents bill the API instead of your subscription). Whoever runs `repos` commands also
needs `git subtree`: included with git on macOS/Windows/Debian; Fedora: `sudo dnf install git-subtree`.

**1. Create the working repo** on GitLab (empty, e.g. `org/contest`). Protect `main` only: TFP
force-pushes `wip/*`, `status/*` and `sync-status`.

**2. Add TFP and push:**
```bash
git clone git@gitlab.com:org/contest.git && cd contest
cp ../tfp/{tasks.py,agents.py,board.py,repos.py,AGENTS.md,.gitignore} .
git add -A && git commit -m "TFP" && git branch -M main && git push -u origin main
```

**3. Import the original repos as folders** (with their history; each command commits and pushes):
```bash
python tasks.py repos add backend  git@gitlab.com:org/backend.git
python tasks.py repos add frontend git@gitlab.com:org/frontend.git
# later, for a new app: let the agents build android/, create an empty GitLab repo, then
# python tasks.py repos add android git@gitlab.com:org/android.git
```

**4. Write the task board, the check and the brief:**
```bash
cp -r ../tfp/templates/fullstack/tasks .     # or: python tasks.py new <id> "<title>" --deps a,b
echo 'make -C backend check && make -C frontend check' > .taskcheck   # must pass before a task is done
# put the contest brief in BRIEF.md (the `spec` task reads it)
python tasks.py check && git add -A && git commit -m "task board" && git push
```
Edit the template first. The repos already exist, so drop or rewrite `repo-scaffold` and the tasks
that set up what is already there. Delete tasks you don't need (e.g. auth, AI), add domain-specific
ones, and remove deleted ids from other tasks' `deps`. Give each task `touches:` (the folders/files it
changes) and write acceptance criteria that say how they are verified ("passes against the real login",
"`docker build` succeeds"), see "Done means verified". Keep `.taskcheck` fast (< 2 min): it runs on
every `done` and every sync; `done` warns above 2 min and gives up after 15.

**5. Start agents** on each laptop (in a clone of the working repo):
```bash
python agents.py start 4                          # agents are named <host>/agent-N
python agents.py status
python tasks.py watch                             # live dashboard: open board.html, on any laptop
```

**6. Keep the original repos updated**, either way:
- **GitLab CI (recommended):** copy `templates/gitlab-ci-sync.yml` to `.gitlab-ci.yml` and follow its
  header: a `SYNC_TOKEN` variable, a pipeline schedule every 30 min, and tools for `.taskcheck`.
- **A laptop:** in a separate clone that nobody works in:
  `python tasks.py repos sync --every 30m`

`python tasks.py repos sync` publishes immediately (e.g. at a milestone), and `repos list` or the
dashboard shows when each repo was last updated.

## Several repos: how the sync works
Each sync round:
1. merges commits someone pushed directly to an original repo;
2. runs `.taskcheck` and publishes nothing if it fails;
3. pushes each changed folder to its repo, fast-forward, with the agents' commits and messages
   (no claim/heartbeat noise);
4. tags the synced commit (`sync-<time>`) and shows the state on the dashboard.

Cross-repo tasks stay single commits, and one `.taskcheck` covers all folders. A conflict with an
outside change blocks only that repo: resolve it with `python tasks.py repos pull <folder>` in a clean
clone, commit, push. Each push adds one small `Split '<folder>/'` merge commit to main, so the next
merge from that repo uses the right base. The sync resets its clone to origin every round, which is
why it needs a clone nobody works in (CI gets a fresh one).

## Watching the team
`python tasks.py watch` (in any clone, on any machine) fetches origin every 20 s (`-n` to change) and
rewrites `board.html`, which reloads itself. It never touches your working tree. The page shows:
- **agents** on every laptop: current task, a live one-line "doing" (`$ make check`, `editing
  backend/auth.py`, `plan: write login tests`), heartbeat health, tasks finished; offline/stopped agents
- **dependency graph**: status colors, owner, heartbeat stripe on active tasks, critical tasks in red
- **tasks table**: status, owner, what each is waiting on, time taken, attempts/takeovers/releases
- **activity feed**: claims, takeovers, redos, releases, finishes and code commits
- progress and the longest chain of tasks still to go

The "doing" line comes from the runner: it reads Claude's `stream-json` output as it arrives and
force-pushes a one-commit `status/<host>/agent-N` branch every 10-60 s, so main's history is untouched.
`--once` writes the page once; `tasks.py graph` writes it from your local checkout, plus `BOARD.md`.

## Done means verified
A task is done when each acceptance criterion was seen to work, not when the code looks finished. The
agent records the evidence per criterion, in the task file next to it:
```bash
python tasks.py verify api 2 "npx playwright test login against docker compose up -> 8 passed"
python tasks.py verify api 3 --skip "no Docker on this laptop"     # not met/verifiable here: why
```
`done` refuses while a criterion is unverified (`- [ ]`; `--force` overrides), and adds a line with the
check it ran, the commit and how many criteria were verified or skipped. Skipped criteria (`- [~]`) stay
visible in the file for a human to follow up. AGENTS.md "Evidence" tells agents what counts: the thing
the criterion names actually ran (browser tests executed, the real build path used), with command and
result; tests that were only found, or "should work", don't count.

`done` gives the check `TASK_CHECK_TIMEOUT` seconds (default 900), then kills it and everything it
started and refuses: a check that never ends verifies nothing. Above `TASK_CHECK_SLOW` (default 120 s)
it warns. The check's output is read as UTF-8 and printed safely on any console, so a failure is never
hidden behind an encoding error (e.g. a Windows cp1252 console).

## Tasks that touch the same files
`touches: [frontend/src/app/state, docs/adr]` in a task file (or `new --touches a,b`) names what it
changes. Paths overlap when one is inside the other. While someone works on a task, `next` and `claim`
(without an id) offer tasks that overlap it last and say why; claiming one anyway prints a heads-up
naming the other task. `tasks.py check` warns about unfinished tasks that overlap but may run in
parallel (neither depends on the other): add a dep, or split the files. Tasks without `touches` are
never treated as overlapping.

## Merge conflicts and the check
Put the project's validation command in `.taskcheck` (one line, e.g. `make check`; the `checks` task
creates it) or set `TASK_CHECK`. `tasks.py done` merges the latest main, runs it on the merged result, and
refuses if it fails. This catches both merge conflicts and clean merges that break the build. Agents
resolve conflicts following the steps in AGENTS.md; when a conflict isn't obvious, `tasks.py redo <id>`
saves the attempt as `wip/<id>-attemptN` and restarts the task from main, with the old attempt as a
reference. Abandoned attempts are kept after the task is done (the dashboard lists them per task, linked
to GitLab/GitHub's compare view), and no commits are ever squashed, so the history shows what happened.

## Crashes and leases
Agent claims carry a lease (`--lease`, default 20 min) that the runner renews every 5 min while it is alive,
and the runner backs up the agent's commits to `origin/wip/<task>`. If a laptop dies, its tasks turn
`stale` when the lease runs out and the next free agent takes them over, starting from `wip/<task>`.
If the dead laptop comes back, its runner notices the task was taken over, stops the session and parks
the local work on a `lost/<time>` branch in its clone instead of pushing it. `release` likewise moves the
releaser's unpushed commits to `wip/<task>` and pushes only the status change to main. `done`/`release` refuse
tasks you don't own (`--force` overrides). Human claims (no `TASK_LEASE`) never expire; free a dead
human's task with `python tasks.py release --force <id>`. Allow force-pushes to `wip/*`, `status/*` and `sync-status` but not to `main`.

## Tasks that take too long
Each agent gets `--task-budget` sessions per task (default 2, up to 30 min each). Then its next session
is a review instead of more coding. The agent must choose one of:
- `tasks.py extend`: nearly done, one more session (once).
- `tasks.py split --from <file>`: create 2-6 subtasks; the original then only integrates and verifies.
- `tasks.py release --note`: hand off; the task is still resumable (e.g. only finalization is left) but
  splitting it would be artificial, so another agent continues it from `wip/<task>`.
- `tasks.py hold`: stuck; a person is really needed (unclear requirement, missing dependency, a decision).

Being over budget alone never needs a human: a review that decides nothing releases the task with a
handoff note. The agent that gave it up then leaves it to other agents for `--avoid` seconds (default
300; it takes it back after that if nobody did, so a single agent never deadlocks). A Claude usage limit
is different: it releases with `--handoff`, which is not counted. A task goes on hold automatically after
being released 3 times (counted releases, so a task nobody can finish stops bouncing), after 3 failed
`redo` attempts, or when a subtask would be split again. Held tasks show at the top of the dashboard under **Needs a human**, with the reason. Fix the cause (clarify
SPEC.md, edit the task, add a missing task), then run `python tasks.py unhold <id>`. Agents pick it up
again within a minute, with a fresh budget.

## Changing the plan while agents work
An agent reads its task file when it claims the task, and never again. So you change the plan by changing
the board:
- **Not started (todo):** edit the task file, commit, push. Or `drop` / `hold` it.
- **In progress:** `hold <id> "<why>" --force` or `drop <id> --note "<why>" --force`. The agent's runner
  notices at its next heartbeat (within ~5 min, `--lease`/4), stops the session, and the work so far stays
  on `wip/<id>`. To change the spec of a running task: hold it, edit the task file, unhold: the next
  agent continues from `wip/<id>` with the new spec.
- **Done:** the code is on main. Add a task that changes or removes it.
- **Big replan:** `freeze "<reason>"` first. `claim` refuses while the board is frozen and runners
  wait (their dashboard card says "board frozen"); tasks already running carry on unless you hold/drop them.
  `unfreeze` when the new plan is pushed.

`drop` takes a task off the board (`dropped` status; `undrop` brings it back). Tasks that depend on a
dropped task wait under **Needs a human** until you edit their deps; `check` lists them.

Agents record every decision they took without a human with `tasks.py assume <id> "<text>"` (a line
in the task's Notes, pushed right away). `tasks.py assumptions` and the dashboard list the ones still to review,
newest first, numbered per task (`#n`). A human confirms a right one (`tasks.py confirm <id> <n>`, or `--all`) or
rejects a wrong one (`tasks.py reject <id> <n> --note "<why>"`, then edits or adds a task to change what was
built). Either way the line stays in the Notes, marked with who reviewed it, and leaves the list (`assumptions --all`
shows everything); the dashboard has Confirm / Reject buttons for the same.

## Usage limits
When an agent's Claude subscription runs out (e.g. the 5-hour limit), its runner notices and hands the
task back: uncommitted work is committed, `release` moves it to `wip/<task>` with a note, and an agent
on another laptop continues from there. The runner then waits until the limit resets (the time Claude
reports, else `--limit-wait`, default 15 min) and rejoins by itself; the dashboard shows it as
"usage limit". Sessions that fail 3 times in a row for other reasons (e.g. an expired login) are
handled the same way and show as "paused". Check the log in that case.

Humans can use the same commands (`next`, `claim`, `done`) or claim tasks for themselves with
`TASK_OWNER=alice`.
