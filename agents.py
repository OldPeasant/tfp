#!/usr/bin/env python3
"""agents.py - run N AI agents against the git task board. Stdlib only.

  python agents.py start 4            # 4 agents, each in its own clone, in the background
  python agents.py status             # who is alive, what they own, last log line
  python agents.py logs 2             # tail agent-2's log
  python agents.py stop              # now: ends the sessions (their tasks stay claimed)
  python agents.py stop --finish     # gently: no new tasks, finish the current ones, then exit

Each agent loops: pull -> (resume own in_progress task | claim best ready task) -> work -> done,
one fresh agent session per task, until every task is done. Needs an 'origin' remote.

Leases: claims are owned as <host>/agent-N and carry a lease (--lease, default 1200 s). While the
runner is alive it renews the lease every lease/4 s from a separate clone (<agent>.hb) and backs up the
agent's commits to origin/wip/<task>. If a machine dies, its lease runs out and another agent takes the
task over, starting from wip/<task>. If a runner finds its task was taken over (e.g. the laptop slept),
it kills the session and parks the local work on a lost/<time> branch in its clone, so it never reaches
main.

Usage limits: when a session fails with a usage limit (or 3 sessions fail in a row), the runner
releases its task (work saved to wip/<task>, a note says why) so agents on other machines continue it,
then waits until the limit resets (the time Claude reports, else --limit-wait) and carries on.

Task budget: after --task-budget sessions (default 2, ~30 min each) on one task, the next session is a
review: the agent must extend it once (nearly done), split it into subtasks, hand it to another agent
(release with a handoff note) or, only if a person is really needed, put it on hold. A review that decides
nothing releases the task too: being over budget alone never needs a human. Releases are counted, so a
task nobody can finish still ends up on hold after a few (tasks.py MAX_RELEASES). The runner then
prefers other ready tasks for a while (--avoid, default 300 s) instead of reclaiming the one it gave up.

Live status: the runner reads the agent's output as it streams, turns each step into a one-line
"doing" (e.g. `$ make check`, `editing backend/auth.py`), and force-pushes it every ~10-60 s as a
one-commit branch status/<host>/agent-N. `python tasks.py watch` shows these on any machine; main's
history is not touched.

Runs on Linux, macOS and Windows (on Windows: Git for Windows and Claude Code's native claude.exe).

Agent command (default: Claude Code headless). Override with --cmd or AGENT_CMD; the prompt is
appended as the last argument. --yolo adds --dangerously-skip-permissions: agents can then run
ANY command on this machine unattended, so use a VM/container.
"""
import argparse, json, os, re, shlex, shutil, signal, socket, subprocess, sys, time
sys.dont_write_bytecode = True  # importing tasks.py must not litter clones with __pycache__
from pathlib import Path

WINDOWS = os.name == "nt"
if WINDOWS and not sys.flags.utf8_mode:  # UTF-8 for files, logs, git output and every child process
    os.environ["PYTHONUTF8"] = "1"
    try:
        sys.exit(subprocess.call([sys.executable, *sys.argv]))
    except KeyboardInterrupt:
        sys.exit(130)

HERE = Path(__file__).resolve().parent
STREAM = "--output-format stream-json --verbose"  # one JSON event per step, so we can see progress live
# --settings: Claude Code ignores .claude/settings.json in folders nobody marked as trusted, which the
# agents' fresh clones are, so the project's allow/deny rules are passed explicitly
DEFAULT_CMD = f"claude -p {STREAM} --permission-mode acceptEdits --settings .claude/settings.json"
YOLO_CMD = f"claude -p {STREAM} --dangerously-skip-permissions"
CLAIMED = re.compile(r"CLAIMED (\S+) as ")
LIMIT = re.compile(r"(usage|rate|session|weekly|5-hour)[ -]limit|limit (reached|exceeded)|hit your limit|"
                   r"too many requests|\b429\b", re.I)

PROMPT = """You are {name}, one of several AI agents building this project in parallel. Other agents work
at the same time in their own clones; git (origin) is the shared source of truth.
Read AGENTS.md and follow the task protocol exactly (python tasks.py ...).
{job}
Commit often: your commits are backed up so another agent can continue if this machine dies. Never
`git push` yourself (done does it). Run `git pull --rebase` every few commits so conflicts stay small; on a conflict follow AGENTS.md
"Merge conflicts". If a tasks.py command prints LOST, or REFUSED because the task is not yours, stop
immediately.
Nobody answers questions at night: when something is unclear, choose the most reasonable option, record it
with `python tasks.py assume <id> "<what you assumed and why>"`, and carry on.
Do exactly ONE task, then stop. Before finishing, verify each acceptance criterion by actually running
what it names and record it: `python tasks.py verify <id> <n> "<what you ran> -> <result>"` (AGENTS.md
"Evidence"; a test that was found but not run is no evidence). Finish with `python tasks.py done <id>`: it merges the latest main,
runs the project check on the result and pushes; you are finished only when it prints DONE. If you cannot finish, commit what you
have and run `python tasks.py release <id> --note "<what is done, what blocks>"` (your commits then go
to wip/<id>, not main)."""
JOB_RESUME = "You already own task `{id}` (in_progress) from an earlier session: continue it, do not claim another."
JOB_CONFLICT = ("\nYour clone is in the middle of an unfinished {op} with conflicts in: {files}. Resolve it first "
                "(AGENTS.md \"Merge conflicts\"): `git status` shows where you are.")
REVIEW = """You are {name}, one of several AI agents building this project in parallel (see AGENTS.md).
You have spent {n} sessions (about {mins} min) on task `{id}` without finishing it. That is longer than
a task should take, so do NOT continue implementing in this session. Instead:
1. Commit your current work (never git push).
2. Assess honestly: read tasks/{id}.md and `git log`, work out what is done and what is left, run the check.
3. Choose exactly one, run it, then stop:
{extend}   - Too big: write the remaining work as 2-6 subtasks into a file (format: AGENTS.md "Too big or
     stuck"), then `python tasks.py split {id} --from <file> --note "<what is done so far>"`
   - Hand off (the task is still valid and resumable, e.g. only finalization is left, but splitting it
     would be artificial): `python tasks.py release {id} --note "<what is done, exactly what is left>"`;
     another agent continues from your commits
   - Stuck, ONLY if a person is really needed (unclear requirement, missing dependency, a decision is
     needed, the check fails for reasons outside this task): `python tasks.py hold {id} "<what a human
     must decide or do>"`. Taking longer than planned is not a reason to hold.
If you choose nothing, the task is released for another agent with a generic note."""
REVIEW_EXTEND = ('   - Nearly done (one more session will finish it): '
                 '`python tasks.py extend {id} --note "<what is left>"`\n')
JOB_NEW = "Run `python tasks.py claim` (no id = best ready task). If it reports LOST RACE, run it again."


def sh(*a, cwd=None, env=None):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)


def git_in(cwd, data, *a):
    """git with data on stdin, as bytes: text-mode pipes would turn \n into \r\n on Windows."""
    return subprocess.run(["git", *a], cwd=cwd, input=data.encode(), capture_output=True).stdout.decode().strip()


def one_line(s, n=140):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "\u2026"


def tool_doing(name, inp, clone):
    def rel(p):
        try:
            return os.path.relpath(p, clone) if p and os.path.isabs(p) else (p or "")
        except ValueError:  # Windows: another drive
            return p
    if name == "Bash":
        return "$ " + one_line(inp.get("command", ""))
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return "editing " + rel(inp.get("file_path") or inp.get("notebook_path"))
    if name == "Read":
        return "reading " + rel(inp.get("file_path"))
    if name in ("Grep", "Glob"):
        return "searching " + one_line(inp.get("pattern", ""))
    if name == "TodoWrite":
        now = [t.get("content", "") for t in inp.get("todos") or [] if t.get("status") == "in_progress"]
        return "plan: " + one_line(now[0] if now else f"{len(inp.get('todos') or [])} steps")
    if name in ("Task", "Agent"):
        return "subagent: " + one_line(inp.get("description", ""))
    if name in ("WebFetch", "WebSearch"):
        return "web: " + one_line(inp.get("url") or inp.get("query") or "")
    return name


def limit_info(text):
    """(message, reset time as epoch or None) if a failed session's last output says we hit a usage
    limit. Understands 'limit reached|<epoch>' and '... resets 3pm' / 'resets at 15:40'."""
    hits = [l for l in text.splitlines()[-40:] if LIMIT.search(l)]
    if not hits:
        return None
    line = hits[-1]
    msg = (describe(line, ".")[0] or one_line(line)).removeprefix("failed: ")
    m = re.search(r"limit reached\|(\d{9,11})", line)
    if m:
        return msg, int(m.group(1))
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\b", line, re.I)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        h = h % 12 + (12 if ap == "pm" else 0) if ap else h
        if h < 24 and mi < 60:
            t = time.localtime()
            reset = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, mi, 0, 0, 0, -1))
            return msg, int(reset if reset > time.time() else reset + 86400)
    return msg, None


def describe(line, clone):
    """One line of agent output -> (doing, final_result). Understands Claude Code's stream-json events;
    any other output (a custom --cmd) is shown as plain text."""
    line = line.strip()
    if not line:
        return None, None
    try:
        ev = json.loads(line)
    except ValueError:
        return one_line(line), None
    if not isinstance(ev, dict):
        return None, None
    if ev.get("type") == "result":
        r = one_line(ev.get("result") or ev.get("subtype") or "")
        if ev.get("is_error") or ev.get("subtype", "success") != "success":
            return "failed: " + r, None  # no final result: the runner backs off (rate limit, API error)
        return ("finished: " + r if r else None), r
    if ev.get("type") != "assistant":
        return None, None
    for c in reversed((ev.get("message") or {}).get("content") or []):
        if c.get("type") == "tool_use":
            return tool_doing(c.get("name", ""), c.get("input") or {}, clone), None
        if c.get("type") == "text" and c.get("text", "").strip():
            return "\u00bb " + one_line(c["text"]), None
    return None, None


def repo_root():
    p = sh("git", "rev-parse", "--show-toplevel")
    if p.returncode:
        sys.exit("run this inside your project's git repo")
    return Path(p.stdout.strip())


def base_dir(root):
    return root.parent / f"{root.name}-agents"


def state_file(root):
    return base_dir(root) / "agents.json"


def host(a=None):
    return getattr(a, "host", None) or os.environ.get("AGENT_HOST") or socket.gethostname().split(".")[0]


# ---------- processes (POSIX: sessions + signals; Windows: process groups + taskkill) ----------
def alive(pid):
    if WINDOWS:  # os.kill(pid, 0) would *terminate* the process on Windows
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def detached(breakaway=False):
    """Popen kwargs for a child that has its own process group and survives this terminal closing."""
    if not WINDOWS:
        return dict(start_new_session=True)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return dict(creationflags=flags | (subprocess.CREATE_BREAKAWAY_FROM_JOB if breakaway else 0))


def kill_tree(pid):
    """Stop a process and everything it started (the agent session: claude + its git, tests, servers)."""
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass


def stop_flag(base, name):
    return base / "logs" / f"{name}.stop"


def finish_flag(base, name):
    """`stop --finish`: claim nothing new, exit once we hold no task."""
    return base / "logs" / f"{name}.finish"


def agent_argv(cmd, prompt):
    argv = shlex.split(cmd, posix=not WINDOWS)
    if WINDOWS:
        argv = [x[1:-1] if len(x) > 1 and x[0] == x[-1] == '"' else x for x in argv]
    argv[0] = shutil.which(argv[0]) or argv[0]  # Windows: finds claude.exe (PATHEXT)
    return [*argv, prompt]


# ---------- launcher ----------
def cmd_start(a):
    root = repo_root()
    origin = sh("git", "remote", "get-url", "origin", cwd=root).stdout.strip()
    if not origin:
        sys.exit("no 'origin' remote: agents share state through it. Push the repo first.")
    if sh("git", "status", "--porcelain", cwd=root).stdout.strip():
        sys.exit("uncommitted changes: commit and push first, agents clone from origin")
    ahead = sh("git", "rev-list", "--count", "@{u}..HEAD", cwd=root)
    if ahead.returncode:
        sys.exit("branch has no upstream: run `git push -u origin <branch>` first")
    if ahead.stdout.strip() != "0":
        sys.exit("unpushed commits: git push first, agents clone from origin")
    if not (root / "tasks.py").exists() or not list((root / "tasks").glob("*.md")):
        sys.exit("need tasks.py and tasks/*.md committed in the repo")
    branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=root).stdout.strip()
    cmd = a.cmd or os.environ.get("AGENT_CMD") or (YOLO_CMD if a.yolo else DEFAULT_CMD)
    base = base_dir(root)
    (base / "logs").mkdir(parents=True, exist_ok=True)
    st = json.loads(state_file(root).read_text()) if state_file(root).exists() else {}
    for i in range(a.first, a.first + a.n):
        name = f"agent-{i}"
        owner = f"{host(a)}/{name}"  # unique across machines: the claim owner and git author
        if name in st and alive(st[name]["pid"]):
            print(f"{name} already running (pid {st[name]['pid']})")
            continue
        clone = base / name
        if not clone.exists():
            p = sh("git", "clone", "-b", branch, origin, str(clone))
            if p.returncode:
                sys.exit(p.stderr)
        sh("git", "config", "user.name", owner, cwd=clone)
        sh("git", "config", "user.email", f"{name}@agents.local", cwd=clone)
        stop_flag(base, name).unlink(missing_ok=True)
        finish_flag(base, name).unlink(missing_ok=True)
        log = open(base / "logs" / f"{name}.log", "a", encoding="utf-8")
        argv = [sys.executable, str(Path(__file__).resolve()), "run", name, str(clone), "--owner", owner,
                "--cmd", cmd, "--timeout", str(a.timeout), "--lease", str(a.lease), "--limit-wait", str(a.limit_wait),
                "--task-budget", str(a.task_budget), "--avoid", str(a.avoid)]
        io = dict(stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:  # Windows: leave the terminal's job object too, if allowed, so closing the terminal is safe
            proc = subprocess.Popen(argv, **io, **detached(breakaway=True))
        except OSError:
            proc = subprocess.Popen(argv, **io, **detached())
        st[name] = {"pid": proc.pid, "clone": str(clone), "owner": owner}
        print(f"started {name} pid {proc.pid} in {clone}")
        time.sleep(a.stagger)  # de-synchronise first claims
    state_file(root).write_text(json.dumps(st, indent=2))
    print(f"\nlogs: {base/'logs'}   status: python agents.py status   stop: python agents.py stop")


def load_state():
    root = repo_root()
    f = state_file(root)
    return root, (json.loads(f.read_text()) if f.exists() else {})


def cmd_status(a):
    root, st = load_state()
    if not st:
        return print("no agents started")
    os.environ["TASKS_ROOT"] = str(root)
    sys.path.insert(0, str(root))
    import tasks
    sh("git", "pull", "--rebase", "--autostash", cwd=root)
    ts = tasks.load()
    for name, s in st.items():
        owner = s.get("owner", name)
        mine = [t["id"] for t in ts.values() if t["owner"] == owner and t["status"] == "in_progress"]
        done = sum(1 for t in ts.values() if t["owner"] == owner and t["status"] == "done")
        lf = base_dir(root) / "logs" / f"{name}.log"
        last = lf.read_text().strip().splitlines()[-1][:90] if lf.exists() and lf.stat().st_size else ""
        sf = base_dir(root) / "logs" / f"{name}.status.json"
        doing = json.loads(sf.read_text()).get("doing") if sf.exists() and alive(s["pid"]) else None
        finishing = alive(s["pid"]) and finish_flag(base_dir(root), name).exists()
        print(f"{name}: {'ALIVE' if alive(s['pid']) else 'stopped':7}{' (finishing, then stops)' if finishing else ''} working on {mine or '-'}  finished {done}  | "
              f"{doing or last}")
    n = {k: sum(1 for t in ts.values() if t["status"] == k) for k in tasks.STATUSES}
    print(f"\nboard: {n}")


def cmd_logs(a):
    """Last 60 lines of an agent's log, then follow it (like tail -f, on every OS)."""
    f = base_dir(repo_root()) / "logs" / f"agent-{a.n}.log"
    if not f.exists():
        sys.exit(f"no log yet: {f}")
    with open(f, encoding="utf-8", errors="replace") as fh:
        print("".join(fh.readlines()[-60:]), end="", flush=True)
        try:
            while True:
                line = fh.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass


def cmd_stop(a):
    """Ask each runner to stop (a flag file it checks every few seconds: it then ends its session and
    reports "stopped"); force-kill whatever is still running after 20 s. With --finish, only ask them to
    claim nothing new: each finishes (or gives back) the task it holds, then exits by itself."""
    root, st = load_state()
    base = base_dir(root)
    running = {n: s for n, s in st.items() if alive(s["pid"])}
    if a.finish:
        for name in running:
            finish_flag(base, name).touch()
        return print(f"asked {len(running)} agent(s) to finish their current task and then stop; no new claims.\n"
                     "`python agents.py status` shows who is still finishing; when all are 'stopped', you can shut\n"
                     "down without blocking anyone. Stop right away instead: python agents.py stop")
    for name in running:
        stop_flag(base, name).touch()
    end = time.time() + 20
    while time.time() < end and any(alive(s["pid"]) for s in running.values()):
        time.sleep(0.5)
    for name, s in running.items():
        if alive(s["pid"]):
            kill_tree(s["pid"])  # POSIX: the runner's group; its SIGTERM handler ends the session too
        stop_flag(base, name).unlink(missing_ok=True)
        print(f"stopped {name}")
    print("note: a task an agent held stays in_progress. `start` again resumes it; otherwise its lease\n"
          "expires and another agent takes it over (or free it now: python tasks.py release --force <id>)")


# ---------- per-agent loop ----------
def park(clone, log):
    """Move local work that belongs to no task we own (lease lost, released, crash) onto a local
    lost/<time> branch and reset to origin, so it never reaches main with the next task."""
    for op, marker in (("rebase", "rebase-merge"), ("rebase", "rebase-apply"), ("merge", "MERGE_HEAD")):
        if (clone / sh("git", "rev-parse", "--git-path", marker, cwd=clone).stdout.strip()).exists():
            sh("git", op, "--abort", cwd=clone)
    dirty = sh("git", "status", "--porcelain", cwd=clone).stdout.strip()
    ahead = sh("git", "rev-list", "--count", "@{u}..HEAD", cwd=clone).stdout.strip()
    if not dirty and ahead in ("0", ""):
        return
    if dirty:
        sh("git", "add", "-A", cwd=clone)
        sh("git", "commit", "-qm", "wip snapshot (task no longer owned)", cwd=clone)
    branch = f"lost/{time.strftime('%Y%m%d-%H%M%S')}"
    sh("git", "branch", branch, cwd=clone)
    sh("git", "reset", "-q", "--hard", "@{u}", cwd=clone)
    log(f"parked local work that belongs to no task we own on branch {branch} in {clone}")


def cmd_run(a):
    clone = Path(a.clone)
    hb = clone.parent / f"{a.name}.hb"  # heartbeats commit here, never in the agent's working tree
    os.environ.update(TASKS_ROOT=str(clone), TASK_OWNER=a.owner, TASK_LEASE=str(a.lease))
    # git has no network timeout: a stalled HTTPS fetch would block heartbeats and sessions forever
    os.environ.setdefault("GIT_HTTP_LOW_SPEED_LIMIT", "1000")
    os.environ.setdefault("GIT_HTTP_LOW_SPEED_TIME", "60")
    if not WINDOWS and not shutil.which("python"):  # agents type `python tasks.py`; some systems only have python3
        shim = clone.parent / "bin"
        shim.mkdir(exist_ok=True)
        if not (shim / "python").exists():
            (shim / "python").symlink_to(sys.executable)
        os.environ["PATH"] = f"{shim}{os.pathsep}{os.environ['PATH']}"
    sys.path.insert(0, str(clone))
    import tasks
    log = lambda m: print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)
    log(f"{a.name} up as {a.owner}; lease {a.lease}s; cmd: {a.cmd}")
    if not hb.exists():
        origin = sh("git", "remote", "get-url", "origin", cwd=clone).stdout.strip()
        branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=clone).stdout.strip()
        p = sh("git", "clone", "-q", "-b", branch, origin, str(hb))
        if p.returncode:
            sys.exit(p.stderr)
    sh("git", "config", "user.name", a.owner, cwd=hb)
    sh("git", "config", "user.email", f"{a.name}@agents.local", cwd=hb)
    every = max(a.lease / 4, 1)
    held, last = set(), [0.0]

    def beat():
        """Renew our leases (throttled) and back up our commits to wip/<task>. Returns ids we lost."""
        if time.time() - last[0] < every:
            return set()
        last[0] = time.time()
        p = sh(sys.executable, "tasks.py", "heartbeat", *sorted(held), cwd=hb,
               env={**os.environ, "TASKS_ROOT": str(hb)})
        if p.returncode:
            log("heartbeat failed (offline?): " + (p.stderr or p.stdout).strip()[-200:])
            return set()
        lines = [l.split(" ", 2) for l in p.stdout.splitlines() if l.strip()]
        held.clear()
        held.update(w[1] for w in lines if w[0] == "HEARTBEAT")
        lost = {w[1] for w in lines if w[0] == "LOST"}
        for w in lines:
            if w[0] == "LOST":
                log(f"LOST {w[1]}: {w[2] if len(w) > 2 else ''}")
        ahead = sh("git", "rev-list", "--count", "@{u}..HEAD", cwd=clone).stdout.strip()
        if ahead not in ("0", "") and not tasks.merge_state():  # mid-rebase HEAD is half-done: keep the old backup
            for tid in held:
                sh("git", "push", "-q", "-f", "origin", f"HEAD:refs/heads/{tasks.wip_branch(tid)}", cwd=clone)
        return lost

    status = dict(owner=a.owner, state="starting", task=None, doing=None, doing_at=None, updated=0)
    pushed = [0.0, None]
    status_file = clone.parent / "logs" / f"{a.name}.status.json"

    def publish(force=False, **kw):
        """Push our live status to origin as branch status/<owner> (a single force-pushed commit, built
        in the heartbeat clone without touching any working tree). At most every 10 s; at least every 60 s."""
        status.update(kw)
        now, key = time.time(), (status["state"], status["task"], status["doing"])
        if not force and now - pushed[0] < 60 and (key == pushed[1] or now - pushed[0] < 10):
            return
        status["updated"] = int(now)
        data = json.dumps(status)
        status_file.write_text(data)
        blob = git_in(hb, data, "hash-object", "-w", "--stdin")
        tree = git_in(hb, f"100644 blob {blob}\tstatus.json\n", "mktree")
        commit = sh("git", "commit-tree", tree, "-m", f"status {a.owner}", cwd=hb).stdout.strip()
        if commit and sh("git", "push", "-q", "-f", "origin", f"{commit}:refs/heads/status/{a.owner}", cwd=hb).returncode == 0:
            pushed[:] = [now, key]

    def idle(secs, task=None, **kw):
        publish(force=True, task=task, doing_at=int(time.time()), **kw)
        end = time.time() + secs
        while time.time() < end:
            time.sleep(min(5, max(end - time.time(), 0)))
            if stop_flag(clone.parent, a.name).exists():
                shutdown()
            if finish_flag(clone.parent, a.name).exists() and not owned():
                finished()
            publish()

    def hand_off(reason):
        """We can't work for a while (usage limit): give our tasks back, with the work saved to
        wip/<task> by `release`, so agents on other machines continue them instead of waiting."""
        for t in [t for t in tasks.load().values() if t["status"] == "in_progress" and t["owner"] == a.owner]:
            tid = t["id"]
            if tasks.merge_state():
                sh("git", tasks.merge_state(), "--abort", cwd=clone)
            if sh("git", "status", "--porcelain", cwd=clone).stdout.strip():
                sh("git", "add", "-A", cwd=clone)
                sh("git", "commit", "-qm", f"wip: {tid} (handed off: {one_line(reason, 60)})", cwd=clone)
            note = f"handed off by {a.owner}: {one_line(reason, 120)}"
            p = sh(sys.executable, "tasks.py", "release", tid, "--handoff", "--note", note, cwd=clone)
            if p.returncode:  # e.g. a conflict while catching up: save the work to wip/<task> ourselves
                if tasks.merge_state():
                    sh("git", tasks.merge_state(), "--abort", cwd=clone)
                sh("git", "push", "-q", "-f", "origin", f"HEAD:refs/heads/{tasks.wip_branch(tid)}", cwd=clone)
                sh("git", "fetch", "-q", cwd=clone)
                sh("git", "reset", "-q", "--hard", "@{u}", cwd=clone)
                p = sh(sys.executable, "tasks.py", "release", tid, "--handoff", "--note", note, cwd=clone)
            log(f"handed off {tid}: " + ((p.stdout + p.stderr).strip().splitlines() or ["?"])[-1])
            held.discard(tid)

    def wait_limited(reason, reset, limited):
        now = time.time()
        until = min(max(reset + 60 if reset else now + a.limit_wait, now + 60), now + 6 * 3600)
        hhmm = time.strftime("%H:%M", time.localtime(until))
        what = "usage limit" if limited else "sessions keep failing"
        log(f"{what} ({reason}): waiting until {hhmm}")
        idle(until - now, state="limited" if limited else "paused",
             doing=f"{what}, retrying at {hhmm}: {one_line(reason, 80)}")

    def force_release(tid, note):
        """A review that decided nothing: commit what is there and give the task to another agent. Over
        budget is not a reason for a human; a counted release still holds it after MAX_RELEASES."""
        if tasks.merge_state():
            sh("git", tasks.merge_state(), "--abort", cwd=clone)
        if sh("git", "status", "--porcelain", cwd=clone).stdout.strip():
            sh("git", "add", "-A", cwd=clone)
            sh("git", "commit", "-qm", f"wip: {tid} (before release)", cwd=clone)
        p = sh(sys.executable, "tasks.py", "release", tid, "--note", note, cwd=clone)
        log(f"released {tid} for another agent: " + ((p.stdout + p.stderr).strip().splitlines() or ["?"])[-1])

    child = [None]
    fails = 0  # failed sessions in a row
    spent = {}  # task id -> [sessions, seconds] since this runner claimed it (budget: --task-budget sessions)
    avoid = {}  # task id -> until (epoch): gave it up over budget, so let another agent continue it first

    def shutdown(*_):  # `agents.py stop` (flag file, or SIGTERM on POSIX): end the session too
        if child[0] and child[0].poll() is None:
            kill_tree(child[0].pid)
        publish(force=True, state="stopped", doing=None)
        stop_flag(clone.parent, a.name).unlink(missing_ok=True)
        finish_flag(clone.parent, a.name).unlink(missing_ok=True)
        log("stopped")
        sys.exit(0)

    def owned():
        return [t for t in tasks.load().values() if t["status"] == "in_progress" and t["owner"] == a.owner]

    def finished():  # `agents.py stop --finish`, and we hold no task (any more)
        publish(force=True, state="stopped", task=None, doing=None)
        finish_flag(clone.parent, a.name).unlink(missing_ok=True)
        log("stopped: finished current work (stop --finish)")
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)

    while True:
        if stop_flag(clone.parent, a.name).exists():
            shutdown()
        sh("git", "pull", "--rebase", "--autostash", cwd=clone)
        ts = tasks.analyze(tasks.load())
        if all(t["status"] in tasks.CLOSED for t in ts.values()):
            publish(force=True, state="finished", task=None, doing=None)
            return log("all tasks done, exiting")
        mine = [t for t in ts.values() if t["status"] == "in_progress" and t["owner"] == a.owner]
        if not mine:
            park(clone, log)
            if finish_flag(clone.parent, a.name).exists():
                finished()
        held.update(t["id"] for t in mine)
        beat()
        if not mine and tasks.frozen():
            log(f"board frozen, waiting: {tasks.frozen()}")
            idle(60, state="waiting", doing=f"board frozen (plan change): {one_line(tasks.frozen(), 80)}")
            continue
        if not mine and not tasks.ranked(ts):
            on_hold = [t["id"] for t in ts.values() if t["hold"] or t["orphaned"]]
            if on_hold and not any(t["status"] == "in_progress" for t in ts.values()):
                log(f"nothing to do until a human unholds: {', '.join(on_hold)}")
                idle(60, state="waiting", doing=f"waiting for a human: {', '.join(on_hold)} on hold")
                continue
            if not any(t["status"] == "in_progress" for t in ts.values()):
                publish(force=True, state="blocked", task=None, doing="nothing ready: blocked or cyclic deps?")
                return log("nothing ready, nothing in progress, not all done: blocked/cyclic? run `tasks.py check`")
            log("nothing ready, waiting for others...")
            idle(30, state="waiting", doing="waiting for dependencies to finish")
            continue
        now = time.time()
        for tid in [tid for tid, until in avoid.items() if until <= now]:
            del avoid[tid]
        if not mine and avoid and all(t["id"] in avoid for t in tasks.ranked(ts)):
            log(f"only {', '.join(t['id'] for t in tasks.ranked(ts))} ready, which we gave up over budget: "
                "leaving it to another agent for now")
            idle(30, state="waiting", doing="leaving tasks we gave up to other agents: " + ", ".join(sorted(avoid)))
            continue
        job = JOB_RESUME.format(id=mine[0]["id"]) if mine else JOB_NEW
        if mine and tasks.merge_state():
            job += JOB_CONFLICT.format(op=tasks.merge_state(), files=" ".join(tasks.conflicted()) or "-")
        prompt = PROMPT.format(name=a.name, job=job)
        review = None  # over budget: this session must decide extend / split / release / hold instead of coding
        if mine:
            tid = mine[0]["id"]
            n, secs = spent.get(tid, [0, 0])
            extended = mine[0]["extended"] == a.owner
            if n >= a.task_budget + extended:
                review = (tid, extended)
                prompt = REVIEW.format(name=a.name, id=tid, n=n, mins=int(secs // 60),
                                       extend="" if extended else REVIEW_EXTEND.format(id=tid))
        log("session start: " + (f"REVIEW {review[0]} (over budget)" if review else
                                 f"resume {mine[0]['id']}" if mine else "claim next"))
        t0 = time.time()
        out = clone.parent / "logs" / f"{a.name}.session.txt"
        publish(force=True, state="working", task=mine[0]["id"] if mine else None,
                doing="reviewing: over budget" if review else "resuming" if mine else "claiming a task",
                doing_at=int(t0))
        final, buf, errored = [None], [""], [False]

        def drain(reader):
            buf[0] += reader.read()
            *lines, buf[0] = buf[0].split("\n")
            for line in lines:
                m = CLAIMED.search(line)
                if m and m.group(1) in ts:
                    status["task"] = m.group(1)
                    held.add(m.group(1))
                doing, result = describe(line, clone)
                if doing and doing.startswith("failed: "):
                    errored[0] = True
                if doing:
                    status.update(doing=doing, doing_at=int(time.time()))
                if result is not None:
                    final[0] = result
            publish()

        with open(out, "w", encoding="utf-8") as f, open(out, encoding="utf-8", errors="replace") as reader:
            env = {**os.environ, "TASK_AVOID": ",".join(sorted(avoid))}  # claim without id skips these
            child[0] = p = subprocess.Popen(agent_argv(a.cmd, prompt), cwd=clone, stdout=f, env=env,
                                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **detached())
            why = None
            while p.poll() is None:
                try:
                    p.wait(timeout=min(5, every))
                except subprocess.TimeoutExpired:
                    pass
                drain(reader)
                if p.poll() is not None:
                    break
                if stop_flag(clone.parent, a.name).exists():
                    shutdown()
                lost = beat()
                why = ("task taken over: " + ", ".join(sorted(lost))) if lost else (
                    f"timed out after {a.timeout}s; will resume" if time.time() - t0 > a.timeout else None)
                if why:
                    kill_tree(p.pid)
                    p.wait()
            drain(reader)
        child[0] = None
        tail = final[0] or " | ".join(filter(None, (describe(l, clone)[0] for l in
                                                    out.read_text(encoding="utf-8", errors="replace").splitlines()[-3:])))
        log(f"session {'killed, ' + why if why else f'exit {p.returncode}'}: {one_line(tail, 300)}")
        failed = not why and (p.returncode != 0 or errored[0])
        fails = fails + 1 if failed else 0
        if not failed and not (why or "").startswith("task taken over"):
            now_mine = {t["id"]: t for t in tasks.load().values()
                        if t["status"] == "in_progress" and t["owner"] == a.owner}
            for tid in now_mine:
                if review and tid == review[0]:
                    continue  # a review is not work on the task: it doesn't use up budget
                if tid not in {t["id"] for t in mine}:  # claimed in this session: a fresh budget
                    spent[tid] = [0, 0]
                s = spent.setdefault(tid, [0, 0])
                s[0], s[1] = s[0] + 1, s[1] + time.time() - t0
            if review and review[0] in now_mine:
                tid, was_extended = review
                if was_extended or now_mine[tid]["extended"] != a.owner:  # no extend, split, release or hold chosen
                    force_release(tid, f"over budget after {spent[tid][0]} sessions (~{int(spent[tid][1] // 60)} "
                                       f"min) and a review that decided nothing; still resumable: continue "
                                       f"from wip/{tid} (see git log)")
                    now_mine.pop(tid)
            if review and review[0] not in now_mine:  # released (by the review or by us), split or held
                avoid[review[0]] = time.time() + a.avoid
                spent.pop(review[0], None)
                held.discard(review[0])  # else the next heartbeat reports it LOST and kills the next session
        limit = limit_info(out.read_text(encoding="utf-8", errors="replace")) if failed else None
        if limit or fails >= 3:  # out of tokens (or broken for a while): hand the work to other machines
            reason = limit[0] if limit else f"{fails} failed sessions in a row: {one_line(tail, 100)}"
            hand_off(reason)
            wait_limited(reason, limit[1] if limit else None, bool(limit))
            fails = 0
        elif not why and time.time() - t0 < 20 and (p.returncode or final[0] is None):  # crashed
            idle(30, task=status["task"], state="backoff", doing=f"session ended after {int(time.time() - t0)}s "
                 f"(rate limit or error?): {one_line(tail, 80)}; retrying")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="c", required=True)
    s = sp.add_parser("start"); s.add_argument("n", type=int); s.add_argument("--cmd")
    s.add_argument("--yolo", action="store_true"); s.add_argument("--timeout", type=int, default=1800)
    s.add_argument("--lease", type=int, default=1200, help="seconds a dead agent's task stays blocked")
    s.add_argument("--first", type=int, default=1, help="number of first agent (to add more later)")
    s.add_argument("--stagger", type=float, default=3)
    s.add_argument("--host", help="name for this machine in owner names (default: $AGENT_HOST or hostname)")
    s.add_argument("--limit-wait", type=int, default=900, help="seconds to wait after a usage limit when "
                   "Claude doesn't say when it resets")
    s.add_argument("--task-budget", type=int, default=2, help="sessions per task before the agent must "
                   "review it: extend (once), split, hand off or hold")
    s.add_argument("--avoid", type=int, default=300, help="seconds an agent leaves a task it gave up over "
                   "budget to other agents before it may claim it again")
    s.set_defaults(f=cmd_start)
    sp.add_parser("status").set_defaults(f=cmd_status)
    s = sp.add_parser("stop"); s.set_defaults(f=cmd_stop)
    s.add_argument("--finish", action="store_true", help="claim no new tasks; each agent finishes its current "
                   "one, then exits (safe to shut down once all are stopped)")
    s = sp.add_parser("logs"); s.add_argument("n", type=int); s.set_defaults(f=cmd_logs)
    s = sp.add_parser("run"); s.add_argument("name"); s.add_argument("clone")
    s.add_argument("--owner", required=True); s.add_argument("--lease", type=int, default=1200)
    s.add_argument("--cmd", required=True); s.add_argument("--timeout", type=int, default=1800)
    s.add_argument("--limit-wait", type=int, default=900); s.add_argument("--task-budget", type=int, default=2)
    s.add_argument("--avoid", type=int, default=300)
    s.set_defaults(f=cmd_run)
    a = p.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
