# Review evidence and the completion gate

This file switches the gate on: copy it to `docs/QUALITY.md` in the working repo and commit it
(SETUP.md A.8). While it is on main, every task needs committed review evidence before
`python tasks.py done <id>` accepts it. Without it, `done` checks no review evidence.
`done` enforces this for the task being completed, on its work merged with the latest main.
Neither `--force` nor `--skip-check` bypasses it. Tests: `python3 tests/test_quality_gate.py`.

## Artifacts: `reviews/<task-id>/`

| File | Written by | Required |
|---|---|---|
| `tests.md` | `test-writer` sub-agent | always, also when no tests had to be added |
| `review.md` | `code-reviewer` sub-agent | always, also when there are no findings |
| `fixes.md` | `fixer` sub-agent | when validated findings required fixes |
| `quality.json` | the parent worker | always |

Each helper writes its own report. The parent worker checks the reports and commits them with
the task's code. **The parent never writes or edits a helper's report to satisfy the gate.** If
a helper can't run, the parent resolves that (retry, or a narrower scope) or holds the task.

Every report names the task (a `Task: <task-id> ...` line), the Git scope it examined, the
commands it ran with their results, and its findings with their disposition. A report must
contain the closing section of its template: `## Coverage` in `tests.md`,
`## Review conclusion` in `review.md`, `## Final status` in `fixes.md`. Unfilled template
placeholders (`<YYYY-MM-DD>`, `<command>`, ...), `TODO` and `TBD` are rejected.

`tests.md` always records the task-scoped coverage and any significant gaps. If coverage could
not be measured, it records the concrete reason instead. Raw coverage output stays Git-ignored
(`build/`, `coverage/`). Credentials, personal data and conversation transcripts never go into
these files.

## `quality.json` (schema 1)

```json
{
  "schema": 1,
  "task": "gtd-inbox-api",
  "reviewed_revision": "4f9c2e0d8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d",
  "helpers": {
    "test-writer":   {"status": "completed", "report": "reviews/gtd-inbox-api/tests.md"},
    "code-reviewer": {"status": "completed", "report": "reviews/gtd-inbox-api/review.md"},
    "fixer":         {"status": "completed", "report": "reviews/gtd-inbox-api/fixes.md"}
  },
  "fixes_required": true,
  "findings": [
    {"id": "CR-1", "source": "code-reviewer", "severity": "P1", "disposition": "fixed",
     "note": "missing 404 for unknown task id"},
    {"id": "TW-1", "source": "test-writer", "severity": "P3", "disposition": "accepted",
     "note": "log message wording; not worth a change"}
  ],
  "validation": [
    {"command": "cd server && ./gradlew test", "result": "pass"},
    {"command": "sh -c \"$(head -1 .taskcheck)\"", "result": "pass"}
  ]
}
```

| Field | Rule |
|---|---|
| `schema` | `1` |
| `task` | the id of the task being completed |
| `reviewed_revision` | full 40-character hash of the commit holding the code the helpers last reviewed and tested (after the fixes) |
| `helpers.test-writer`, `helpers.code-reviewer` | `status: "completed"`, `report` = the path above |
| `helpers.fixer` | if `fixes_required` is true: `status: "completed"` and `report`. Otherwise `{"status": "not_required", "reason": "<why>"}` |
| `fixes_required` | `true` if any finding was fixed |
| `findings[]` | every finding from all reports: `id`, `source`, `severity` (`P0`–`P3`, or `High/Medium/Low` for test-writer bugs), `disposition`, `note` |
| `disposition` | `fixed`, `invalid` (shown not to be a defect), `out_of_scope` (belongs to another task, which must exist or be created), `deferred` (non-blocking, follow-up task named in `note`), `accepted` (non-blocking, deliberately left). `open` means unresolved and is rejected. P0/P1 findings must be `fixed` or `invalid`. Every disposition except `fixed` needs a `note` |
| `validation[]` | the final commands run on the final code, each with `result: "pass"`; at least one |

## Freshness: what may change after the review

`done` compares the task's own code change at `reviewed_revision` with the change at the final
merged HEAD. The comparison uses `git patch-id` over the diff from the merge-base with main,
`-U0`, with `tasks/` and `reviews/` excluded.
- Rebasing on, or merging, main is fine, even while other agents commit task state or code
  elsewhere.
- Commits that touch only `reviews/` or `tasks/` (amended reports, `verify`, `assume`) are fine.
- Any other change to the task's code after the review (a late fix, conflict resolution that
  changes lines) makes the evidence stale. Run the test-writer and code-reviewer again, then
  update the reports and `reviewed_revision`.

## Exemptions

Tasks require evidence by default. A task without code to test or review (pure documentation, a
human decision, a manual deployment) can be exempted **while the board is planned**:
`python tasks.py new <id> "<title>" --quality-exempt "<reason>"`. This writes
`quality: exempt` and `quality_reason:` into the task file. An exemption counts only if it was
already in the task file when the task was claimed. A worker that adds one to its own task gets
refused. `python tasks.py check` flags an exemption without a reason.

## Limits

The gate enforces that **recorded** evidence is present, complete, consistent and current. It can't
prove that a report is genuine or that the review was good. That is why the dry run (SETUP.md E)
inspects the actual helper invocations in the agent session logs, and why humans spot-check reports.
