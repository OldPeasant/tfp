#!/usr/bin/env python3
"""Tests for catching up with main (`sync`, run by claim, done, ...) when the unpushed work contains a merge:
the previous owner's `wip/<id>` merged after a claim, or `git merge origin/main` after conflicts (AGENTS.md
"Merge conflicts" b). A rebase would flatten that merge and replay every merged commit, with the very
conflicts the merge resolved, so `done` could never finish. Linear work is still rebased.
Runs tasks.py against a local bare remote. Run: python3 tests/test_sync_merge.py"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from test_done_gate import Board, sh  # noqa: E402


class SyncWithMerge(Board):
    def setUp(self):
        super().setUp()
        (self.w / "shared.txt").write_text("base\n")
        sh(self.w, "git", "add", "shared.txt")
        sh(self.w, "git", "commit", "-qm", "shared file")
        sh(self.w, "git", "push", "-q")
        sh(self.tmp, "git", "clone", "-q", "remote.git", "other")
        self.other = self.tmp / "other"
        sh(self.other, "git", "config", "user.email", "o@o")
        sh(self.other, "git", "config", "user.name", "other agent")

    def other_pushes(self, name, text):
        sh(self.other, "git", "pull", "-q", "--rebase")
        (self.other / name).write_text(text)
        sh(self.other, "git", "add", name)
        sh(self.other, "git", "commit", "-qm", f"other: {name}")
        sh(self.other, "git", "push", "-q")

    def claim_and_merge_conflicting_main(self):
        """Task a claimed, own commit on shared.txt, main changed it too: resolved once by merging main."""
        self.tp("claim", "a")
        (self.w / "shared.txt").write_text("mine\n")
        sh(self.w, "git", "commit", "-qam", "a: change shared")
        self.other_pushes("shared.txt", "theirs\n")
        sh(self.w, "git", "fetch", "-q")
        self.assertNotEqual(sh(self.w, "git", "merge", "origin/main", check=False).returncode, 0)  # conflict
        (self.w / "shared.txt").write_text("resolved\n")
        sh(self.w, "git", "add", "shared.txt")
        sh(self.w, "git", "commit", "-qm", "merge main into a")
        self.verify_all()

    def main_file(self, name):
        return sh(self.tmp, "git", "--git-dir=remote.git", "show", f"main:{name}").stdout

    def test_done_after_merge_when_main_did_not_move(self):
        self.claim_and_merge_conflicting_main()
        p = self.tp("done", "a", check=False)
        self.assertNotIn("CONFLICT", p.stdout + p.stderr)
        self.assertIn("DONE a", p.stdout)
        self.assertEqual(self.main_file("shared.txt"), "resolved\n")
        self.assertIn("status: done", self.remote())

    def test_done_after_merge_when_main_moved_again(self):
        self.claim_and_merge_conflicting_main()
        self.other_pushes("unrelated.txt", "x\n")
        p = self.tp("done", "a", check=False)
        self.assertNotIn("CONFLICT", p.stdout + p.stderr)
        self.assertIn("DONE a", p.stdout)
        self.assertEqual(self.main_file("shared.txt"), "resolved\n")
        self.assertEqual(self.main_file("unrelated.txt"), "x\n")

    def test_new_conflict_after_merge_is_reported_as_merge(self):
        self.claim_and_merge_conflicting_main()
        self.other_pushes("shared.txt", "theirs again\n")
        p = self.tp("done", "a", check=False)
        self.assertIn("CONFLICT while catching up with main in: shared.txt", p.stderr)
        self.assertTrue((self.w / ".git" / "MERGE_HEAD").exists())  # one merge to resolve, not a rebase
        self.assertFalse((self.w / ".git" / "rebase-merge").exists())

    def test_linear_work_is_still_rebased(self):
        self.tp("claim", "a")
        self.commit_work()
        self.verify_all()
        self.other_pushes("unrelated.txt", "x\n")
        self.assertIn("DONE a", self.tp("done", "a").stdout)
        merges = sh(self.tmp, "git", "--git-dir=remote.git", "rev-list", "--merges", "main").stdout
        self.assertEqual(merges, "")


if __name__ == "__main__":
    unittest.main()
