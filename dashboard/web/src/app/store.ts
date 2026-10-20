import { Injectable, computed, signal } from '@angular/core';

/** `n` names it for confirm/reject; `review` is '' while it waits for a human. */
export interface Assumption {
  n: number;
  at: string;
  who: string;
  text: string;
  review: '' | 'confirmed' | 'rejected';
  reviewed_at: string;
  reviewed_by: string;
  note: string;
}

export const openAssumptions = (t: { assumptions: Assumption[] }) => t.assumptions.filter((a) => !a.review);
/** An acceptance criterion: mark ' ' open, 'x' verified, '~' skipped with a reason (`tasks.py verify`). */
export interface Criterion { n: number; mark: ' ' | 'x' | '~'; text: string; evidence: string[] }
export const openCriteria = (t: { criteria: Criterion[] }) => t.criteria.filter((c) => c.mark === ' ');
export interface SavedAttempt { n: number; branch: string; url: string | null }

export interface Task {
  id: string;
  title: string;
  status: 'todo' | 'in_progress' | 'done' | 'dropped';
  state: 'todo' | 'in_progress' | 'done' | 'dropped' | 'hold' | 'stale';
  owner: string;
  lease_until: string;
  lease_end: number | null;
  attempts: number;
  releases: number;
  takeovers: number;
  hold: string;
  extended: string;
  parent: string;
  subtasks: string;
  deps: string[];
  touches: string[];
  prio: number;
  body: string;
  dependents: number;
  level: number;
  stale: boolean;
  ready: boolean;
  orphaned: string[];
  claimed_at: number | null;
  done_at: number | null;
  seen_at: number | null;
  health: '' | 'ok' | 'warn' | 'bad';
  assumptions: Assumption[];
  criteria: Criterion[];
  saved_attempts: SavedAttempt[];
  unblocks_direct: string[];
  has_wip: boolean;
}

export interface FeedEvent {
  at: number;
  kind: string;
  who: string;
  task: string | null;
  prev?: string | null;
  text: string;
  attempt?: string | null;
}

export interface LiveAgent {
  owner: string;
  state: string;
  task: string | null;
  doing: string | null;
  doing_at: number | null;
  updated: number;
}

export interface RepoSync {
  run_at?: number;
  result?: string;
  message?: string;
  repos?: Record<string, { url?: string; branch?: string; pushed_at?: number; commit?: string; result?: string; message?: string }>;
}

export interface Board {
  ref: string;
  head: string;
  base: string;
  generated_at: number;
  tasks: Task[];
  feed: FeedEvent[];
  agents: LiveAgent[];
  sync: RepoSync | null;
  problems: string[];
  frozen: string | null;
  critical_path: number;
  offline_after: number;
}

export interface LocalAgent {
  name: string;
  pid: number;
  owner: string;
  clone: string;
  alive: boolean;
  finishing: boolean;
  stopping: boolean;
  status: LiveAgent | null;
  last_log: string;
}

export interface Local {
  root: string;
  name: string;
  branch: string;
  upstream: string | null;
  ahead: number;
  behind: number;
  dirty: string[];
  merge_state: string | null;
  conflicted: string[];
  me: string;
  git_user: string;
  acting_as: string | null;
  check_cmd: string | null;
  agents_dir: string;
  agents: LocalAgent[];
  host: string;
  repos: boolean;
}

export interface State {
  version: number;
  board: Board | null;
  error: string | null;
  fetched_at: number | null;
  interval: number;
  local: Local;
  now: number;
  busy: boolean;
}

export interface Job {
  id: number;
  title: string;
  display: string;
  state: 'queued' | 'running' | 'ok' | 'failed';
  output: string;
  exit: number | null;
  queued_at: number;
  started_at: number | null;
  ended_at: number | null;
}

/** Everything the page shows, polled from the local dashboard server (dashboard/server.py). */
@Injectable({ providedIn: 'root' })
export class Store {
  readonly state = signal<State | null>(null);
  readonly jobs = signal<Job[]>([]);
  readonly offline = signal<string | null>(null);
  /** Seconds since epoch, ticking, so "3 min ago" stays true between polls. */
  readonly now = signal(Date.now() / 1000);

  readonly board = computed(() => this.state()?.board ?? null);
  readonly local = computed(() => this.state()?.local ?? null);
  readonly tasks = computed(() => this.board()?.tasks ?? []);
  readonly byId = computed(() => new Map(this.tasks().map((t) => [t.id, t])));
  readonly me = computed(() => this.local()?.me ?? '');

  /** Board-wide numbers for the header. Dropped tasks are no longer part of the plan. */
  readonly counts = computed(() => {
    const ts = this.tasks();
    const n = (f: (t: Task) => boolean) => ts.filter(f).length;
    const live = ts.filter((t) => t.state !== 'dropped');
    return {
      total: live.length,
      done: n((t) => t.state === 'done'),
      working: n((t) => t.state === 'in_progress'),
      stale: n((t) => t.state === 'stale'),
      human: n((t) => t.state === 'hold'),
      ready: n((t) => t.ready && t.status === 'todo' && !t.hold),
      waiting: n((t) => t.state === 'todo' && !t.ready),
      dropped: n((t) => t.state === 'dropped'),
    };
  });

  /** The order `tasks.py next` uses: most-blocking first, then prio, then id. */
  readonly ranked = computed(() =>
    this.tasks()
      .filter((t) => t.ready)
      .sort((a, b) => b.dependents - a.dependents || a.prio - b.prio || a.id.localeCompare(b.id)),
  );

  readonly mine = computed(() =>
    this.tasks().filter((t) => t.status === 'in_progress' && t.owner === this.me()),
  );

  private token = '';
  private timer?: ReturnType<typeof setTimeout>;
  private jobTimer?: ReturnType<typeof setTimeout>;
  private watched = new Set<number>();
  private listeners: ((j: Job) => void)[] = [];

  constructor() {
    const meta = document.querySelector('meta[name="tfp-token"]')?.getAttribute('content');
    const q = new URLSearchParams(location.search).get('token');
    if (q) {
      try { sessionStorage.setItem('tfp-token', q); } catch {}
    }
    let stored: string | null = null;
    try { stored = sessionStorage.getItem('tfp-token'); } catch {}
    this.token = meta || q || stored || '';
    setInterval(() => this.now.set(Date.now() / 1000), 1000);
    this.poll();
    this.pollJobs();
  }

  private async api<T>(path: string, init?: RequestInit): Promise<T> {
    const r = await fetch(path, {
      ...init,
      headers: { 'Content-Type': 'application/json', 'X-TFP-Token': this.token },
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `${r.status} ${r.statusText}`);
    return body as T;
  }

  get<T>(path: string) {
    return this.api<T>(path);
  }

  async poll() {
    clearTimeout(this.timer);
    try {
      this.state.set(await this.api<State>('/api/state'));
      this.offline.set(null);
    } catch (e) {
      this.offline.set(e instanceof TypeError ? 'The dashboard server is not running. Start it again with: python dashboard/server.py' : String((e as Error).message));
    }
    this.timer = setTimeout(() => this.poll(), document.hidden ? 15000 : 3000);
  }

  async refresh() {
    await this.api('/api/refresh', { method: 'POST', body: '{}' }).catch(() => {});
    setTimeout(() => this.poll(), 1500);
  }

  /** Queue an action on the server: the job right away, and a promise for when it has finished. */
  async run(action: string, params: Record<string, unknown> = {}): Promise<{ job: Job; done: Promise<Job> }> {
    const job = await this.api<Job>('/api/action', { method: 'POST', body: JSON.stringify({ action, params }) });
    this.jobs.update((js) => [...js.filter((j) => j.id !== job.id), job]);
    this.watched.add(job.id);
    const done = new Promise<Job>((resolve) => {
      const l = (j: Job) => {
        if (j.id === job.id) {
          this.listeners = this.listeners.filter((x) => x !== l);
          resolve(j);
        }
      };
      this.listeners.push(l);
    });
    this.pollJobs(300);
    return { job, done };
  }

  private async pollJobs(delay = 0) {
    clearTimeout(this.jobTimer);
    this.jobTimer = setTimeout(async () => {
      let active = false;
      try {
        const js = await this.api<Job[]>('/api/jobs');
        this.jobs.set(js);
        for (const j of js) {
          if (j.state === 'queued' || j.state === 'running') active = true;
          else if (this.watched.delete(j.id)) {
            this.listeners.forEach((l) => l(j));
            this.poll();
          }
        }
      } catch {}
      this.pollJobs(active || this.watched.size ? 700 : 5000);
    }, delay);
  }
}
