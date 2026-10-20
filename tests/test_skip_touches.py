#!/usr/bin/env python3
"""Tests for TASK_SKIP_TOUCHES: a machine that can't work on some paths (e.g. no Android SDK) is never
offered or given tasks touching them, by `next`, `claim` without an id or the runner's ranking, and
`claim <id>` refuses them unless --force. Runs tasks.py against a local bare remote in a temp dir.
Run: python3 tests/test_skip_touches.py"""
import os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True


def sh(cwd, *a, env=None, check=True):
    p = subprocess.run(a, cwd=cwd, capture_output=True, text=True, env=env)
    if check and p.returncode:
        raise AssertionError(f"{' '.join(map(str, a))} failed:\n{p.stdout}{p.stderr}")
    return p


class Skip(unittest.TestCase):
    """Tasks: and (android only, ranked first), mix (android + web), web (web only)."""
    tasks = (("and", "Android task", "1", "android/app"), ("mix", "Mixed task", "2", "web/src,android/app/res"),
             ("web", "Web task", "3", "web/src"))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", "remote.git")
        sh(self.tmp, "git", "clone", "-q", "remote.git", "w")
        self.w = self.tmp / "w"
        sh(self.w, "git", "config", "user.email", "human@t")
        sh(self.w, "git", "config", "user.name", "human")
        for f in ("tasks.py", "board.py"):
            shutil.copy(SRC / f, self.w / f)
        for tid, title, prio, touches in self.tasks:
            self.tp("new", tid, title, "--prio", prio, "--touches", touches)
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "init")
        sh(self.w, "git", "push", "-q", "-u", "origin", "main")

    def env(self, skip):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TASK_OWNER", "TASK_AVOID", "TASK_LEASE", "TASK_SKIP_TOUCHES")}
        env.update(TASKS_ROOT=str(self.w), TASK_OWNER="x/agent-1")
        if skip is not None:
            env["TASK_SKIP_TOUCHES"] = skip
        return env

    def tp(self, *a, skip=None, check=True):
        return sh(self.w, sys.executable, "tasks.py", *a, env=self.env(skip), check=check)

    def ranked_ids(self, skip):
        code = "import tasks; print(' '.join(t['id'] for t in tasks.ranked(tasks.analyze(tasks.load()))))"
        return sh(self.w, sys.executable, "-c", code, env=self.env(skip)).stdout.split()

    def test_without_setting_everything_is_offered(self):
        self.assertEqual([l.split()[0] for l in self.tp("next").stdout.splitlines()], ["and", "mix", "web"])

    def test_next_hides_tasks_touching_skipped_paths(self):
        self.assertEqual([l.split()[0] for l in self.tp("next", skip="android").stdout.splitlines()], ["web"])

    def test_runner_ranking_hides_them_too(self):
        self.assertEqual(self.ranked_ids(" android/ , ios"), ["web"])
        self.assertEqual(self.ranked_ids(""), ["and", "mix", "web"])

    def test_claim_without_id_takes_a_task_this_machine_can_do(self):
        self.assertIn("CLAIMED web as x/agent-1", self.tp("claim", skip="android").stdout)

    def test_claim_without_id_finds_nothing_when_only_skipped_tasks_are_ready(self):
        self.tp("claim", "web")
        p = self.tp("claim", skip="android", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no ready task", p.stderr)

    def test_claim_by_id_refuses_a_skipped_task_unless_forced(self):
        p = self.tp("claim", "mix", skip="android", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("which this machine skips (TASK_SKIP_TOUCHES)", p.stderr)
        self.assertIn("CLAIMED mix", self.tp("claim", "mix", "--force", skip="android").stdout)

    def test_a_path_that_only_shares_a_prefix_is_not_skipped(self):
        self.assertEqual(self.ranked_ids("andr"), ["and", "mix", "web"])


if __name__ == "__main__":
    unittest.main()
