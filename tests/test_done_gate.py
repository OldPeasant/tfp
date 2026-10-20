#!/usr/bin/env python3
"""Tests for what `done` demands before a task counts as finished: every acceptance criterion verified
with evidence (`verify`), a check that ends within its time limit, failure output that stays readable
whatever the console's encoding; and for `touches`, which keeps agents off the same files at once.
Runs tasks.py against a local bare remote in a temp dir. Run: python3 tests/test_done_gate.py"""
import os, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.dont_write_bytecode = True
import tasks  # noqa: E402

# these tests are about the other gates of done; the review-evidence gate has tests/test_quality_gate.py
EXEMPT = ("--quality-exempt", "fixture for the criteria/check gates")

CRITERIA = """## Acceptance criteria
- [ ] login works in the browser
- [ ] container image builds
- [ ]

## Notes
"""


def sh(cwd, *a, env=None, check=True):
    p = subprocess.run(a, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if check and p.returncode:
        raise AssertionError(f"{' '.join(map(str, a))} failed:\n{p.stdout}{p.stderr}")
    return p


class Board(unittest.TestCase):
    """A bare remote and a clone 'w' with task `a` (two criteria, one empty placeholder) and `b`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", "remote.git")
        sh(self.tmp, "git", "clone", "-q", "remote.git", "w")
        self.w = self.tmp / "w"
        sh(self.w, "git", "config", "user.email", "t@t")
        sh(self.w, "git", "config", "user.name", "human")
        for f in ("tasks.py", "board.py"):
            shutil.copy(SRC / f, self.w / f)
        self.tp("new", "a", "Task A", "--touches", "frontend/src/app/state", *EXEMPT)
        self.tp("new", "b", "Task B", "--touches", "frontend/src/app/state/catalog.store.ts,docs", *EXEMPT)
        a = self.w / "tasks" / "a.md"
        a.write_text(a.read_text().split("## Acceptance criteria")[0] + CRITERIA)
        (self.w / ".taskcheck").write_text("true\n")
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "init")
        sh(self.w, "git", "push", "-q", "-u", "origin", "main")

    def tp(self, *a, who="agent-1", check=True, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("TASK_", "TASKS_"))}
        e.update(TASK_OWNER=who, **env)
        return sh(self.w, sys.executable, "tasks.py", *a, env=e, check=check)

    def remote(self, tid="a"):
        return sh(self.tmp, "git", "--git-dir=remote.git", "show", f"main:tasks/{tid}.md").stdout

    def commit_work(self):
        (self.w / "code.txt").write_text("work\n")
        sh(self.w, "git", "add", "code.txt")
        sh(self.w, "git", "commit", "-qm", "work")

    def verify_all(self):
        self.tp("verify", "a", "1", "npx playwright test login -> 3 passed")
        self.tp("verify", "a", "2", "--skip", "no docker on this machine")


class Criteria(Board):
    def test_done_refuses_unverified_criteria(self):
        self.tp("claim", "a")
        self.commit_work()
        p = self.tp("done", "a", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nobody verified", p.stderr)
        self.assertIn("#1 login works in the browser", p.stderr)
        self.assertIn("#2 container image builds", p.stderr)
        self.assertNotIn("#3", p.stderr)  # the empty placeholder is not a criterion
        self.assertIn("status: in_progress", self.remote())
        self.assertNotIn("code.txt", sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "main").stdout)

    def test_verify_lists_ticks_and_records_evidence(self):
        self.tp("claim", "a")
        self.assertIn("#1 [ ] login works in the browser", self.tp("verify", "a").stdout)
        out = self.tp("verify", "a", "1", "npx playwright test login -> 3 passed").stdout
        self.assertIn("VERIFIED a #1 (1 still to verify)", out)
        self.assertIn("SKIPPED a #2", self.tp("verify", "a", "2", "--skip", "no docker here").stdout)
        r = self.remote()
        self.assertIn("- [x] login works in the browser\n  - verified (", r)
        self.assertIn("by agent-1): npx playwright test login -> 3 passed\n- [~] container image builds", r)
        self.assertIn("  - skipped (", r)

    def test_verify_twice_keeps_both_lines_in_order(self):
        self.tp("claim", "a")
        self.tp("verify", "a", "1", "first run")
        self.tp("verify", "a", "1", "second run")
        r = self.remote()
        self.assertLess(r.index("first run"), r.index("second run"))
        self.assertLess(r.index("second run"), r.index("- [ ] container image builds"))

    def test_verify_refuses_other_owner_and_bad_number(self):
        self.tp("claim", "a")
        p = self.tp("verify", "a", "1", "x", who="agent-2", check=False)
        self.assertIn("not owned by agent-2", p.stderr)
        p = self.tp("verify", "a", "3", "x", check=False)
        self.assertIn("has 2 acceptance criteria", p.stderr)
        p = self.tp("verify", "a", "1", check=False)
        self.assertIn("say what you ran", p.stderr)

    def test_done_records_what_was_checked(self):
        self.tp("claim", "a")
        self.commit_work()
        self.verify_all()
        self.assertIn("DONE a", self.tp("done", "a").stdout)
        r = self.remote()
        self.assertIn("status: done", r)
        self.assertRegex(r, r"- done by agent-1: `true` passed in \ds on [0-9a-f]{9}; criteria verified 1/2, "
                            r"skipped #2\n$")

    def test_done_force_bypasses_unverified_criteria(self):
        self.tp("claim", "a")
        self.assertIn("DONE a", self.tp("done", "a", "--force").stdout)
        self.assertIn("criteria verified 0/2", self.remote())

    def test_task_without_criteria_is_not_blocked(self):
        self.tp("claim", "b")
        self.assertIn("DONE b", self.tp("done", "b").stdout)


class Check(Board):
    def finish(self, check, **env):
        (self.w / ".taskcheck").write_text(check + "\n")
        sh(self.w, "git", "commit", "-qam", "check")
        self.tp("claim", "b")
        return self.tp("done", "b", check=False, **env)

    def test_timeout_kills_the_whole_check_and_refuses(self):
        t0 = time.time()
        p = self.finish("echo started; sleep 60 & sleep 60", TASK_CHECK_TIMEOUT="2")
        self.assertLess(time.time() - t0, 30)  # the background sleep must not keep the pipes open
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("started", p.stderr)
        self.assertIn("did not finish within 2s", p.stderr)
        self.assertIn("status: in_progress", self.remote("b"))

    def test_slow_check_warns(self):
        p = self.finish("sleep 1", TASK_CHECK_SLOW="0")
        self.assertIn("DONE b", p.stdout)
        self.assertIn("warning: the check took", p.stdout)
        self.assertNotIn("warning", self.finish_again())

    def finish_again(self):
        self.tp("claim", "a")
        return self.tp("done", "a", "--force").stdout

    def test_failure_output_survives_a_narrow_console_encoding(self):
        # a step name with U+276F, then bytes that aren't UTF-8 (a tool writing in its code page)
        p = self.finish(r"""printf 'step frontend \342\235\257 FAILED\n\377\376 caf\351\n'; exit 3""",
                        PYTHONIOENCODING="cp1252")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("step frontend", p.stderr)
        self.assertIn("FAILED", p.stderr)
        self.assertIn("exit 3", p.stderr)
        self.assertIn("status: in_progress", self.remote("b"))

    def test_non_ascii_output_of_a_passing_check(self):
        p = self.finish(r"""printf '\342\234\223 all good\n'""", PYTHONIOENCODING="cp1252")
        self.assertIn("DONE b", p.stdout)


class Touches(Board):
    def test_overlaps(self):
        self.assertEqual(tasks.overlaps(["frontend/src"], ["./frontend/src/app/x.ts"]), ["frontend/src/app/x.ts"])
        self.assertEqual(tasks.overlaps(["frontend/"], ["frontend"]), ["frontend"])
        self.assertEqual(tasks.overlaps(["frontend/src"], ["frontend/srcx", "backend"]), [])
        self.assertEqual(tasks.overlaps(["."], ["docs"]), ["docs"])
        self.assertEqual(tasks.overlaps([], ["docs"]), [])

    def test_new_writes_touches(self):
        self.assertIn("touches: [frontend/src/app/state]\n", self.remote())
        self.tp("new", "c", "Task C")
        self.assertNotIn("touches", (self.w / "tasks" / "c.md").read_text())

    def test_next_prefers_tasks_away_from_work_in_progress(self):
        self.tp("new", "c", "Task C", "--prio", "1")
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "c")
        sh(self.w, "git", "push", "-q")
        self.tp("claim", "b", who="agent-2")
        out = self.tp("next").stdout.splitlines()
        self.assertTrue(out[0].startswith("c "), out)
        self.assertIn("(same files as b, in progress: frontend/src/app/state/catalog.store.ts)", out[1])
        claim = self.tp("claim").stdout
        self.assertIn("CLAIMED c", claim)
        self.assertNotIn("Heads-up", claim)
        claim = self.tp("claim", "a", who="agent-3").stdout
        self.assertIn("Heads-up: b (agent-2) is in progress and touches the same files", claim)

    def test_own_task_is_not_a_conflict(self):
        self.tp("claim", "b")
        self.assertNotIn("same files", self.tp("next").stdout)

    def test_check_warns_about_parallel_tasks_on_the_same_files(self):
        out = self.tp("check").stdout
        self.assertIn("ok", out)
        self.assertIn("warning: a and b both touch frontend/src/app/state/catalog.store.ts", out)
        b = self.w / "tasks" / "b.md"
        b.write_text(b.read_text().replace("deps: []", "deps: [a]"))
        self.assertNotIn("warning", self.tp("check").stdout)


if __name__ == "__main__":
    unittest.main()
