#!/usr/bin/env python3
"""TFP (Task Flow Protocol) - git-native task board for humans and AI agents. Stdlib only.

One file per task in tasks/<id>.md. Git is the source of truth; a claim is a
commit that flips status todo -> in_progress and is pushed. If the push loses
the race, the claim is rolled back and you are told who won.

Leases: with TASK_LEASE=<seconds> set (agents.py does this), a claim records
`lease_until`, which the owner renews with `heartbeat`. An in_progress task whose
lease has expired is "stale" and can be claimed again (a takeover), so a crashed
machine cannot block the board. Claims without TASK_LEASE (humans) never expire.
"""
import argparse, calendar, json, os, re, subprocess, sys, time
from pathlib import Path

if __name__ == "__main__" and os.name == "nt" and not sys.flags.utf8_mode:  # Windows: UTF-8 for files,
    os.environ["PYTHONUTF8"] = "1"  # git output and every child process
    try:
        sys.exit(subprocess.call([sys.executable, *sys.argv]))
    except KeyboardInterrupt:
        sys.exit(130)

ROOT = Path(os.environ.get("TASKS_ROOT", ".")).resolve()
DIR = ROOT / "tasks"
STATUSES = ["todo", "in_progress", "done", "dropped"]
CLOSED = ("done", "dropped")  # finished for the board: nothing left to do
COLORS = {"todo": "#e5e7eb", "in_progress": "#fde68a", "done": "#86efac", "dropped": "#f3f4f6"}
FREEZE = "FROZEN"  # tasks/FROZEN holds the reason while the board is frozen (no new claims)
# "- ASSUMPTION (<when> by <who>): <text>", reviewed: "- ASSUMPTION (<when> by <who>) [confirmed <when> by <who>]: ..."
# or "... [rejected <when> by <who>: <note>]: ..." (`confirm` / `reject` rewrite the line, so the history stays)
ASSUMPTION = re.compile(r"^- ASSUMPTION \((.+?) by ([^)]+)\)"
                        r"(?: \[(confirmed|rejected) (\S+ \S+) by ([^:\]]+)(?:: ([^\]]*))?\])?: (.*)$", re.M)
REVIEWED = {"confirm": "confirmed", "reject": "rejected"}
LEASE_FMT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_LEASE = 1200


# ---------- model ----------
def parse(path):
    return parse_text(path.read_text(), path.stem, path)


def parse_text(text, tid, path=None):
    m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
    meta, body = {}, text
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    deps, touches = ([d.strip() for d in meta.get(k, "").strip("[]").split(",") if d.strip()] for k in ("deps", "touches"))
    return dict(id=tid, path=path, title=meta.get("title", tid),
                status=meta.get("status", "todo"), owner=meta.get("owner", ""),
                lease_until=meta.get("lease_until", ""), attempts=int(meta.get("attempts") or 1),
                hold=meta.get("hold", ""), extended=meta.get("extended", ""), parent=meta.get("parent", ""),
                releases=int(meta.get("releases") or 0), subtasks=meta.get("subtasks", ""),
                quality=meta.get("quality", ""), quality_reason=meta.get("quality_reason", ""),
                deps=deps, touches=touches, prio=int(meta.get("prio", "2") or 2), body=body)


def lease_end(t):
    """Lease expiry as epoch seconds; None if the task has no (valid) lease."""
    try:
        return calendar.timegm(time.strptime(t["lease_until"], LEASE_FMT))
    except ValueError:
        return None


def new_lease(secs):
    return time.strftime(LEASE_FMT, time.gmtime(time.time() + secs))


def is_stale(t):
    end = lease_end(t)
    return t["status"] == "in_progress" and end is not None and end < time.time()


def load():
    return {t["id"]: t for t in map(parse, sorted(DIR.glob("*.md")))}


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:  # LF on every OS: no line-ending churn
        f.write(text)


def set_fields(t, **kw):
    text = t["path"].read_text()
    for k, v in kw.items():
        text, n = re.subn(rf"^{k}:.*$", lambda _: f"{k}: {v}", text, count=1, flags=re.M)
        if not n:
            text = text.replace("\n---\n", f"\n{k}: {v}\n---\n", 1)
    write(t["path"], text)


def analyze(ts):
    """Adds: stale, ready, dependents (transitive, open count), level (layer in graph), orphaned (dropped deps)."""
    rev = {i: [] for i in ts}
    for t in ts.values():
        for d in t["deps"]:
            if d in rev:
                rev[d].append(t["id"])

    def closure(i, seen=None):
        seen = set() if seen is None else seen
        for c in rev[i]:
            if c not in seen:
                seen.add(c)
                closure(c, seen)
        return seen

    memo = {}

    def level(i, stack=()):
        if i in stack:
            return 0
        if i not in memo:
            memo[i] = 1 + max([level(d, stack + (i,)) for d in ts[i]["deps"] if d in ts], default=-1)
        return memo[i]

    for i, t in ts.items():
        t["dependents"] = len([c for c in closure(i) if ts[c]["status"] not in CLOSED])
        t["level"] = level(i)
        t["stale"] = is_stale(t)
        t["ready"] = (t["status"] == "todo" or t["stale"]) and not t["hold"] and all(
            d in ts and ts[d]["status"] == "done" for d in t["deps"])
        t["orphaned"] = [d for d in t["deps"] if t["status"] not in CLOSED and ts.get(d, {}).get("status") == "dropped"]
    return ts


def assumption_entries(t):
    """Every assumption recorded on a task, in file order. `n` (from 1) names it for confirm/reject;
    `review` is "" while it waits for a human, else "confirmed" / "rejected"."""
    return [dict(n=n, at=m[0], who=m[1], review=m[2], reviewed_at=m[3], reviewed_by=m[4].strip(), note=m[5],
                 text=m[6]) for n, m in enumerate(ASSUMPTION.findall(t["body"]), 1)]


def assumptions(t):
    """[(when, who, text)] recorded with `tasks.py assume` that no human has confirmed or rejected yet."""
    return [(e["at"], e["who"], e["text"]) for e in assumption_entries(t) if not e["review"]]


def criteria(text):
    """The acceptance criteria of a task file: [(line index, mark, text)], mark " " = open, "x" = verified,
    "~" = skipped with a reason (both via `verify`). Empty boxes (the placeholder of `new`) don't count."""
    out, inside = [], False
    for i, line in enumerate(text.split("\n")):
        if line.startswith("## "):
            inside = line[3:].strip().lower().startswith("acceptance criteria")
        elif inside:
            m = re.match(r"- \[([ xX~])\] (.*\S)", line)
            if m:
                out.append((i, m[1].lower(), m[2]))
    return out


def criteria_entries(t):
    """The acceptance criteria of a parsed task, numbered from 1 as `verify` counts them, each with the
    evidence lines recorded under it (for the dashboard)."""
    lines = t["body"].split("\n")
    out = []
    for n, (i, mark, text) in enumerate(criteria(t["body"]), 1):
        ev, j = [], i + 1
        while j < len(lines) and lines[j].startswith("  "):
            ev.append(re.sub(r"^- ", "", lines[j].strip()))
            j += 1
        out.append(dict(n=n, mark=mark, text=text, evidence=ev))
    return out


def overlaps(a, b):
    """The paths two `touches` lists share: the same path, or one inside the other (the inner one)."""
    def norm(p):
        p = p.strip().strip("/")
        return "" if p == "." else p[2:] if p.startswith("./") else p
    out = set()
    for x in map(norm, a):
        for y in map(norm, b):
            if x == y or not x or not y or y.startswith(x + "/") or x.startswith(y + "/"):
                out.add(max(x, y, key=len) or "/")
    return sorted(out)


def crowded(t, ts, who):
    """[(task, paths)]: tasks someone else is working on now that touch the same files as t."""
    out = []
    for o in ts.values():
        if o["id"] != t["id"] and o["status"] == "in_progress" and not o["stale"] and o["owner"] != who:
            paths = overlaps(t["touches"], o["touches"])
            if paths:
                out.append((o, paths))
    return out


def overlap_warnings(ts):
    """Unfinished tasks that touch the same files but may run in parallel (neither depends on the other)."""
    def ancestors(i, seen=None):
        seen = set() if seen is None else seen
        for d in ts[i]["deps"]:
            if d in ts and d not in seen:
                seen.add(d)
                ancestors(d, seen)
        return seen
    open_ = sorted(i for i, t in ts.items() if t["status"] not in CLOSED and t["touches"])
    anc = {i: ancestors(i) for i in open_}
    out = []
    for n, i in enumerate(open_):
        for j in open_[n + 1:]:
            paths = overlaps(ts[i]["touches"], ts[j]["touches"])
            if paths and i not in anc[j] and j not in anc[i]:
                out.append(f"{i} and {j} both touch {', '.join(paths)} and may run in parallel: "
                           "add a dep between them, or split the files")
    return out


def problems(ts):
    out = []
    for t in ts.values():
        if t["status"] not in STATUSES:
            out.append(f"{t['id']}: bad status '{t['status']}'")
        if t["status"] == "in_progress" and not t["owner"]:
            out.append(f"{t['id']}: in_progress without owner")
        if t["quality"] not in ("", "required", "exempt"):
            out.append(f"{t['id']}: bad quality '{t['quality']}' (want required or exempt)")
        if t["quality"] == "exempt" and not t["quality_reason"]:
            out.append(f"{t['id']}: quality: exempt needs a quality_reason (why there is no code to review)")
        if t["lease_until"] and lease_end(t) is None:
            out.append(f"{t['id']}: bad lease_until '{t['lease_until']}' (want {LEASE_FMT})")
        for d in t["deps"]:
            if d not in ts:
                out.append(f"{t['id']}: unknown dependency '{d}'")
            elif ts[d]["status"] == "dropped" and t["status"] not in CLOSED:
                out.append(f"{t['id']}: depends on dropped '{d}': edit its deps, drop it too, or undrop {d}")
    color = {}

    def dfs(i, path):
        color[i] = 1
        for d in ts[i]["deps"]:
            if d not in ts:
                continue
            if color.get(d) == 1:
                out.append("cycle: " + " -> ".join(path + [i, d]))
            elif d not in color:
                dfs(d, path + [i])
        color[i] = 2
    for i in ts:
        if i not in color:
            dfs(i, [])
    return out


def skipped_here():
    """Paths this machine can't work on (TASK_SKIP_TOUCHES, comma-separated, e.g. `android` where no
    Android SDK is installed): tasks touching any of them are never offered or claimed here."""
    return [p.strip() for p in os.environ.get("TASK_SKIP_TOUCHES", "").split(",") if p.strip()]


def skipped(t):
    """The TASK_SKIP_TOUCHES paths task t touches ([] = it can be done here)."""
    skip = skipped_here()
    return overlaps(t["touches"], skip) if skip and t["touches"] else []


def ranked(ts):
    r = [t for t in ts.values() if t["ready"] and not skipped(t)]
    return sorted(r, key=lambda t: (-t["dependents"], t["prio"], t["id"]))


def avoided():
    """Tasks this agent gave up over budget (TASK_AVOID, set by agents.py): offered last, so another
    agent continues them first; still offered when nothing else is ready."""
    return {i.strip() for i in os.environ.get("TASK_AVOID", "").split(",") if i.strip()}


def offered(ts):
    """ranked(), with the tasks this agent should leave to others moved to the end: first those that
    touch the same files as a task someone is working on (conflicts), last the ones it gave up."""
    skip, who = avoided(), me()
    return sorted(ranked(ts), key=lambda t: (t["id"] in skip, bool(crowded(t, ts, who))))  # stable: keeps the rank otherwise


# ---------- git ----------
def git(*a, check=True, env=None):
    p = subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    if check and p.returncode:
        sys.exit(f"git {' '.join(a)} failed:\n{p.stderr.strip()}")
    return p


PUSH_FAILURE = "tfp-push-failed"  # in the git dir: the push the remote refused last, read by agents.py
CMD, push_error = [""], [""]  # the tasks.py command running, git's error of the last failed push
REASON = [""]  # the reason of a `hold` running: the runner retries the hold with it once pushes work


def pushed(*a):
    """git push; returns True if it worked, else remembers git's error (on one line) for push_failed()."""
    p = git("push", *a, check=False)
    if p.returncode:
        push_error[0] = " | ".join(l.strip() for l in p.stderr.splitlines() if l.strip()) or f"exit {p.returncode}"
    return p.returncode == 0


def push_failed(tid, hint):
    """Exit with `PUSH FAILED: <git error>` and a hint, no traceback. Also leaves the failure in the git
    dir for the runner (agents.py), so it stops starting sessions on a task whose pushes keep failing."""
    err = push_error[0] or "unknown error"
    try:
        f = ROOT / git("rev-parse", "--git-path", PUSH_FAILURE).stdout.strip()
        f.write_text(json.dumps(dict(task=tid, cmd=CMD[0], error=err, at=int(time.time()),
                                     **({"reason": REASON[0]} if REASON[0] else {}))))
    except OSError:
        pass
    sys.exit(f"PUSH FAILED: {err}\n{hint}")


def upstream():
    p = git("rev-parse", "--abbrev-ref", "@{u}", check=False)
    return p.stdout.strip() if p.returncode == 0 else None


CONFLICT_HELP = """Your work is safe. Follow AGENTS.md "Merge conflicts":
  a. obvious fix: edit the files, `git add` them, `git rebase --continue` (or `git commit` for a merge)
  b. many conflicts: `git rebase --abort`, then `git merge origin/main` (resolve once, not per commit)
  c. unclear: `python tasks.py redo <id>` (saves this attempt, restarts from main; use it as a reference)
  d. the tasks disagree on design: `python tasks.py release <id> --note "..."` after (c)"""


def merge_state():
    """'rebase' / 'merge' if one is unfinished in ROOT (usually conflicts), else None."""
    for op, marker in (("rebase", "rebase-merge"), ("rebase", "rebase-apply"), ("merge", "MERGE_HEAD")):
        if (ROOT / git("rev-parse", "--git-path", marker).stdout.strip()).exists():
            return op
    return None


def conflicted():
    return git("diff", "--name-only", "--diff-filter=U", check=False).stdout.split()


def no_unfinished_merge():
    op = merge_state()
    if op:
        sys.exit(f"CONFLICT: a {op} is in progress (unresolved: {' '.join(conflicted()) or 'none'}); "
                 f"finish or abort it first.\n{CONFLICT_HELP}")


def sync():
    no_unfinished_merge()
    if upstream() and git("pull", "--rebase", "--autostash", check=False).returncode:
        if merge_state():
            sys.exit(f"CONFLICT while catching up with main in: {' '.join(conflicted())}\n{CONFLICT_HELP}")
        git("pull", "--rebase", "--autostash")  # not a conflict: fail with git's own message


def check_cmd():
    """The project's validation command: $TASK_CHECK, else the first line of .taskcheck, else None."""
    if os.environ.get("TASK_CHECK"):
        return os.environ["TASK_CHECK"]
    f = ROOT / ".taskcheck"
    lines = [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")] if f.exists() else []
    return lines[0] if lines else None


def check_limits():
    """(timeout, slow) in seconds for the check: TASK_CHECK_TIMEOUT (default 900: it must end well within
    an agent's 30-min session) and TASK_CHECK_SLOW (default 120: above this, `done` warns)."""
    return (int(os.environ.get("TASK_CHECK_TIMEOUT") or 900), int(os.environ.get("TASK_CHECK_SLOW") or 120))


def run_check(cmd, timeout=None):
    """Run .taskcheck in sh on every OS (on Windows: Git for Windows' bash), so one command line works
    for the whole team. Output is decoded as UTF-8 and never fails on bytes that aren't (a tool printing
    in the console's code page must not hide the failure). After `timeout` seconds the check and
    everything it started is killed; the result then has timed_out set."""
    argv = ["/bin/sh", "-c", cmd]
    if os.name == "nt":
        git_root = Path(git("--exec-path").stdout.strip()).parents[2]  # <Git>/mingw64/libexec/git-core
        bash = [b for b in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe") if b.exists()]
        argv = [str(bash[0]), "-c", cmd] if bash else ["cmd", "/c", cmd]
    p = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", start_new_session=os.name != "nt")
    timed_out = False
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:  # kill the whole tree: a test server left running would hold the pipes
        timed_out = True
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, 9)
        out, err = p.communicate()
    r = subprocess.CompletedProcess(argv, p.returncode, out, err)
    r.timed_out = timed_out
    return r


def dirty():
    return git("status", "--porcelain", "--untracked-files=no").stdout.strip()


def code_changed(since):
    """True if anything outside tasks/ changed between commit `since` and HEAD (heartbeats don't count)."""
    return git("diff", "--quiet", since, "HEAD", "--", ".", ":(exclude)tasks", check=False).returncode != 0


def me():
    return (os.environ.get("TASK_OWNER") or git("config", "user.name", check=False).stdout.strip()
            or "unknown")


def remote():
    u = upstream()
    return u.split("/", 1)[0] if u else None


def publish(paths, msg):
    """Commit the working-tree state of `paths` (task files) directly on top of the upstream branch and
    push only that commit, so a status change never carries unfinished local commits to main (only `done`
    pushes code, after the check). Local commits are then replayed on top of it. Returns False if the push
    lost a race. Either way the files are back as HEAD has them, so the caller can re-read and retry."""
    up = upstream()
    r, branch = up.split("/", 1)
    base = git("rev-parse", up).stdout.strip()
    rels = [Path(p).resolve().relative_to(ROOT).as_posix() for p in paths]
    index = ROOT / git("rev-parse", "--git-path", "tfp-index").stdout.strip()
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}  # a private index: the agent's staged work stays
    try:
        git("read-tree", base, env=env)
        for rel in rels:
            if (ROOT / rel).exists():
                blob = git("hash-object", "-w", "--", rel).stdout.strip()
                git("update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}", env=env)
            else:
                git("update-index", "--force-remove", "--", rel, env=env)
        tree = git("write-tree", env=env).stdout.strip()
    finally:
        index.unlink(missing_ok=True)
    new = git("commit-tree", tree, "-p", base, "-m", msg).stdout.strip()
    ok = pushed("-q", r, f"{new}:refs/heads/{branch}")
    tracked = set(git("ls-tree", "-r", "--name-only", "HEAD", "--", *rels).stdout.split())
    if tracked:
        git("checkout", "HEAD", "--", *sorted(tracked))
    for rel in rels:
        if rel not in tracked:
            (ROOT / rel).unlink(missing_ok=True)
    if not ok:
        return False
    git("update-ref", f"refs/remotes/{up}", new)
    if git("rebase", "-q", "--autostash", "--onto", new, base, check=False).returncode:
        git("rebase", "--abort", check=False)
        print(f"warning: could not replay your local commits on top of {up}: run `git pull --rebase`",
              file=sys.stderr)
    return True


def update(tid, msg, guard, append=None, extra=None, with_code=False, **fields):
    """Compare-and-swap on one task file. Pull, let guard(t) veto (returns an error string) on the
    freshest state, commit the field change, push. The push only succeeds if nobody pushed in between;
    if it is rejected, drop our commit and start over, so the guard always judges the winner's state.
    `append` is a line (or append(t) returns it) added to the end of the file (the Notes section). `extra(t)` may write more files
    (returns their paths) that go into the same commit. Only the task files are pushed, never local code
    commits, unless `with_code` (done, after the check). Returns (task, error)."""
    for _ in range(5):
        sync()
        ts = analyze(load())
        if tid not in ts:
            sys.exit(f"no such task: {tid}")
        t = ts[tid]
        err = guard(t)
        if err:
            return t, err
        m = msg(t) if callable(msg) else msg
        set_fields(t, **fields)
        line = append(t) if callable(append) else append
        if line:
            write(t["path"], t["path"].read_text().rstrip("\n") + f"\n{line}\n")
        paths = [str(t["path"]), *map(str, extra(t) if extra else [])]
        if not git("status", "--porcelain", "--", *paths).stdout.strip():
            return t, None
        if upstream() and not with_code:
            if publish(paths, m):
                return t, None
            continue  # lost the push race: re-read, re-judge
        git("add", *paths)
        git("commit", "-m", m, "--", *paths)
        if not upstream() or pushed():
            return t, None
        git("reset", "--keep", "HEAD~1")  # lost the push race: forget our commit, re-read, re-judge
    push_failed(tid, f"Could not record '{m}' on the board after 5 attempts: the remote "
                "refuses the push (auth, permissions, a hook?), so retrying won't help. Nothing changed on the board. "
                f"Only a human can fix it: stop working on {tid} and end the session.")


def owned_by_me(t):
    if t["status"] != "in_progress" or t["owner"] != me():
        return f"{t['id']} is {t['status']} (owner: {t['owner'] or '-'}), not owned by {me()}"
    return None


def frozen(ref=None):
    """The freeze reason, or None. With ref: as on that commit (the dashboard reads origin/main)."""
    if ref:
        p = git("show", f"{ref}:tasks/{FREEZE}", check=False)
        return (p.stdout.strip() or "frozen") if p.returncode == 0 else None
    f = DIR / FREEZE
    return (f.read_text().strip() or "frozen") if f.exists() else None


def set_frozen(reason):
    """Create (reason) or remove (None) tasks/FROZEN in one pushed commit, retrying lost push races."""
    f = DIR / FREEZE
    for _ in range(5):
        sync()
        if (reason is None) == (not f.exists()):
            return False
        if reason is None:
            f.unlink()
        else:
            write(f, reason + "\n")
        m = f"{'freeze' if reason else 'unfreeze'}: board by {me()}"
        if not upstream():
            git("add", "-A", "--", str(f))
            git("commit", "-qm", m, "--", str(f))
            return True
        if publish([str(f)], m):
            return True
    sys.exit("could not push after 5 attempts (network/auth problem?)")


def wip_branch(tid):
    return f"wip/{tid}"


def attempts(tid):
    """Saved earlier attempts of a task (by `redo`), oldest first."""
    r = remote()
    out = (git("ls-remote", "--heads", r, f"{wip_branch(tid)}-attempt*", check=False).stdout if r
           else git("branch", "--list", "--format=refs/heads/%(refname:short)", f"{wip_branch(tid)}-attempt*").stdout)
    names = [l.split("refs/heads/", 1)[1] for l in out.splitlines() if "refs/heads/" in l]
    return sorted(names, key=lambda n: int(n.rsplit("attempt", 1)[1] or 0))


# ---------- commands ----------
def cmd_new(a):
    DIR.mkdir(exist_ok=True)
    p = DIR / f"{a.id}.md"
    if p.exists():
        sys.exit("exists")
    touches = f"touches: [{a.touches}]\n" if a.touches else ""
    if a.quality_exempt is not None and not a.quality_exempt.strip():
        sys.exit("--quality-exempt needs a reason (why this task produces no code to test and review)")
    exempt = f"quality: exempt\nquality_reason: {a.quality_exempt.strip()}\n" if a.quality_exempt else ""
    write(p, f"---\ntitle: {a.title}\nstatus: todo\nowner:\ndeps: [{a.deps}]\n{touches}{exempt}prio: {a.prio}\n---\n"
                 "## Goal\n\n## Acceptance criteria\n- [ ] \n\n## Notes\n")
    print(f"created {p.relative_to(ROOT)}")


def cmd_list(a):
    ts = analyze(load())
    print(f"{'ID':24} {'STATUS':12} {'OWNER':20} {'UNBLOCKS':8} READY  TITLE")
    for t in sorted(ts.values(), key=lambda t: (t["level"], -t["dependents"], t["id"])):
        if a.status and t["status"] != a.status:
            continue
        st = "HOLD" if t["hold"] else "stale" if t["stale"] else t["status"]
        print(f"{t['id']:24} {st:12} {t['owner'][:20]:20} {t['dependents']:<8} "
              f"{'yes' if t['ready'] else '-':6} {t['title']}")


def cmd_next(a):
    sync()
    if frozen():
        return print(f"FROZEN: {frozen()}\nThe board is frozen while humans change the plan: claim nothing new. "
                     "Finish a task you already own; otherwise stop.")
    ts = analyze(load())
    r, who = offered(ts), me()
    if not r:
        print("no ready tasks (all claimed, done, or blocked)")
    for t in r[:a.n]:
        stale = f"  (stale: takeover from {t['owner']})" if t["stale"] else ""
        gave_up = "  (you gave it up: leave it to another agent if you can)" if t["id"] in avoided() else ""
        busy = "".join(f"  (same files as {o['id']}, in progress: {', '.join(p)})" for o, p in crowded(t, ts, who))
        print(f"{t['id']:24} unblocks {t['dependents']:<3} prio {t['prio']}  {t['title']}{stale}{gave_up}{busy}")


def cmd_claim(a):
    sync()
    if frozen() and not a.force:
        sys.exit(f"FROZEN: {frozen()}\nThe board is frozen while humans change the plan: claim nothing new, "
                 "stop. (Humans: --force claims anyway.)")
    ts = analyze(load())
    tid = a.id or (offered(ts)[0]["id"] if offered(ts) else None)
    if not tid or tid not in ts:
        sys.exit("no such / no ready task")
    who = a.who or me()
    prev = {}

    def guard(t):
        if t["status"] == "dropped":
            return f"{tid} was dropped (the plan changed): pick another task: python tasks.py next"
        if t["status"] != "todo" and not t["stale"]:
            return (f"LOST RACE: {tid} is already {t['status']} (owner: {t['owner']}). "
                    "Pick another task: python tasks.py next")
        if t["hold"] and not a.force:
            return f"{tid} is on hold: {t['hold']}"
        if not t["ready"] and not a.force:
            return f"{tid} has unfinished dependencies: {[d for d in t['deps'] if ts.get(d, {}).get('status') != 'done']}"
        if skipped(t) and not a.force:
            return (f"{tid} touches {', '.join(skipped(t))}, which this machine skips (TASK_SKIP_TOUCHES): "
                    "leave it to another machine. Pick another task: python tasks.py next")
        prev["owner"] = t["owner"] if t["stale"] else ""
        return None

    lease = int(os.environ.get("TASK_LEASE", "0") or 0)
    t, err = update(tid, lambda t: f"claim: {tid} by {who}" + (f" (takeover from {prev['owner']})" if prev["owner"] else ""),
                    guard, status="in_progress", owner=who, lease_until=new_lease(lease) if lease else "")
    if err:
        sys.exit(err)
    print(f"CLAIMED {tid} as {who}" + (f" (took over from {prev['owner']}, whose lease expired)" if prev["owner"] else ""))
    r = remote()
    if r and git("ls-remote", "--exit-code", "--heads", r, wip_branch(tid), check=False).returncode == 0:
        print(f"\nEarlier unfinished work for this task is on {r}/{wip_branch(tid)}. Start from it:\n"
              f"  git fetch {r} && git merge {r}/{wip_branch(tid)}")
    for b in attempts(tid):
        print(f"\nAbandoned earlier attempt (reference only, do not merge): git diff HEAD...{r or ''}{'/' if r else ''}{b}")
    for o, paths in crowded(t, analyze(load()), who):
        print(f"\nHeads-up: {o['id']} ({o['owner']}) is in progress and touches the same files ({', '.join(paths)}). "
              "Run `git pull --rebase` often, keep your changes there small, and don't redo what it does.")
    print(f"\n{t['path'].read_text()}")


def open_criteria(tid):
    """REFUSED message if tid has acceptance criteria nobody verified (or skipped with a reason), else None."""
    t = load().get(tid)
    left = [f"  #{n} {c}" for n, (_, mark, c) in enumerate(criteria(t["path"].read_text()) if t else [], 1)
            if mark == " "]
    return None if not left else (
        f"REFUSED: {tid} has acceptance criteria that nobody verified:\n" + "\n".join(left) +
        f"\nCheck each one, then: python tasks.py verify {tid} <n> \"<what you ran and its result>\". Only what "
        "actually ran counts: a test that was found but not run, a build that was not tried, or \"should work\" "
        f"is no evidence. If one can't be met or verified here: python tasks.py verify {tid} <n> --skip "
        "\"<why>\" (and `assume` what follows from it), or hold the task if a person is needed.")


# ---------- quality gate ----------
# Opt-in: a project switches it on by committing docs/QUALITY.md (template: templates/quality/QUALITY.md).
# Then every task must have committed review evidence before `done` accepts it (docs/QUALITY.md):
# reviews/<id>/tests.md (test-writer), review.md (code-reviewer), fixes.md (fixer, when fixes were
# required) and quality.json (the parent's record). Only a task classified `quality: exempt` with a
# `quality_reason:` when it was claimed skips it. --force and --skip-check never do.
QUALITY_SCHEMA = 1
QUALITY_DOC = "docs/QUALITY.md"
REPORT = {"test-writer": "tests.md", "code-reviewer": "review.md", "fixer": "fixes.md"}
REPORT_SECTION = {"tests.md": "## Coverage", "review.md": "## Review conclusion", "fixes.md": "## Final status"}
PLACEHOLDER = re.compile(r"<(?:YYYY-MM-DD|task-id[^>]*|resolved Git scope|sources used|hash[^>]*|count|"
                         r"command[^>]*|path|number|Pass / Fail[^>]*|bug title|finding-id|title|"
                         r"current task or supplied scope|P[0-3] count)>|\bTODO\b|\bTBD\b|lorem ipsum", re.I)
MIN_REPORT_CHARS = 200  # non-whitespace characters: a heading and "ok" is not a report
RESOLVED = {"fixed", "invalid", "out_of_scope", "deferred", "accepted"}  # "open" = unresolved
FIXED_ONLY = {"P0", "P1"}  # blocking severities: must be fixed or shown invalid
QUALITY_EXCLUDE = (":(exclude)tasks", ":(exclude)reviews")  # bookkeeping, not code: may change after review


def committed(path, ref="HEAD"):
    """Content of `path` in commit `ref`, or None."""
    p = git("show", f"{ref}:{path}", check=False)
    return p.stdout if p.returncode == 0 else None


def task_patch_id(head, base):
    """Stable patch-id of the task's code change base..head (tasks/ and reviews/ excluded), "" if none.
    It survives rebases and merges of main, and changes when the task's own code changes."""
    diff = git("diff", "-U0", "--no-color", "--no-ext-diff", base, head, "--", ".", *QUALITY_EXCLUDE).stdout
    if not diff.strip():
        return ""
    p = subprocess.run(["git", "-C", str(ROOT), "patch-id", "--stable"], input=diff, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.stdout.split()[0] if p.stdout.split() else ""


def claimed_version(tid):
    """The task file as it was when it was last claimed (the claim commit on main), or None."""
    ref = upstream() or "HEAD"
    sha = git("log", ref, "-n1", "--format=%H", "--fixed-strings", f"--grep=claim: {tid} by", check=False).stdout.strip()
    text = committed(f"tasks/{tid}.md", sha) if sha else None
    return parse_text(text, tid) if text else None


def quality_exemption(t):
    """(exempt, error): an exemption counts only if it was classified at planning, i.e. was already in the
    task file when it was claimed; a worker adding it to its own task does not count."""
    if t["quality"] != "exempt":
        return False, None
    c = claimed_version(t["id"])
    if not c or c["quality"] != "exempt" or not c["quality_reason"]:
        return False, (f"{t['id']} says `quality: exempt`, but it was not classified so (with a quality_reason) "
                       "when it was claimed. Exemptions are decided while planning the board, not by the worker.")
    return True, None


def quality_errors(tid, ref="HEAD"):
    """Everything missing or stale in the committed review evidence of task `tid` at `ref`."""
    d, errs = f"reviews/{tid}", []
    raw = committed(f"{d}/quality.json", ref)
    if raw is None:
        return [f"{d}/quality.json is missing" + (" (it exists but is not committed)"
                                                  if (ROOT / d / "quality.json").exists() else "")]
    try:
        q = __import__("json").loads(raw)
    except ValueError as e:
        return [f"{d}/quality.json is not valid JSON: {e}"]
    if not isinstance(q, dict):
        return [f"{d}/quality.json must be a JSON object"]
    if q.get("schema") != QUALITY_SCHEMA:
        errs.append(f"quality.json: schema must be {QUALITY_SCHEMA}")
    if q.get("task") != tid:
        errs.append(f"quality.json: task is {q.get('task')!r}, not {tid!r} (evidence of another task?)")
    helpers = q.get("helpers") if isinstance(q.get("helpers"), dict) else {}
    fixes = q.get("fixes_required")
    if not isinstance(fixes, bool):
        errs.append("quality.json: fixes_required must be true or false")
    needed = ["test-writer", "code-reviewer"] + (["fixer"] if fixes else [])
    for h in needed:
        e = helpers.get(h) if isinstance(helpers.get(h), dict) else {}
        if e.get("status") != "completed":
            errs.append(f"quality.json: helpers.{h}.status is {e.get('status')!r}, must be \"completed\" "
                        f"(invoke the {h} sub-agent)")
        path = f"{d}/{REPORT[h]}"
        if e.get("report") != path:
            errs.append(f"quality.json: helpers.{h}.report must be {path!r}")
        text = committed(path, ref)
        if text is None:
            errs.append(f"{path} is missing" + (" (it exists but is not committed)" if (ROOT / path).exists() else ""))
            continue
        if len(re.sub(r"\s", "", text)) < MIN_REPORT_CHARS:
            errs.append(f"{path} is empty or too short to be a report")
        ph = PLACEHOLDER.search(text)
        if ph:
            errs.append(f"{path} still contains the placeholder {ph[0]!r}")
        if not re.search(rf"^Task:\s*[`*]*{re.escape(tid)}(?![\w-])", text, re.M):
            errs.append(f"{path} does not name the task {tid!r} (a 'Task: {tid} ...' line)")
        if REPORT_SECTION[REPORT[h]] not in text:
            errs.append(f"{path} has no '{REPORT_SECTION[REPORT[h]]}' section")
    if fixes is False:
        e = helpers.get("fixer") if isinstance(helpers.get("fixer"), dict) else {}
        if e.get("status") != "not_required" or not str(e.get("reason", "")).strip():
            errs.append('quality.json: without fixes, helpers.fixer must be {"status": "not_required", '
                        '"reason": "<why>"}')
    findings = q.get("findings")
    if not isinstance(findings, list):
        errs.append("quality.json: findings must be a list (empty when there were none)")
        findings = []
    for f in findings:
        f = f if isinstance(f, dict) else {}
        fid, disp, sev = f.get("id", "?"), f.get("disposition"), str(f.get("severity", "")).upper()
        if disp not in RESOLVED:
            errs.append(f"finding {fid}: disposition {disp!r} is unresolved (want one of {', '.join(sorted(RESOLVED))})")
        elif sev in FIXED_ONLY and disp not in ("fixed", "invalid"):
            errs.append(f"finding {fid}: {sev} is blocking, so it must be fixed or shown invalid, not {disp!r}")
        elif disp != "fixed" and not str(f.get("note", "")).strip():
            errs.append(f"finding {fid}: a {disp!r} finding needs a note saying why")
        if disp == "fixed" and fixes is False:
            errs.append(f"finding {fid} is fixed, so fixes_required must be true (with a fixer report)")
    val = q.get("validation")
    if not isinstance(val, list) or not val:
        errs.append("quality.json: validation must list the final commands run and their results")
    else:
        for v in val:
            v = v if isinstance(v, dict) else {}
            if not str(v.get("command", "")).strip() or v.get("result") != "pass":
                errs.append(f"validation {v.get('command')!r}: result is {v.get('result')!r}, must be \"pass\"")
    rev = str(q.get("reviewed_revision", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", rev):
        errs.append("quality.json: reviewed_revision must be the full commit hash the helpers reviewed")
    elif git("cat-file", "-e", f"{rev}^{{commit}}", check=False).returncode:
        errs.append(f"reviewed_revision {rev[:9]} is not a known commit: review the current code again")
    else:
        main = upstream() or ref
        then = git("merge-base", rev, main, check=False).stdout.strip()
        now = git("merge-base", ref, main, check=False).stdout.strip()
        if not then or not now or task_patch_id(rev, then) != task_patch_id(ref, now):
            errs.append(f"the task's code changed after the review of {rev[:9]} (only reviews/ and tasks/ may "
                        "change afterwards): run test-writer and code-reviewer on the current code, then update "
                        "the reports and reviewed_revision")
    return errs


def quality_enabled():
    """The gate is on when docs/QUALITY.md is committed, on HEAD or on main: deleting it in a task's
    own commits does not switch it off."""
    return any(committed(QUALITY_DOC, r) is not None for r in ("HEAD", upstream()) if r)


def quality_gate(tid):
    """REFUSED message if the task's committed review evidence is missing, incomplete or stale, else None."""
    t = load().get(tid)
    if not t or not quality_enabled():
        return None
    exempt, err = quality_exemption(t)
    if err:
        return f"REFUSED: {err}"
    if exempt:
        print(f"quality gate: {tid} is exempt ({t['quality_reason']})", flush=True)
        return None
    errs = quality_errors(tid)
    return None if not errs else (
        f"REFUSED: {tid} has no complete, current review evidence:\n" + "\n".join(f"  - {e}" for e in errs) +
        f"\nRun the quality workflow (AGENTS.md \"Quality gate\"): the test-writer writes reviews/{tid}/tests.md, "
        f"the code-reviewer reviews/{tid}/review.md, the fixer reviews/{tid}/fixes.md when fixes are needed; "
        f"you record reviews/{tid}/quality.json (docs/QUALITY.md) and commit them with the code. "
        "--force and --skip-check do not bypass this.")


def cmd_done(a):
    """Catch up with main, check every acceptance criterion was verified and the review evidence is
    complete and current, run the project check on the merged result, then mark done and push, with a
    note of what was checked. If main moved (code, not just task files) while the check ran, check again."""
    no_unfinished_merge()
    if dirty():
        sys.exit("REFUSED: uncommitted changes. Commit your work first (it is pushed with `done`).")
    sync()
    err = None if a.force else open_criteria(a.id)
    if err:
        sys.exit(err)
    cmd = None if a.skip_check else check_cmd()
    if not cmd and not a.skip_check:
        print("warning: no check configured (.taskcheck or TASK_CHECK); marking done without testing the merge")
    timeout, slow = check_limits()
    for _ in range(3):
        sync()
        tested = git("rev-parse", "HEAD").stdout.strip()
        err = quality_gate(a.id)  # on the merged result, every round: no option skips it
        if err:
            sys.exit(err)
        checked = "check skipped (--skip-check)" if a.skip_check else "no check configured"
        if cmd:
            print(f"checking merged result: {cmd}", flush=True)
            t0 = time.time()
            p = run_check(cmd, timeout)
            secs = round(time.time() - t0)
            tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-40:])
            if p.timed_out:
                sys.exit(f"{tail}\n\nREFUSED: `{cmd}` did not finish within {timeout}s (TASK_CHECK_TIMEOUT), so "
                         "nothing was verified. If your change made it hang or slow (a server left running, a "
                         "test waiting for input), fix that and run done again. Otherwise the check itself is too "
                         f"slow for every done: python tasks.py hold {a.id} \"the check takes over {timeout}s: "
                         "make .taskcheck faster\".")
            if p.returncode:
                sys.exit(f"{tail}\n\nREFUSED: `{cmd}` fails on your work merged with the latest main "
                         f"(exit {p.returncode}). Fix it (another task may have changed what you depend on), "
                         f"commit, and run done again. If you cannot, see AGENTS.md \"Merge conflicts\" (c)/(d).")
            if secs > slow:
                print(f"warning: the check took {secs}s (aim for < {slow}s, TASK_CHECK_SLOW): it runs on every "
                      "done and every sync; move slow suites (e2e, full builds) out of .taskcheck", flush=True)
            checked = f"`{cmd}` passed in {secs}s on {tested[:9]}"

        def guard(t):
            err = None if a.force else owned_by_me(t)
            return err or ("RETEST" if cmd and code_changed(tested) else None)

        def note(t):
            cs = criteria(t["path"].read_text())
            skipped = [f"#{n}" for n, (_, mark, _) in enumerate(cs, 1) if mark == "~"]
            return (f"- done by {me()}: {checked}; criteria verified {sum(m == 'x' for _, m, _ in cs)}/{len(cs)}"
                    + (f", skipped {' '.join(skipped)}" if skipped else ""))
        t, err = update(a.id, f"done: {a.id} by {me()}", guard, append=note, with_code=True, status="done",
                        lease_until="")
        if err != "RETEST":
            break
        print("main changed while checking; checking again")
    else:
        sys.exit("main keeps changing; run done again")
    if err:
        sys.exit(f"REFUSED: {err}. The task is no longer yours: stop working on it.")
    if remote():  # the backup is on main now; abandoned attempts (wip/<id>-attemptN) are kept as history
        git("push", "-q", remote(), "--delete", wip_branch(a.id), check=False)
    print(f"DONE {a.id}")


def cmd_redo(a):
    """Give up on merging this attempt: save it to wip/<id>-attemptN, reset to the latest main, keep the
    task. The saved attempt is a reference for re-implementing, not something to merge."""
    ts = load()
    if a.id not in ts:
        sys.exit(f"no such task: {a.id}")
    if owned_by_me(ts[a.id]):
        sys.exit(f"REFUSED: {owned_by_me(ts[a.id])}")
    op = merge_state()
    if op:
        git(op, "--abort")
        print(f"aborted the unfinished {op}")
    if git("status", "--porcelain").stdout.strip():
        git("add", "-A")
        git("commit", "-qm", f"redo {a.id}: snapshot of abandoned attempt")
    base = upstream() or "HEAD"
    if upstream():
        git("fetch", "-q")
    if git("rev-list", "--count", f"{base}..HEAD").stdout.strip() == "0":
        sys.exit("nothing to redo: no local work beyond main")
    n = 1 + max([int(b.rsplit("attempt", 1)[1]) for b in attempts(a.id)], default=0)
    if n + 1 >= MAX_ATTEMPTS:
        print(f"{a.id} has had {n} attempts: putting it on hold for a human instead of attempt {n + 1}")
    b = f"{wip_branch(a.id)}-attempt{n}"
    git("branch", "-f", b, "HEAD")
    if remote():
        git("push", "-q", "-f", remote(), f"{b}:refs/heads/{b}")
        git("push", "-q", remote(), "--delete", wip_branch(a.id), check=False)  # the conflicting backup
    git("reset", "-q", "--hard", base)
    update(a.id, f"redo: {a.id} attempt {n + 1} by {me()}", owned_by_me, attempts=n + 1)  # for the board
    ref = f"{remote()}/{b}" if remote() else b
    if n + 1 >= MAX_ATTEMPTS:
        give_back(a.id, "hold", f"{n} attempts saved ({wip_branch(a.id)}-attempt1..{n}), none mergeable",
                  count=False, hold=f"{n} failed attempts: needs a human decision")
        sys.exit(f"ON HOLD {a.id}: {n} attempts failed. Stop working on it.")
    print(f"REDO {a.id}: attempt saved as {ref}; your clone is now at the latest main and you still own the task.\n"
          f"Re-implement the task here. Use the old attempt as a reference, don't merge it:\n"
          f"  git diff HEAD...{ref}   # what the attempt changed\n"
          f"  git show {ref}:<path>   # one file as the attempt had it\n"
          f"  git checkout {ref} -- <test files>   # its tests often carry over unchanged")


MAX_RELEASES, MAX_ATTEMPTS = 3, 4  # beyond this a task goes on hold for a human instead of bouncing on


def save_work(tid):
    """Move unpushed commits to wip/<tid> (never main) before giving a task back."""
    sync()
    ts = load()
    if tid not in ts:
        sys.exit(f"no such task: {tid}")
    ahead = git("rev-list", "--count", "@{u}..HEAD", check=False).stdout.strip() if upstream() else ""
    if ahead in ("0", ""):
        return ts
    if owned_by_me(ts[tid]):
        sys.exit(f"REFUSED: you have {ahead} unpushed commit(s) and {tid} is not your task; "
                 "push or move them first, or they would land on main with the release.")
    if dirty():
        sys.exit(f"REFUSED: commit your changes first; they will be saved to {wip_branch(tid)}, not main.")
    if not pushed("-q", "-f", remote(), f"HEAD:refs/heads/{wip_branch(tid)}"):
        push_failed(tid, f"Could not save your {ahead} commit(s) to {remote()}/{wip_branch(tid)}: the remote refuses "
                    f"them, so retrying won't help. {tid} is unchanged on the board (still yours) and your commits are "
                    "still here, unpushed. Only a human can fix it (e.g. a token without `workflow` scope cannot push "
                    f".github/workflows/*). Stop working on {tid} and end the session; the runner waits for the fix.")
    git("reset", "-q", "--keep", "@{u}")
    print(f"saved {ahead} unfinished commit(s) to {remote()}/{wip_branch(tid)} (not main)")
    return ts


def give_back(tid, verb, note, force=False, count=True, extra=None, **fields):
    """Release a task (status todo, no owner) with a note. Counts releases: the MAX_RELEASES-th puts
    the task on hold, so a task nobody can finish stops bouncing between agents."""
    save_work(tid)
    held = {}

    def fields_for(t):
        n = t["releases"] + (1 if count else 0)
        auto = count and not fields.get("hold") and n >= MAX_RELEASES
        held["auto"] = auto
        return dict(fields, releases=n, **({"hold": f"given back {n} times; last: {note}"[:200]} if auto else {}))

    def guard(t):
        err = None if force else owned_by_me(t)
        if not err:
            set_fields(t, **fields_for(t))
        return err
    t, err = update(tid, f"{verb}: {tid} by {me()}", guard, append=f"- {verb} by {me()}: {note}" if note else None,
                    extra=extra, status="todo", owner="", lease_until="", extended="")
    if err:
        sys.exit(f"REFUSED: {err}. Use --force for someone else's task.")
    if held.get("auto"):
        print(f"ON HOLD {tid}: given back {MAX_RELEASES} times, a human has to look at it (tasks.py unhold)")
    return t


def cmd_release(a):
    give_back(a.id, "release", a.note, force=a.force, count=not a.handoff)
    print(f"RELEASED {a.id}")


def cmd_hold(a):
    """Stop agents from working on a task until a human has looked at it (`unhold`)."""
    ts = load()
    if a.id in ts and ts[a.id]["status"] == "todo":  # not claimed: just mark it
        _, err = update(a.id, f"hold: {a.id} by {me()}", lambda t: None, append=f"- hold by {me()}: {a.reason}",
                        hold=a.reason[:200])
    else:
        give_back(a.id, "hold", a.reason, force=a.force, count=False, hold=a.reason[:200])
    print(f"ON HOLD {a.id}: {a.reason}")


def cmd_unhold(a):
    _, err = update(a.id, f"unhold: {a.id} by {me()}", lambda t: None if t["hold"] else f"{a.id} is not on hold",
                    append=f"- unhold by {me()}: {a.note}" if a.note else None, hold="", releases=0, extended="")
    if err:
        sys.exit(err)
    print(f"UNHELD {a.id}: agents can pick it up again")


def cmd_extend(a):
    """One more session for a task that is nearly done (once per agent and task)."""
    def guard(t):
        return owned_by_me(t) or (f"REFUSED: {a.id} was extended for you already: split it or put it on hold"
                                  if t["extended"] == me() else None)
    _, err = update(a.id, f"extend: {a.id} by {me()}", guard, append=f"- extended by {me()}: {a.note}", extended=me())
    if err:
        sys.exit(err)
    print(f"EXTENDED {a.id}: one more session")


def parse_split(text):
    """Sections '## <id>: <title>' with an optional 'deps: a, b' line; the rest is the task body."""
    subs = []
    for block in re.split(r"^## ", text, flags=re.M)[1:]:
        head, _, body = block.partition("\n")
        m = re.match(r"\s*([a-z0-9][a-z0-9-]*)\s*:\s*(.+)", head)
        if not m:
            sys.exit(f"bad section header '## {head}': want '## <id>: <title>' (id: a-z, 0-9, -)")
        deps = re.search(r"^deps:\s*(.*)$", body, re.M)
        body = re.sub(r"^deps:.*\n?", "", body, flags=re.M).strip()
        subs.append(dict(id=m.group(1), title=m.group(2).strip(), body=body,
                         deps=[d.strip() for d in deps.group(1).split(",") if d.strip()] if deps else []))
    return subs


def cmd_split(a):
    """Replace a too-big task by subtasks (one commit): the original waits for them and then only
    integrates and verifies. The work so far is saved to wip/<id> for the subtasks."""
    if not Path(a.file).is_file():
        sys.exit(f"no such file: {a.file}")
    subs = parse_split(Path(a.file).read_text(encoding="utf-8"))
    if not 2 <= len(subs) <= 6:
        sys.exit("split into 2-6 subtasks (sections '## <id>: <title>' in the file)")
    ids = [s["id"] for s in subs]
    ts = load()
    if a.id not in ts:
        sys.exit(f"no such task: {a.id}")
    if ts[a.id]["parent"]:
        sys.exit(f"REFUSED: {a.id} is already a subtask of {ts[a.id]['parent']}: don't split further, "
                 f"put it on hold: python tasks.py hold {a.id} \"<why>\"")
    clash = [i for i in ids if i in ts or ids.count(i) > 1]
    if clash:
        sys.exit(f"task ids already exist or repeat: {clash}")
    bad = [d for s in subs for d in s["deps"] if d not in ids and d not in ts]
    if bad:
        sys.exit(f"unknown deps: {bad}")

    def create(t):
        paths = []
        for s in subs:
            p = DIR / f"{s['id']}.md"
            deps = ", ".join(t["deps"] + s["deps"])
            goal, _, crit = s["body"].partition("- [")  # checklist lines become the acceptance criteria
            crit = f"\n## Acceptance criteria\n- [{crit.rstrip()}\n" if crit else ""
            write(p, f"---\ntitle: {s['title']}\nstatus: todo\nowner:\ndeps: [{deps}]\nprio: {t['prio']}\n"
                     f"parent: {a.id}\n---\n## Goal\nPart of `{a.id}` ({t['title']}), which was split.\n"
                     f"{goal.strip()}\n{crit}\n## Notes\n- Work done on `{a.id}` before the split is on "
                     f"`origin/{wip_branch(a.id)}` (if it exists): merge it first if your part builds on it.\n")
            paths.append(p)
        return paths
    note = (f"into {', '.join(ids)}. {a.note or ''} Once they are done, this task is only integration: "
            f"merge what is left on {wip_branch(a.id)} (if anything), verify the acceptance criteria end to end, done.")
    give_back(a.id, "split", " ".join(note.split()), count=False, extra=create,
              deps=f"[{', '.join(ts[a.id]['deps'] + ids)}]", subtasks=", ".join(ids))
    print(f"SPLIT {a.id} into {', '.join(ids)}. {a.id} now waits for them; whoever claims it then "
          f"integrates and verifies its acceptance criteria.")


def cmd_drop(a):
    """The plan changed and the task is no longer wanted. An agent working on it stops at its next
    heartbeat (within ~5 min); its work so far stays on wip/<id>. Reversible with `undrop`."""
    ts = load()
    if a.id not in ts:
        sys.exit(f"no such task: {a.id}")
    if ts[a.id]["status"] == "in_progress" and ts[a.id]["owner"] == me():
        save_work(a.id)  # my unpushed commits go to wip/<id>, not main

    def guard(t):
        if t["status"] == "done":
            return (f"REFUSED: {a.id} is done: its code is on main already. Add a task that removes or "
                    "changes it (python tasks.py new ...)")
        if t["status"] == "dropped":
            return f"{a.id} is dropped already"
        if t["status"] == "in_progress" and t["owner"] != me() and not t["stale"] and not a.force:
            return (f"REFUSED: {a.id} is in progress (owner: {t['owner']}). --force drops it anyway: that "
                    f"agent stops within ~5 min, its work stays on {wip_branch(a.id)}")
        return None
    _, err = update(a.id, f"drop: {a.id} by {me()}", guard, append=f"- dropped by {me()}: {a.note}",
                    status="dropped", owner="", lease_until="", hold="", extended="")
    if err:
        sys.exit(err)
    print(f"DROPPED {a.id}")
    orphans = [t["id"] for t in analyze(load()).values() if a.id in t["orphaned"]]
    if orphans:
        print(f"These tasks depend on {a.id} and now wait for a human: {', '.join(orphans)}\n"
              f"Edit their deps (or drop them too), commit and push, then check: python tasks.py check")


def cmd_undrop(a):
    _, err = update(a.id, f"undrop: {a.id} by {me()}",
                    lambda t: None if t["status"] == "dropped" else f"{a.id} is {t['status']}, not dropped",
                    append=f"- undropped by {me()}: {a.note}" if a.note else None, status="todo", releases=0)
    if err:
        sys.exit(err)
    print(f"UNDROPPED {a.id}: back on the board; earlier work (if any) is on {wip_branch(a.id)}")


def cmd_freeze(a):
    """Stop all new claims while humans change the plan. Running tasks carry on (hold/drop --force
    stops them); nothing else changes."""
    print(f"FROZEN: {a.reason}" if set_frozen(a.reason[:300]) else f"already frozen: {frozen()}")
    busy = [f"{t['id']}[{t['owner']}]" for t in load().values() if t["status"] == "in_progress"]
    if busy:
        print(f"still running (they finish unless you hold/drop them with --force): {', '.join(busy)}")


def cmd_unfreeze(a):
    print("UNFROZEN: agents claim tasks again" if set_frozen(None) else "not frozen")


def cmd_assume(a):
    """Record a decision taken without a human, so humans can review it later (`assumptions`)."""
    when = time.strftime("%Y-%m-%d %H:%M%z")
    text = " ".join(a.text.split())
    _, err = update(a.id, f"assume: {a.id} by {me()}", lambda t: None,
                    append=f"- ASSUMPTION ({when} by {me()}): {text}")
    if err:
        sys.exit(err)
    print(f"RECORDED assumption on {a.id}")


def cmd_verify(a):
    """Tick acceptance criterion n, with the evidence under it (what ran, and its result); with --skip,
    mark it as not met or not verifiable here, with why. Without n: list the criteria, numbered."""
    if a.n is None:
        t = load().get(a.id) or sys.exit(f"no such task: {a.id}")
        cs = criteria(t["path"].read_text())
        for n, (_, mark, text) in enumerate(cs, 1):
            print(f"#{n} [{mark}] {text}")
        return print("" if cs else f"{a.id} has no acceptance criteria")
    if not a.text:
        sys.exit("say what you ran and its result (or with --skip: why it can't be met or verified here)")
    when, mark = time.strftime("%Y-%m-%d %H:%M%z"), "~" if a.skip else "x"
    evidence = " ".join(a.text.split())

    def guard(t):
        err = None if a.force else owned_by_me(t)
        if err:
            return err
        text = t["path"].read_text()
        cs = criteria(text)
        if not 1 <= a.n <= len(cs):
            return f"{a.id} has {len(cs)} acceptance criteria (list them: python tasks.py verify {a.id})"
        lines, (i, _, crit) = text.split("\n"), cs[a.n - 1]
        lines[i] = f"- [{mark}] {crit}"
        j = i + 1
        while j < len(lines) and lines[j].startswith("  "):  # after earlier evidence for this criterion
            j += 1
        lines.insert(j, f"  - {'skipped' if a.skip else 'verified'} ({when} by {me()}): {evidence}")
        write(t["path"], "\n".join(lines))
        return None
    t, err = update(a.id, f"verify: {a.id} #{a.n} by {me()}", guard)
    if err:
        sys.exit(f"REFUSED: {err}")
    left = sum(m == " " for _, m, _ in criteria(t["path"].read_text()))
    print(f"{'SKIPPED' if a.skip else 'VERIFIED'} {a.id} #{a.n}" + (f" ({left} still to verify)" if left else ""))


def cmd_assumptions(a):
    """Assumptions waiting for a human (with --all: reviewed ones too), newest first."""
    if a.pull:
        sync()
    rows = sorted(((e["at"], i, e) for i, t in load().items() for e in assumption_entries(t)
                   if a.all or not e["review"]), key=lambda r: (r[0], r[1], r[2]["n"]), reverse=True)
    for w, i, e in rows:
        rev = (f"  [{e['review']} by {e['reviewed_by']}" + (f": {e['note']}" if e["note"] else "") + "]"
               if e["review"] else "")
        print(f"{w}  {i:24} #{e['n']} {e['who']}{rev}\n    {e['text']}")
    n_open = sum(1 for _, _, e in rows if not e["review"])
    print(f"{n_open} assumption(s) to review" + (f", {len(rows) - n_open} reviewed" if a.all else "")
          + ("; python tasks.py confirm <id> <n> if it is right, reject <id> <n> --note \"...\" if not "
             "(then edit or add a task to change it)" if n_open else ""))


def cmd_review_assumption(a):
    """A human confirms or rejects assumptions: the line stays in the task's Notes, marked, and leaves the
    list of assumptions to review."""
    verdict = REVIEWED[a.cmd]
    note = " ".join((getattr(a, "note", None) or "").split()).replace("[", "(").replace("]", ")")
    if verdict == "rejected" and not note:
        sys.exit("say why with --note (what is wrong, what should happen instead)")
    if not a.n and not a.all:
        sys.exit(f"which assumption? give its number (python tasks.py assumptions shows #n) or --all")
    who = me().replace(":", " ").replace("]", ")")
    when = time.strftime("%Y-%m-%d %H:%M%z")
    picked = []

    def guard(t):
        es = assumption_entries(t)
        want = sorted({e["n"] for e in es if not e["review"]} if a.all else set(a.n))
        if not want:
            return f"{a.id} has no assumptions to review"
        missing = [n for n in want if not 1 <= n <= len(es)]
        if missing:
            return f"{a.id} has {len(es)} assumption(s), no #{', #'.join(map(str, missing))}"
        twice = [f"#{n} is {es[n - 1]['review']} already" for n in want if es[n - 1]["review"]]
        if twice:
            return f"{a.id}: {', '.join(twice)}"
        picked[:] = want
        return None

    def mark(t):
        lines, k = t["path"].read_text().split("\n"), 0
        tag = f"{verdict} {when} by {who}" + (f": {note}" if note else "")
        for i, line in enumerate(lines):
            m = ASSUMPTION.match(line)
            if m:
                k += 1
                if k in picked:
                    lines[i] = f"- ASSUMPTION ({m[1]} by {m[2]}) [{tag}]: {m[7]}"
        write(t["path"], "\n".join(lines))
        return []
    _, err = update(a.id, lambda t: f"{a.cmd}: {a.id} by {me()}", guard, extra=mark)
    if err:
        sys.exit(err)
    print(f"{verdict.upper()} {a.id} #{', #'.join(map(str, picked))}"
          + (f"\nTo change what was built, edit the task (if not started) or add one: python tasks.py new ..."
             if verdict == "rejected" else ""))


def cmd_heartbeat(a):
    """Renew the lease on every in_progress task I own (plus any ids given, to learn if I lost them).
    Prints one line per task: HEARTBEAT <id> <until> | DONE <id> | LOST <id> <why>."""
    sync()
    who, ts = me(), analyze(load())
    lease = int(os.environ.get("TASK_LEASE", "0") or 0) or DEFAULT_LEASE
    ids = set(a.ids) | {i for i, t in ts.items() if t["status"] == "in_progress" and t["owner"] == who}
    for i in sorted(ids):
        if i not in ts:
            print(f"LOST {i} no such task")
            continue
        until = new_lease(lease)
        t, err = update(i, f"heartbeat: {i} by {who}", owned_by_me, lease_until=until)
        if not err:
            print(f"HEARTBEAT {i} {until}")
        elif t["status"] == "done" and t["owner"] == who:
            print(f"DONE {i}")
        else:
            print(f"LOST {i} {err}")


def cmd_check(a):
    ts = load()
    ps = problems(ts)
    print("\n".join(ps) or "ok")
    for w in overlap_warnings(ts) if not ps else []:  # advice, not an error
        print(f"warning: {w}")
    sys.exit(1 if ps else 0)


def cmd_graph(a):
    if a.pull:
        sync()
    ts = analyze(load())
    # Mermaid (renders on GitHub / GitLab)
    md = ["# Task board", "", "```mermaid", "graph LR"]
    for t in ts.values():
        who = f"<br/>@{t['owner']}" if t["owner"] else ""
        md.append(f'  {t["id"].replace("-", "_")}["{t["title"]}{who}"]:::{t["status"]}')
    for t in ts.values():
        for d in t["deps"]:
            if d in ts:
                md.append(f'  {d.replace("-", "_")} --> {t["id"].replace("-", "_")}')
    md += [f"  classDef todo fill:{COLORS['todo']}", f"  classDef in_progress fill:{COLORS['in_progress']}",
           f"  classDef done fill:{COLORS['done']}", f"  classDef dropped fill:{COLORS['dropped']}", "```", "",
           "| Task | Status | Owner | Unblocks | Ready |", "|---|---|---|---|---|"]
    for t in sorted(ts.values(), key=lambda t: (-t["dependents"], t["id"])):
        md.append(f"| {t['id']} | {t['status']} | {t['owner']} | {t['dependents']} | {'yes' if t['ready'] else ''} |")
    (ROOT / "BOARD.md").write_text("\n".join(md) + "\n")
    board().write(sys.modules[__name__], ROOT / "board.html", "HEAD", ts=ts)
    print("wrote BOARD.md and board.html")


def board():
    return companion("board")


def companion(name):
    """Import board.py / repos.py from next to this file."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.dont_write_bytecode = True
    try:
        return __import__(name)
    except ImportError:
        sys.exit(f"{name}.py not found next to tasks.py: copy it from TFP")


def cmd_repos(a):
    companion("repos").main(sys.modules[__name__], a)


def cmd_watch(a):
    """Live dashboard from origin: fetch every N seconds, rewrite board.html. Never touches your
    working tree or branch, so it is safe to run in any clone, even one you are working in."""
    b, me_ = board(), sys.modules[__name__]
    ref = upstream() or "HEAD"
    out = ROOT / "board.html"
    print(f"watching {ref}: open {out.as_uri()} in a browser (Ctrl-C to stop)")
    while True:
        if ref != "HEAD":
            git("fetch", "-q", "--prune", remote(), check=False)
        ts = b.write(me_, out, ref, refresh=None if a.once else a.interval)
        n = {k: sum(1 for t in ts.values() if t["status"] == k) for k in STATUSES}
        print(f"\r[{time.strftime('%H:%M:%S')}] done {n['done']}/{len(ts)}, in progress {n['in_progress']}, "
              f"stale {sum(t['stale'] for t in ts.values())}   ", end="", flush=True)
        if a.once:
            return print()
        time.sleep(a.interval)


def cmd_status(a):
    ts = analyze(load())
    for k in STATUSES:
        items = [t for t in ts.values() if t["status"] == k]
        print(f"{k} ({len(items)}): " + ", ".join(f"{t['id']}" + (f"[{t['owner']}]" if t["owner"] else "")
                                                   + (" STALE" if t["stale"] else "")
                                                   + (" HOLD" if t["hold"] else "") for t in items))
    if frozen():
        print(f"\nFROZEN (no new claims): {frozen()}   (python tasks.py unfreeze)")
    held = [t for t in ts.values() if t["hold"]]
    orphans = [t for t in ts.values() if t["orphaned"]]
    if held or orphans:
        print("\nneeds a human:\n" + "\n".join(
            [f"  {t['id']}: {t['hold']}   (python tasks.py unhold {t['id']})" for t in held] +
            [f"  {t['id']}: depends on dropped {', '.join(t['orphaned'])}   (edit its deps, or drop it)" for t in orphans]))
    n = sum(len(assumptions(t)) for t in ts.values())
    if n:
        print(f"\n{n} assumption(s) to review: python tasks.py assumptions")


def main():
    for f in (sys.stdout, sys.stderr):  # a console that can't show a character must not hide the output
        if hasattr(f, "reconfigure"):
            f.reconfigure(errors="backslashreplace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("new"); s.add_argument("id"); s.add_argument("title")
    s.add_argument("--deps", default=""); s.add_argument("--prio", type=int, default=2)
    s.add_argument("--touches", default="", help="files/folders it changes, e.g. frontend/src/app/state,docs/adr: "
                   "agents then avoid working on two such tasks at once")
    s.add_argument("--quality-exempt", metavar="REASON", help="classify a non-implementation task (no code to "
                   "test or review) as exempt from the review-evidence gate, with the reason; decided while planning")
    s.set_defaults(f=cmd_new)
    s = sp.add_parser("list"); s.add_argument("--status"); s.set_defaults(f=cmd_list)
    s = sp.add_parser("next"); s.add_argument("-n", type=int, default=5); s.set_defaults(f=cmd_next)
    s = sp.add_parser("claim"); s.add_argument("id", nargs="?"); s.add_argument("--who")
    s.add_argument("--force", action="store_true"); s.set_defaults(f=cmd_claim)
    for n, f in (("done", cmd_done), ("release", cmd_release)):
        s = sp.add_parser(n); s.add_argument("id"); s.set_defaults(f=f)
        s.add_argument("--force", action="store_true", help="someone else's task" +
                       (", or acceptance criteria nobody verified" if n == "done" else ""))
        if n == "done":
            s.add_argument("--skip-check", action="store_true", help="don't run the project check")
        if n == "release":
            s.add_argument("--note", help="why: appended to the task's Notes section")
            s.add_argument("--handoff", action="store_true", help="not the task's fault (e.g. usage limit): "
                           "doesn't count towards putting it on hold")
    s = sp.add_parser("redo"); s.add_argument("id"); s.set_defaults(f=cmd_redo)
    s = sp.add_parser("hold", help="stop agents working on a task until a human looks at it")
    s.add_argument("id"); s.add_argument("reason"); s.add_argument("--force", action="store_true"); s.set_defaults(f=cmd_hold)
    s = sp.add_parser("unhold"); s.add_argument("id"); s.add_argument("--note"); s.set_defaults(f=cmd_unhold)
    s = sp.add_parser("extend", help="one more session for a nearly finished task (once)")
    s.add_argument("id"); s.add_argument("--note", required=True); s.set_defaults(f=cmd_extend)
    s = sp.add_parser("split", help="replace a too-big task by subtasks described in a file")
    s.add_argument("id"); s.add_argument("--from", dest="file", required=True); s.add_argument("--note")
    s.set_defaults(f=cmd_split)
    s = sp.add_parser("drop", help="the plan changed: remove a task from the board (stops its agent with --force)")
    s.add_argument("id"); s.add_argument("--note", required=True); s.add_argument("--force", action="store_true")
    s.set_defaults(f=cmd_drop)
    s = sp.add_parser("undrop"); s.add_argument("id"); s.add_argument("--note"); s.set_defaults(f=cmd_undrop)
    s = sp.add_parser("freeze", help="no new claims until unfreeze (while humans change the plan)")
    s.add_argument("reason"); s.set_defaults(f=cmd_freeze)
    sp.add_parser("unfreeze").set_defaults(f=cmd_unfreeze)
    s = sp.add_parser("assume", help="record an assumption taken without a human, for later review")
    s.add_argument("id"); s.add_argument("text"); s.set_defaults(f=cmd_assume)
    s = sp.add_parser("verify", help="tick an acceptance criterion with its evidence (no n: list them)")
    s.add_argument("id"); s.add_argument("n", nargs="?", type=int); s.add_argument("text", nargs="?")
    s.add_argument("--skip", action="store_true", help="can't be met or verified here: text says why")
    s.add_argument("--force", action="store_true", help="someone else's task"); s.set_defaults(f=cmd_verify)
    s = sp.add_parser("assumptions", help="list assumptions waiting for a human, newest first")
    s.add_argument("--pull", action="store_true")
    s.add_argument("--all", action="store_true", help="also confirmed and rejected ones"); s.set_defaults(f=cmd_assumptions)
    for n in REVIEWED:
        s = sp.add_parser(n, help=f"{n} assumptions on a task (a human reviewed them): they leave the list")
        s.add_argument("id"); s.add_argument("n", nargs="*", type=int, help="assumption numbers (#n in `assumptions`)")
        s.add_argument("--all", action="store_true", help="every assumption on the task still to review")
        if n == "reject":
            s.add_argument("--note", required=True, help="what is wrong, what should happen instead")
        s.set_defaults(f=cmd_review_assumption)
    s = sp.add_parser("heartbeat"); s.add_argument("ids", nargs="*"); s.set_defaults(f=cmd_heartbeat)
    sp.add_parser("check").set_defaults(f=cmd_check)
    sp.add_parser("status").set_defaults(f=cmd_status)
    s = sp.add_parser("graph"); s.add_argument("--pull", action="store_true"); s.set_defaults(f=cmd_graph)
    s = sp.add_parser("repos", help="sync folders with the original repos (see repos.py)")
    s.add_argument("action", choices=["add", "list", "pull", "sync", "push"]); s.add_argument("args", nargs="*")
    s.add_argument("--branch", default="main"); s.add_argument("--every", help="repeat, e.g. 30m")
    s.add_argument("--skip-check", action="store_true"); s.set_defaults(f=cmd_repos)
    s = sp.add_parser("watch"); s.add_argument("-n", "--interval", type=int, default=20)
    s.add_argument("--once", action="store_true", help="write board.html once and exit"); s.set_defaults(f=cmd_watch)
    a = p.parse_args()
    CMD[0], REASON[0] = a.cmd, (a.reason if a.cmd == "hold" else "")
    a.f(a)


if __name__ == "__main__":
    main()
