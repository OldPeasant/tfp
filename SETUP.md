# Setup: from repos + brief to autonomous agents

For the team lead. Open Claude Code in the folder that contains `tfp/` and say:
**"Guide me through tfp/SETUP.md, one phase at a time."**

Claude: work through the phases in order. Do each step yourself where you can; ask me only for
decisions, credentials and GitLab clicks. Keep the conversation going until phase F is done: this
takes a few hours, most of it in phases C and D. Don't start phase D before I approve SPEC.md.

## A. Plumbing (30 min)
1. Check every tool: `python3 --version`, `git subtree -h` (Fedora: `sudo dnf install git-subtree`),
   `claude --version`, and that `ANTHROPIC_API_KEY` is not set. If the project's tests will need Docker
   (e.g. Testcontainers), `docker info` must answer: start Docker Desktop and let it start at login.
2. I create an empty GitLab repo for the working repo (e.g. `org/contest`) and protect only `main`.
   Ask me for its URL and the URLs of the original repos.
3. Follow README.md "Contest quick start", steps 2–3: clone, copy TFP, `repos add` for each repo.
4. Copy `tfp/TEAM.md` into the repo root and fill in its `<working-repo-url>`.
5. Commit `.claude/settings.json`, so that headless agents may run what they need (anything not
   allowed is denied without asking). Adjust the list to the stack:
   ```json
   {"permissions": {
     "allow": ["Bash(python tasks.py:*)", "Bash(python3 tasks.py:*)", "Bash(git:*)", "Bash(make:*)",
               "Bash(npm:*)", "Bash(npx:*)", "Bash(pytest:*)", "Bash(ls:*)", "Bash(cat:*)",
               "Agent", "Task", "Skill"],
     "deny":  ["Bash(git push:*)"]
   }}
   ```
   Keep helper invocation and skill permissions when adapting this list. Helper tools may be
   exposed as Agent or Task depending on the installed Claude Code version; verify the actual
   tool used during the dry run. Skill permits the frontend-design workflow when applicable.
   These permissions do not replace the requirement to invoke the helpers.

   The deny rule enforces "agents never push": `tasks.py done` pushes for them. `agents.py` passes this
   file with `--settings`, because Claude Code ignores it in the agents' clones (untrusted folders).
   It also applies to Claude in your own clone: from now on you push, Claude commits. Once agents run,
   they push to main every minute, so push with a retry loop:
   `for i in 1 2 3 4 5 6; do git pull -q --rebase && git push && break; sleep 3; done`.
6. Install the TFP quality sub-agents into the working repo:

   ```sh
   mkdir -p .claude/agents
   cp tfp/templates/claude-agents/code-reviewer.md .claude/agents/code-reviewer.md
   cp tfp/templates/claude-agents/test-writer.md .claude/agents/test-writer.md
   cp tfp/templates/claude-agents/fixer.md .claude/agents/fixer.md
   ```

   These are helper sub-agents used inside the lifecycle of a claimed TFP task.

   They must not claim, release, hold, complete, or otherwise manipulate TFP tasks. They must not push, change branches, launch additional TFP workers, or wait for human input.

   Verify that all three files exist under `.claude/agents/` before continuing.
   Ensure AGENTS.md explicitly requires the parent worker to:
   - invoke test-writer and code-reviewer for every implementation task;
   - invoke fixer when validated findings require fixes;
   - commit their reports in reviews/ together with the task's code, including no-findings reports;
   - record the task id, reviewed Git scope, findings, and validation results in each report;
   - preserve task-scoped coverage results and significant gaps in the test report, even when raw
     coverage output is Git-ignored (or record the concrete reason it could not be measured);
   - resolve a required helper invocation failure or hold the task, rather than silently skipping
     the helper and marking the task done.
   File existence alone does not prove that headless workers can invoke these helpers.
7. Ensure the official `frontend-design` plugin is available in Claude Code. Install or enable Anthropic's official Frontend Design plugin for the Claude account used by the autonomous workers. 
   Verify that the plugin is available in Claude Code before starting the dry run. Frontend Design is used only for tasks involving user-facing frontend or UI implementation. It must not be treated as a requirement for backend-only, infrastructure, documentation, or other non-visual tasks. If the plugin is unavailable, report it during setup rather than blocking TFP itself.
8. Install and verify a mandatory review-artifact completion gate in the generated working repo.
   This is a setup deliverable, not a reminder for workers. If the copied TFP version does
   not provide this gate, implement it and its tests before starting autonomous workers.
   Do not describe the gate as installed merely because AGENTS.md mentions reviews.

   Use a consistent artifact contract in AGENTS.md and all three helper definitions:
   - Every implementation task writes reviews/<task-id>/tests.md and reviews/<task-id>/review.md,
     including when there are no findings or no additional tests are needed.
   - If validated findings require fixes, the fixer writes reviews/<task-id>/fixes.md. Otherwise
     the parent records "fixer not required" with the reason in the task's quality record.
   - Each helper writes its own report. The parent verifies and commits the reports with the work;
     it must not fabricate reports to satisfy the gate.
   - Reports identify the task, scope of code examined, commands and results, and findings with
     their disposition. The test report includes task-scoped coverage and material gaps, or the
     specific reason measurement was unavailable. Raw HTML/JSON coverage can remain Git-ignored.
   - Commit a machine-readable reviews/<task-id>/quality.json record, with a documented schema:
     task id, reviewed code revision, helper completion results, report paths, whether fixes were
     required, finding dispositions, and final validation results. Never store credentials or
     private conversation transcripts in these artifacts.

   Integrate validation into tasks.py done for the specific task being completed:
   - Check committed artifacts, not just files in the working directory. Require nonempty reports
     and valid metadata matching that task; reject placeholders and missing required fields.
   - Require successful test-writer and code-reviewer completion, and a fixer report when fixes
     were required. Reject unresolved blocking findings or failed required validation.
   - Check freshness against the final task code after merging main. Allow report-only and task
     bookkeeping commits after review; relevant code changes require renewed review/validation.
     Define the compared scope explicitly so concurrent task-state commits do not invalidate it.
   - On failure, leave the task unfinished and do not publish it as done; print the exact missing
     or stale evidence and how to resolve it. Keep existing lease, merge, and project-check guards.
   - Normal worker completion, including skip-check or force options, must not bypass this gate.
     Scope it to the task being completed, not all other workers' unfinished tasks.
   - Default tasks to requiring these artifacts. Any non-implementation exemption must be
     explicitly classified and justified during task planning, not silently inferred by a worker.

   Add automated tests in an isolated temporary Git repository with no real remote writes:
   missing/empty reports, uncommitted reports, wrong-task metadata, stale evidence, missing fixer
   evidence when required, unresolved findings, attempted bypass, and a valid complete report set.
   Verify both a failed completion leaving task state unchanged and a successful completion.
   Document that this gate enforces recorded evidence: file presence alone cannot prove the
   quality or authenticity of an independent review. The dry run must also inspect actual helper
   invocations.

## B. Understand the code (30–60 min)
1. Read `backend/` and `frontend/`. Write `docs/ARCHITECTURE.md`: stack, an "Install" section for
   Linux/macOS/Windows (teammates' agents run the tests on their laptops), how to run and test each part,
   env vars, and the API between frontend and backend.
2. Make one fast check (< 2 min) that runs lint + tests of all folders, and put the command in
   `.taskcheck`. It always runs in `sh` (on Windows: Git for Windows' bash), but tools like `make`
   are missing on Windows, so prefer e.g. `npm run check` or a small script. Run it and fix it until
   it passes on a clean clone, and time it: `done` warns above 2 min and gives up after 15
   (`TASK_CHECK_TIMEOUT`). Slow suites (browser e2e, container builds) don't belong in it: give them
   their own tasks, whose criteria require running them. Make sure every path that builds or ships
   the code (CI jobs, Docker/Gradle/Maven builds that download their own Node or JDK) uses the same
   runtime versions as the check; note any mismatch as a task. Everything the check needs (JDK, Node version, Docker...) goes into the
   "Install" section and TEAM.md: an agent whose check can't run puts its task on hold.
3. Configure code coverage for the project's test stack.
   Detect the test framework already chosen for the project and use its standard coverage tooling.
   For greenfield projects, install and configure the appropriate coverage package as part of the initial test setup.
   
   Examples:
      - Vitest: `@vitest/coverage-v8`
      - Jest: use Jest's existing coverage support
      - pytest: `pytest-cov`
      - Gradle/JVM: JaCoCo
      - other stacks: use the standard coverage tooling already associated with the chosen test framework
   
   Do not introduce a second test framework solely for coverage.
   Coverage must run non-interactively so headless TFP workers can use it.
   Add a coverage command to the project's normal scripts or build configuration and document it in `docs/ARCHITECTURE.md`.
   
   Also document:
      - the coverage command;
      - the coverage provider/tool;
      - where reports are written;
      - how the test-writer should obtain coverage for production files changed by the current TFP task.
   
   Prefer machine-readable coverage output when supported.
   Do not enforce a repository-wide percentage unless the project explicitly requires one.
   Coverage is primarily used by the `test-writer` to identify meaningful untested behavior in code changed by the current task.
4. Add a short "Project notes" section to AGENTS.md: how to run, how to test, and the code
   conventions every agent must follow.

## C. Agree on what to build (1–2 h, the important part)
I paste the contest brief into `BRIEF.md`. Then interview me, a few questions at a time:
- What do the judges reward? What does a winning demo look like, step by step?
- Must-haves vs nice-to-haves; what we deliberately skip.
- Features: for each, the user story, which folders it touches, API changes, data changes.
- Constraints: deadline, deployment target, AI/LLM usage, secrets, anything that needs a human.

Write the answers into `SPEC.md` as they come: goals, demo script, features (MoSCoW), API contract
changes, out of scope, open questions. Show it to me until I say "approved". Commit and push.

## D. Generate the task board (1 h)
Replace the template tasks with ones derived from SPEC.md (`python tasks.py new ...`, then write
Goal / Acceptance criteria into each file). Rules for good tasks:
- One agent, 30–90 min, one clear outcome. Acceptance criteria are checkable, and tests are included.
  Agents must `verify` each criterion with what they ran before `done` accepts the task, so say how it
  is verified where that isn't obvious: "e2e login test passes against Keycloak (not the dev login)",
  "`docker build` succeeds with the CI runtime", "late response of an older request is ignored
  (test reverses the order)". Something agents can't run (a real device, production) is a human task.
- Contract first: API/data model tasks come before the backend and frontend tasks that use them.
- Two tasks that could run in parallel must not edit the same files. Give every task `touches:` (the
  folders/files it changes); `python tasks.py check` then warns about overlapping tasks without a dep
  between them. Add a dep, merge them, or split the files. Several fixes in one store or module are
  usually one task, or a chain.
- `deps` only where really needed. The board must be wide enough to keep all agents busy
  (count: agents on all laptops).
- `prio: 1` for the demo-critical path. End with demo polish, README and a final smoke test.

Then `python tasks.py check` and `python tasks.py graph`, and walk me through board.html: the critical
path, how many tasks can run in parallel, and anything unclear. Revise until I approve. Push.

## E. Dry run (30 min)
1. Set up the sync: README step 6 (GitLab CI template, or `repos sync --every 30m` in a spare clone).
   Run one sync by hand and check the original repos.
2. Check that every tool of `.taskcheck` works in the shell you start agents from (agents inherit its
   PATH, e.g. the nvm Node version; Docker running). Then `python3 agents.py start 1` and watch
   `python3 tasks.py watch` until the first task is done. Look at `../<repo>-agents/logs/agent-1.log`
   and grep `agent-1.session.txt` for `permission_denied`: each denial is a command to allow or avoid.
3. Verify the full quality workflow on an implementation task, not only its done status:
   - confirm the session shows actual test-writer and code-reviewer invocations;
   - confirm their reports exist in reviews/ and are committed and present on origin/main;
   - when findings required fixes, confirm a fixer invocation, its committed report, and retesting;
   - inspect the test report for measured coverage and significant gaps, or a concrete explanation
     of why coverage could not be measured;
   - confirm the project check passed;
   - confirm the completion-gate tests passed, including rejection of missing review artifacts.
   Do not proceed to phase F unless the artifact gate and the end-to-end helper workflow both
   pass. A missing report is a failed dry run even if lint, tests, and task completion succeeded.
   Save a copy of agent-1.session.txt immediately after the implementation session, before another
   session starts: the runner overwrites that file. Inspect tool errors and permission_denials as
   well as permission_denied text. A completed task or absent denial text alone is insufficient.
   If the next session already replaced the log, run another bounded implementation dry run and
   capture its session evidence. Do not invent reports or claim helper execution after the fact.
4. Fix whatever went wrong (permissions denied, a failing check, vague tasks) in
   `.claude/settings.json`, AGENTS.md or the task files. Repeat until one implementation task passes both the project check and the review-evidence checks.

## F. Go autonomous
Precondition: phase E verified helper execution, committed artifacts, and enforced rejection of
missing artifacts. Do not launch the full worker pool while any of these remains unverified.

1. `python3 agents.py start <n>` here (2 per Claude subscription is a good start).
2. Send teammates the repo URL and TEAM.md.
3. From now on I only watch the dashboard. Things to act on: the **Needs a human** panel (fix the
   cause, then `python tasks.py unhold <id>`), stale tasks, a failing sync. Add tasks with
   `python tasks.py new` any time.
