# Enable GitHub Copilot as an agent backend for TFP

Brief for an AI coding agent (GitHub Copilot with a Claude model) running on a machine that has
the Copilot CLI and a Copilot subscription. Work on the `ghcp` branch of the TFP repo.

## Goal

TFP (Task Flow Protocol) runs today with Claude Code as the agent. Make it also run with
**GitHub Copilot CLI** as the agent, without forking the project: Claude Code stays the default,
Copilot becomes a selectable backend. Nothing that works with Claude Code today may break.

## Background: what is agent-specific and what isn't

Agent-agnostic (plain Python + git), no changes expected:
- `tasks.py`, `board.py`, `repos.py`: the protocol itself (claim, verify, done, leases, `.taskcheck`,
  quality gate in `docs/QUALITY.md`).
- `AGENTS.md`: the protocol the agents read. Copilot reads `AGENTS.md` natively.

Claude-Code-specific (this is what the task is about):

| Piece | Location | Claude Code specifics |
|---|---|---|
| Agent command | `agents.py` ~line 64-68: `STREAM`, `DEFAULT_CMD`, `YOLO_CMD` | `claude -p --output-format stream-json --verbose --permission-mode acceptEdits --settings .claude/settings.json`; `--yolo` uses `--dangerously-skip-permissions` |
| Prompt passing | `agent_argv()` ~line 269 | prompt appended as the last argv element |
| Progress parsing | `describe()` ~line 176, `tool_doing()` ~line 130 | parses Claude's stream-json events (`assistant` with `tool_use`/`text`, final `result` with `is_error`/`subtype`); tool names Bash/Edit/Read/Grep/Task/... Non-JSON lines already fall back to plain one-line text |
| Session success | `describe()` returns a final result only on a successful `result` event | the runner treats "no final result" as a failed session and backs off |
| Usage limits | `LIMIT` regex ~line 70, `limit_info()` ~line 154 | Claude's wording: "5-hour limit", "resets 3pm", `limit reached\|<epoch>` |
| Permissions | `.claude/settings.json` (SETUP.md A.5), passed via `--settings` | allow/deny lists, notably deny `git push` |
| Quality helpers | `templates/claude-agents/{test-writer,code-reviewer,fixer}.md` → `.claude/agents/` (SETUP.md A.6) | Claude Code sub-agent frontmatter (`name`, `description`, `tools: Bash, Read, ...`) |
| Helper invocation wording | `AGENTS.md` "Quality gate" | "Agent/Task tool, `subagent_type: test-writer`" |
| frontend-design | `AGENTS.md` "Frontend implementation", SETUP.md A.7 | Claude Code plugin; no Copilot equivalent |
| Docs | `agents.py` docstring, README.md, SETUP.md | assume Claude Code |

Out of scope: the Copilot *coding agent* (cloud, one PR per issue). Its model conflicts with TFP's
"many agents claim tasks, `done` pushes to main". Mention this in the docs; don't build it.

## Step 0: discover the real Copilot CLI behavior first (do not guess)

The Copilot CLI changes quickly. Before writing code, find out and **write down in the commit
message / docs** what the installed version actually does:

1. `copilot --version`, `copilot --help` (and `copilot help <topic>` if present).
2. Non-interactive mode: how to pass a prompt (`-p` / `--prompt`?), and whether it exits when done.
3. Model selection: the flag (`--model`?) and the exact id of the Claude model to use.
4. Tool permissions: allow-all flag (`--allow-all-tools`?), fine-grained allow/deny
   (`--allow-tool 'shell(git)'`, `--deny-tool 'shell(git push)'`?), and how shell commands with
   subcommands are matched. Verify a deny rule for `git push` really blocks it.
5. Output: is there a machine-readable/streaming format (e.g. `--output-format json`, a log
   option)? If not, what does plain output look like per step (tool calls, text, final answer)?
   Capture a sample: `copilot -p "list the files here, then run git status" <permission flags> > sample.log 2>&1`.
   Keep a trimmed sample as a test fixture.
6. Exit code on success, on error, on quota exhaustion. Exact wording of the premium-request /
   rate-limit message, and whether it says when the quota resets.
7. Custom agents: where Copilot CLI loads them from (`.github/agents/*.agent.md`? `~/.copilot/agents/`?),
   their frontmatter fields (name, description, tools, model?), and whether a **headless** session can
   delegate to them as sub-agents. This is the riskiest point: TFP's quality gate needs the parent
   worker to call test-writer, code-reviewer and fixer. Test it with a tiny agent that writes a file.
8. Whether instructions files are read in a fresh clone without a trust prompt (`AGENTS.md`,
   `.github/copilot-instructions.md`), and whether any first-run trust/login prompt would block an
   unattended run (agents run with stdin closed).
9. Windows: the executable name (`copilot.cmd`?) - `agent_argv()` resolves via `shutil.which`.

If step 7 shows that headless sub-agents are impossible, still do the rest, and implement the
fallback described under "Quality helpers" below. Record the limitation in the docs.

## Implementation

### 1. Backend selection in `agents.py`

- Add `--agent {claude,copilot}` (default `claude`, also env `AGENT_BACKEND`) to `start` and to the
  internal run subcommand, and pass it through wherever `--cmd` is passed today (`cmd_start` builds
  the child argv ~line 314).
- Group per-backend settings in one small table/dict, e.g.:
  `default_cmd`, `yolo_cmd`, `describe` (line parser), `limit` (regex + reset parser).
- `--cmd` / `AGENT_CMD` still override the command for either backend.
- `--yolo` maps to Copilot's allow-everything flag.
- The Copilot default command must: run non-interactively, select the Claude model (make the model
  overridable, e.g. `--model` passthrough or env `COPILOT_MODEL`), allow what agents need
  (python tasks.py, git, the project's build/test tools, file edits, sub-agents) and deny `git push`.
  If Copilot has no project-level permission file, put the allow/deny list into a file the runner
  reads (e.g. `.github/copilot-permissions.txt`, one rule per line) so projects can adapt it like
  `.claude/settings.json` today. Document the format.

### 2. Output parsing and session result

- Write `describe_copilot(line, clone)` returning `(doing, final_result)` like `describe()`:
  turn tool calls into the same short "doing" strings (`$ <cmd>`, `editing <file>`, `reading <file>`,
  `subagent: <name>`) so `tasks.py watch` looks the same for both backends.
- Decide how a successful session is detected (final JSON event if available, else exit code 0
  plus the last text). The runner must still see "failed" for errors and quota exhaustion, so its
  back-off and `wait_limited` logic keep working. Check how the main loop (~line 760-840) uses
  `final_result` and the exit code, and keep its behavior identical for Claude.

### 3. Usage limits

- Extend limit detection with Copilot's wording (premium requests exhausted, rate limited, 429...).
  Parse a reset time if Copilot reports one; otherwise `--limit-wait` applies, as today.

### 4. Quality helpers

- Add `templates/copilot-agents/` with `test-writer`, `code-reviewer`, `fixer` in Copilot's custom
  agent format. Reuse the prompt bodies of `templates/claude-agents/*.md` verbatim; only adapt the
  frontmatter (tool names, file naming). Don't duplicate by hand if avoidable: a small generator
  script or a test that checks the bodies stay identical is welcome.
- Make `AGENTS.md` backend-neutral: "invoke the `test-writer` helper agent (Claude Code: Agent/Task
  tool with `subagent_type: test-writer`; Copilot: <the mechanism found in step 0.7>)". Keep every
  rule of the quality workflow unchanged.
- Fallback if headless Copilot can't call custom agents: the parent worker runs the helper as a
  separate non-interactive Copilot session (`copilot -p` with the helper's instructions as the prompt,
  `--agent <name>` if that exists) from the shell, and commits the report it writes. Document this in
  AGENTS.md as the Copilot way of invoking a helper. The rule "never write a helper's report yourself"
  still applies.
- frontend-design: for Copilot, make it optional. Say so in AGENTS.md ("use it when your agent has
  it"). Don't invent a replacement plugin.

### 5. Docs

- `agents.py` docstring: mention both backends and `--agent copilot`.
- README.md: one short section "Using GitHub Copilot instead of Claude Code".
- SETUP.md: Copilot variants of A.1 (check `copilot --version`, logged in, Claude model available),
  A.5 (permissions), A.6 (copy `templates/copilot-agents/` to where Copilot loads agents), A.7
  (frontend-design optional), and the start command (`python3 agents.py start <n> --agent copilot`).
  Note the cost: each session uses premium requests; recommend starting with 1-2 agents.
- State that the Copilot coding agent (cloud, issue → PR) is not supported, and why.

## Tests

The existing tests use a scripted fake agent instead of Claude (see `FAKE_AGENT` in
`tests/test_overbudget.py`). Follow that pattern; no test may need a real Copilot subscription.

- Unit tests for `describe_copilot` using the trimmed real output sample(s) from step 0.5 as fixtures:
  tool calls → expected "doing" strings, final success → final result, error → "failed: ...".
- Unit tests for Copilot limit detection with the real quota message from step 0.6 (and the reset
  time, if any).
- One runner test: `agents.py` with `--agent copilot` and a fake agent that prints Copilot-style
  output claims a task, runs `done`, and the runner records the session as successful; a variant
  printing the quota message makes the runner release the task and wait (like the existing
  usage-limit test).
- Run the whole existing suite to prove nothing changed for Claude:
  `bash tests/test_publish.sh` and every `python3 tests/test_*.py`.

## End-to-end check (on the Copilot machine)

1. Create a throwaway project following README.md "Contest quick start" (local bare repo as origin
   is fine), with 2 trivial tasks, a `.taskcheck` that runs a tiny test, and the quality gate enabled
   (`docs/QUALITY.md` from `templates/quality/`).
2. `python3 agents.py start 1 --agent copilot`.
3. Confirm: the agent claims a task, `tasks.py watch` shows live "doing" lines, the helpers write
   `reviews/<id>/tests.md` and `review.md`, `quality.json` is committed, `done` passes, main has the
   task, the second task follows. Confirm an attempted `git push` by the agent is denied.
4. `python3 agents.py stop` and check no process is left over.

Record what you ran and what came out in the final summary.

## Acceptance criteria

- [ ] Step 0 findings are documented (Copilot CLI version, flags used, output format, limit message,
      sub-agent mechanism or its absence).
- [ ] `python3 agents.py start <n>` without `--agent` behaves exactly as before (Claude Code).
- [ ] `python3 agents.py start <n> --agent copilot` runs Copilot CLI headless with a Claude model,
      with `git push` denied for the agent.
- [ ] Live status works for Copilot sessions (`tasks.py watch` shows meaningful "doing" lines).
- [ ] Failed sessions and Copilot quota exhaustion are detected; the runner backs off / releases
      as it does for Claude.
- [ ] `templates/copilot-agents/` holds the three helpers; AGENTS.md describes how to invoke them
      with either backend; the quality workflow rules are unchanged.
- [ ] README.md and SETUP.md describe the Copilot setup; Copilot coding agent marked unsupported.
- [ ] New tests pass; the full existing test suite still passes.
- [ ] The end-to-end check above completed one task with the quality gate on.

## Constraints

- Keep the code style of the repo: standard library only, small functions, short comments that say why.
- Linux, macOS and Windows must keep working (see `WINDOWS` handling in `agents.py`).
- Don't change `tasks.py` unless step 0 proves it necessary; if you do, explain why.
- Commit in small steps on `ghcp`. Don't push to `main`.
