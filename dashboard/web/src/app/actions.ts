import { openCriteria, Task } from './store';

/** Every action the dashboard can run: what it is called, what it does (in plain words, shown before you
 * confirm), what it asks for, and the CLI command it runs (shown live, so the page teaches the CLI). */
export interface Field {
  key: string;
  label: string;
  type: 'text' | 'textarea' | 'check' | 'number';
  required?: boolean;
  help?: string;
  placeholder?: string;
  min?: number;
  max?: number;
  advanced?: boolean;
}

export type Tone = 'go' | 'normal' | 'caution' | 'danger';

export interface ActionDef {
  action: string;
  label: string;
  explain: string;
  fields?: Field[];
  tone?: Tone;
  /** Params always sent (e.g. which repos sub-command). */
  fixed?: Record<string, any>;
  cli: (p: Record<string, any>) => string;
}

const q = (s: unknown) => {
  const v = String(s ?? '');
  return /^[A-Za-z0-9._/=:@%+,-]+$/.test(v) ? v : `"${v.replace(/(["\\$`])/g, '\\$1')}"`;
};
const opt = (p: Record<string, any>, k: string, flag: string) => (p[k] ? ` ${flag} ${q(p[k])}` : '');
const sw = (p: Record<string, any>, k: string, flag: string) => (p[k] ? ` ${flag}` : '');
const T = 'python tasks.py';

const force: Field = {
  key: 'force',
  label: 'Do it although it is not my task',
  type: 'check',
  help: 'Needed when someone else owns it. Their agent notices within ~5 minutes and stops; work so far stays on wip/<id>.',
};

export const ACTIONS: Record<string, ActionDef> = {
  claim: {
    action: 'claim',
    label: 'Claim',
    tone: 'go',
    explain:
      'Marks the task as yours (in progress) and pushes that to main. If someone else claimed it a moment earlier, git refuses and you are told who won. Your claim never expires, so release it if you stop working on it.',
    fields: [
      { key: 'who', label: 'Claim for someone else', type: 'text', placeholder: 'leave empty: you', advanced: true },
      { key: 'force', label: 'Claim even if it is on hold, its dependencies are open, or the board is frozen', type: 'check', advanced: true },
    ],
    cli: (p) => `${T} claim${p['id'] ? ' ' + p['id'] : ''}${opt(p, 'who', '--who')}${sw(p, 'force', '--force')}`,
  },
  done: {
    action: 'done',
    label: 'Mark done',
    tone: 'go',
    explain:
      'Refuses while an acceptance criterion is unverified. Then merges the latest main into this clone, runs the project check on the result, and only if it passes marks the task done and pushes your commits to main. Commit your work first. If the check fails or takes too long, nothing is pushed and the output tells you why.',
    fields: [
      { key: 'skip_check', label: 'Skip the project check', type: 'check', help: 'Only if there is no meaningful check. Broken code would reach main.', advanced: true },
      { ...force, label: 'Do it although it is not my task or criteria are unverified', advanced: true },
    ],
    cli: (p) => `${T} done ${p['id']}${sw(p, 'skip_check', '--skip-check')}${sw(p, 'force', '--force')}`,
  },
  release: {
    action: 'release',
    label: 'Release',
    tone: 'caution',
    explain:
      'Gives the task back to the board (todo, no owner) so someone else can take it. Your unpushed commits go to wip/<id>, never to main, and the next owner starts from there. The third release of a task puts it on hold for a human.',
    fields: [
      { key: 'note', label: 'What is done, what blocks', type: 'textarea', placeholder: 'Added the model and tests; the migration still fails on…' },
      { key: 'handoff', label: 'Not the task’s fault (e.g. I ran out of time): don’t count it towards putting it on hold', type: 'check' },
      force,
    ],
    cli: (p) => `${T} release ${p['id']}${opt(p, 'note', '--note')}${sw(p, 'handoff', '--handoff')}${sw(p, 'force', '--force')}`,
  },
  redo: {
    action: 'redo',
    label: 'Start over from main',
    tone: 'caution',
    explain:
      'For merge conflicts you can’t resolve with confidence. Saves your attempt as wip/<id>-attemptN, resets this clone to the latest main, and you keep the task. Re-implement it, using the old attempt as a reference. After 3 failed attempts the task goes on hold.',
    cli: (p) => `${T} redo ${p['id']}`,
  },
  extend: {
    action: 'extend',
    label: 'Extend',
    explain:
      'For agents over their session budget: one more session to finish a nearly done task (once per owner).',
    fields: [{ key: 'note', label: 'What is left', type: 'textarea', required: true }],
    cli: (p) => `${T} extend ${p['id']}${opt(p, 'note', '--note')}`,
  },
  hold: {
    action: 'hold',
    label: 'Put on hold',
    tone: 'caution',
    explain:
      'Stops agents from working on the task until a human takes it off hold. If someone is working on it, their agent stops within ~5 minutes and the work so far stays on wip/<id>. The reason appears under “Needs a human”.',
    fields: [
      { key: 'reason', label: 'What must a human decide or do?', type: 'textarea', required: true },
      force,
    ],
    cli: (p) => `${T} hold ${p['id']} ${q(p['reason'] || '<reason>')}${sw(p, 'force', '--force')}`,
  },
  unhold: {
    action: 'unhold',
    label: 'Take off hold',
    tone: 'go',
    explain:
      'Agents may pick the task up again (within a minute), with a fresh budget. Fix the cause first: clarify the spec, edit the task, add a missing task. The next owner continues from wip/<id>.',
    fields: [{ key: 'note', label: 'What changed', type: 'textarea', placeholder: 'Clarified in SPEC.md §3' }],
    cli: (p) => `${T} unhold ${p['id']}${opt(p, 'note', '--note')}`,
  },
  drop: {
    action: 'drop',
    label: 'Drop',
    tone: 'danger',
    explain:
      'Takes the task off the board: it is no longer wanted. A running agent stops within ~5 minutes; its work stays on wip/<id>. Tasks that depend on it then wait for a human (edit their dependencies). Reversible with “Bring back”.',
    fields: [{ key: 'note', label: 'Why is it no longer wanted?', type: 'textarea', required: true }, force],
    cli: (p) => `${T} drop ${p['id']}${opt(p, 'note', '--note')}${sw(p, 'force', '--force')}`,
  },
  undrop: {
    action: 'undrop',
    label: 'Bring back',
    tone: 'go',
    explain: 'Puts a dropped task back on the board as todo. Earlier work, if any, is on wip/<id>.',
    fields: [{ key: 'note', label: 'Why', type: 'textarea' }],
    cli: (p) => `${T} undrop ${p['id']}${opt(p, 'note', '--note')}`,
  },
  assume: {
    action: 'assume',
    label: 'Record an assumption',
    explain:
      'Writes a decision taken without asking anyone into the task’s notes, so humans can review it later under “Assumptions”.',
    fields: [{ key: 'text', label: 'What was assumed, and why', type: 'textarea', required: true }],
    cli: (p) => `${T} assume ${p['id']} ${q(p['text'] || '<text>')}`,
  },
  verify: {
    action: 'verify',
    label: 'Verify criterion',
    tone: 'go',
    explain:
      'Ticks this acceptance criterion and writes your evidence under it in the task file. Only what actually ran counts: the command, the setup it ran against, and its result. Tests that were found but not run, or “should work”, are no evidence.',
    fields: [
      { key: 'text', label: 'What you ran, and its result (or why it is skipped)', type: 'textarea', required: true, placeholder: 'npx playwright test login against docker compose up -> 8 passed' },
      { key: 'skip', label: 'Can’t be met or verified here', type: 'check', help: 'Marks it skipped with your reason instead of verified. It stays visible for a human to follow up.' },
      { ...force, advanced: true },
    ],
    cli: (p) => `${T} verify ${p['id']} ${p['n']} ${q(p['text'] || '<evidence>')}${sw(p, 'skip', '--skip')}${sw(p, 'force', '--force')}`,
  },
  confirm: {
    action: 'confirm',
    label: 'Confirm assumption',
    tone: 'go',
    explain:
      'You agree with what the agent assumed. The line stays in the task’s notes, marked as confirmed by you, and leaves the list of assumptions to review.',
    cli: (p) => `${T} confirm ${p['id']} ${p['n']}`,
  },
  reject: {
    action: 'reject',
    label: 'Reject assumption',
    tone: 'caution',
    explain:
      'The agent assumed wrong. The line is marked as rejected, with your reason, and leaves the list. This changes no code: to fix what was built, add a task (or edit the task if it hasn’t started).',
    fields: [{ key: 'note', label: 'What is wrong, and what should happen instead?', type: 'textarea', required: true }],
    cli: (p) => `${T} reject ${p['id']} ${p['n']}${opt(p, 'note', '--note')}`,
  },
  freeze: {
    action: 'freeze',
    label: 'Freeze the board',
    tone: 'caution',
    explain:
      'No new claims anywhere until the board is unfrozen; agents wait. Use it while you change the plan. Tasks already running carry on (put them on hold to stop them).',
    fields: [{ key: 'reason', label: 'Reason, shown to everyone', type: 'text', required: true, placeholder: 'New plan for payments, back at 10:00' }],
    cli: (p) => `${T} freeze ${q(p['reason'] || '<reason>')}`,
  },
  unfreeze: {
    action: 'unfreeze',
    label: 'Unfreeze',
    tone: 'go',
    explain: 'Agents claim tasks again within a minute. Push your new plan first.',
    cli: () => `${T} unfreeze`,
  },
  check: {
    action: 'check',
    label: 'Check the board',
    explain: 'Looks for unknown dependencies, cycles, tasks depending on dropped ones, and broken fields.',
    cli: () => `${T} check`,
  },
  act_as: {
    action: 'act_as',
    label: 'Switch',
    explain:
      'Claims, releases and the like are recorded under this name (TASK_OWNER). Leave it empty to use your git user name. Only this dashboard is affected.',
    fields: [{ key: 'who', label: 'Act as', type: 'text', placeholder: 'your git user name' }],
    cli: (p) => (p['who'] ? `export TASK_OWNER=${q(p['who'])}` : 'unset TASK_OWNER'),
  },
  pull: {
    action: 'pull',
    label: 'Pull',
    explain: 'Brings this clone up to date with main (git pull --rebase). Your own commits stay on top.',
    cli: () => 'git pull --rebase --autostash',
  },
  agents_start: {
    action: 'agents_start',
    label: 'Start agents',
    tone: 'go',
    explain:
      'Starts AI agents on this laptop, in the background, each in its own clone next to this repo. They pick tasks off the board until everything is done, and use your Claude subscription. Closing this page doesn’t stop them; the laptop must stay awake.',
    fields: [
      { key: 'n', label: 'How many', type: 'number', min: 1, max: 32, required: true, help: 'They share your subscription’s usage limit. 2 is a good start.' },
      { key: 'host', label: 'Your name (tells laptops apart)', type: 'text', placeholder: 'default: this computer’s name' },
      { key: 'first', label: 'Number of the first agent', type: 'number', min: 1, max: 999, help: 'To add agents next to running ones, start after the highest number.', advanced: true },
      { key: 'task_budget', label: 'Sessions per task before a review', type: 'number', min: 1, max: 20, placeholder: '2', advanced: true },
      { key: 'lease', label: 'Lease (seconds a dead agent’s task stays blocked)', type: 'number', min: 120, max: 86400, placeholder: '1200', advanced: true },
      { key: 'timeout', label: 'Session timeout (seconds)', type: 'number', min: 60, max: 86400, placeholder: '1800', advanced: true },
      { key: 'limit_wait', label: 'Wait after a usage limit (seconds)', type: 'number', min: 60, max: 86400, placeholder: '900', advanced: true },
      { key: 'cmd', label: 'Agent command', type: 'text', placeholder: 'default: claude -p … (Claude Code)', advanced: true },
      { key: 'yolo', label: 'Skip all permission prompts (--yolo): agents may run ANY command on this machine. Only in a VM or container.', type: 'check', advanced: true },
    ],
    cli: (p) =>
      `python agents.py start ${p['n'] || 1}${p['first'] && +p['first'] !== 1 ? ' --first ' + p['first'] : ''}${opt(p, 'host', '--host')}` +
      `${opt(p, 'task_budget', '--task-budget')}${opt(p, 'lease', '--lease')}${opt(p, 'timeout', '--timeout')}` +
      `${opt(p, 'limit_wait', '--limit-wait')}${opt(p, 'cmd', '--cmd')}${sw(p, 'yolo', '--yolo')}`,
  },
  agents_finish: {
    action: 'agents_stop',
    fixed: { finish: true },
    label: 'Finish, then stop',
    explain:
      'Leaving soon? Your agents claim nothing new, finish the task they hold, then stop. When all show “stopped”, you can shut down without blocking anyone.',
    cli: () => 'python agents.py stop --finish',
  },
  agents_stop: {
    action: 'agents_stop',
    label: 'Stop all now',
    tone: 'danger',
    explain:
      'Ends every agent session on this laptop right away. Their tasks stay claimed: starting again resumes them; otherwise after the lease (20 min) another agent takes them over.',
    cli: () => 'python agents.py stop',
  },
  agent_stop: {
    action: 'agent_stop',
    label: 'Stop',
    tone: 'caution',
    explain:
      'Ends this agent’s session within a few seconds. Its task stays claimed: start it again to resume, or another agent takes it over when the lease runs out.',
    cli: (p) => `python agents.py stop   # this agent only: touch ../<repo>-agents/logs/${p['name']}.stop`,
  },
  agent_finish: {
    action: 'agent_stop',
    fixed: { finish: true },
    label: 'Finish, then stop',
    explain: 'This agent claims nothing new, finishes the task it holds, then stops.',
    cli: (p) => `python agents.py stop --finish   # this agent only: touch …/logs/${p['name']}.finish`,
  },
  repos_sync: {
    action: 'repos',
    fixed: { what: 'sync' },
    label: 'Sync now',
    explain:
      'Publishes each folder to its original repo: merges outside changes, runs the project check, pushes what changed. Nothing is published if the check fails.',
    fields: [{ key: 'skip_check', label: 'Skip the check', type: 'check', advanced: true }],
    cli: (p) => `${T} repos sync${sw(p, 'skip_check', '--skip-check')}`,
  },
  repos_pull: {
    action: 'repos',
    fixed: { what: 'pull' },
    label: 'Pull outside changes',
    explain: 'Merges commits pushed directly to an original repo into its folder here. Resolve conflicts, commit and push afterwards.',
    fields: [{ key: 'folder', label: 'Folder', type: 'text', required: true }],
    cli: (p) => `${T} repos pull ${p['folder'] || '<folder>'}`,
  },
  repos_add: {
    action: 'repos',
    fixed: { what: 'add' },
    label: 'Import a repo',
    tone: 'caution',
    explain: 'Imports an existing repo, with its history, as a folder of this repo, and pushes that.',
    fields: [
      { key: 'folder', label: 'Folder name', type: 'text', required: true, placeholder: 'backend' },
      { key: 'url', label: 'Repo URL', type: 'text', required: true, placeholder: 'git@gitlab.com:org/backend.git' },
      { key: 'branch', label: 'Branch', type: 'text', placeholder: 'main' },
    ],
    cli: (p) => `${T} repos add ${p['folder'] || '<folder>'} ${p['url'] || '<url>'}${p['branch'] && p['branch'] !== 'main' ? ' --branch ' + p['branch'] : ''}`,
  },
  repos_list: {
    action: 'repos',
    fixed: { what: 'list' },
    label: 'List repos',
    explain: 'Shows the imported folders, their repos and when each was last updated.',
    cli: () => `${T} repos list`,
  },
};

export interface Offer {
  key: string;
  label?: string;
  hint: string;
  preset?: Record<string, any>;
  /** Run without the dialog (no fields to fill and nothing to lose). */
  direct?: boolean;
}

/** Which actions make sense for a task right now, for the person looking at it. */
export function offersFor(t: Task, me: string, frozen: boolean): { main: Offer[]; more: Offer[] } {
  const id = { id: t.id };
  const mine = t.status === 'in_progress' && t.owner === me;
  const main: Offer[] = [];
  const more: Offer[] = [];
  const edit: Offer = { key: 'edit', label: 'Edit task', hint: 'Change the title, dependencies, goal or acceptance criteria.' };
  const assume: Offer = { key: 'assume', hint: 'Note a decision taken without asking.', preset: id };
  if (t.state === 'dropped') {
    main.push({ key: 'undrop', hint: 'Put it back on the board.', preset: id });
  } else if (t.state === 'done') {
    main.push({ key: 'followup', label: 'Add a follow-up task', hint: 'Done work is on main. Change it with a new task that depends on this one.' });
  } else if (mine) {
    const left = openCriteria(t).length;
    main.push({
      key: 'done',
      hint: left
        ? `${left} acceptance criteri${left === 1 ? 'on is' : 'a are'} still unverified: verify ${left === 1 ? 'it' : 'them'} below first.`
        : 'Commit first. Runs the check on your work merged with main, then pushes.',
      preset: id,
    });
    main.push({ key: 'release', hint: 'Give it back; your commits go to wip/' + t.id + '.', preset: id });
    more.push({ key: 'redo', hint: 'Unresolvable merge conflict: save this attempt, restart from main.', preset: id });
    more.push({ key: 'split', label: 'Split into subtasks', hint: 'Too big: describe the rest as 2–6 subtasks.' });
    more.push({ key: 'hold', hint: 'Stuck: a human must decide something.', preset: id });
    more.push(assume);
    more.push({ key: 'drop', hint: 'Not wanted any more.', preset: id });
  } else if (t.state === 'stale') {
    main.push({ key: 'claim', label: 'Take over', hint: `${t.owner} went quiet: its lease expired. Continue from wip/${t.id}.`, preset: id });
    more.push({ key: 'release', label: 'Release for them', hint: 'Free it for anyone.', preset: { ...id, force: true } });
    more.push({ key: 'hold', hint: 'Keep everyone off it for now.', preset: { ...id, force: true } });
    more.push({ key: 'drop', hint: 'Not wanted any more.', preset: { ...id, force: true } });
  } else if (t.status === 'in_progress') {
    main.push({ key: 'hold', label: 'Stop and put on hold', hint: `${t.owner}'s agent stops within ~5 min; work stays on wip/${t.id}.`, preset: { ...id, force: true } });
    more.push({ key: 'release', label: 'Release for them', hint: 'Free a task whose owner is gone (e.g. a human who left).', preset: { ...id, force: true } });
    more.push({ key: 'drop', label: 'Stop and drop', hint: 'Not wanted any more.', preset: { ...id, force: true } });
    more.push(assume);
  } else if (t.hold) {
    main.push({ key: 'unhold', hint: 'Resolved? Let agents pick it up again.', preset: id });
    main.push(edit);
    more.push({ key: 'drop', hint: 'Not wanted any more.', preset: id });
    more.push(assume);
  } else if (t.orphaned.length) {
    main.push({ key: 'edit', label: 'Fix dependencies', hint: `It depends on dropped ${t.orphaned.join(', ')}.` });
    more.push({ key: 'drop', hint: 'Drop it too.', preset: id });
  } else {
    if (t.ready)
      main.push({ key: 'claim', label: 'Claim for me', hint: frozen ? 'The board is frozen: claiming needs “force”.' : 'Work on it yourself.', preset: frozen ? { ...id, force: true } : id });
    else
      more.push({ key: 'claim', label: 'Claim anyway', hint: 'Its dependencies are not done yet.', preset: { ...id, force: true } });
    main.push(edit);
    more.push({ key: 'hold', hint: 'Keep agents off it until a human decided something.', preset: id });
    more.push({ key: 'drop', hint: 'Not wanted any more.', preset: id });
    more.push(assume);
  }
  return { main, more };
}
