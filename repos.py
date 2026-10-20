"""repos.py - keep the original repos up to date from folders of this working repo (git subtree).
Stdlib only. Used as `python tasks.py repos ...`.

repos.txt maps folders to repos, one per line:   backend  git@gitlab.com:org/backend.git  main

  repos add <folder> <url> [--branch B]  import a repo with its history into <folder>/ (or register an
                                          existing folder, e.g. a new app whose repo is still empty)
  repos list                              the mapping and the state of the last sync
  repos pull [folder...]                  merge changes someone pushed directly to an original repo
  repos sync [--every 30m]                one sync round (or one every 30 min):
      1. merge outside changes from the originals (only if they have commits we didn't push)
      2. run the project check (.taskcheck) on the result; if it fails, push nothing this round
      3. push each folder whose content changed to its repo, with its own commit history
      4. tag the synced commit (sync-<time>) and record the state on branch sync-status,
         which `tasks.py watch` shows

sync works in the clone it runs in, but only if that clone has no local work: each round resets it
to origin. Use a dedicated clone (or CI) for `--every`.
"""
import json, re, subprocess, sys, time

CONF = "repos.txt"
STATE_REF = "sync-status"


# ---------- config / state ----------
def load(T):
    f = T.ROOT / CONF
    out = []
    for line in (f.read_text().splitlines() if f.exists() else []):
        w = line.split("#", 1)[0].split()
        if w:
            out.append(dict(prefix=w[0].strip("/"), url=w[1], branch=w[2] if len(w) > 2 else "main"))
    return out


def read_state(T):
    r = T.remote() or "origin"
    p = T.git("show", f"refs/remotes/{r}/{STATE_REF}:sync.json", check=False)
    try:
        return json.loads(p.stdout) if p.returncode == 0 else {}
    except ValueError:
        return {}


def write_state(T, state):
    """Force-push state as a one-commit branch (like the agents' status branches): no history on main."""
    data = json.dumps(state, indent=1)
    blob = git_in(T, data, "hash-object", "-w", "--stdin")
    tree = git_in(T, f"100644 blob {blob}\tsync.json\n", "mktree")
    commit = T.git("commit-tree", tree, "-m", "sync status").stdout.strip()
    if T.remote():
        T.git("push", "-q", "-f", T.remote(), f"{commit}:refs/heads/{STATE_REF}", check=False)
        T.git("fetch", "-q", T.remote(), STATE_REF, check=False)


# ---------- git helpers ----------
def git_in(T, data, *a):
    """git with data on stdin, as bytes: text-mode pipes would turn \n into \r\n on Windows."""
    return subprocess.run(["git", "-C", str(T.ROOT), *a], input=data.encode(), capture_output=True).stdout.decode().strip()


def need_subtree(T):
    if "not a git command" in T.git("subtree", check=False).stderr:
        sys.exit("git subtree is not installed. Fedora/RHEL: sudo dnf install git-subtree. "
                 "Debian/Ubuntu: install -m755 /usr/share/doc/git/contrib/subtree/git-subtree.sh "
                 "\"$(git --exec-path)/git-subtree\". macOS/Windows git include it.")


def need_clean(T):
    """This clone must hold no local work: then resetting it to origin is just a fast-forward."""
    if not T.upstream():
        sys.exit("current branch has no upstream (git push -u origin main)")
    T.no_unfinished_merge()
    if T.dirty():
        sys.exit("uncommitted changes: run this in a clean clone (a dedicated one for `sync --every`)")
    T.git("fetch", "-q", T.remote())
    if T.git("rev-list", "--count", "@{u}..HEAD").stdout.strip() != "0":
        sys.exit("unpushed commits: push them first, or use a dedicated clone")
    T.git("reset", "-q", "--hard", "@{u}")


def remote_tip(T, r):
    out = T.git("ls-remote", r["url"], f"refs/heads/{r['branch']}", check=False)
    if out.returncode:
        raise RuntimeError(f"cannot reach {r['url']}: {out.stderr.strip()[-200:]}")
    return out.stdout.split()[0] if out.stdout.strip() else None


def tree_of(T, prefix):
    p = T.git("rev-parse", f"HEAD:{prefix}", check=False)
    return p.stdout.strip() if p.returncode == 0 else None


def pull_in(T, r, msg_extra=""):
    """Merge the original repo's branch into <prefix>/. Returns None or an error string; on a conflict
    the merge is left in place for the caller to abort or resolve."""
    p = T.git("subtree", "pull", f"--prefix={r['prefix']}", r["url"], r["branch"],
              "-m", f"repos: merge {r['branch']} of {r['url']} into {r['prefix']}/{msg_extra}", check=False)
    if p.returncode == 0:
        return None
    if T.merge_state():
        return f"conflict in {' '.join(T.conflicted()) or '?'}"
    return (p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip() else "subtree pull failed"


# ---------- commands ----------
def cmd_add(T, a):
    need_subtree(T)
    if len(a.args) != 2:
        sys.exit("usage: tasks.py repos add <folder> <url> [--branch main]")
    prefix, url = a.args[0].strip("/"), a.args[1]
    if any(r["prefix"] == prefix for r in load(T)):
        sys.exit(f"{prefix} is already in {CONF}")
    need_clean(T)
    r = dict(prefix=prefix, url=url, branch=a.branch)
    if tree_of(T, prefix):
        print(f"{prefix}/ exists already: registering it; the first sync pushes it to {url}")
    else:
        if not remote_tip(T, r):
            sys.exit(f"{url} has no branch {a.branch}, and there is no {prefix}/ folder to push to it")
        p = T.git("subtree", "add", f"--prefix={prefix}", url, a.branch, "-m",
                  f"repos: import {a.branch} of {url} into {prefix}/", check=False)
        if p.returncode:
            sys.exit(p.stderr.strip())
    f = T.ROOT / CONF
    head = "" if f.exists() else "# folder  repo-url  branch   (tasks.py repos sync pushes each folder to its repo)\n"
    T.write(f, (f.read_text() if f.exists() else head) + f"{prefix}  {url}  {a.branch}\n")
    T.git("add", CONF)
    T.git("commit", "-qm", f"repos: map {prefix}/ to {url}")
    if T.git("push", "-q", check=False).returncode:
        sys.exit("committed locally but the push was rejected; run `git pull --rebase=merges && git push`")
    print(f"ADDED {prefix}/ <-> {url} ({a.branch})")


def cmd_list(T, a):
    state = read_state(T)
    rs = load(T)
    if not rs:
        return print(f"no repos configured ({CONF}); add one: tasks.py repos add <folder> <url>")
    now = time.time()
    for r in rs:
        s = state.get("repos", {}).get(r["prefix"], {})
        when = f"pushed {int((now - s['pushed_at']) // 60)} min ago" if s.get("pushed_at") else "never pushed"
        print(f"{r['prefix'] + '/':14} {r['url']} ({r['branch']})  {when}  {s.get('result', '')} {s.get('message', '')}")
    if state:
        print(f"\nlast sync run: {int((now - state.get('run_at', now)) // 60)} min ago: {state.get('result')} "
              f"{state.get('message', '')}")


def cmd_pull(T, a):
    """Manual pull for when sync reported a conflict: leaves the conflict here to resolve."""
    need_subtree(T)
    need_clean(T)
    rs = [r for r in load(T) if not a.args or r["prefix"] in a.args]
    for r in rs:
        err = pull_in(T, r)
        if err and T.merge_state():
            sys.exit(f"{r['prefix']}: {err}\nResolve the files, `git add` them, `git commit`, then `git push` "
                     "(if the push is rejected: `git pull --rebase=merges`, push again).")
        print(f"{r['prefix']}: {err or 'up to date / merged'}")
    if T.git("rev-list", "--count", "@{u}..HEAD").stdout.strip() != "0":
        if T.git("push", "-q", check=False).returncode:
            sys.exit("merged locally, but the push was rejected: `git pull --rebase=merges && git push`")
        print("pushed the merge to main")


def sync_round(T, a):
    """One sync. Returns the new state dict (also written to the sync-status branch).
    Main is updated first (outside changes merged in, plus a --rejoin record of each split we are
    about to push, so the next merge from that repo uses the right base); the originals only after
    main's push succeeded. If someone pushes to main meanwhile, the whole round is redone."""
    need_subtree(T)
    need_clean(T)  # first: config and state must come from the latest main
    rs = load(T)
    if not rs:
        sys.exit(f"no repos configured ({CONF})")
    old = read_state(T).get("repos", {})
    now = int(time.time())
    for attempt in range(3):
        if attempt:  # lost the race for main: drop our local merges, redo on the new main
            T.git("fetch", "-q", T.remote())
            T.git("reset", "-q", "--hard", "@{u}")
        need_clean(T)
        st = {p: dict(v, url=r["url"], branch=r["branch"]) for r in rs for p, v in [(r["prefix"], old.get(r["prefix"], {}))]}
        notes, problems, tips = [], {}, {}

        # 1. outside changes: pull only if the repo has commits we did not push (no empty merges)
        for r in rs:
            p = r["prefix"]
            try:
                tips[p] = tip = remote_tip(T, r)
            except RuntimeError as ex:
                problems[p] = ("error", str(ex))
                continue
            if not tip or tip == old.get(p, {}).get("pushed") or \
                    T.git("merge-base", "--is-ancestor", tip, "HEAD", check=False).returncode == 0:
                continue
            err = pull_in(T, r)
            if err:
                if T.merge_state():
                    T.git("merge", "--abort", check=False)
                problems[p] = ("conflict" if err.startswith("conflict") else "error",
                               err + f" (resolve with: python tasks.py repos pull {p})")
            else:
                notes.append(f"merged outside changes into {p}/")

        # 2. which folders changed since we last pushed them?
        todo = []
        for r in rs:
            p = r["prefix"]
            if p in problems:
                continue
            if not tree_of(T, p):
                problems[p] = ("error", f"no {p}/ folder on main")
            elif tree_of(T, p) == old.get(p, {}).get("tree") and tips.get(p) == old.get(p, {}).get("pushed"):
                st[p].update(result="up to date", message="", checked_at=now)
            else:
                todo.append(r)
        for p, (res, msg) in problems.items():
            st[p].update(result=res, message=msg, checked_at=now)
        head = T.git("rev-parse", "HEAD").stdout.strip()
        result, message = None, None

        # 3. check the exact content we are about to publish
        cmd = None if a.skip_check else T.check_cmd()
        if todo and cmd:
            print(f"checking {head[:9]}: {cmd}", flush=True)
            p = T.run_check(cmd, T.check_limits()[0])
            if p.returncode:
                tail = " | ".join((p.stdout + p.stderr).strip().splitlines()[-3:])[-300:]
                for r in todo:
                    st[r["prefix"]].update(result="check failed", message=tail, checked_at=now)
                todo, result, message = [], "check failed", f"`{cmd}` failed on {head[:9]}: {tail}"
        elif todo and not a.skip_check:
            notes.append("no check configured (.taskcheck): pushed unchecked")

        # 4. split each changed folder; --rejoin records the split on main
        splits = {}
        for r in todo:
            p = r["prefix"]
            t0 = time.time()
            sp = T.git("subtree", "split", f"--prefix={p}", "--rejoin", "-q", check=False)
            sha = sp.stdout.strip().splitlines()[-1] if sp.returncode == 0 and sp.stdout.strip() else None
            if sha:
                splits[p] = (sha, round(time.time() - t0, 1))
            else:
                st[p].update(result="error", message="subtree split failed: " + sp.stderr.strip()[-200:], checked_at=now)
        if T.git("rev-list", "--count", "@{u}..HEAD").stdout.strip() == "0" or \
                T.git("push", "-q", check=False).returncode == 0:
            break
    else:
        raise RuntimeError("main kept changing during the sync; try again")

    # 5. publish: fast-forward only, so outside commits are never overwritten
    pushed = []
    for r in todo:
        p = r["prefix"]
        if p not in splits:
            continue
        sha, secs = splits[p]
        pp = T.git("push", "-q", r["url"], f"{sha}:refs/heads/{r['branch']}", check=False)
        if pp.returncode:
            st[p].update(result="error", checked_at=now, message="push rejected (someone pushed meanwhile? the next "
                         "round merges it): " + (pp.stderr.strip().splitlines() or [""])[-1][-200:])
            continue
        st[p].update(result="pushed", message="", pushed=sha, tree=tree_of(T, p), commit=head,
                     pushed_at=now, checked_at=now, split_secs=secs)
        pushed.append(p)

    if pushed:
        tag = time.strftime("sync-%Y%m%d-%H%M%S", time.gmtime(now))
        T.git("tag", "-a", tag, "-m", f"synced {', '.join(pushed)} to their repos", head)
        T.git("push", "-q", T.remote(), tag, check=False)
    if not result:
        bad = [p for p in st if st[p].get("result") in ("conflict", "error")]
        result = "problem" if bad else "pushed" if pushed else "up to date"
        message = "; ".join(notes + [f"{p}: {st[p]['message']}" for p in bad])
    state = dict(run_at=now, result=result, message=message, commit=head, repos=st)
    write_state(T, state)
    return state


def cmd_sync(T, a):
    every = parse_every(a.every) if a.every else None
    while True:
        try:
            s = sync_round(T, a)
            summary = ", ".join(f"{p}: {v['result']}" for p, v in s["repos"].items())
            print(f"[{time.strftime('%H:%M:%S')}] {s['result']}: {summary}" + (f"\n  {s['message']}" if s["message"] else ""),
                  flush=True)
            failed = s["result"] in ("check failed", "problem")
        except (SystemExit, RuntimeError) as ex:  # keep looping: the next round may work
            print(f"[{time.strftime('%H:%M:%S')}] sync failed: {ex}", flush=True)
            if not every:
                raise
            failed = True
        if not every:
            sys.exit(1 if failed else 0)
        time.sleep(every)


def parse_every(s):
    m = re.fullmatch(r"(\d+)\s*([smh]?)", s.strip())
    if not m:
        sys.exit(f"bad interval {s!r}: use e.g. 30m, 1h, 900s")
    return int(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def main(T, a):
    {"add": cmd_add, "list": cmd_list, "pull": cmd_pull, "sync": cmd_sync, "push": cmd_sync}[a.action](T, a)
