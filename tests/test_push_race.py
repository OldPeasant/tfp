#!/usr/bin/env python3
"""Tests for two runner bugs: a runner whose clone is behind main (an older tasks.py without what agents.py
needs) must not crash, and pushes that only lost races with other agents (non-fast-forward, `cannot lock
ref`, a transient `remote rejected ... (failed)`) must not be reported as refused (PUSH FAILED, the
push-failure record that blocks the task), while a remote that really refuses (auth, hook) still is.
Runs tasks.py and agents.py against a local bare remote. Run: python3 tests/test_push_race.py"""
import shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from test_overbudget import SRC, Board, field, sh  # noqa: E402
from test_push_failure import FAKE_AGENT, PushRunner, exempt, hook  # noqa: E402

sys.path.insert(0, str(SRC))
import tasks  # noqa: E402


def race_hook(tmp, prefix, times):
    """Another agent's push lands on main during each of our next `times` pushes to main whose commit subject
    starts with `prefix` (git: `cannot lock ref ... but expected ...`, `(incorrect old value provided)`), like
    a real race. Counts them in remote.git/races."""
    return hook(tmp, f'''#!/bin/sh
unset GIT_QUARANTINE_PATH
while read old new ref; do
  [ "$ref" = refs/heads/main ] || continue
  case "$(git log -1 --format=%s "$new")" in "{prefix}"*) ;; *) continue ;; esac
  n=$(cat races 2>/dev/null || echo 0)
  [ "$n" -ge {times} ] && continue
  echo $((n + 1)) > races
  c=$(git commit-tree -p "$old" -m "another agent's push" "$(git rev-parse "$old^{{tree}}")")
  git update-ref "$ref" "$c" "$old"
done
exit 0
''')


def flaky_hook(tmp, times):
    """The connection drops during each of the next `times` pushes (git: `the remote end hung up unexpectedly`):
    an error that is neither a lost race nor a refusal. Counts them in remote.git/drops."""
    return hook(tmp, f'''#!/bin/sh
n=$(cat drops 2>/dev/null || echo 0)
[ "$n" -ge {times} ] && exit 0
echo $((n + 1)) > drops
kill -9 $PPID
exit 1
''')


def refusing_hook(tmp, refs):
    """The remote refuses every push to a ref matching the shell pattern `refs` (git: `(pre-receive hook
    declined)`), like a protected branch. Counts the refused pushes in remote.git/refusals."""
    return hook(tmp, f'''#!/bin/sh
while read old new ref; do
  case "$ref" in {refs})
    n=$(cat refusals 2>/dev/null || echo 0)
    echo $((n + 1)) > refusals
    echo "pushes to $ref are not allowed for this account" >&2
    exit 1 ;;
  esac
done
exit 0
''')


def counted(tmp, name):
    f = tmp / "remote.git" / name
    return int(f.read_text()) if f.exists() else 0


def drops(tmp):
    return counted(tmp, "drops")


def races(tmp):
    f = tmp / "remote.git" / "races"
    return int(f.read_text()) if f.exists() else 0


class ImportTasks(unittest.TestCase):
    """agents.py's import_tasks(): the tasks.py next to agents.py, not the (maybe older) clone's copy."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        (self.clone / "tasks.py").write_text("WHICH = 'clone'\n")
        self.empty = self.tmp / "cwd"
        self.empty.mkdir()

    def imported(self, runner_dir):
        code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import agents; "
                "t = agents.import_tasks(Path(sys.argv[2])); print(Path(t.__file__).resolve().parent); "
                "print(getattr(t, 'WHICH', 'runner'), hasattr(t, 'PUSH_FAILURE'))")
        p = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(runner_dir), str(self.clone)],
                           cwd=self.empty, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout.split("\n")

    def test_uses_the_tasks_py_next_to_agents_py(self):
        runner = self.tmp / "checkout"
        runner.mkdir()
        for f in ("agents.py", "tasks.py"):
            shutil.copy(SRC / f, runner / f)
        where, which = self.imported(runner)[:2]
        self.assertEqual(Path(where), runner.resolve())
        self.assertEqual(which, "runner True")

    def test_falls_back_to_the_clones_tasks_py_without_one_next_to_agents_py(self):
        runner = self.tmp / "only-agents"
        runner.mkdir()
        shutil.copy(SRC / "agents.py", runner / "agents.py")
        where, which = self.imported(runner)[:2]
        self.assertEqual(Path(where), self.clone.resolve())
        self.assertEqual(which, "clone False")


class Classify(unittest.TestCase):
    def test_lost_races(self):
        for err in (" ! [rejected]        main -> main (fetch first) | error: failed to push some refs",
                    " ! [rejected]        main -> main (non-fast-forward)",
                    "remote: error: cannot lock ref 'refs/heads/main': Unable to create 'main.lock': File exists. | "
                    " ! [remote rejected] main -> main (reference already exists)",
                    " ! [remote rejected] main -> main (failed)",
                    " ! [remote rejected] main -> main (failed to update ref)",
                    " ! [rejected]        main -> main (stale info)",
                    # what git prints when another push landed in between (seen against a local bare remote)
                    "remote: error: cannot lock ref 'refs/heads/main': is at d3aed29e but expected aa47698a | "
                    "To /x/remote.git | ! [remote rejected] HEAD -> main (incorrect old value provided) | "
                    "error: failed to push some refs to '/x/remote.git'",
                    " ! [REJECTED] MAIN -> MAIN (NON-FAST-FORWARD)"):  # case does not matter
            self.assertTrue(tasks.lost_race(err), err)

    def test_refusals(self):
        for err in (" ! [remote rejected] main -> main (pre-receive hook declined)",
                    "remote: Permission to o/r.git denied to bot. | fatal: unable to access: The requested URL "
                    "returned error: 403",
                    "fatal: Authentication failed for 'https://example.test/r.git/'",
                    " ! [remote rejected] main -> main (protected branch hook declined)",
                    " ! [remote rejected] main -> main (refusing to allow an OAuth App to create or update workflow "
                    "`.github/workflows/ci.yml` without `workflow` scope)",
                    # a hook that happens to mention a race word is still a hook refusal
                    "remote: fetch first, then try | ! [remote rejected] main -> main (pre-receive hook declined)",
                    # a server with receive.denyNonFastForwards refuses a forced push (wip): no race can cause it
                    "remote: error: denying non-fast-forward refs/heads/wip/x (you should pull first) | "
                    "To /x/remote.git | ! [remote rejected] HEAD -> wip/x (non-fast-forward) | "
                    "error: failed to push some refs to '/x/remote.git'"):
            self.assertFalse(tasks.lost_race(err), err)
            self.assertTrue(tasks.refused(err), err)

    def test_unknown_errors_are_neither(self):  # retried like a race, reported as PUSH FAILED after 5 attempts
        for err in ("error: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500 | "
                    "fatal: the remote end hung up unexpectedly",
                    "send-pack: unexpected disconnect while reading sideband packet | "
                    "fatal: the remote end hung up unexpectedly",
                    "fatal: Could not read from remote repository.",
                    ""):
            self.assertFalse(tasks.lost_race(err), err)
            self.assertFalse(tasks.refused(err), err)

    def test_more_real_world_refusals(self):
        for err in ("git@example.test: Permission denied (publickey). | fatal: Could not read from remote "
                    "repository.",
                    "remote: GitLab: You are not allowed to push code to protected branches on this project. | "
                    " ! [remote rejected] main -> main (pre-receive hook declined)",
                    "remote: Write access to repository not granted. | fatal: unable to access "
                    "'https://example.test/o/r.git/': The requested URL returned error: 403",
                    # a GitHub repository ruleset (e.g. required pull requests or status checks on main)
                    "remote: error: GH013: Repository rule violations found for refs/heads/main. | "
                    "To https://example.test/o/r.git | "
                    " ! [remote rejected] main -> main (push declined due to repository rule violations) | "
                    "error: failed to push some refs to 'https://example.test/o/r.git'"):
            self.assertTrue(tasks.refused(err), err)
            self.assertFalse(tasks.lost_race(err), err)

    def test_ruleset_wordings_are_refusals_on_their_own(self):  # each of "push declined"/"rule violation" alone
        for err in ("remote: error: GH013: Repository rule violations found for refs/heads/main. | "
                    "error: failed to push some refs to 'https://example.test/o/r.git'",
                    " ! [remote rejected] main -> main (push declined) | "
                    "error: failed to push some refs to 'https://example.test/o/r.git'"):
            self.assertTrue(tasks.refused(err), err)
            self.assertFalse(tasks.lost_race(err), err)

    def test_without_an_argument_the_last_push_error_is_classified(self):  # how update()/save_work() call them
        old = tasks.push_error[0]
        self.addCleanup(tasks.push_error.__setitem__, 0, old)
        tasks.push_error[0] = " ! [rejected]        main -> main (fetch first)"
        self.assertTrue(tasks.lost_race())
        self.assertFalse(tasks.refused())
        tasks.push_error[0] = " ! [remote rejected] main -> main (pre-receive hook declined)"
        self.assertFalse(tasks.lost_race())
        self.assertTrue(tasks.refused())
        tasks.push_error[0] = "fatal: the remote end hung up unexpectedly"
        self.assertFalse(tasks.lost_race())
        self.assertFalse(tasks.refused())


class Races(Board):
    """tasks.py alone against a remote whose branch is locked by another push in progress."""
    who = "x/agent-1"
    prepare = staticmethod(exempt)  # `done` gets as far as its push

    def setUp(self):
        super().setUp()
        self.tp("claim", "a", who=self.who)
        self.refs = self.tmp / "remote.git" / "refs" / "heads"

    def lock(self, ref):
        f = self.refs / f"{ref}.lock"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")
        return f

    def failure_file(self):
        rel = sh(self.w, "git", "rev-parse", "--git-path", "tfp-push-failed").stdout.strip()
        return self.w / rel

    def assert_lost_race(self, p):
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("BUSY: ", out)
        self.assertIn("lost the push race 5 times", out)
        self.assertIn("try again", out)
        self.assertNotIn("PUSH FAILED", out)
        self.assertNotIn("Only a human", out)
        self.assertNotIn("Traceback", out)
        self.assertFalse(self.failure_file().exists(), "a lost race must not block the task")
        t = self.remote_task("a")
        self.assertEqual((field(t, "status"), field(t, "owner")), ("in_progress", self.who))

    def test_board_push_that_loses_every_race_is_not_refused(self):
        lock = self.lock("main")
        self.assert_lost_race(self.tp("release", "a", "--note", "x", who=self.who, check=False))
        lock.unlink()  # the other push finished: trying again works
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("RELEASED a", p.stdout)

    def test_wip_push_that_loses_every_race_is_not_refused(self):
        (self.w / "work.txt").write_text("work\n")
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "work")
        lock = self.lock("wip/a")
        self.assert_lost_race(self.tp("release", "a", "--note", "x", who=self.who, check=False))
        self.assertTrue((self.w / "work.txt").exists())  # the work is still here
        lock.unlink()
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "--name-only", "wip/a").stdout
        self.assertIn("work.txt", wip)

    def test_hook_refusal_with_race_words_is_still_refused(self):
        hook(self.tmp, "#!/bin/sh\necho 'cannot lock ref: branch is protected, fetch first' >&2\nexit 1\n")
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("PUSH FAILED: ", out)
        self.assertIn("Only a human can fix it", out)
        self.assertNotIn("BUSY", out)
        self.assertTrue(self.failure_file().exists())

    def assert_refused(self, p):
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("PUSH FAILED: ", out)
        self.assertIn("Only a human can fix it", out)
        self.assertNotIn("BUSY", out)
        self.assertTrue(self.failure_file().exists())

    def test_forced_wip_push_denied_as_non_fast_forward_is_refused(self):
        sh(self.tmp, "git", "--git-dir=remote.git", "config", "receive.denyNonFastForwards", "true")
        other = sh(self.w, "git", "commit-tree", "HEAD^{tree}", "-m", "unrelated").stdout.strip()
        sh(self.w, "git", "push", "-q", "origin", f"{other}:refs/heads/wip/a")
        (self.w / "work.txt").write_text("work\n")
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "work")
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        self.assert_refused(p)
        self.assertIn("denying non-fast-forward", p.stdout + p.stderr)

    def test_board_push_that_fails_once_for_an_unknown_reason_is_retried(self):
        flaky_hook(self.tmp, 1)
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("RELEASED a", out)
        self.assertNotIn("PUSH FAILED", out)
        self.assertEqual(drops(self.tmp), 1)
        self.assertFalse(self.failure_file().exists())
        self.assertEqual(field(self.remote_task("a"), "status"), "todo")

    def test_wip_push_that_fails_once_for_an_unknown_reason_is_retried(self):
        (self.w / "work.txt").write_text("work\n")
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "work")
        flaky_hook(self.tmp, 1)  # the first push is the one to wip/a
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("PUSH FAILED", out)
        self.assertEqual(drops(self.tmp), 1)
        self.assertFalse(self.failure_file().exists())
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "--name-only", "wip/a").stdout
        self.assertIn("work.txt", wip)

    def test_board_push_that_keeps_failing_for_an_unknown_reason_fails_after_5_attempts(self):
        flaky_hook(self.tmp, 100)
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        self.assert_refused(p)
        self.assertIn("hung up unexpectedly", p.stdout + p.stderr)
        self.assertEqual(drops(self.tmp), 5)

    def commit_work(self):
        (self.w / "work.txt").write_text("work\n")
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "work")
        return sh(self.w, "git", "rev-parse", "HEAD").stdout.strip()

    def test_board_push_refused_by_the_remote_fails_at_once(self):
        refusing_hook(self.tmp, "refs/heads/main")
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        self.assert_refused(p)
        self.assertIn("hook declined", p.stdout + p.stderr)
        self.assertEqual(counted(self.tmp, "refusals"), 1)  # retrying a refusal won't help: no 5 attempts
        t = self.remote_task("a")
        self.assertEqual((field(t, "status"), field(t, "owner")), ("in_progress", self.who))

    def test_done_push_refused_by_the_remote_fails_at_once_and_keeps_the_code(self):
        head = self.commit_work()
        refusing_hook(self.tmp, "refs/heads/main")
        p = self.tp("done", "a", "--force", who=self.who, check=False)
        self.assert_refused(p)
        self.assertEqual(counted(self.tmp, "refusals"), 1)
        self.assertEqual(sh(self.w, "git", "rev-parse", "HEAD").stdout.strip(), head)
        self.assertEqual(field(self.remote_task("a"), "status"), "in_progress")

    def test_wip_push_refused_by_the_remote_fails_at_once_and_keeps_the_work(self):
        head = self.commit_work()
        refusing_hook(self.tmp, "refs/heads/wip/*")
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        self.assert_refused(p)
        self.assertIn("still here, unpushed", p.stdout + p.stderr)
        self.assertEqual(counted(self.tmp, "refusals"), 1)
        self.assertEqual(sh(self.w, "git", "rev-parse", "HEAD").stdout.strip(), head)  # not reset away
        t = self.remote_task("a")
        self.assertEqual((field(t, "status"), field(t, "owner")), ("in_progress", self.who))

    def test_wip_push_that_keeps_failing_for_an_unknown_reason_fails_after_5_attempts(self):
        head = self.commit_work()
        flaky_hook(self.tmp, 100)  # every push fails, the wip push is the first one
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        self.assert_refused(p)
        self.assertIn("hung up unexpectedly", p.stdout + p.stderr)
        self.assertEqual(drops(self.tmp), 5)
        self.assertEqual(sh(self.w, "git", "rev-parse", "HEAD").stdout.strip(), head)
        t = self.remote_task("a")
        self.assertEqual((field(t, "status"), field(t, "owner")), ("in_progress", self.who))

    def test_board_push_that_loses_to_concurrent_pushes_is_not_refused(self):
        race_hook(self.tmp, "release: a", 5)  # another agent's push lands during each of our 5 attempts
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        self.assert_lost_race(p)
        self.assertIn("incorrect old value", p.stdout + p.stderr)  # git's error is shown
        self.assertEqual(races(self.tmp), 5)
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)  # the others are done: works
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("RELEASED a", p.stdout)
        self.assertEqual(field(self.remote_task("a"), "status"), "todo")

    def test_board_push_that_loses_four_races_succeeds_in_the_fifth_attempt(self):
        race_hook(self.tmp, "release: a", 4)
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("RELEASED a", out)
        self.assertNotIn("BUSY", out)
        self.assertEqual(races(self.tmp), 4)
        self.assertFalse(self.failure_file().exists())
        main = sh(self.tmp, "git", "--git-dir=remote.git", "log", "--format=%s", "main").stdout
        self.assertEqual(main.count("another agent's push"), 4)  # the winners' commits are kept, not overwritten

    def test_hold_that_loses_every_race_is_not_refused(self):
        lock = self.lock("main")
        p = self.tp("hold", "a", "needs a human", who=self.who, check=False)
        self.assert_lost_race(p)
        self.assertNotIn("ON HOLD", p.stdout)
        self.assertEqual(field(self.remote_task("a"), "hold"), "")
        lock.unlink()
        p = self.tp("hold", "a", "needs a human", who=self.who, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(field(self.remote_task("a"), "hold"), "needs a human")

    def test_done_that_loses_every_race_is_not_refused_and_keeps_the_code(self):
        (self.w / "work.txt").write_text("work\n")
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "work")
        head = sh(self.w, "git", "rev-parse", "HEAD").stdout.strip()
        lock = self.lock("main")
        self.assert_lost_race(self.tp("done", "a", "--force", who=self.who, check=False))
        self.assertEqual(sh(self.w, "git", "rev-parse", "HEAD").stdout.strip(), head)  # our code commit is kept
        self.assertNotIn("work", sh(self.tmp, "git", "--git-dir=remote.git", "log", "--format=%s", "main").stdout
                         .splitlines())
        lock.unlink()
        p = self.tp("done", "a", "--force", who=self.who, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(field(self.remote_task("a"), "status"), "done")
        self.assertIn("work.txt", sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "--name-only", "main").stdout)


class BehindClone(PushRunner):
    """The agent's clone is on an older main whose tasks.py lacks PUSH_FAILURE (which agents.py reads after
    every session); the human's checkout (agents.py) is newer. The runner must keep working."""
    fake_agent, mode, budget = FAKE_AGENT, "release", 10

    def prepare(self, w):
        super().prepare(w)
        f = w / "tasks.py"
        old = "\n".join(l for l in f.read_text().splitlines() if not l.startswith("PUSH_FAILURE ="))
        f.write_text(old + "\n")

    def before_start(self):  # the clone exists (old main); then the human pushes the new tasks.py
        shutil.copy(SRC / "tasks.py", self.w / "tasks.py")
        sh(self.w, "git", "commit", "-qam", "new tasks.py")
        sh(self.w, "git", "push", "-q")
        clone_tp = (self.clone / "tasks.py").read_text()
        self.assertNotIn("\nPUSH_FAILURE =", clone_tp)

    def test_runner_on_a_clone_behind_main_does_not_crash(self):
        self.wait_for("2 sessions", lambda: self.sessions() >= 2)
        time.sleep(2)
        log = self.log.read_text()
        self.assertNotIn("Traceback", log)
        self.assertNotIn("AttributeError", log)
        self.assertIsNone(self.proc.poll(), log)  # still running


class AgentReleaseLosesRaces(PushRunner):
    """The agent's own `release` loses all 5 races in its first session: the runner must not block the task
    (no push-failure record), the next session runs and its release goes through."""
    fake_agent, mode, budget = FAKE_AGENT, "release", 10

    def before_start(self):
        race_hook(self.tmp, "release: a", 5)

    def test_lost_races_in_a_session_do_not_block_the_task(self):
        self.wait_for("a released", lambda: "release by box/agent-1" in self.state("a")["text"], timeout=120)
        log = self.log.read_text()
        self.assertEqual(races(self.tmp), 5)
        self.assertGreaterEqual(self.sessions(), 2, log)  # the release that lost every race did not stop sessions
        self.assertNotIn("BLOCKED", log)
        self.assertNotIn("PUSH FAILED", log)
        self.assertNotIn("Traceback", log)
        self.assertNotEqual(self.status()["state"], "blocked")


class ForceReleaseKeepsLosingRaces(PushRunner):
    """The runner's own release (give_up) loses every race in all of its 6 attempts (30 races): the task is not
    blocked (nothing for a human), the next round's review and release go through."""
    mode, budget = "", 1

    def before_start(self):
        race_hook(self.tmp, "release: a", 30)

    def test_give_up_does_not_block_a_task_whose_release_only_lost_races(self):
        self.wait_for("released log", lambda: "released a for another agent" in self.log.read_text(), timeout=300)
        log = self.log.read_text()
        self.assertEqual(races(self.tmp), 30)
        self.assertIn("release of a lost every push race to other agents, trying again later", log)
        self.assertNotIn("release FAILED", log)
        self.assertNotIn("BLOCKED", log)
        self.assertNotIn("Traceback", log)
        a = self.state("a")
        self.assertNotEqual(a["owner"], "box/agent-1")
        self.assertIn("release by box/agent-1", a["text"])


class BlockedRetryLosesRaces(PushRunner):
    """A task blocked by a real refusal (the default PushRunner hook refuses the workflow file in wip/a): a human
    fixes the remote, but the runner's retry then loses every race (30). That is not a reason to wait another
    --blocked-retry: the next retry comes after about 60 s, and it releases the task."""
    mode, budget, retry = "release", 10, 110

    def test_retry_that_only_lost_races_is_retried_soon(self):
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text())
        race_hook(self.tmp, "release: a", 30)  # replaces the refusing hook: pushes work, but others keep winning
        self.wait_for("lost races", lambda: "lost every push race to other agents" in self.log.read_text(),
                      timeout=self.retry + 90)
        self.wait_for("released log", lambda: "released a for another agent" in self.log.read_text(), timeout=150)
        log = self.log.read_text()
        self.assertEqual(races(self.tmp), 30)
        self.assertNotIn("Traceback", log)
        self.assertEqual(log.count("BLOCKED on a"), 1, log)  # the lost races did not block it again

        def at(line):
            h, m, s = map(int, line[1:9].split(":"))
            return h * 3600 + m * 60 + s

        lines = log.splitlines()
        lost = at(next(l for l in lines if "lost every push race to other agents" in l))
        retry = at([l for l in lines if "retrying to release a" in l][-1])
        self.assertLess((retry - lost) % 86400, self.retry - 25, log)  # about 60 s, not --blocked-retry
        self.assertIn("handed back by box/agent-1", self.state("a")["text"])


class ForceReleaseLosesRaces(PushRunner):
    """A review that decided nothing -> the runner's own release (give_up) loses all 5 races once: it tries
    again after a pause instead of logging FAILED and blocking the task."""
    mode, budget = "", 1

    def before_start(self):
        race_hook(self.tmp, "release: a", 5)

    def test_give_up_retries_a_release_that_lost_every_race(self):
        self.wait_for("released log", lambda: "released a for another agent" in self.log.read_text(), timeout=150)
        log = self.log.read_text()
        self.assertEqual(races(self.tmp), 5)
        self.assertNotIn("release FAILED", log)
        self.assertNotIn("BLOCKED", log)
        self.assertNotIn("Traceback", log)
        a = self.state("a")
        self.assertNotEqual(a["owner"], "box/agent-1")
        self.assertIn("release by box/agent-1", a["text"])


if __name__ == "__main__":
    unittest.main()
