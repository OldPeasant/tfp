#!/bin/bash
# Scenario tests for tasks.py against a local bare repo with three clones (A, B, C) acting as agents:
# status changes (claim, assume, extend, freeze, hold, split) must never push unfinished local code
# commits to main; only `done` pushes code. Run: bash tests/test_publish.sh  (needs git and python3)
set -u
unset TASKS_ROOT TASK_OWNER TASK_LEASE TASK_AVOID TASK_CHECK  # an agent runner's settings must not leak in
SRC="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
cd "$W"
FAIL=0
ok()   { echo "  ok   $1"; }
bad()  { echo "  FAIL $1"; FAIL=1; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }
on_main() { git -C remote.git cat-file -e "main:$1" 2>/dev/null; }          # file exists on remote main
tp() { local who=$1; shift; (cd "$W/$who" && TASK_OWNER=$who python3 tasks.py "$@"); }

git init -q --bare -b main remote.git
git clone -q remote.git seed 2>/dev/null && cd seed
git config user.email t@t; git config user.name seed
cp "$SRC/tasks.py" "$SRC/board.py" "$SRC/repos.py" .
echo "true" > .taskcheck
for t in a b c d e; do TASKS_ROOT=. python3 tasks.py new $t "Task $t" --quality-exempt "fixture: publish scenarios" >/dev/null; done
git add -A && git commit -qm init && git push -q -u origin main 2>/dev/null
cd "$W"
for c in A B C; do git clone -q remote.git $c; git -C $c config user.email t@t; git -C $c config user.name $c; done

echo "1. assume with an unpushed code commit"
tp A claim a >/dev/null
(cd A && echo wip > code-a.txt && git add code-a.txt && git commit -qm "wip a")
tp A assume a "an assumption" >/dev/null
check "assumption is on remote main" "git -C remote.git show main:tasks/a.md | grep -q 'an assumption'"
check "code commit is NOT on remote main" "! on_main code-a.txt"
check "code commit still local, on top" "[ \"\$(git -C A log -1 --format=%s)\" = 'wip a' ] && [ -f A/code-a.txt ]"
check "A is exactly 1 ahead, 0 behind" "[ \"\$(git -C A rev-list --left-right --count @{u}...HEAD)\" = \"$(printf '0\t1')\" ]"
check "A working tree clean" "[ -z \"\$(git -C A status --porcelain)\" ]"

echo "2. extend and claim of another task with the code commit still local"
tp A extend a --note "more" >/dev/null
tp A claim b >/dev/null
check "b claimed on remote" "git -C remote.git show main:tasks/b.md | grep -q 'owner: A'"
check "code still not on remote" "! on_main code-a.txt"

echo "3. uncommitted and staged changes survive a status change"
(cd A && echo staged > staged.txt && git add staged.txt && echo dirty >> code-a.txt)
tp A assume b "second" >/dev/null
check "staged file still staged" "git -C A diff --cached --name-only | grep -q staged.txt"
check "dirty change kept" "grep -q dirty A/code-a.txt"
check "staged/dirty not on remote" "! on_main staged.txt"
(cd A && git reset -q staged.txt && rm staged.txt && git checkout -q code-a.txt)

echo "4. someone else pushes in between: A catches up and still publishes only task files"
tp B claim c >/dev/null
tp A assume a "after B" >/dev/null
check "B's claim and A's assumption both on remote" "git -C remote.git show main:tasks/c.md | grep -q 'owner: B' && git -C remote.git show main:tasks/a.md | grep -q 'after B'"
check "code still not on remote" "! on_main code-a.txt"

echo "5. concurrent claims of one task: exactly one wins"
git -C B pull -q --rebase; git -C C pull -q --rebase
for i in 1 2 3; do
  (cd C && TASK_OWNER=C python3 tasks.py claim d > ../claimC.$i 2>&1) &
  (cd B && TASK_OWNER=B python3 tasks.py claim d > ../claimB.$i 2>&1) &
  wait
  w=$(grep -l "^CLAIMED d" claimC.$i claimB.$i | wc -l | tr -d ' ')
  check "round $i: one winner (got $w)" "[ $w = 1 ]"
  winner=$(grep -l "^CLAIMED d" claimC.$i claimB.$i | head -1 | sed 's/claim\(.\).*/\1/')
  tp $winner release d --note "again" >/dev/null 2>&1
  git -C C pull -q --rebase 2>/dev/null; git -C B pull -q --rebase 2>/dev/null
done
check "code still not on remote" "! on_main code-a.txt"

echo "6. freeze / unfreeze with the code commit local"
tp A freeze "replanning" >/dev/null
check "FROZEN on remote" "on_main tasks/FROZEN"
check "FROZEN present locally" "[ -f A/tasks/FROZEN ]"
tp A unfreeze >/dev/null
check "FROZEN gone on remote" "! on_main tasks/FROZEN"
check "FROZEN gone locally" "[ ! -f A/tasks/FROZEN ]"
check "code still not on remote" "! on_main code-a.txt"

check "A still owns b and has its code commit" "git -C remote.git show main:tasks/b.md | grep -q 'owner: A' && [ -f A/code-a.txt ]"
tp A hold b "parked for the test" >/dev/null 2>&1 || true
check "hold b (A's code is for a) moved it to wip/b, not main" "! on_main code-a.txt"
(cd A && git fetch -q && git merge -q --no-edit origin/wip/b)  # take the code back for task a

echo "7. hold of an owned task moves the code to wip/, not main"
tp A hold a "needs a human" >/dev/null
check "code not on main" "! on_main code-a.txt"
check "code on wip/a" "git -C remote.git cat-file -e wip/a:code-a.txt"
check "A back at origin/main" "[ \"\$(git -C A rev-parse HEAD)\" = \"\$(git -C A rev-parse @{u})\" ]"

echo "8. done pushes the code (after the check)"
tp A unhold a >/dev/null
tp A claim a | grep -q "wip/a" && (cd A && git fetch -q && git merge -q origin/wip/a 2>/dev/null)
tp A done a > done.out 2>&1
check "done succeeded" "grep -q '^DONE a' done.out"
check "code on main after done" "on_main code-a.txt"
check "a is done on remote" "git -C remote.git show main:tasks/a.md | grep -q 'status: done'"

echo "9. split creates new task files with only task commits"
tp A claim e >/dev/null
(cd A && echo e > code-e.txt && git add code-e.txt && git commit -qm "wip e")
printf '## e1: Part one\n- [ ] x\n## e2: Part two\ndeps: e1\n- [ ] y\n' > split.md
tp A split e --from "$W/split.md" --note "too big" >/dev/null
check "subtasks on remote" "on_main tasks/e1.md && on_main tasks/e2.md"
check "code-e on wip/e, not main" "! on_main code-e.txt && git -C remote.git cat-file -e wip/e:code-e.txt"
check "local subtask files present" "[ -f A/tasks/e1.md ] && [ -f A/tasks/e2.md ]"
check "board consistent" "(cd A && python3 tasks.py check >/dev/null)"

echo "10. B sees a consistent board"
git -C B pull -q --rebase
check "B check ok" "(cd B && python3 tasks.py check >/dev/null)"
check "B clean" "[ -z \"\$(git -C B status --porcelain)\" ]"

echo; [ $FAIL = 0 ] && echo "ALL PASSED" || echo "SOME FAILED"
exit $FAIL
