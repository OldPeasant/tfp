#!/usr/bin/env python3
"""Tests for the dashboard server: python dashboard/test_dashboard.py

Each test run builds a throwaway origin + clone with this repo's tasks.py/board.py/agents.py, so nothing
touches your real repo or remote."""
import json, os, shutil, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
PY = sys.executable


def run(*a, cwd, env=None, check=True):
    p = subprocess.run(a, cwd=cwd, capture_output=True, text=True, env=env)
    if check and p.returncode:
        raise AssertionError(f"{a} failed: {p.stdout}{p.stderr}")
    return p


class Sandbox:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tf-dash-"))
        self.origin, self.clone = self.dir / "origin.git", self.dir / "proj"
        run("git", "init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.dir)
        run("git", "clone", "-q", str(self.origin), str(self.clone), cwd=self.dir)
        for k, v in (("user.name", "alice"), ("user.email", "a@x")):
            run("git", "config", k, v, cwd=self.clone)
        for f in ("tasks.py", "board.py", "agents.py"):
            shutil.copy(SRC / f, self.clone / f)
        self.env = {**os.environ, "TASKS_ROOT": str(self.clone)}
        self.env.pop("TASK_OWNER", None)
        self.env.pop("TASK_LEASE", None)
        for tid, title, deps in (("spec", "Spec", ""), ("api", "API", "spec"), ("ui", "UI", "api")):
            self.tasks("new", tid, title, "--deps", deps, "--quality-exempt", "dashboard test fixture")
        run("git", "add", "-A", cwd=self.clone)
        run("git", "commit", "-qm", "init", cwd=self.clone)
        run("git", "push", "-qu", "origin", "main", cwd=self.clone)

    def tasks(self, *a, check=True):
        return run(PY, "tasks.py", *a, cwd=self.clone, env=self.env, check=check)

    def edit(self, tid, text, create=False):
        return subprocess.run([PY, str(HERE / "taskedit.py"), tid, *(["--create"] if create else [])], input=text,
                              cwd=self.clone, env=self.env, capture_output=True, text=True)

    def on_origin(self, tid):
        run("git", "fetch", "-q", cwd=self.clone)
        return run("git", "show", f"origin/main:tasks/{tid}.md", cwd=self.clone, check=False).stdout


class TaskEdit(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        shutil.rmtree(self.sb.dir, ignore_errors=True)

    def test_create_ignores_protocol_fields_and_pushes(self):
        p = self.sb.edit("perf", "---\ntitle: Perf\nstatus: done\nowner: mallory\ndeps: [api]\nprio: 1\n---\n## Goal\nFast\n", create=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        text = self.sb.on_origin("perf")
        self.assertIn("status: todo", text)
        self.assertNotIn("mallory", text)
        self.assertIn("deps: [api]", text)
        self.assertIn("Fast", text)

    def test_create_refuses_unknown_dep_cycle_and_existing_id(self):
        self.assertIn("unknown dependency", self.sb.edit("x", "---\ntitle: X\ndeps: [nope]\n---\n", create=True).stderr)
        self.assertIn("exists already", self.sb.edit("api", "---\ntitle: X\n---\n", create=True).stderr)
        self.assertIn("cycle", self.sb.edit("spec", "---\ntitle: Spec\ndeps: [ui]\n---\n").stderr)

    def test_edit_keeps_status_and_refuses_running_or_done_tasks(self):
        self.sb.tasks("hold", "ui", "unclear")
        p = self.sb.edit("ui", "---\ntitle: UI shell\nstatus: done\nhold:\ndeps: [api]\n---\nnew body\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        text = self.sb.on_origin("ui")
        self.assertIn("title: UI shell", text)
        self.assertIn("hold: unclear", text)
        self.assertIn("status: todo", text)
        self.sb.tasks("claim", "spec")
        self.assertIn("in progress", self.sb.edit("spec", "---\ntitle: S\n---\n").stderr)
        self.sb.tasks("done", "spec", "--skip-check")
        self.assertIn("is done", self.sb.edit("spec", "---\ntitle: S\n---\n").stderr)

    def test_edit_does_not_push_unfinished_local_commits(self):
        (self.sb.clone / "wip.txt").write_text("half done")
        run("git", "add", "wip.txt", cwd=self.sb.clone)
        run("git", "commit", "-qm", "wip", cwd=self.sb.clone)
        self.assertEqual(self.sb.edit("ui", "---\ntitle: UI 2\ndeps: [api]\n---\n").returncode, 0)
        self.assertEqual(run("git", "show", "origin/main:wip.txt", cwd=self.sb.clone, check=False).returncode, 128)
        self.assertTrue((self.sb.clone / "wip.txt").exists())


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox()
        sys.path.insert(0, str(HERE))
        import server as S
        cls.S = S
        S.load_modules(cls.sb.clone)
        os.environ.pop("TASK_OWNER", None)
        board = S.Board(3600)
        cls.srv = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
        cls.srv.board, cls.srv.jobs, cls.srv.verbose, cls.srv.token = board, S.Jobs(board), False, "tok"
        cls.port = cls.srv.server_address[1]
        cls.srv.hosts = {f"127.0.0.1:{cls.port}"}
        board.refresh()
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.sb.dir, ignore_errors=True)

    def call(self, path, body=None, token="tok", host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("X-TFP-Token", token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def act(self, action, **params):
        code, job = self.call("/api/action", {"action": action, "params": params})
        self.assertEqual(code, 200, job)
        for _ in range(200):
            j = next(j for j in self.call("/api/jobs")[1] if j["id"] == job["id"])
            if j["state"] in ("ok", "failed"):
                return j
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_auth(self):
        self.assertEqual(self.call("/api/state", token=None)[0], 401)
        self.assertEqual(self.call("/api/state", token="wrong")[0], 401)
        self.assertEqual(self.call("/api/state", host="evil.example:80")[0], 403)
        self.assertEqual(self.call("/api/action", {"action": "check"}, token=None)[0], 401)

    def test_state(self):
        code, st = self.call("/api/state")
        self.assertEqual(code, 200)
        ids = {t["id"]: t for t in st["board"]["tasks"]}
        self.assertLessEqual({"spec", "api", "ui"}, set(ids))
        self.assertEqual(ids["api"]["deps"], ["spec"])
        self.assertEqual(ids["api"]["unblocks_direct"], ["ui"])
        self.assertFalse(ids["ui"]["ready"])
        self.assertIn(ids["ui"]["state"], ("todo", "hold"))
        self.assertEqual(st["local"]["me"], "alice")

    def test_validation(self):
        for action, params in (("claim", {"id": "--force"}), ("hold", {"id": "ui"}), ("drop", {"id": "ui"}),
                               ("nope", {}), ("agents_start", {"n": 99}), ("agents_start", {"n": 1, "host": "a b"}),
                               ("repos", {"what": "add", "folder": "../x", "url": "u"}), ("agent_stop", {"name": "x; rm"}),
                               ("confirm", {"id": "api"}), ("confirm", {"id": "api", "n": "1; rm"}),
                               ("reject", {"id": "api", "n": 1}), ("verify", {"id": "api", "n": 1}),
                               ("verify", {"id": "api", "text": "x"})):
            code, r = self.call("/api/action", {"action": action, "params": params})
            self.assertEqual(code, 400, (action, params, r))

    def test_argv_never_goes_through_a_shell(self):
        spec = self.S.build("hold", {"id": "ui", "reason": "a; rm -rf / `x` $(y)"}, None)
        self.assertEqual(spec["argv"][1:], ["tasks.py", "hold", "ui", "a; rm -rf / `x` $(y)"])

    def test_claim_release_and_refusal(self):
        j = self.act("claim", id="spec")
        self.assertEqual(j["state"], "ok", j["output"])
        self.assertIn("CLAIMED spec as alice", j["output"])
        self.assertIn("owner: alice", self.sb.on_origin("spec"))
        j = self.act("claim", id="ui")
        self.assertEqual(j["state"], "failed")
        self.assertIn("unfinished dependencies", j["output"])
        j = self.act("release", id="spec", note="later")
        self.assertEqual(j["state"], "ok", j["output"])
        self.assertIn("status: todo", self.sb.on_origin("spec"))

    def test_confirm_and_reject_assumptions(self):
        for text in ("REST, not GraphQL", "ids are UUIDs"):
            self.assertEqual(self.act("assume", id="api", text=text)["state"], "ok")
        j = self.act("confirm", id="api", n=1)
        self.assertEqual(j["state"], "ok", j["output"])
        j = self.act("reject", id="api", n=2, note="numeric ids")
        self.assertEqual(j["state"], "ok", j["output"])
        self.assertIn("[confirmed ", self.sb.on_origin("api"))
        for _ in range(100):  # the board refreshes in the background after each job
            api = next(t for t in self.call("/api/state")[1]["board"]["tasks"] if t["id"] == "api")
            got = {a["text"]: (a["n"], a["review"], a["note"]) for a in api["assumptions"]}
            if got.get("ids are UUIDs", (0, ""))[1]:
                break
            self.call("/api/refresh", {})
            time.sleep(0.1)
        self.assertEqual(got["REST, not GraphQL"][:2], (1, "confirmed"))
        self.assertEqual(got["ids are UUIDs"], (2, "rejected", "numeric ids"))
        self.assertEqual(self.act("confirm", id="api", n=1)["state"], "failed")  # already confirmed

    def test_verify_criteria_and_done_gate(self):
        r = self.sb.edit("crit", "---\ntitle: Criteria\ndeps: []\ntouches: [frontend]\n---\n## Goal\n\n"
                                 "## Acceptance criteria\n- [ ] login works\n- [ ] image builds\n\n## Notes\n", create=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.act("claim", id="crit")["state"], "ok")
        j = self.act("done", id="crit")
        self.assertEqual(j["state"], "failed")
        self.assertIn("nobody verified", j["output"])
        j = self.act("verify", id="crit", n=1, text="playwright login -> 3 passed")
        self.assertEqual(j["state"], "ok", j["output"])
        self.assertEqual(self.act("verify", id="crit", n=2, text="no docker", skip=True)["state"], "ok")
        self.assertIn("- [x] login works\n  - verified (", self.sb.on_origin("crit"))
        for _ in range(100):
            crit = next((t for t in self.call("/api/state")[1]["board"]["tasks"] if t["id"] == "crit"), None)
            if crit and crit["criteria"][1]["mark"] == "~":
                break
            self.call("/api/refresh", {})
            time.sleep(0.1)
        self.assertEqual([(c["n"], c["mark"], c["text"]) for c in crit["criteria"]],
                         [(1, "x", "login works"), (2, "~", "image builds")])
        self.assertIn("playwright login -> 3 passed", crit["criteria"][0]["evidence"][0])
        self.assertEqual(crit["touches"], ["frontend"])
        self.assertEqual(self.act("release", id="crit")["state"], "ok")

    def test_act_as(self):
        self.assertEqual(self.act("act_as", who="bob")["state"], "ok")
        try:
            j = self.act("claim", id="spec")
            self.assertIn("CLAIMED spec as bob", j["output"])
            self.act("release", id="spec")
        finally:
            self.act("act_as", who="")

    def test_split_removes_its_temp_file(self):
        self.act("claim", id="spec")
        before = set(Path(tempfile.gettempdir()).glob("split-spec-*"))
        j = self.act("split", id="spec", text="## spec-a: A\n- [ ] a\n## spec-b: B\ndeps: spec-a\n- [ ] b\n")
        self.assertEqual(j["state"], "ok", j["output"])
        self.assertIn("deps: [spec-a]", self.sb.on_origin("spec-b"))
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("split-spec-*")), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
