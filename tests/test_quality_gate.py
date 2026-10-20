#!/usr/bin/env python3
"""Tests for the review-evidence gate of `done` (docs/QUALITY.md): a task is done only with committed,
complete and current reports of the test-writer and code-reviewer (and the fixer when fixes were
required) plus a valid reviews/<id>/quality.json. The gate is opt-in: only a repo with a committed
docs/QUALITY.md has it. Runs tasks.py against a local bare remote in a temp
dir: no real remote is written. Run: python3 tests/test_quality_gate.py"""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True

# the contract that switches the gate on: the template in TFP itself, docs/QUALITY.md in a working repo
CONTRACT = next(p for p in (SRC / "templates" / "quality" / "QUALITY.md", SRC / "docs" / "QUALITY.md") if p.exists())
FILLER = "Checked every acceptance criterion against the diff and the spec; commands and results below. " * 3


def sh(cwd, *a, env=None, check=True):
    p = subprocess.run(a, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if check and p.returncode:
        raise AssertionError(f"{' '.join(map(str, a))} failed:\n{p.stdout}{p.stderr}")
    return p


def report(tid, section, extra=""):
    return f"# Report\n\nTask: {tid}\n\nScope: task commits\n\n{FILLER}\n\n{section}\n\nAll good.\n{extra}"


class Gate(unittest.TestCase):
    """A bare remote and a clone 'w' with tasks `a` and `b` (no criteria), a passing check and, with
    GATE, the gate switched on (docs/QUALITY.md committed)."""
    GATE = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", "remote.git")
        sh(self.tmp, "git", "clone", "-q", "remote.git", "w")
        self.w = self.tmp / "w"
        for k, v in (("user.email", "t@t"), ("user.name", "human")):
            sh(self.w, "git", "config", k, v)
        for f in ("tasks.py", "board.py"):
            shutil.copy(SRC / f, self.w / f)
        self.tp("new", "a", "Task A", "--touches", "src/a")
        self.tp("new", "b", "Task B", "--touches", "src/b")
        self.tp("new", "c", "Write the ADR", "--quality-exempt", "documentation only, nothing to test")
        (self.w / ".taskcheck").write_text("true\n")
        if self.GATE:
            (self.w / "docs").mkdir()
            shutil.copy(CONTRACT, self.w / "docs" / "QUALITY.md")
        self.commit_all("init")
        sh(self.w, "git", "push", "-q", "-u", "origin", "main")

    def tp(self, *a, who="agent-1", check=True, cwd=None, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("TASK_", "TASKS_"))}
        e.update(TASK_OWNER=who, **env)
        return sh(cwd or self.w, sys.executable, "tasks.py", *a, env=e, check=check)

    def commit_all(self, msg, cwd=None):
        sh(cwd or self.w, "git", "add", "-A")
        sh(cwd or self.w, "git", "commit", "-qm", msg)

    def remote(self, path):
        p = sh(self.tmp, "git", "--git-dir=remote.git", "show", f"main:{path}", check=False)
        return p.stdout if p.returncode == 0 else None

    def code(self, tid="a", text="work\n"):
        d = self.w / "src" / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.txt").write_text(text)
        self.commit_all(f"{tid}: code")

    def evidence(self, tid="a", commit=True, fixes=False, findings=(), validation=None, **override):
        """Write the helpers' reports and quality.json for the code at HEAD (the reviewed revision)."""
        d = self.w / "reviews" / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "tests.md").write_text(report(tid, "## Coverage"))
        (d / "review.md").write_text(report(tid, "## Review conclusion"))
        helpers = {"test-writer": {"status": "completed", "report": f"reviews/{tid}/tests.md"},
                   "code-reviewer": {"status": "completed", "report": f"reviews/{tid}/review.md"},
                   "fixer": {"status": "not_required", "reason": "no findings"}}
        if fixes:
            (d / "fixes.md").write_text(report(tid, "## Final status"))
            helpers["fixer"] = {"status": "completed", "report": f"reviews/{tid}/fixes.md"}
        q = dict(schema=1, task=tid, reviewed_revision=sh(self.w, "git", "rev-parse", "HEAD").stdout.strip(),
                 helpers=helpers, fixes_required=fixes, findings=list(findings),
                 validation=validation or [{"command": "sh -c \"$(cat .taskcheck)\"", "result": "pass"}])
        q.update(override)
        (d / "quality.json").write_text(json.dumps(q, indent=2))
        if commit:
            self.commit_all(f"{tid}: review evidence")
        return d

    def refused(self, *args, tid="a"):
        p = self.tp("done", tid, *args, check=False)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertIn("REFUSED", p.stderr)
        # nothing was published: the task is still in progress and its code is not on main
        self.assertIn("status: in_progress", self.remote(f"tasks/{tid}.md"))
        self.assertIsNone(self.remote(f"src/{tid}/x.txt"))
        return p.stderr


class Refusals(Gate):
    def test_missing_evidence(self):
        self.tp("claim", "a")
        self.code()
        err = self.refused()
        self.assertIn("reviews/a/quality.json is missing", err)
        self.assertIn("--force and --skip-check do not bypass this", err)

    def test_reports_not_committed(self):
        self.tp("claim", "a")
        self.code()
        self.evidence(commit=False)  # untracked files: `done` does not count them as dirty
        err = self.refused()
        self.assertIn("quality.json is missing (it exists but is not committed)", err)

    def test_empty_and_placeholder_reports(self):
        self.tp("claim", "a")
        self.code()
        d = self.evidence(commit=False)
        (d / "tests.md").write_text("")
        (d / "review.md").write_text(report("a", "## Review conclusion", "Date: <YYYY-MM-DD>\n"))
        self.commit_all("evidence")
        err = self.refused()
        self.assertIn("reviews/a/tests.md is empty or too short", err)
        self.assertIn("reviews/a/review.md still contains the placeholder '<YYYY-MM-DD>'", err)

    def test_wrong_task_metadata(self):
        self.tp("claim", "a")
        self.code()
        self.evidence(task="b")
        self.assertIn("task is 'b', not 'a'", self.refused())

    def test_report_of_another_task(self):
        self.tp("claim", "a")
        self.code()
        d = self.evidence(commit=False)
        (d / "review.md").write_text(report("b", "## Review conclusion"))
        self.commit_all("evidence")
        self.assertIn("reviews/a/review.md does not name the task 'a'", self.refused())

    def test_helper_not_completed(self):
        self.tp("claim", "a")
        self.code()
        self.evidence(helpers={"test-writer": {"status": "failed", "report": "reviews/a/tests.md"},
                               "code-reviewer": {"status": "completed", "report": "reviews/a/review.md"},
                               "fixer": {"status": "not_required", "reason": "none"}})
        self.assertIn("helpers.test-writer.status is 'failed'", self.refused())

    def test_stale_evidence_after_code_change(self):
        self.tp("claim", "a")
        self.code()
        self.evidence()
        self.code(text="changed after the review\n")
        self.assertIn("the task's code changed after the review", self.refused())

    def test_fixes_required_without_fixer_report(self):
        self.tp("claim", "a")
        self.code()
        d = self.evidence(commit=False, fixes=True,
                          findings=[{"id": "CR-1", "severity": "P1", "disposition": "fixed"}])
        (d / "fixes.md").unlink()
        self.commit_all("evidence")
        self.assertIn("reviews/a/fixes.md is missing", self.refused())

    def test_fixed_finding_without_fixes_required(self):
        self.tp("claim", "a")
        self.code()
        self.evidence(findings=[{"id": "CR-1", "severity": "P2", "disposition": "fixed"}])
        self.assertIn("CR-1 is fixed, so fixes_required must be true", self.refused())

    def test_unresolved_findings(self):
        self.tp("claim", "a")
        self.code()
        self.evidence(findings=[{"id": "CR-1", "severity": "P2", "disposition": "open"},
                                {"id": "CR-2", "severity": "P1", "disposition": "deferred", "note": "later"},
                                {"id": "TW-1", "severity": "P3", "disposition": "accepted"}])
        err = self.refused()
        self.assertIn("finding CR-1: disposition 'open' is unresolved", err)
        self.assertIn("finding CR-2: P1 is blocking", err)
        self.assertIn("finding TW-1: a 'accepted' finding needs a note", err)

    def test_failed_validation(self):
        self.tp("claim", "a")
        self.code()
        self.evidence(validation=[{"command": "./gradlew test", "result": "fail"}])
        self.assertIn("validation './gradlew test': result is 'fail'", self.refused())

    def test_force_and_skip_check_do_not_bypass(self):
        self.tp("claim", "a")
        self.code()
        self.refused("--force")
        self.refused("--skip-check")
        self.refused("--force", "--skip-check")

    def test_worker_cannot_exempt_its_own_task(self):
        self.tp("claim", "a")
        self.code()
        t = self.w / "tasks" / "a.md"
        t.write_text(t.read_text().replace("prio:", "quality: exempt\nquality_reason: trust me\nprio:", 1))
        self.commit_all("a: exempt myself")
        self.assertIn("was not classified so (with a quality_reason) when it was claimed", self.refused())

    def test_deleting_the_contract_in_the_task_does_not_switch_it_off(self):
        self.tp("claim", "a")
        self.code()
        sh(self.w, "git", "rm", "-q", "docs/QUALITY.md")
        self.commit_all("a: no more gate")
        self.assertIn("reviews/a/quality.json is missing", self.refused())


class Accepted(Gate):
    def test_complete_evidence_is_published_with_the_code(self):
        self.tp("claim", "a")
        self.code()
        self.evidence(fixes=True, findings=[{"id": "CR-1", "severity": "P1", "disposition": "fixed"},
                                            {"id": "CR-2", "severity": "P3", "disposition": "invalid",
                                             "note": "the value is validated upstream"}])
        self.assertIn("DONE a", self.tp("done", "a").stdout)
        self.assertIn("status: done", self.remote("tasks/a.md"))
        for f in ("src/a/x.txt", "reviews/a/tests.md", "reviews/a/review.md", "reviews/a/fixes.md",
                  "reviews/a/quality.json"):
            self.assertIsNotNone(self.remote(f), f)

    def test_report_only_commits_after_review_are_allowed(self):
        self.tp("claim", "a")
        self.code()
        d = self.evidence()
        (d / "review.md").write_text(report("a", "## Review conclusion", "Re-read: no material issues.\n"))
        self.commit_all("a: amend review report")
        self.assertIn("DONE a", self.tp("done", "a").stdout)

    def test_evidence_stays_current_when_main_moves(self):
        """Another agent finishes b (code + task state) after a's review: done rebases a onto it, and the
        review of a's own change still counts."""
        self.tp("claim", "a")
        self.code()
        self.evidence()
        sh(self.tmp, "git", "clone", "-q", "remote.git", "w2")
        w2 = self.tmp / "w2"
        for k, v in (("user.email", "t@t"), ("user.name", "other")):
            sh(w2, "git", "config", k, v)
        self.tp("claim", "b", who="agent-2", cwd=w2)
        (w2 / "src" / "b").mkdir(parents=True)
        (w2 / "src" / "b" / "x.txt").write_text("b\n")
        self.commit_all("b: code", cwd=w2)
        w, self.w = self.w, w2
        try:
            self.evidence("b")
        finally:
            self.w = w
        self.assertIn("DONE b", self.tp("done", "b", who="agent-2", cwd=w2).stdout)
        self.assertIn("DONE a", self.tp("done", "a").stdout)
        self.assertIsNotNone(self.remote("src/b/x.txt"))
        self.assertIsNotNone(self.remote("src/a/x.txt"))

    def test_planned_exemption(self):
        self.tp("claim", "c")
        out = self.tp("done", "c").stdout
        self.assertIn("quality gate: c is exempt (documentation only, nothing to test)", out)
        self.assertIn("DONE c", out)

    def test_check_validates_quality_fields(self):
        t = self.w / "tasks" / "b.md"
        t.write_text(t.read_text().replace("prio:", "quality: exempt\nprio:", 1))
        out = self.tp("check", check=False)
        self.assertIn("b: quality: exempt needs a quality_reason", out.stdout + out.stderr)
        self.assertIn("needs a reason", self.tp("new", "d", "D", "--quality-exempt", " ", check=False).stderr)


class OptIn(Gate):
    GATE = False

    def test_without_the_contract_done_needs_no_evidence(self):
        self.tp("claim", "a")
        self.code()
        out = self.tp("done", "a").stdout
        self.assertIn("DONE a", out)
        self.assertIsNotNone(self.remote("src/a/x.txt"))


if __name__ == "__main__":
    unittest.main()
