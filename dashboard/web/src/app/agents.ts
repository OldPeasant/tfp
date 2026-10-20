import { Component, computed, effect, inject, signal } from '@angular/core';
import { LiveAgent, LocalAgent, Store } from './store';
import { Ui } from './ui';
import { AgoPipe, splitOwner } from './util';

const STATE_HELP: Record<string, string> = {
  working: 'in a session',
  waiting: 'waiting for work',
  starting: 'starting',
  limited: 'usage limit: resumes by itself',
  paused: 'sessions keep failing: check its log',
  backoff: 'retrying after an error',
  blocked: 'nothing can become ready, or its pushes fail: a human must look (see its log)',
  finished: 'all tasks done',
  stopped: 'stopped',
  offline: 'no news: laptop asleep or gone?',
  nostatus: 'a human, or an old runner',
};

interface Card { owner: string; host: string; name: string; state: string; task: string | null; doing: string | null; at: number | null; done: number }

@Component({
  selector: 'tf-agents',
  imports: [AgoPipe],
  template: `
    <section class="block">
      <div class="block-head">
        <div>
          <p class="cmd"><span class="prompt">$</span> python agents.py status</p>
          <h2>On this laptop</h2>
          <p class="lede">
            Agents run in the background in <code>{{ local()?.agents_dir }}</code>, one clone each, using your Claude subscription.
            They keep working when you close this page.
          </p>
        </div>
        <div class="btns">
          @if (local()?.agents?.length) {
            <button class="btn tone-go" (click)="ui.act('agents_start')">{{ running().length ? 'Start more agents' : 'Start agents' }}</button>
          }
          @if (running().length) {
            <button class="btn" (click)="ui.act('agents_finish')">Finish, then stop</button>
            <button class="btn tone-danger" (click)="ui.act('agents_stop')">Stop all now</button>
          }
        </div>
      </div>
      @if (local()?.agents?.length) {
        <div class="local-agents">
          @for (a of local()!.agents; track a.name) {
            <article class="lagent panel" [class.dead]="!a.alive">
              <div class="row-between">
                <div>
                  <b class="name">{{ a.name }}</b>&nbsp;<span class="muted">&#64; {{ host(a.owner) }}</span>
                </div>
                <span class="pill" [attr.data-s]="lstate(a)">{{ lstateLabel(a) }}</span>
              </div>
              @if (a.alive && a.status?.task) {
                <p class="task-line">on <button class="link" (click)="ui.open(a.status!.task!)">{{ a.status!.task }}</button>
                  {{ store.byId().get(a.status!.task!)?.title }}</p>
              }
              @if (a.alive && a.status?.doing) {
                <p class="doing mono">{{ a.status!.doing }}</p>
              } @else if (a.last_log) {
                <p class="doing mono muted">{{ a.last_log }}</p>
              }
              <div class="btns small">
                <button class="btn ghost" (click)="toggleLog(a.name)">{{ open() === a.name ? 'Hide log' : 'Show log' }}</button>
                @if (a.alive) {
                  @if (!a.finishing) { <button class="btn ghost" (click)="ui.act('agent_finish', { name: a.name })">Finish, then stop</button> }
                  <button class="btn ghost tone-danger" (click)="ui.act('agent_stop', { name: a.name })">Stop</button>
                } @else {
                  <button class="btn ghost tone-go" (click)="restart(a)">Start again</button>
                }
              </div>
              @if (open() === a.name) {
                <div class="log">
                  <div class="seg">
                    <button [class.on]="logKind() === 'steps'" (click)="logKind.set('steps')">Session steps</button>
                    <button [class.on]="logKind() === 'log'" (click)="logKind.set('log')">Runner log</button>
                  </div>
                  <pre class="output tall">{{ logLines().join('\n') || 'Nothing logged yet.' }}</pre>
                  <p class="muted small">Updates every few seconds. Same as <code>python agents.py logs {{ a.name.replace('agent-', '') }}</code>.</p>
                </div>
              }
            </article>
          }
        </div>
      } @else {
        <div class="panel empty-state">
          <h3>No agents on this laptop yet</h3>
          <p>Start one or two. Each takes the most blocking ready task, works on it in its own clone, runs the project check and pushes when it passes, then takes the next.</p>
          <button class="btn tone-go" (click)="ui.act('agents_start')">Start agents</button>
        </div>
      }
    </section>

    <section class="block">
      <h2>Everyone</h2>
      <p class="lede">All agents and people working on the board, from every laptop. Agents report every 10–60 seconds.</p>
      <div class="agent-grid">
        @for (c of cards(); track c.owner) {
          <article class="panel acard" [attr.data-s]="c.state">
            <div class="row-between">
              <div><b>{{ c.name }}</b>@if (c.host) { <span class="muted"> @ {{ c.host }}</span> }</div>
              <span class="pill" [attr.data-s]="c.state">{{ c.state === 'nostatus' ? 'no live status' : c.state === 'limited' ? 'usage limit' : c.state }}</span>
            </div>
            <p class="muted small">{{ help(c.state) }}</p>
            @if (c.task) {
              <p class="task-line"><button class="link" (click)="ui.open(c.task)">{{ c.task }}</button> {{ store.byId().get(c.task)?.title }}</p>
            }
            @if (c.doing) { <p class="doing mono">{{ c.doing }}</p> }
            <p class="muted small">{{ c.done }} done@if (c.at) { · updated {{ c.at | ago: store.now() }} }</p>
          </article>
        } @empty {
          <div class="panel empty-state"><p>Nobody has reported yet. Agents appear here as soon as they start, on any laptop.</p></div>
        }
      </div>
    </section>
  `,
})
export class AgentsView {
  store = inject(Store);
  ui = inject(Ui);
  readonly local = this.store.local;
  readonly open = signal<string | null>(null);
  readonly logKind = signal<'steps' | 'log'>('steps');
  readonly logLines = signal<string[]>([]);
  readonly running = computed(() => (this.local()?.agents ?? []).filter((a) => a.alive));

  readonly cards = computed<Card[]>(() => {
    const b = this.store.board();
    if (!b) return [];
    const now = this.store.now();
    const done = new Map<string, number>();
    const active = new Map<string, string>();
    for (const t of b.tasks) {
      if (t.status === 'done' && t.owner) done.set(t.owner, (done.get(t.owner) ?? 0) + 1);
      if (t.status === 'in_progress') active.set(t.owner, t.id);
    }
    const seen = new Set(b.agents.map((a) => a.owner));
    const rows: (LiveAgent & { live: boolean })[] = [
      ...b.agents.map((a) => ({ ...a, live: true })),
      ...[...active.keys()].filter((o) => !seen.has(o)).map((o) => ({ owner: o, state: 'nostatus', task: null, doing: null, doing_at: null, updated: 0, live: false })),
    ];
    return rows
      .map((a) => {
        let state = a.state;
        if (a.live && !['stopped', 'finished', 'blocked'].includes(state) && now - a.updated > b.offline_after) state = 'offline';
        const task = active.get(a.owner) ?? null;
        const quiet = ['stopped', 'finished', 'offline'].includes(state);
        return { owner: a.owner, ...splitOwner(a.owner), state, task, doing: a.live && !quiet ? a.doing : null, at: a.doing_at || a.updated || null, done: done.get(a.owner) ?? 0 };
      })
      .filter((c) => !(['stopped', 'finished'].includes(c.state) && !c.task && !c.done))
      .sort((x, y) => +['stopped', 'finished'].includes(x.state) - +['stopped', 'finished'].includes(y.state) || x.owner.localeCompare(y.owner));
  });

  constructor() {
    let timer: ReturnType<typeof setInterval> | undefined;
    effect((cleanup) => {
      const name = this.open();
      const kind = this.logKind();
      clearInterval(timer);
      if (!name) return;
      const load = () =>
        this.store.get<{ lines: string[] }>(`/api/agents/${name}/${kind}?n=400`).then((r) => this.logLines.set(r.lines)).catch(() => {});
      load();
      timer = setInterval(load, 3000);
      cleanup(() => clearInterval(timer));
    });
  }

  toggleLog(name: string) {
    this.logLines.set([]);
    this.open.set(this.open() === name ? null : name);
  }

  restart(a: LocalAgent) {
    const n = +a.name.replace('agent-', '');
    this.ui.act('agents_start', { n: 1, first: n, host: splitOwner(a.owner).host || undefined });
  }

  lstate(a: LocalAgent) {
    if (!a.alive) return 'stopped';
    if (a.stopping) return 'paused';
    if (a.finishing) return 'waiting';
    return a.status?.state ?? 'starting';
  }

  lstateLabel(a: LocalAgent) {
    if (!a.alive) return 'not running';
    if (a.stopping) return 'stopping…';
    if (a.finishing) return 'finishing, then stops';
    return a.status?.state === 'limited' ? 'usage limit' : a.status?.state ?? 'starting';
  }

  host(owner: string) {
    return splitOwner(owner).host || owner;
  }

  help(s: string) {
    return STATE_HELP[s] ?? '';
  }
}
