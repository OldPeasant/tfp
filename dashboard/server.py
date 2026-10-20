#!/usr/bin/env python3
"""TFP dashboard: a local web app to watch the board and do everything tasks.py / agents.py can do.

  python dashboard/server.py                  # then open the printed link
  python dashboard/server.py --port 8800 --no-browser

Run it in your own clone of the working repo (not in ../<repo>-agents/). Stdlib only, like the rest of
TFP. It reads the board the same way `tasks.py watch` does (origin, fetched every --interval seconds)
and performs every action by running the real CLI (`python tasks.py claim ...`, `python agents.py start
...`) in this clone, one at a time, so the protocol rules (CAS pushes, guards, refusals) are exactly the
CLI's. The page shows each command and its output.

Security: listens on 127.0.0.1 only, and every API call needs the random token embedded in the page, so
other web pages and other users of this machine can't trigger actions.
"""
import argparse, json, mimetypes, os, re, secrets, subprocess, sys, tempfile, threading, time, traceback
import urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __name__ == "__main__" and os.name == "nt" and not sys.flags.utf8_mode:  # same as tasks.py
    os.environ["PYTHONUTF8"] = "1"
    try:
        sys.exit(subprocess.call([sys.executable, *sys.argv]))
    except KeyboardInterrupt:
        sys.exit(130)

HERE = Path(__file__).resolve().parent
APP = HERE / "app"  # the built Angular app (dashboard/web -> npm run build)
ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
AGENT = re.compile(r"^agent-(\d+)$")
HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_OUTPUT = 400_000  # chars kept per job
for ext, typ in ((".woff2", "font/woff2"), (".woff", "font/woff"), (".svg", "image/svg+xml"), (".js", "text/javascript")):
    mimetypes.add_type(typ, ext)

T = B = A = None  # tasks, board, agents modules of the watched repo
ROOT = None


def load_modules(root):
    global T, B, A, ROOT
    ROOT = root
    os.environ["TASKS_ROOT"] = str(root)
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    try:
        import tasks, board, agents
    except ImportError as e:
        sys.exit(f"{root} has no {e.name}.py: run the dashboard in a clone of the working repo "
                 "(where tasks.py, board.py and agents.py are), or pass --repo")
    T, B, A = tasks, board, agents


def git(*a):
    return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True)


# ---------- board snapshot (remote: what everyone sees) ----------
class Board:
    def __init__(self, interval, ref=None):
        self.interval, self.fixed_ref = interval, ref
        self.data, self.error, self.fetched_at = None, None, None
        self.lock, self.wake = threading.Lock(), threading.Event()
        self.version = 0

    def ref(self):
        return self.fixed_ref or T.upstream() or "HEAD"

    def refresh(self, fetch=True):
        ref = self.ref()
        if fetch and ref != "HEAD" and T.remote():
            p = git("fetch", "-q", "--prune", T.remote())
            self.fetched_at = time.time()
            self.error = ("could not fetch from origin (offline?): " + p.stderr.strip()[-300:]) if p.returncode else None
        try:
            data = snapshot(ref)
        except BaseException as e:  # tasks.git() calls sys.exit on errors
            self.error = f"could not read the board at {ref}: {e}"
            return
        with self.lock:
            self.data = data
            self.version += 1

    def loop(self):
        while True:
            try:
                self.refresh()
            except Exception:
                traceback.print_exc()
            self.wake.wait(self.interval)
            self.wake.clear()


def snapshot(ref):
    now = time.time()
    ts = B.tasks_at(T, ref)
    feed = B.history(T, ref, ts)
    tries = B.attempt_branches(T)
    base = (ref.split("/", 1)[1] if ref != "HEAD" and "/" in ref else
            git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main")
    problems = T.problems(ts)
    out = []
    for t in ts.values():
        d = {k: v for k, v in t.items() if k != "path"}
        d.update(state=B.state_of(t), health=B.health(T, t, now), lease_end=T.lease_end(t),
                 assumptions=T.assumption_entries(t), criteria=T.criteria_entries(t),
                 saved_attempts=[dict(n=n, branch=br, url=B.compare_url(T, base, br)) for n, br in tries.get(t["id"], [])],
                 unblocks_direct=[c["id"] for c in ts.values() if t["id"] in c["deps"]])
        out.append(d)
    remote = T.remote()
    wips = set()
    if remote:
        for r in git("for-each-ref", "--format=%(refname:short)", f"refs/remotes/{remote}/wip/").stdout.split():
            wips.add(r.split("/", 2)[2] if r.count("/") >= 2 else r)
    for d in out:
        d["has_wip"] = f"wip/{d['id']}" in wips
    return dict(ref=ref, head=git("rev-parse", "--short", ref).stdout.strip(), base=base, generated_at=now,
                tasks=out, feed=feed[:300], agents=B.agents(T), sync=B.sync_state(T), problems=problems,
                frozen=T.frozen(ref if ref != "HEAD" else None), critical_path=B.critical_path(ts),
                offline_after=B.OFFLINE_AFTER)


# ---------- this laptop (local: my clone, my agents) ----------
def local_state(acting_as):
    st_file = A.state_file(ROOT)
    base = A.base_dir(ROOT)
    try:
        st = json.loads(st_file.read_text()) if st_file.exists() else {}
    except ValueError:
        st = {}
    agents = []
    for name, s in sorted(st.items(), key=lambda kv: int((AGENT.match(kv[0]) or [0, 0])[1])):
        alive = A.alive(s.get("pid", 0))
        sf = base / "logs" / f"{name}.status.json"
        try:
            status = json.loads(sf.read_text()) if sf.exists() else None
        except ValueError:
            status = None
        lf = base / "logs" / f"{name}.log"
        agents.append(dict(name=name, pid=s.get("pid"), owner=s.get("owner", name), clone=s.get("clone"), alive=alive,
                           finishing=alive and A.finish_flag(base, name).exists(),
                           stopping=alive and A.stop_flag(base, name).exists(),
                           status=status, last_log=tail(lf, 1)[0] if lf.exists() else ""))
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    up = T.upstream()
    ahead = git("rev-list", "--count", "@{u}..HEAD").stdout.strip() if up else ""
    behind = git("rev-list", "--count", "HEAD..@{u}").stdout.strip() if up else ""
    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip().splitlines()
    op = T.merge_state()
    return dict(root=str(ROOT), name=ROOT.name, branch=branch, upstream=up, ahead=int(ahead or 0), behind=int(behind or 0),
                dirty=dirty[:50], merge_state=op, conflicted=T.conflicted() if op else [], me=acting_as or T.me(),
                git_user=git("config", "user.name").stdout.strip(), acting_as=acting_as,
                check_cmd=T.check_cmd(), agents_dir=str(base), agents=agents,
                host=os.environ.get("AGENT_HOST") or __import__("socket").gethostname().split(".")[0],
                repos=(ROOT / "repos.txt").exists())


def tail(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024 * max(1, n // 200)))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return lines[-n:] or [""]
    except OSError:
        return [""]


def session_steps(name, n):
    """What the agent did in its current/last session, one line per step (from the stream-json output)."""
    f = A.base_dir(ROOT) / "logs" / f"{name}.session.txt"
    if not f.exists():
        return []
    out = []
    for line in tail(f, 4000):
        doing, _ = A.describe(line, ROOT)
        if doing:
            out.append(doing)
    return out[-n:]


# ---------- actions: run the real CLI, one at a time ----------
class Jobs:
    def __init__(self, board):
        self.board, self.jobs, self.lock = board, [], threading.Lock()
        self.queue, self.cv, self.seq = [], threading.Condition(), 0
        self.acting_as = None  # TASK_OWNER override chosen in the page; None = git user.name
        threading.Thread(target=self.worker, daemon=True).start()

    def add(self, title, argv, display, stdin=None, cleanup=None, local=None):
        with self.cv:
            self.seq += 1
            job = dict(id=self.seq, title=title, display=display, state="queued", output="", exit=None,
                       queued_at=time.time(), started_at=None, ended_at=None)
            self.jobs.append(job)
            del self.jobs[:-60]
            self.queue.append((job, argv, stdin, cleanup, local))
            self.cv.notify()
        return job

    def worker(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                job, argv, stdin, cleanup, local = self.queue.pop(0)
            job.update(state="running", started_at=time.time())
            try:
                if local:
                    job["output"], code = local()
                else:
                    code = self.run(job, argv, stdin)
            except Exception as e:
                job["output"] += f"\n{type(e).__name__}: {e}"
                code = -1
            finally:
                if cleanup:
                    cleanup()
            job.update(exit=code, state="ok" if code == 0 else "failed", ended_at=time.time())
            self.board.refresh(fetch=False)  # the CLI has pushed: show the result right away
            self.board.wake.set()

    def run(self, job, argv, stdin):
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "TASKS_ROOT": str(ROOT)}
        env.pop("TASK_LEASE", None)  # a human's claims never expire
        if self.acting_as:
            env["TASK_OWNER"] = self.acting_as
        p = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", bufsize=1)
        if stdin is not None:
            p.stdin.write(stdin)
            p.stdin.close()
        for line in p.stdout:
            job["output"] = (job["output"] + line)[-MAX_OUTPUT:]
        return p.wait()


def quote(a):
    return a if re.match(r"^[A-Za-z0-9._/=:@%+,-]+$", a) else '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Bad(Exception):
    pass


def need_id(p, key="id"):
    v = str(p.get(key) or "").strip()
    if not ID.match(v):
        raise Bad(f"'{v}' is not a task id (a-z, 0-9, -)")
    return v


def need_text(p, key, what):
    v = " ".join(str(p.get(key) or "").split())
    if not v:
        raise Bad(f"{what} is required")
    return v


def opt(p, key):
    v = " ".join(str(p.get(key) or "").split())
    return v or None


def num(p, key, lo, hi, default=None):
    v = p.get(key, default)
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise Bad(f"{key} must be a number")
    if not lo <= v <= hi:
        raise Bad(f"{key} must be between {lo} and {hi}")
    return v


def job(title, argv=None, display=None, stdin=None, cleanup=None, local=None):
    """What to run for an action: argv (a CLI call) or local (a function returning (output, exit code))."""
    if display is None:
        display = " ".join("python" if a == sys.executable else quote(a) for a in argv)
    return dict(title=title, argv=argv, display=display, stdin=stdin, cleanup=cleanup, local=local)


def build(action, p, jobs):
    """action name + params -> (title, argv for `python <script> ...`) or a local job. Validates everything:
    only these commands can run, never an arbitrary command line."""
    py = sys.executable
    tasks_ = lambda *a: [py, "tasks.py", *a]
    agents_ = lambda *a: [py, "agents.py", *a]
    flag = lambda k, name: [name] if p.get(k) else []
    note = lambda k="note": (["--note", opt(p, k)] if opt(p, k) else [])
    if action == "claim":
        tid = need_id(p) if p.get("id") else None
        who = opt(p, "who")
        return job(f"Claim {tid or 'the best ready task'}", tasks_("claim", *([tid] if tid else []),
                                                               *(["--who", who] if who else []), *flag("force", "--force")))
    if action == "done":
        tid = need_id(p)
        return job(f"Finish {tid}", tasks_("done", tid, *flag("force", "--force"), *flag("skip_check", "--skip-check")))
    if action == "release":
        tid = need_id(p)
        return job(f"Release {tid}", tasks_("release", tid, *note(), *flag("handoff", "--handoff"), *flag("force", "--force")))
    if action == "redo":
        tid = need_id(p)
        return job(f"Restart {tid} from main", tasks_("redo", tid))
    if action == "hold":
        tid = need_id(p)
        return job(f"Put {tid} on hold", tasks_("hold", tid, need_text(p, "reason", "A reason"), *flag("force", "--force")))
    if action == "unhold":
        tid = need_id(p)
        return job(f"Take {tid} off hold", tasks_("unhold", tid, *note()))
    if action == "extend":
        tid = need_id(p)
        return job(f"Extend {tid}", tasks_("extend", tid, "--note", need_text(p, "note", "What is left")))
    if action == "drop":
        tid = need_id(p)
        return job(f"Drop {tid}", tasks_("drop", tid, "--note", need_text(p, "note", "A reason"), *flag("force", "--force")))
    if action == "undrop":
        tid = need_id(p)
        return job(f"Bring back {tid}", tasks_("undrop", tid, *note()))
    if action == "assume":
        tid = need_id(p)
        return job(f"Record an assumption on {tid}", tasks_("assume", tid, need_text(p, "text", "The assumption")))
    if action == "verify":
        tid, n = need_id(p), num(p, "n", 1, 999)
        return job(f"{'Skip' if p.get('skip') else 'Verify'} criterion #{n} of {tid}",
                   tasks_("verify", tid, str(n), need_text(p, "text", "The evidence" if not p.get("skip") else "A reason"),
                          *flag("skip", "--skip"), *flag("force", "--force")))
    if action in ("confirm", "reject"):
        tid = need_id(p)
        which = ["--all"] if p.get("all") else [str(num(p, "n", 1, 999))]
        why = ["--note", need_text(p, "note", "What is wrong")] if action == "reject" else []
        return job(f"{action.capitalize()} assumption{'s' if p.get('all') else ' #' + which[0]} on {tid}",
                   tasks_(action, tid, *which, *why))
    if action == "freeze":
        return job("Freeze the board", tasks_("freeze", need_text(p, "reason", "A reason")))
    if action == "unfreeze":
        return job("Unfreeze the board", tasks_("unfreeze"))
    if action == "check":
        return job("Check the board", tasks_("check"))
    if action == "pull":
        return job("Pull the latest main", ["git", "pull", "--rebase", "--autostash"])
    if action == "split":
        tid = need_id(p)
        text = str(p.get("text") or "")
        if not text.strip():
            raise Bad("describe the subtasks")
        fd, path = tempfile.mkstemp(prefix=f"split-{tid}-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return job(f"Split {tid}", tasks_("split", tid, "--from", path, *note()),
                   cleanup=lambda: Path(path).unlink(missing_ok=True),
                   display=f"python tasks.py split {tid} --from <subtasks file>"
                   + (f" --note {quote(opt(p, 'note'))}" if opt(p, "note") else ""))
    if action == "save_task":
        tid = need_id(p)
        text = str(p.get("text") or "")
        create = bool(p.get("create"))
        return job(f"{'Create' if create else 'Save'} task {tid}",
                   [py, str(HERE / "taskedit.py"), tid, *(["--create"] if create else [])], stdin=text,
                   display=f"edit tasks/{tid}.md, commit and push it")
    if action == "repos":
        what = p.get("what")
        if what in ("list", "sync"):
            return job(f"Repos: {what}", tasks_("repos", what, *flag("skip_check", "--skip-check")))
        if what == "pull":
            folder = need_folder(p)
            return job(f"Repos: pull {folder}", tasks_("repos", "pull", folder))
        if what == "add":
            folder, url = need_folder(p), need_text(p, "url", "The repo URL")
            if url.startswith("-") or " " in url:
                raise Bad("that is not a repo URL")
            return job(f"Repos: add {folder}", tasks_("repos", "add", folder, url, "--branch", opt(p, "branch") or "main"))
        raise Bad("unknown repos action")
    if action == "agents_start":
        n = num(p, "n", 1, 32, 1)
        args = ["start", str(n), "--first", str(num(p, "first", 1, 999, 1))]
        host = opt(p, "host")
        if host:
            if not HOST.match(host):
                raise Bad("the host name may use letters, digits, '.', '_' and '-'")
            args += ["--host", host]
        for k, lo, hi in (("timeout", 60, 86400), ("lease", 120, 86400), ("limit_wait", 60, 86400), ("task_budget", 1, 20)):
            if p.get(k) not in (None, ""):
                args += [f"--{k.replace('_', '-')}", str(num(p, k, lo, hi))]
        if opt(p, "cmd"):
            args += ["--cmd", opt(p, "cmd")]
        args += flag("yolo", "--yolo")
        return job(f"Start {n} agent{'s' * (n > 1)}", agents_(*args))
    if action == "agents_stop":
        return job("Ask all agents to finish, then stop" if p.get("finish") else "Stop all agents now",
                   agents_("stop", *flag("finish", "--finish")))
    if action == "agent_stop":
        name = need_agent(p)
        kind = A.finish_flag if p.get("finish") else A.stop_flag

        def local():
            st = json.loads(A.state_file(ROOT).read_text()) if A.state_file(ROOT).exists() else {}
            if name not in st or not A.alive(st[name]["pid"]):
                return f"{name} is not running", 1
            kind(A.base_dir(ROOT), name).touch()
            return (f"asked {name} to finish its current task and then stop (no new claims)" if p.get("finish") else
                    f"asked {name} to stop now: it ends its session within a few seconds; its task stays claimed "
                    "until it starts again or the lease runs out"), 0
        return job(f"{'Finish and stop' if p.get('finish') else 'Stop'} {name}", local=local,
                   display=f"touch ../{A.base_dir(ROOT).name}/logs/{name}.{'finish' if p.get('finish') else 'stop'}")
    if action == "act_as":
        who = opt(p, "who")
        if who and not re.match(r"^[^\s\"'`$\\]{1,64}$", who):
            raise Bad("a name without spaces or quotes")

        def local():
            jobs.acting_as = who
            return f"actions now run as {who or T.me()}", 0
        return job("Act as " + (who or "yourself"), local=local,
                   display=f"export TASK_OWNER={who}" if who else "unset TASK_OWNER")
    raise Bad(f"unknown action '{action}'")


def need_folder(p):
    v = str(p.get("folder") or "").strip().strip("/")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", v):
        raise Bad("folder: a single folder name")
    return v


def need_agent(p):
    v = str(p.get("name") or "")
    if not AGENT.match(v):
        raise Bad("not an agent name")
    return v


# ---------- http ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "tfp-dashboard"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def allowed_host(self):
        # DNS rebinding: a foreign page whose domain resolves to 127.0.0.1 still sends its own Host
        host = (self.headers.get("Host") or "").lower()
        return host in self.server.hosts

    def authed(self):
        tok = self.headers.get("X-TFP-Token") or ""
        return self.allowed_host() and secrets.compare_digest(tok, self.server.token)

    def do_GET(self):
        if not self.allowed_host():
            return self.send(403, {"error": "unknown host"})
        u = urllib.parse.urlsplit(self.path)
        if u.path.startswith("/api/"):
            if not self.authed():
                return self.send(401, {"error": "reload the page (the dashboard was restarted)"})
            return self.api_get(u.path, urllib.parse.parse_qs(u.query))
        return self.static(u.path)

    def do_POST(self):
        if not self.authed():
            return self.send(401, {"error": "reload the page (the dashboard was restarted)"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 2_000_000)) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        path = urllib.parse.urlsplit(self.path).path
        srv = self.server
        if path == "/api/refresh":
            threading.Thread(target=srv.board.refresh, daemon=True).start()
            return self.send(202, {"ok": True})
        if path == "/api/action":
            try:
                spec = build(str(body.get("action")), body.get("params") or {}, srv.jobs)
            except Bad as e:
                return self.send(400, {"error": str(e)})
            return self.send(200, srv.jobs.add(**spec))
        return self.send(404, {"error": "no such endpoint"})

    def api_get(self, path, q):
        srv = self.server
        if path == "/api/state":
            with srv.board.lock:
                data, version = srv.board.data, srv.board.version
            return self.send(200, dict(version=version, board=data, error=srv.board.error,
                                       fetched_at=srv.board.fetched_at, interval=srv.board.interval,
                                       local=local_state(srv.jobs.acting_as), now=time.time(),
                                       busy=any(j["state"] in ("queued", "running") for j in srv.jobs.jobs)))
        if path == "/api/jobs":
            return self.send(200, srv.jobs.jobs[-30:])
        m = re.match(r"^/api/agents/(agent-\d+)/(log|steps)$", path)
        if m:
            n = max(10, min(int((q.get("n") or ["300"])[0]), 3000))
            lines = (tail(A.base_dir(ROOT) / "logs" / f"{m.group(1)}.log", n) if m.group(2) == "log"
                     else session_steps(m.group(1), n))
            return self.send(200, {"lines": lines})
        m = re.match(r"^/api/tasks/([a-z0-9][a-z0-9-]*)/(text|history)$", path)
        if m:
            ref = srv.board.ref()
            if m.group(2) == "text":
                p = git("show", f"{ref}:tasks/{m.group(1)}.md")
                return self.send(200 if p.returncode == 0 else 404, {"text": p.stdout, "ref": ref})
            p = git("log", ref, "--first-parent", "-n200", "--format=%ct%x09%an%x09%h%x09%s", "--", f"tasks/{m.group(1)}.md")
            rows = [dict(zip(("at", "who", "commit", "text"), l.split("\t", 3))) for l in p.stdout.splitlines() if l.count("\t") >= 3]
            return self.send(200, {"commits": [dict(r, at=int(r["at"])) for r in rows if not r["text"].startswith("heartbeat:")]})
        return self.send(404, {"error": "no such endpoint"})

    def static(self, path):
        if not APP.exists():
            return self.send(503, b"<h1>The dashboard is not built</h1><p>cd dashboard/web &amp;&amp; npm install &amp;&amp; npm run build</p>",
                             "text/html; charset=utf-8")
        f = (APP / urllib.parse.unquote(path).lstrip("/")).resolve()
        if not f.is_file() or APP.resolve() not in f.parents:
            f = APP / "index.html"  # single-page app
        data = f.read_bytes()
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.name == "index.html":
            data = data.replace(b"</head>", f'<meta name="tfp-token" content="{self.server.token}"></head>'.encode(), 1)
            ctype = "text/html; charset=utf-8"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8" if "charset" not in ctype else ""
        self.send(200, data, ctype, {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                     "img-src 'self' data:; frame-ancestors 'none'"} if f.name == "index.html" else None)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", help="clone to manage (default: the repo this dashboard folder is in)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("-n", "--interval", type=int, default=20, help="seconds between fetches from origin")
    p.add_argument("--ref", help="ref to show (default: the upstream of the checked-out branch, like tasks.py watch)")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="log every request")
    a = p.parse_args()
    root = Path(a.repo).resolve() if a.repo else HERE.parent
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode:
        sys.exit(f"{root} is not a git repo")
    load_modules(Path(top.stdout.strip()).resolve())
    board = Board(a.interval, a.ref)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    srv.board, srv.jobs, srv.verbose = board, Jobs(board), a.verbose
    srv.token = os.environ.get("TFP_DASHBOARD_TOKEN") or os.environ.get("TASKFLOW_DASHBOARD_TOKEN") or secrets.token_urlsafe(24)
    srv.hosts = {f"127.0.0.1:{a.port}", f"localhost:{a.port}"}
    board.refresh()
    threading.Thread(target=board.loop, daemon=True).start()
    url = f"http://127.0.0.1:{a.port}/"
    print(f"TFP dashboard for {ROOT} ({board.ref()}): {url}   (Ctrl-C to stop)", flush=True)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
