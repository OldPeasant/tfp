#!/usr/bin/env python3
"""Tests for reviewing assumptions (`assume`, `assumptions`, `confirm`, `reject`) against a local bare
remote in a temp dir. Run: python3 tests/test_assumptions.py"""
import os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent


def sh(cwd, *a, env=None, check=True):
    p = subprocess.run(a, cwd=cwd, capture_output=True, text=True, env=env)
    if check and p.returncode:
        raise AssertionError(f"{' '.join(a)} failed:\n{p.stdout}{p.stderr}")
    return p


class Assumptions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", "remote.git")
        sh(self.tmp, "git", "clone", "-q", "remote.git", "w")
        self.w = self.tmp / "w"
        sh(self.w, "git", "config", "user.email", "t@t")
        sh(self.w, "git", "config", "user.name", "Peter Gürtler")  # a human name with a space
        for f in ("tasks.py", "board.py"):
            shutil.copy(SRC / f, self.w / f)
        self.tp("new", "api", "API")
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "init")
        sh(self.w, "git", "push", "-q", "-u", "origin", "main")
        for text in ("REST, not GraphQL", "ids are UUIDs", "dates in UTC"):
            self.tp("assume", "api", text, who="dell/agent-1")

    def tp(self, *a, who=None, check=True):
        env = {k: v for k, v in os.environ.items() if k != "TASK_OWNER" and not k.startswith("TASKS_")}
        if who:
            env["TASK_OWNER"] = who
        return sh(self.w, sys.executable, "tasks.py", *a, env=env, check=check)

    def remote_task(self):
        return sh(self.tmp, "git", "--git-dir=remote.git", "show", "main:tasks/api.md").stdout

    def test_list_numbers_open_assumptions(self):
        out = self.tp("assumptions").stdout
        for n, text in ((1, "REST, not GraphQL"), (2, "ids are UUIDs"), (3, "dates in UTC")):
            self.assertIn(f"#{n} dell/agent-1", out)
            self.assertIn(text, out)
        self.assertIn("3 assumption(s) to review", out)

    def test_confirm_and_reject_leave_the_list_but_stay_in_the_file(self):
        self.assertIn("CONFIRMED api #1", self.tp("confirm", "api", "1").stdout)
        out = self.tp("reject", "api", "2", "--note", "use numeric ids [db default]").stdout
        self.assertIn("REJECTED api #2", out)
        self.assertIn("add one", out)
        open_ = self.tp("assumptions").stdout
        self.assertIn("1 assumption(s) to review", open_)
        self.assertNotIn("REST, not GraphQL", open_)
        self.assertIn("dates in UTC", open_)
        everything = self.tp("assumptions", "--all").stdout
        self.assertIn("[confirmed by Peter Gürtler]", everything)
        self.assertIn("[rejected by Peter Gürtler: use numeric ids (db default)]", everything)
        self.assertIn("1 assumption(s) to review, 2 reviewed", everything)
        remote = self.remote_task()  # pushed, like `assume`
        self.assertRegex(remote, r"- ASSUMPTION \(.+ by dell/agent-1\) \[confirmed \S+ \S+ by Peter Gürtler\]: REST")
        self.assertIn("1 assumption(s) to review", self.tp("status").stdout)

    def test_confirm_all(self):
        self.tp("confirm", "api", "2")
        self.assertIn("CONFIRMED api #1, #3", self.tp("confirm", "api", "--all").stdout)
        self.assertIn("0 assumption(s) to review", self.tp("assumptions").stdout)
        p = self.tp("confirm", "api", "--all", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no assumptions to review", p.stderr)

    def test_refusals(self):
        for args, err in ((("confirm", "api", "9"), "has 3 assumption(s), no #9"),
                          (("confirm", "api"), "give its number"),
                          (("reject", "api", "1", "--note", " "), "say why with --note")):
            p = self.tp(*args, check=False)
            self.assertNotEqual(p.returncode, 0, args)
            self.assertIn(err, p.stderr)
        self.tp("confirm", "api", "1")
        p = self.tp("reject", "api", "1", "--note", "x", check=False)
        self.assertIn("#1 is confirmed already", p.stderr)

    def test_human_assumption_with_space_in_name_is_listed(self):
        self.tp("assume", "api", "decided by a human")  # owner = git user.name "Peter Gürtler"
        out = self.tp("assumptions").stdout
        self.assertIn("#4 Peter Gürtler", out)
        self.assertIn("decided by a human", out)

    def test_review_does_not_push_unfinished_code(self):
        (self.w / "wip.txt").write_text("unfinished\n")
        sh(self.w, "git", "add", "wip.txt")
        sh(self.w, "git", "commit", "-qm", "wip")
        self.tp("confirm", "api", "1")
        self.assertNotEqual(sh(self.tmp, "git", "--git-dir=remote.git", "cat-file", "-e", "main:wip.txt",
                               check=False).returncode, 0)
        self.assertTrue((self.w / "wip.txt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
