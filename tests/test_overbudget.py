#!/usr/bin/env python3
"""Tests for what happens when a task runs out of budget: the runner hands it to another agent instead
of putting it on hold for a human, doesn't take it straight back, a human hold still holds, counted
releases still end in a hold, and a usage limit still releases with --handoff (not counted).
Runs tasks.py and agents.py (with a scripted fake agent instead of Claude) against a local bare remote
in a temp dir. Run: python3 tests/test_overbudget.py"""
import os, re, shlex, shutil, signal, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
OWNER = "box/agent-1"

FAKE_AGENT = r'''
import json, os, subprocess, sys, time
prompt, mode = sys.argv[-1], os.environ.get("FAKE_MODE", "")
def tp(*a):
    p = subprocess.run([sys.executable, "tasks.py", *a], capture_output=True, text=True)
    print(p.stdout + p.stderr, flush=True)
    return p.stdout
def git(*a):
    subprocess.run(["git", *a], capture_output=True)
def work(tid):
    with open(f"work-{tid}.txt", "a") as f:
        f.write("more\n")
    git("add", "-A"); git("commit", "-qm", f"work on {tid}")
def result(text, error=False):
    print(json.dumps({"type": "result", "subtype": "error" if error else "success", "is_error": error,
                      "result": text}), flush=True)
if "do NOT continue implementing" in prompt:  # over-budget review
    tid = prompt.split("on task `")[1].split("`")[0]
    if "Nearly done" in prompt:
        tp("extend", tid, "--note", "almost there")
    elif mode == "hold":
        tp("hold", tid, "which database? SPEC.md says nothing")
    result("reviewed")
elif "You already own task `" in prompt:
    work(prompt.split("You already own task `")[1].split("`")[0])
    result("worked")
else:
    out = tp("claim")
    tid = out.split("CLAIMED ")[1].split()[0] if "CLAIMED " in out else None
    if tid:
        work(tid)
    if mode == "limit":
        result(f"Claude AI usage limit reached|{int(time.time()) + 3600}", error=True)
        sys.exit(1)
    result("claimed")
'''


def sh(cwd, *a, env=None, check=True):
    p = subprocess.run(a, cwd=cwd, capture_output=True, text=True, env=env)
    if check and p.returncode:
        raise AssertionError(f"{' '.join(map(str, a))} failed:\n{p.stdout}{p.stderr}")
    return p


def field(text, name):
    m = re.search(rf"^{name}:\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


class Board(unittest.TestCase):
    """A bare remote 'remote.git' and a human's clone 'w' with tasks a (ranked first) and b."""
    tasks = (("a", "Task A", "1"), ("b", "Task B", "2"))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", "remote.git")
        sh(self.tmp, "git", "clone", "-q", "remote.git", "w")
        self.w = self.tmp / "w"
        self.config(self.w, "human")
        for f in ("tasks.py", "board.py"):
            shutil.copy(SRC / f, self.w / f)
        for tid, title, prio in self.tasks:
            self.tp("new", tid, title, "--prio", prio)
        self.prepare(self.w)
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "init")
        sh(self.w, "git", "push", "-q", "-u", "origin", "main")

    def prepare(self, w):
        """Hook: change the task files before the first push."""

    def config(self, clone, name):
        sh(clone, "git", "config", "user.email", f"{name}@t")
        sh(clone, "git", "config", "user.name", name)

    def tp(self, *a, who=None, avoid=None, check=True):
        env = {k: v for k, v in os.environ.items() if k not in ("TASK_OWNER", "TASK_AVOID", "TASK_LEASE") and not k.startswith("TASKS_")}
        env["TASKS_ROOT"] = str(self.w)
        if who:
            env["TASK_OWNER"] = who
        if avoid:
            env["TASK_AVOID"] = avoid
        return sh(self.w, sys.executable, "tasks.py", *a, env=env, check=check)

    def remote_task(self, tid):
        return sh(self.tmp, "git", "--git-dir=remote.git", "show", f"main:tasks/{tid}.md").stdout


class Claiming(Board):
    def test_claim_without_id_leaves_avoided_task_to_others(self):
        out = self.tp("claim", who="x/agent-1", avoid="a").stdout
        self.assertIn("CLAIMED b as x/agent-1", out)

    def test_claim_falls_back_to_avoided_task_when_nothing_else_is_ready(self):
        self.tp("claim", "b", who="x/agent-2")
        out = self.tp("claim", who="x/agent-1", avoid="a").stdout
        self.assertIn("CLAIMED a as x/agent-1", out)

    def test_next_lists_avoided_task_last(self):
        plain = self.tp("next").stdout.splitlines()
        self.assertTrue(plain[0].startswith("a "))
        lines = self.tp("next", avoid="a").stdout.splitlines()
        self.assertTrue(lines[0].startswith("b "))
        self.assertTrue(lines[1].startswith("a "))
        self.assertIn("you gave it up", lines[1])

    def test_counted_releases_end_in_hold_handoffs_do_not(self):
        for i in range(1, 4):
            self.tp("claim", "a", who="x/agent-1")
            out = self.tp("release", "a", "--handoff", "--note", "usage limit", who="x/agent-1").stdout
            self.assertNotIn("ON HOLD", out)
        self.assertEqual(field(self.remote_task("a"), "releases"), "0")
        for i in range(1, 4):
            self.tp("claim", "a", who="x/agent-1")
            out = self.tp("release", "a", "--note", f"try {i}", who="x/agent-1").stdout
            self.assertEqual("ON HOLD" in out, i == 3, out)
        t = self.remote_task("a")
        self.assertEqual(field(t, "releases"), "3")
        self.assertIn("given back 3 times", field(t, "hold"))


class Runner(Board):
    """Runs `agents.py run` for one agent with the fake agent above, waits for an expected board state."""
    mode = ""
    budget, avoid = 1, 300

    def setUp(self):
        super().setUp()
        base = self.tmp / "agents"
        (base / "logs").mkdir(parents=True)
        self.clone, self.log = base / "agent-1", base / "logs" / "agent-1.log"
        sh(self.tmp, "git", "clone", "-q", "remote.git", str(self.clone))
        self.config(self.clone, OWNER)
        fake = self.tmp / "fake_agent.py"
        fake.write_text(textwrap.dedent(FAKE_AGENT))
        env = {k: v for k, v in os.environ.items() if not k.startswith("TASK")}
        env.update(FAKE_MODE=self.mode, AGENT_HOST="box")
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(fake))}"
        out = open(self.log, "w")
        self.addCleanup(out.close)
        self.proc = subprocess.Popen(
            [sys.executable, str(SRC / "agents.py"), "run", "agent-1", str(self.clone), "--owner", OWNER,
             "--cmd", cmd, "--lease", "120", "--limit-wait", "60", "--task-budget", str(self.budget),
             "--avoid", str(self.avoid)],
            stdout=out, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        self.addCleanup(self.kill)

    def kill(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()

    def wait_for(self, what, cond, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                break
            try:
                if cond():
                    return
            except AssertionError:
                pass
            time.sleep(0.5)
        self.fail(f"timed out waiting for {what}; runner log:\n{self.log.read_text()}")

    def state(self, tid):
        t = self.remote_task(tid)
        return dict(status=field(t, "status"), owner=field(t, "owner"), hold=field(t, "hold"),
                    releases=field(t, "releases") or "0", text=t)


class OverBudgetHandoff(Runner):
    def test_over_budget_task_is_released_not_held_and_not_reclaimed_at_once(self):
        # session 1 claims a; review offers extend (taken); session 3 works; review without extend decides
        # nothing -> released for another agent, and this runner continues with b instead
        self.wait_for("a released", lambda: self.state("a")["status"] == "todo" and "over budget" in self.state("a")["text"])
        a = self.state("a")
        self.assertEqual(a["hold"], "", a["text"])
        self.assertEqual(a["releases"], "1")
        self.assertIn("still resumable", a["text"])
        self.assertIn("extended by box/agent-1", a["text"])  # the bug report's path: extend first
        self.wait_for("b claimed", lambda: self.state("b")["status"] == "in_progress")
        self.assertEqual(self.state("b")["owner"], OWNER)
        self.assertEqual(self.state("a")["status"], "todo")  # left for another agent
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "--name-only", "wip/a").stdout
        self.assertIn("work-a.txt", wip)  # the work is there for the next owner
        self.assertNotIn("put a on hold", self.log.read_text())


class OnlyTaskLeft(Runner):
    tasks = (("a", "Task A", "1"),)
    avoid = 5

    def test_runner_waits_before_reclaiming_then_takes_it_back(self):
        self.wait_for("a released", lambda: self.state("a")["releases"] == "1")
        self.wait_for("waiting log", lambda: "leaving it to another agent" in self.log.read_text())
        self.assertEqual(self.state("a")["status"], "todo")
        # nobody else took it: after the cooldown the same agent continues rather than deadlocking
        self.wait_for("a reclaimed", lambda: self.state("a")["status"] == "in_progress", timeout=120)
        self.assertEqual(self.state("a")["owner"], OWNER)


class HumanHold(Runner):
    mode = "hold"

    def test_review_that_needs_a_human_still_holds(self):
        self.wait_for("a on hold", lambda: self.state("a")["hold"] != "")
        a = self.state("a")
        self.assertIn("which database", a["hold"])
        self.assertEqual(a["releases"], "0")
        self.wait_for("b claimed", lambda: self.state("b")["status"] == "in_progress")
        self.assertIn("which database", self.state("a")["hold"])


class RepeatedReleases(Runner):
    def prepare(self, w):
        f = w / "tasks" / "a.md"
        f.write_text(f.read_text().replace("owner:", "releases: 2\nowner:", 1))

    def test_third_counted_release_puts_task_on_hold(self):
        self.wait_for("a on hold", lambda: self.state("a")["hold"] != "")
        a = self.state("a")
        self.assertEqual(a["releases"], "3")
        self.assertIn("given back 3 times", a["hold"])


class UsageLimit(Runner):
    mode = "limit"

    def test_usage_limit_hands_off_uncounted(self):
        self.wait_for("a handed off", lambda: "handed off by" in self.state("a")["text"])
        a = self.state("a")
        self.assertEqual(a["status"], "todo")
        self.assertEqual(a["releases"], "0")
        self.assertEqual(a["hold"], "")
        self.wait_for("limit wait", lambda: "usage limit" in self.log.read_text())


if __name__ == "__main__":
    unittest.main()
