#!/usr/bin/env python3
"""Tests for a remote that keeps refusing pushes (like GitHub refusing .github/workflows/* from a token
without `workflow` scope): `release`/`hold`/`done` print PUSH FAILED and exit non-zero instead of a
traceback or a silent loop, and the runner stops starting sessions on such a task (state `blocked`),
retries only every --blocked-retry s and gives the task back once pushes work again.
Runs tasks.py and agents.py (with a scripted fake agent) against a local bare remote whose pre-receive
hook refuses commits touching .github/workflows/. Run: python3 tests/test_push_failure.py"""
import json, re, sys, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from test_overbudget import Board, Runner, field, sh  # noqa: E402

# like GitHub with a token lacking `workflow` scope: commits that touch .github/workflows/ are refused
WORKFLOW_HOOK = r'''#!/bin/sh
z=0000000000000000000000000000000000000000
while read old new ref; do
  [ "$new" = "$z" ] && continue
  if [ "$old" = "$z" ]; then range="$new --not --all"; else range="$old..$new"; fi
  if git log --name-only --format= $range | grep -q '^\.github/workflows/'; then
    echo "! [remote rejected] refusing to allow an OAuth App to create or update workflow without workflow scope" >&2
    exit 1
  fi
done
'''
REFUSE_ALL_HOOK = "#!/bin/sh\necho 'remote: Permission to the repo denied' >&2\nexit 1\n"

FAKE_AGENT = r'''
import json, os, subprocess, sys, time
prompt, mode = sys.argv[-1], os.environ.get("FAKE_MODE", "")
def tp(*a):
    p = subprocess.run([sys.executable, "tasks.py", *a], capture_output=True, text=True)
    print(p.stdout + p.stderr, flush=True)
    return p.stdout
def work(tid):
    os.makedirs(".github/workflows", exist_ok=True)
    with open(".github/workflows/ci.yml", "a") as f:
        f.write("# more\n")
    subprocess.run(["git", "add", "-A"], capture_output=True)
    subprocess.run(["git", "commit", "-qm", f"work on {tid}"], capture_output=True)
def result(text):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text}), flush=True)
if "do NOT continue implementing" in prompt:  # over-budget review
    tid = prompt.split("on task `")[1].split("`")[0]
    if "Nearly done" in prompt:
        tp("extend", tid, "--note", "almost there")
    result("reviewed")
    sys.exit(0)
if "You already own task `" in prompt:
    tid = prompt.split("You already own task `")[1].split("`")[0]
else:
    out = tp("claim")
    tid = out.split("CLAIMED ")[1].split()[0] if "CLAIMED " in out else None
if tid:
    work(tid)
    if mode == "release":
        tp("release", tid, "--note", "cannot finish")
    elif mode == "done":
        tp("done", tid, "--force")
    elif mode == "limit":  # out of tokens: the runner hands the task off (release --handoff)
        print(json.dumps({"type": "result", "subtype": "error", "is_error": True,
                          "result": f"Claude AI usage limit reached|{int(time.time()) + 3600}"}), flush=True)
        sys.exit(1)
result("worked")
'''


def hook(tmp, script):
    h = tmp / "remote.git" / "hooks" / "pre-receive"
    h.write_text(script)
    h.chmod(0o755)
    return h


def exempt(w):
    """Every task is quality-exempt, so `done` gets as far as its push."""
    for f in (w / "tasks").glob("*.md"):
        f.write_text(f.read_text().replace("owner:", "quality: exempt\nquality_reason: test fixture\nowner:", 1))


class TasksPy(Board):
    """tasks.py alone: the agent's view of a refused push."""
    prepare = staticmethod(exempt)
    who = "x/agent-1"

    def setUp(self):
        super().setUp()
        self.tp("claim", "a", who=self.who)
        hook(self.tmp, WORKFLOW_HOOK)

    def commit_workflow(self):
        (self.w / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (self.w / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        sh(self.w, "git", "add", "-A")
        sh(self.w, "git", "commit", "-qm", "ci")
        return sh(self.w, "git", "rev-parse", "HEAD").stdout.strip()

    def failure_file(self):
        rel = sh(self.w, "git", "rev-parse", "--git-path", "tfp-push-failed").stdout.strip()
        return json.loads((self.w / rel).read_text())

    def assert_push_failed(self, p, hint):
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("PUSH FAILED: ", out)
        self.assertIn("without workflow scope", out.split("PUSH FAILED: ")[1].splitlines()[0])  # git's error
        self.assertIn(hint, out)
        self.assertNotIn("Traceback", out)

    def assert_unchanged(self, head):
        t = self.remote_task("a")
        self.assertEqual((field(t, "status"), field(t, "owner")), ("in_progress", self.who))
        self.assertEqual(sh(self.w, "git", "rev-parse", "HEAD").stdout.strip(), head)  # the work is still here
        self.assertNotIn("wip/a", sh(self.tmp, "git", "--git-dir=remote.git", "branch").stdout)

    def test_release_with_refused_wip_push(self):
        head = self.commit_workflow()
        p = self.tp("release", "a", "--note", "giving up", who=self.who, check=False)
        self.assert_push_failed(p, "Only a human can fix it")
        self.assert_unchanged(head)
        f = self.failure_file()
        self.assertEqual((f["task"], f["cmd"]), ("a", "release"))
        self.assertIn("workflow scope", f["error"])

    def test_hold_with_refused_wip_push(self):
        head = self.commit_workflow()
        p = self.tp("hold", "a", "needs a human", who=self.who, check=False)
        self.assert_push_failed(p, "Only a human can fix it")
        self.assert_unchanged(head)
        self.assertEqual(field(self.remote_task("a"), "hold"), "")
        f = self.failure_file()
        self.assertEqual((f["cmd"], f["reason"]), ("hold", "needs a human"))  # the runner retries this hold

    def test_done_with_refused_push(self):
        head = self.commit_workflow()
        p = self.tp("done", "a", "--force", who=self.who, check=False)
        self.assert_push_failed(p, "Nothing changed on the board")
        self.assert_unchanged(head)
        self.assertEqual(self.failure_file()["cmd"], "done")

    def test_release_with_refused_board_push(self):
        hook(self.tmp, REFUSE_ALL_HOOK)  # nothing to save to wip/a: the board commit itself is refused
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("PUSH FAILED: ", out)
        self.assertIn("Permission to the repo denied", out)
        self.assertIn("Nothing changed on the board", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(field(self.remote_task("a"), "status"), "in_progress")

    def test_release_still_works_when_pushes_do(self):
        self.commit_workflow()
        hook(self.tmp, "#!/bin/sh\nexit 0\n")
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("RELEASED a", p.stdout)
        self.assertNotIn("PUSH FAILED", p.stdout + p.stderr)

    def test_release_with_wip_saved_but_board_push_refused(self):
        # the remote takes wip/a but refuses main (e.g. a protected branch): PUSH FAILED, task still ours
        hook(self.tmp, "#!/bin/sh\nwhile read o n ref; do [ \"$ref\" = refs/heads/main ] && "
                       "{ echo 'protected branch main' >&2; exit 1; }; done\nexit 0\n")
        self.commit_workflow()
        p = self.tp("release", "a", "--note", "x", who=self.who, check=False)
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("PUSH FAILED: ", out)
        self.assertIn("protected branch main", out.split("PUSH FAILED: ")[1].splitlines()[0])
        self.assertNotIn("Traceback", out)
        t = self.remote_task("a")
        self.assertEqual((field(t, "status"), field(t, "owner")), ("in_progress", self.who))
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "-r", "--name-only", "wip/a").stdout
        self.assertIn(".github/workflows/ci.yml", wip)  # the work is safe on wip/a
        self.assertEqual(self.failure_file()["cmd"], "release")

    def test_hold_of_unclaimed_task_with_refused_board_push(self):
        hook(self.tmp, REFUSE_ALL_HOOK)
        p = self.tp("hold", "b", "needs a human", who=self.who, check=False)
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("PUSH FAILED: ", out)
        self.assertIn("Permission to the repo denied", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("ON HOLD", out)
        self.assertEqual(field(self.remote_task("b"), "hold"), "")
        self.assertEqual(self.failure_file()["task"], "b")


class PushRunner(Runner):
    fake_agent = FAKE_AGENT
    prepare = staticmethod(exempt)
    retry = 1800

    @property
    def extra_args(self):
        return ("--blocked-retry", str(self.retry))

    def before_start(self):
        hook(self.tmp, WORKFLOW_HOOK)

    def sessions(self):
        return self.log.read_text().count("session start")

    def status(self):
        return json.loads((self.clone.parent / "logs" / "agent-1.status.json").read_text())

    def assert_blocked_without_new_sessions(self, n):
        self.wait_for("blocked status", lambda: self.status()["state"] == "blocked")
        s = self.status()
        self.assertEqual(s["task"], "a")
        self.assertIn("workflow scope", s["error"])
        time.sleep(8)  # several idle rounds of the main loop
        self.assertEqual(self.sessions(), n, self.log.read_text())
        self.assertEqual(self.status()["state"], "blocked")
        a = self.state("a")
        self.assertEqual((a["status"], a["owner"]), ("in_progress", "box/agent-1"))


class ReleaseRefused(PushRunner):
    mode, budget = "release", 10

    def test_failed_release_in_a_session_blocks_at_once(self):
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text())
        self.assert_blocked_without_new_sessions(1)
        self.assertNotIn("released a", self.log.read_text())


class ReviewReleaseRefused(PushRunner):
    mode, budget = "", 1

    def test_force_release_after_review_logs_failed_and_blocks(self):
        # claim, review (extend), work, review (nothing decided) -> force_release fails
        self.wait_for("release FAILED log", lambda: "release FAILED for a" in self.log.read_text(), timeout=120)
        log = self.log.read_text()
        self.assertNotIn("released a for another agent", log)
        self.assertIn("BLOCKED on a", log)
        self.assert_blocked_without_new_sessions(4)


class DoneRefused(PushRunner):
    mode, budget = "done", 10

    def test_same_done_push_error_three_sessions_in_a_row_blocks(self):
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text(), timeout=120)
        log = self.log.read_text()
        for n in (1, 2, 3):
            self.assertIn(f"({n}/3 sessions in a row)", log)
        self.assert_blocked_without_new_sessions(3)


class RetryAfterFix(PushRunner):
    mode, budget, retry = "release", 10, 5

    def test_retries_then_hands_task_back_once_pushes_work(self):
        h = self.tmp / "remote.git" / "hooks" / "pre-receive"
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text())
        self.wait_for("a retry", lambda: "retrying to release a" in self.log.read_text(), timeout=30)
        self.assertEqual(self.sessions(), 1)
        h.unlink()  # a human fixed the token
        self.wait_for("a handed back", lambda: "pushes had failed" in self.state("a")["text"], timeout=60)
        # the board push comes first, the log line once give_up() returned
        self.wait_for("released log", lambda: "released a for another agent" in self.log.read_text(), timeout=30)
        log = self.log.read_text()
        self.assertIn("released a for another agent", log.split("retrying to release a")[-1])
        self.assertIn("handed back by box/agent-1", self.state("a")["text"])
        self.assertEqual(self.state("a")["hold"], "")  # (the runner may claim it again now: pushes work)
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "-r", "--name-only", "wip/a").stdout
        self.assertIn(".github/workflows/ci.yml", wip)


class UsageLimitHandOffRefused(PushRunner):
    mode, budget = "limit", 10

    def test_refused_hand_off_keeps_the_work_and_blocks(self):
        # usage limit -> hand_off(): `release --handoff` fails (wip/a refused), the fallback wip push fails too:
        # no reset to @{u}, no second release; the task stays ours and the runner is blocked on it
        self.wait_for("hand-off FAILED log", lambda: "hand-off FAILED for a" in self.log.read_text())
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text())
        log = self.log.read_text()
        self.assertIn("workflow scope", log.split("hand-off FAILED for a")[1].splitlines()[0])
        self.assertNotIn("handed off a", log)
        a = self.state("a")
        self.assertEqual((a["status"], a["owner"]), ("in_progress", "box/agent-1"))
        self.assertNotIn("handed off by", a["text"])
        self.assertIn("work on a", sh(self.clone, "git", "log", "--format=%s", "HEAD").stdout)  # not reset away
        self.assertIn(".github/workflows/ci.yml",
                      sh(self.clone, "git", "ls-tree", "-r", "--name-only", "HEAD").stdout)
        self.assertNotIn("wip/a", sh(self.tmp, "git", "--git-dir=remote.git", "branch").stdout)
        self.assertEqual(self.sessions(), 1)
        self.wait_for("limit wait", lambda: "usage limit" in self.log.read_text().split("BLOCKED on a")[1])
        s = self.status()  # waiting out the usage limit, but still shown as blocked on a
        self.assertEqual((s["state"], s["task"]), ("blocked", "a"))
        self.assertIn("workflow scope", s["error"])


# FAKE_AGENT's `limit`, but before the usage limit another clone pushes a conflicting notes.txt to main, so the
# runner's `release --handoff` stops with CONFLICT while catching up (a failure that is not a push failure)
CONFLICT_LIMIT_AGENT = FAKE_AGENT.replace('''    elif mode == "limit":''', '''    elif mode == "limit":
        with open("notes.txt", "w") as f:
            f.write("agent version\\n")
        subprocess.run(["git", "add", "-A"], capture_output=True)
        subprocess.run(["git", "commit", "-qm", f"notes for {tid}"], capture_output=True)
        other = os.path.join("..", "..", "other")
        subprocess.run(["git", "clone", "-q", os.path.join("..", "..", "remote.git"), other], capture_output=True)
        with open(os.path.join(other, "notes.txt"), "w") as f:
            f.write("someone else's version\\n")
        for c in (["add", "-A"], ["-c", "user.name=other", "-c", "user.email=o@t", "commit", "-qm", "other notes"],
                  ["push", "-q", "origin", "HEAD:main"]):
            subprocess.run(["git", *c], cwd=other, capture_output=True)''', 1)
assert CONFLICT_LIMIT_AGENT != FAKE_AGENT


class ConflictLimitRunner(PushRunner):
    fake_agent, mode, budget = CONFLICT_LIMIT_AGENT, "limit", 10


class UsageLimitHandOffConflict(ConflictLimitRunner):
    """hand_off() when `release --handoff` fails for another reason (CONFLICT) and pushes work: as before the
    task: the work goes to wip/a by the fallback push, the clone is reset to @{u} and the release is redone."""

    def before_start(self):
        pass  # the remote takes every push

    def test_hand_off_after_conflict_saves_work_to_wip_and_releases(self):
        self.wait_for("a handed off", lambda: "handed off by" in self.state("a")["text"], timeout=120)
        self.wait_for("hand-off log", lambda: "handed off a: RELEASED a" in self.log.read_text())
        log = self.log.read_text()
        self.assertNotIn("hand-off FAILED", log)
        self.assertNotIn("BLOCKED", log)
        a = self.state("a")
        self.assertEqual((a["status"], a["releases"], a["hold"]), ("todo", "0", ""))
        self.assertNotEqual(a["owner"], "box/agent-1")
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "log", "--format=%s", "wip/a").stdout
        self.assertIn("notes for a", wip)  # the fallback push saved the work (with the rebase aborted first)
        self.assertIn("work on a", wip)
        main = sh(self.tmp, "git", "--git-dir=remote.git", "log", "--format=%s", "main").stdout
        self.assertNotIn("work on a", main)  # never on main
        self.assertIn("someone else's", sh(self.tmp, "git", "--git-dir=remote.git", "show", "main:notes.txt").stdout)
        self.assertEqual(sh(self.clone, "git", "status", "--porcelain").stdout.strip(), "")
        self.assertEqual(self.status()["state"], "limited")


class UsageLimitHandOffConflictWipRefused(ConflictLimitRunner):
    """The same CONFLICT, but the remote refuses the fallback push to wip/a (no tasks.py push failure recorded):
    the runner keeps the work in the clone, does not reset it away, and blocks on a."""

    def before_start(self):
        hook(self.tmp, WORKFLOW_HOOK)  # notes.txt from the other clone passes; the agent's ci.yml commit does not

    def test_hand_off_after_conflict_with_refused_wip_push_blocks(self):
        self.wait_for("hand-off FAILED log", lambda: "hand-off FAILED for a" in self.log.read_text(), timeout=120)
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text())
        self.wait_for("limit wait", lambda: "usage limit" in self.log.read_text().split("BLOCKED on a")[1])
        log = self.log.read_text()
        self.assertIn("workflow scope", log.split("hand-off FAILED for a")[1].splitlines()[0])  # git's push error
        self.assertNotIn("handed off a", log)
        s = self.status()  # waiting out the usage limit, but still shown as blocked on a
        self.assertEqual((s["state"], s["task"]), ("blocked", "a"))
        self.assertIn("workflow scope", s["error"])
        a = self.state("a")
        self.assertEqual((a["status"], a["owner"]), ("in_progress", "box/agent-1"))
        self.assertNotIn("handed off by", a["text"])
        head = sh(self.clone, "git", "log", "--format=%s", "HEAD").stdout
        self.assertIn("work on a", head)  # not reset away
        self.assertIn("notes for a", head)
        self.assertEqual(sh(self.clone, "git", "status", "--porcelain").stdout.strip(), "")  # rebase aborted
        self.assertNotIn("wip/a", sh(self.tmp, "git", "--git-dir=remote.git", "branch").stdout)
        self.assertEqual(self.sessions(), 1)


# FAKE_AGENT, but every over-budget review puts the task on hold for a human
REVIEW_HOLD_AGENT = FAKE_AGENT.replace('''    if "Nearly done" in prompt:
        tp("extend", tid, "--note", "almost there")
''', '''    tp("hold", tid, "which database? SPEC.md says nothing")
''', 1)
assert REVIEW_HOLD_AGENT != FAKE_AGENT


class ReviewHoldRefused(PushRunner):
    """The agent chooses `hold` in its over-budget review and the remote refuses the push: the runner must not
    run its own release over it (that would lose the hold and its reason). Blocked with the hold as the retry;
    once pushes work, the task ends up on hold with the agent's reason, not back on the board."""
    fake_agent, mode, budget, retry = REVIEW_HOLD_AGENT, "", 1, 5

    def test_refused_hold_in_a_review_is_kept_as_a_hold(self):
        # claim + work (ci.yml), then the review: hold -> refused
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text(), timeout=120)
        self.assert_blocked_without_new_sessions(2)
        self.assertEqual(self.state("a")["hold"], "")
        self.wait_for("a hold retry", lambda: "retrying to put a on hold" in self.log.read_text(), timeout=30)
        (self.tmp / "remote.git" / "hooks" / "pre-receive").unlink()  # a human fixed the token
        self.wait_for("a on hold", lambda: self.state("a")["hold"] != "", timeout=60)
        self.wait_for("hold log", lambda: "put a on hold for a human" in self.log.read_text(), timeout=30)
        a = self.state("a")
        self.assertEqual((a["status"], a["hold"]), ("todo", "which database? SPEC.md says nothing"))
        self.assertNotEqual(a["owner"], "box/agent-1")
        self.assertNotIn("review that decided nothing", a["text"])
        self.assertNotIn("handed back by", a["text"])
        log = self.log.read_text()
        self.assertNotIn("release FAILED", log)  # no runner release over the agent's hold
        self.assertNotIn("retrying to release a", log)
        self.assertNotIn("released a for another agent", log)
        blocked = log.split("BLOCKED on a")[1].split("put a on hold for a human")[0]
        self.assertNotIn("session start", blocked, log)  # no session while it was blocked (after it: b, fine)
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "-r", "--name-only", "wip/a").stdout
        self.assertIn(".github/workflows/ci.yml", wip)  # the work is saved for whoever fixes it


# FAKE_AGENT, but every over-budget review tries to finish the task with `done`
REVIEW_DONE_AGENT = FAKE_AGENT.replace('''    if "Nearly done" in prompt:
        tp("extend", tid, "--note", "almost there")
''', '''    tp("done", tid, "--force")
''', 1)
assert REVIEW_DONE_AGENT != FAKE_AGENT


class ReviewDoneRefused(PushRunner):
    """The agent runs `done` in its over-budget review and the remote refuses the push. Unlike a refused
    release/hold (ReviewHoldRefused), the task was not given up: the runner still runs its own release over it
    (refused too: blocked at once, retry = release) and counts the `done` failure in the streak."""
    fake_agent, mode, budget, retry = REVIEW_DONE_AGENT, "", 1, 5

    def test_refused_done_in_a_review_still_force_releases_and_blocks(self):
        # claim + work (ci.yml), then the review: done -> refused, force_release -> refused
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text(), timeout=120)
        self.assert_blocked_without_new_sessions(2)  # blocked after the first review, not after 3 more reviews
        log = self.log.read_text()
        self.assertIn("release FAILED for a", log)  # the runner's own release ran (and was refused)
        self.assertIn("done could not push a (1/3 sessions in a row)", log)  # read before the release's failure
        self.assertNotIn("(2/3 sessions in a row)", log)
        self.assertNotIn("released a for another agent", log)
        self.assertEqual(self.state("a")["hold"], "")
        # the block's retry is the runner's release, not a hold (one done failure is no streak yet)
        self.wait_for("a release retry", lambda: "retrying to release a" in self.log.read_text(), timeout=30)
        log = self.log.read_text()
        self.assertNotIn("retrying to put a on hold", log)
        self.assertEqual(self.sessions(), 2, log)


# FAKE_AGENT, but the over-budget review that decides nothing (no "Nearly done" offer) first commits notes.txt
# while another clone pushes a conflicting notes.txt to main: the runner's force_release() then stops with
# CONFLICT while catching up (not a push failure)
CONFLICT_DEF = '''def conflict(tid):
    with open("notes.txt", "w") as f:
        f.write("agent version\\n")
    subprocess.run(["git", "add", "-A"], capture_output=True)
    subprocess.run(["git", "commit", "-qm", f"notes for {tid}"], capture_output=True)
    other = os.path.join("..", "..", "other")
    subprocess.run(["git", "clone", "-q", os.path.join("..", "..", "remote.git"), other], capture_output=True)
    with open(os.path.join(other, "notes.txt"), "w") as f:
        f.write("someone else's version\\n")
    for c in (["add", "-A"], ["-c", "user.name=other", "-c", "user.email=o@t", "commit", "-qm", "other notes"],
              ["push", "-q", "origin", "HEAD:main"]):
        subprocess.run(["git", *c], cwd=other, capture_output=True)
'''
REVIEW_CONFLICT_AGENT = FAKE_AGENT.replace('if "do NOT continue', CONFLICT_DEF + 'if "do NOT continue', 1).replace(
    '    result("reviewed")', '    if "Nearly done" not in prompt:\n        conflict(tid)\n    result("reviewed")', 1)
assert REVIEW_CONFLICT_AGENT.count("conflict(tid)") == 2


class ReviewReleaseConflict(PushRunner):
    """force_release() after a review that decided nothing, when `release` fails with CONFLICT and the remote takes
    every push: like hand_off(), the work goes to wip/a, the clone is reset to @{u} and the release is redone."""
    fake_agent, mode, budget = REVIEW_CONFLICT_AGENT, "", 1

    def before_start(self):
        pass  # the remote takes every push

    def test_conflict_in_force_release_saves_work_to_wip_and_releases(self):
        self.wait_for("released log", lambda: "released a for another agent" in self.log.read_text(), timeout=120)
        log = self.log.read_text()
        self.assertNotIn("release FAILED", log)
        self.assertNotIn("BLOCKED", log)
        a = self.state("a")
        self.assertEqual((a["status"], a["releases"], a["hold"]), ("todo", "1", ""))  # a counted release
        self.assertIn("review that decided nothing", a["text"])
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "log", "--format=%s", "wip/a").stdout
        self.assertIn("notes for a", wip)
        self.assertIn("work on a", wip)
        self.assertNotIn("work on a", sh(self.tmp, "git", "--git-dir=remote.git", "log", "--format=%s", "main").stdout)


class ReviewReleaseConflictWipRefused(ReviewReleaseConflict):
    """The same CONFLICT, but the remote refuses the fallback push to wip/a: blocked with git's push error (not
    the last line of the conflict help), the work stays in the clone."""

    def before_start(self):
        hook(self.tmp, WORKFLOW_HOOK)  # the other clone's notes.txt passes; the agent's ci.yml commits do not

    def test_conflict_in_force_release_saves_work_to_wip_and_releases(self):
        self.wait_for("release FAILED log", lambda: "release FAILED for a" in self.log.read_text(), timeout=120)
        log = self.log.read_text()
        self.assertIn("workflow scope", log.split("release FAILED for a")[1].splitlines()[0])
        self.assertNotIn("disagree on design", log)
        self.assertNotIn("released a for another agent", log)
        self.assertIn("work on a", sh(self.clone, "git", "log", "--format=%s", "HEAD").stdout)  # not reset away
        self.assert_blocked_without_new_sessions(4)
        self.assertNotIn("disagree on design", self.status()["error"])


# WORKFLOW_HOOK for main only: wip/<id> and board commits pass, `done` (the work on main) is refused
MAIN_WORKFLOW_HOOK = WORKFLOW_HOOK.replace('  [ "$new" = "$z" ] && continue\n',
                                           '  [ "$new" = "$z" ] && continue\n  [ "$ref" = refs/heads/main ] || continue\n', 1)
assert MAIN_WORKFLOW_HOOK != WORKFLOW_HOOK


class DoneRefusedOnlyOnMain(PushRunner):
    """`done` keeps failing to push, but release would work: after the streak the retry puts the task on hold
    (a human must fix it) instead of releasing it, so the runner can't claim it again and loop."""
    mode, budget, retry = "done", 10, 5

    def before_start(self):
        hook(self.tmp, MAIN_WORKFLOW_HOOK)

    def done_fails_of_a(self):
        return self.log.read_text().count("done could not push a ")

    def test_done_streak_puts_the_task_on_hold_not_back_on_the_board(self):
        self.wait_for("a on hold", lambda: self.state("a")["hold"] != "", timeout=180)
        self.wait_for("hold log", lambda: "put a on hold for a human" in self.log.read_text(), timeout=30)
        a = self.state("a")
        self.assertEqual(a["status"], "todo")
        self.assertNotEqual(a["owner"], "box/agent-1")  # (field() reads an empty owner: as the next line)
        self.assertIn("done failed to push 3 sessions in a row", a["hold"])
        log = self.log.read_text()
        self.assertIn("put a on hold for a human", log)
        self.assertNotIn("retrying to release a", log)
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "-r", "--name-only", "wip/a").stdout
        self.assertIn(".github/workflows/ci.yml", wip)  # the work is saved for whoever fixes it
        time.sleep(10)  # the runner goes on (with b), never back to a
        self.assertEqual(self.done_fails_of_a(), 3, self.log.read_text())
        self.assertNotEqual(self.state("a")["hold"], "")
        self.assertNotEqual(self.state("a")["owner"], "box/agent-1")


class DoneRefusedDirtyTree(DoneRefusedOnlyOnMain):
    """As DoneRefusedOnlyOnMain, but each session leaves an uncommitted file after its failed `done`: the hold
    retry commits it first, so it ends up on wip/a instead of being reset away."""
    fake_agent = FAKE_AGENT.replace('        tp("done", tid, "--force")\n',
                                    '        tp("done", tid, "--force")\n'
                                    '        open("scratch.txt", "a").write("not committed yet\\n")\n', 1)
    assert fake_agent != FAKE_AGENT

    def test_done_streak_puts_the_task_on_hold_not_back_on_the_board(self):
        super().test_done_streak_puts_the_task_on_hold_not_back_on_the_board()
        # one line per session: the next session commits the earlier ones, the last one only the hold retry does
        saved = sh(self.tmp, "git", "--git-dir=remote.git", "show", "wip/a:scratch.txt").stdout
        self.assertEqual(saved.count("not committed yet"), 3, saved)
        self.assertIn("wip: a (before hold)",
                      sh(self.tmp, "git", "--git-dir=remote.git", "log", "--format=%s", "wip/a").stdout)


class DoneRefusedMergeInProgress(DoneRefusedOnlyOnMain):
    """As DoneRefusedOnlyOnMain, but the last session leaves an unfinished merge after its failed `done`: the hold
    retry aborts it before it commits a dirty tree, so the half-done merge never ends up on wip/a."""
    fake_agent = FAKE_AGENT.replace('        tp("done", tid, "--force")\n', '''        tp("done", tid, "--force")
        n = int(open(".git/fake-dones").read()) + 1 if os.path.exists(".git/fake-dones") else 1
        open(".git/fake-dones", "w").write(str(n))
        if n == 3:  # the session that starts the block: leave a merge in progress behind
            git = lambda *a: subprocess.run(["git", *a], capture_output=True)
            branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                    text=True).stdout.strip()
            git("checkout", "-qb", "side")
            open("half-merged.txt", "w").write("from an unfinished merge\\n")
            git("add", "half-merged.txt")
            git("commit", "-qm", "side work")
            git("checkout", "-q", branch)
            git("merge", "--no-ff", "--no-commit", "side")
''', 1)
    assert fake_agent != FAKE_AGENT

    def test_done_streak_puts_the_task_on_hold_not_back_on_the_board(self):
        super().test_done_streak_puts_the_task_on_hold_not_back_on_the_board()
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "-r", "--name-only", "wip/a").stdout
        self.assertNotIn("half-merged.txt", wip)  # aborted, not committed as "wip: a (before hold)"
        self.assertNotIn("wip: a (before hold)",
                         sh(self.tmp, "git", "--git-dir=remote.git", "log", "--format=%s", "wip/a").stdout)
        self.assertFalse((self.clone / ".git" / "MERGE_HEAD").exists())


class DoneBlockedHoldRefused(PushRunner):
    """`done` keeps failing to push and the hold retry is refused too (wip/a is refused like main): the task stays
    blocked, every retry is a hold again (never a release, even after a failed hold re-blocked it), no session
    starts; once pushes work, the next retry puts the task on hold for a human."""
    mode, budget, retry = "done", 10, 5

    def test_refused_hold_retry_stays_blocked_then_holds_once_pushes_work(self):
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text(), timeout=120)
        self.wait_for("2 failed hold retries", lambda: self.log.read_text().count("hold FAILED for a") >= 2, timeout=60)
        log = self.log.read_text()
        self.assertIn("retrying to put a on hold", log)
        self.assertIn("workflow scope", log.split("hold FAILED for a")[1].splitlines()[0])  # git's push error
        self.assertNotIn("retrying to release a", log)
        self.assertNotIn("put a on hold for a human", log)
        self.assertEqual(self.sessions(), 3, log)
        a = self.state("a")
        self.assertEqual((a["status"], a["owner"], a["hold"]), ("in_progress", "box/agent-1", ""))
        s = self.status()
        self.assertEqual((s["state"], s["task"]), ("blocked", "a"))
        self.assertIn("workflow scope", s["error"])
        (self.tmp / "remote.git" / "hooks" / "pre-receive").unlink()  # a human fixed the token
        self.wait_for("a on hold", lambda: self.state("a")["hold"] != "", timeout=60)
        self.wait_for("hold log", lambda: "put a on hold for a human" in self.log.read_text(), timeout=30)
        a = self.state("a")
        self.assertIn("done failed to push 3 sessions in a row", a["hold"])
        self.assertNotEqual(a["owner"], "box/agent-1")  # (field() reads an empty owner: as the next line)
        log = self.log.read_text()
        self.assertIn("put a on hold for a human", log)
        self.assertNotIn("retrying to release a", log)
        wip = sh(self.tmp, "git", "--git-dir=remote.git", "ls-tree", "-r", "--name-only", "wip/a").stdout
        self.assertIn(".github/workflows/ci.yml", wip)  # the work is saved for whoever fixes it
        blocked = log.split("BLOCKED on a")[1].split("put a on hold for a human")[0]
        self.assertNotIn("session start", blocked, log)  # no session while it was blocked (after it: b, fine)


# Sessions scripted by FAKE_MODE, a comma list, one entry per session (the last one repeats): `release`,
# `hold`, `done` or `done:<word>` (the hook's error then names <word>, so the push error differs), `work`
# (commit, no tasks.py push: a session without a push failure).
SEQ_AGENT = r"""
import json, os, subprocess, sys
prompt = sys.argv[-1]
def tp(*a):
    p = subprocess.run([sys.executable, "tasks.py", *a], capture_output=True, text=True)
    print(p.stdout + p.stderr, flush=True)
    return p.stdout
def result(text):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text}), flush=True)
seq, count = os.environ.get("FAKE_MODE", "work").split(","), os.path.join("..", "fake-sessions")
n = int(open(count).read()) if os.path.exists(count) else 0
open(count, "w").write(str(n + 1))
mode = seq[min(n, len(seq) - 1)]
if "You already own task `" in prompt:
    tid = prompt.split("You already own task `")[1].split("`")[0]
else:
    out = tp("claim")
    tid = out.split("CLAIMED ")[1].split()[0] if "CLAIMED " in out else None
if tid:
    os.makedirs(".github/workflows", exist_ok=True)
    with open(".github/workflows/ci.yml", "a") as f:
        f.write("# more\n")
    subprocess.run(["git", "add", "-A"], capture_output=True)
    subprocess.run(["git", "commit", "-qm", f"work on {tid}"], capture_output=True)
    if mode == "release":
        tp("release", tid, "--note", "cannot finish")
    elif mode == "hold":
        tp("hold", tid, "cannot finish")
    elif mode.startswith("done"):
        open(os.path.join("..", "..", "hookmsg"), "w").write(mode.partition(":")[2] or "same")
        tp("done", tid, "--force")
result(f"{mode} on {tid}")
"""
# WORKFLOW_HOOK, but the error names the word in <tmp>/hookmsg (the hook runs in remote.git)
MSG_HOOK = WORKFLOW_HOOK.replace("refusing to allow", "rule $(cat ../hookmsg 2>/dev/null): refusing to allow")


class SeqRunner(PushRunner):
    fake_agent = SEQ_AGENT
    budget = 100  # no over-budget reviews in these tests

    def before_start(self):
        hook(self.tmp, MSG_HOOK)

    def done_counts(self):
        return [int(n) for n in re.findall(r"done could not push a \((\d)/3 sessions in a row\)", self.log.read_text())]


class HoldRefusedInSession(SeqRunner):
    """The agent's own `hold` is refused: blocked at once; once pushes work, the retry is that hold with the
    agent's reason, not a release (another agent must not pick up a task its owner said needs a human)."""
    mode, retry = "hold", 5

    def test_failed_hold_in_a_session_blocks_at_once(self):
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text())
        self.assert_blocked_without_new_sessions(1)
        self.assertEqual(self.state("a")["hold"], "")
        self.wait_for("a hold retry", lambda: "retrying to put a on hold" in self.log.read_text(), timeout=30)
        (self.tmp / "remote.git" / "hooks" / "pre-receive").unlink()  # a human fixed the token
        self.wait_for("a on hold", lambda: self.state("a")["hold"] != "", timeout=60)
        self.wait_for("hold log", lambda: "put a on hold for a human" in self.log.read_text(), timeout=30)
        a = self.state("a")
        self.assertEqual((a["status"], a["hold"]), ("todo", "cannot finish"))  # the agent's own reason
        self.assertNotEqual(a["owner"], "box/agent-1")
        log = self.log.read_text()
        self.assertIn("put a on hold for a human", log)
        self.assertNotIn("retrying to release a", log)
        self.assertNotIn("handed back by", a["text"])
        blocked = log.split("BLOCKED on a")[1].split("put a on hold for a human")[0]
        self.assertNotIn("session start", blocked, log)  # no session while it was blocked (after it: b, fine)


class DoneStreakDifferentErrors(SeqRunner):
    mode = "done:one,done:two,done:one,done:two,done:two,done:two"

    def test_a_different_push_error_restarts_the_streak(self):
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text(), timeout=180)
        self.assertEqual(self.done_counts(), [1, 1, 1, 1, 2, 3], self.log.read_text())
        self.assert_blocked_without_new_sessions(6)
        self.assertIn("rule two", self.status()["error"])


class DoneStreakBrokenBySession(SeqRunner):
    mode = "done,done,work,done,done,done"

    def test_a_session_without_push_failure_restarts_the_streak(self):
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text(), timeout=180)
        self.assertEqual(self.done_counts(), [1, 2, 1, 2, 3], self.log.read_text())
        self.assert_blocked_without_new_sessions(6)


class DoneStreakErrorNamesCommit(SeqRunner):
    """The remote's error names the refused commit (as GitHub's rule violations do), so its text differs in every
    session (each one commits new work): still the same error, so 3 sessions in a row block the task."""
    mode = "done"

    def before_start(self):
        hook(self.tmp, WORKFLOW_HOOK.replace("refusing to allow", "commit $new: refusing to allow"))

    def test_same_error_with_a_different_commit_hash_counts_as_the_same(self):
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text(), timeout=180)
        self.assertEqual(self.done_counts(), [1, 2, 3], self.log.read_text())
        lines = [l for l in self.log.read_text().splitlines() if "done could not push a " in l]
        shas = {m.group(1) for l in lines for m in [re.search(r"commit ([0-9a-f]{40}):", l)] if m}
        self.assertEqual(len(shas), 3, lines)  # the raw errors really differ
        self.assert_blocked_without_new_sessions(3)


class TakenAwayWhileBlocked(SeqRunner):
    mode = "release,work"
    how = ("hold", "a", "a human looks at the token", "--force")

    def test_task_taken_away_by_a_human_unblocks_the_runner(self):
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text())
        self.wait_for("blocked status", lambda: self.status()["state"] == "blocked")
        sh(self.w, "git", "pull", "-q")  # (`hold` decides on the clone's view of the task before it syncs)
        self.tp(*self.how, who="human")
        self.wait_for("unblocked log", lambda: "a is no longer ours" in self.log.read_text(), timeout=120)  # idles <= 60 s
        self.wait_for("b claimed", lambda: self.state("b")["owner"] == "box/agent-1", timeout=60)
        self.assertIn("parked local work", self.log.read_text())  # a's refused commits don't ride along with b
        self.assertNotIn("retrying to release a", self.log.read_text())
        self.wait_for("a session on b", lambda: self.status()["task"] == "b" and self.status()["state"] != "blocked")
        self.assertIsNone(self.status().get("error"))
        self.assertNotEqual(self.state("a")["owner"], "box/agent-1")


class DroppedWhileBlocked(TakenAwayWhileBlocked):
    how = ("drop", "a", "--note", "not wanted any more", "--force")


class TakenAwayWithNothingLeft(SeqRunner):
    """A human takes the blocked task away and nothing else is ready: the runner waits without a session, and
    its status no longer shows the old push error."""
    mode = "release"

    def before_start(self):
        super().before_start()
        self.tp("hold", "b", "not now", who="human")

    def test_status_error_is_cleared_when_the_blocked_task_is_taken_away(self):
        self.wait_for("BLOCKED log", lambda: "BLOCKED on a" in self.log.read_text())
        self.wait_for("blocked status", lambda: self.status()["state"] == "blocked")
        self.assertIn("workflow scope", self.status()["error"])
        sh(self.w, "git", "pull", "-q")
        self.tp("hold", "a", "a human looks at the token", "--force", who="human")
        self.wait_for("unblocked log", lambda: "a is no longer ours" in self.log.read_text(), timeout=120)
        self.wait_for("waiting status", lambda: self.status()["state"] == "waiting")
        s = self.status()
        self.assertIsNone(s["task"])
        self.assertIsNone(s.get("error"))
        self.assertEqual(self.sessions(), 1)


class OldPushFailureIgnored(SeqRunner):
    mode = "work"

    def before_start(self):
        super().before_start()
        rel = sh(self.clone, "git", "rev-parse", "--git-path", "tfp-push-failed").stdout.strip()
        self.failure = self.clone / rel
        self.failure.write_text(json.dumps(dict(task="a", cmd="release", error="old error", at=0)))

    def test_a_failure_from_before_the_session_does_not_block(self):
        self.wait_for("3 sessions", lambda: self.sessions() >= 3)
        self.assertNotIn("BLOCKED", self.log.read_text())
        self.assertFalse(self.failure.exists())  # consumed
        self.assertEqual(self.state("a")["owner"], "box/agent-1")


class OtherTaskPushFailureIgnored(OldPushFailureIgnored):
    def before_start(self):
        super().before_start()  # a failure of task b (not ours), recorded "now"
        self.failure.write_text(json.dumps(dict(task="b", cmd="release", error="b error", at=int(time.time()) + 600)))


if __name__ == "__main__":
    unittest.main()
