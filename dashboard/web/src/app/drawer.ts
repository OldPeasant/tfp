import { Component, computed, effect, inject, signal } from '@angular/core';
import { ACTIONS, offersFor } from './actions';
import { Store, Task } from './store';
import { Ui } from './ui';
import { AgoPipe, dur, markdown, stamp, stateLabel } from './util';

interface Commit { at: number; who: string; commit: string; text: string }

/** Everything about one task, and what you can do with it. */
@Component({
  selector: 'tf-drawer',
  imports: [AgoPipe],
  template: `
    @if (task(); as t) {
      <div class="scrim" (click)="ui.task.set(null)"></div>
      <aside class="drawer" role="dialog" [attr.aria-label]="'Task ' + t.id" (keydown.escape)="ui.task.set(null)" tabindex="-1">
        <header class="dr-head" [attr.data-state]="t.state">
          <div class="row-between">
            <span class="mono id">{{ t.id }}</span>
            <button class="x" (click)="ui.task.set(null)" aria-label="Close">×</button>
          </div>
          <h2>{{ t.title }}</h2>
          <span class="state" [attr.data-state]="t.state" [class.ready]="t.ready">{{ label(t) }}</span>
          <p class="meaning">{{ meaning(t) }}</p>
        </header>

        <section class="dr-actions">
          @for (o of offers().main; track o.key + o.label) {
            <button class="btn" [class]="'tone-' + tone(o.key)" (click)="ui.act(o.key, o.preset ?? { id: t.id })">
              {{ o.label || actionLabel(o.key) }}
            </button>
          }
          @if (offers().main.length) {
            <p class="hint">{{ offers().main[0].hint }}</p>
          }
          @if (offers().more.length) {
            <details class="more">
              <summary>Other actions</summary>
              <ul>
                @for (o of offers().more; track o.key + o.label) {
                  <li>
                    <button class="link" (click)="ui.act(o.key, o.preset ?? { id: t.id })">{{ o.label || actionLabel(o.key) }}</button>
                    <span>{{ o.hint }}</span>
                  </li>
                }
              </ul>
            </details>
          }
        </section>

        @if (t.hold || t.orphaned.length) {
          <section class="callout bad">
            <b>Why it waits for a human</b>
            <p>{{ t.hold || 'It depends on ' + t.orphaned.join(', ') + ', which was dropped. Remove that dependency or drop this task too.' }}</p>
          </section>
        }

        <dl class="facts">
          @if (t.owner && t.status !== 'todo') {
            <dt>Owner</dt><dd>{{ t.owner }}@if (t.owner === store.me()) { <span class="muted">(you)</span> }</dd>
          }
          <dt>Depends on</dt>
          <dd>
            @for (d of t.deps; track d) {
              <button class="chip" [attr.data-state]="store.byId().get(d)?.state" (click)="ui.open(d)">{{ d }}</button>
            } @empty { <span class="muted">nothing: it can start any time</span> }
          </dd>
          <dt>Unblocks</dt>
          <dd>
            @for (d of t.unblocks_direct; track d) {
              <button class="chip" [attr.data-state]="store.byId().get(d)?.state" (click)="ui.open(d)">{{ d }}</button>
            } @empty { <span class="muted">no other task</span> }
            @if (t.dependents > t.unblocks_direct.length) {
              <span class="muted"> · {{ t.dependents }} open tasks wait on it in total</span>
            }
          </dd>
          <dt>Priority</dt><dd>{{ t.prio }} <span class="muted">{{ t.prio === 1 ? 'first among equals' : t.prio === 2 ? 'normal' : 'later' }}</span></dd>
          @if (time(t); as tm) {<dt>Time</dt><dd>{{ tm }}</dd>}
          @if (t.status === 'in_progress' && t.lease_end) {
            <dt>Lease</dt>
            <dd>
              <span class="dot" [attr.data-h]="t.health"></span>
              @if (t.stale) { expired {{ t.lease_end | ago: store.now() }} } @else { heartbeat {{ t.seen_at | ago: store.now() }} }
            </dd>
          }
          @if (t.attempts > 1 || t.releases || t.takeovers) {
            <dt>History</dt>
            <dd>
              {{ t.attempts > 1 ? t.attempts + ' attempts' : '' }}
              {{ t.releases ? ' · released ' + t.releases + '×' : '' }}
              {{ t.takeovers ? ' · taken over ' + t.takeovers + '×' : '' }}
              @for (a of t.saved_attempts; track a.n) {
                @if (a.url) { <a [href]="a.url" target="_blank" rel="noopener">attempt {{ a.n }}</a> }
                @else { <code [title]="'git diff origin/main...origin/' + a.branch">{{ a.branch }}</code> }
              }
            </dd>
          }
          @if (t.has_wip) {
            <dt>Unfinished work</dt><dd><code>origin/wip/{{ t.id }}</code> <span class="muted">— the next owner starts from it</span></dd>
          }
          @if (t.touches.length) {
            <dt>Touches</dt>
            <dd>@for (p of t.touches; track p) {<code class="touch">{{ p }}</code>}</dd>
          }
          @if (t.parent) {<dt>Part of</dt><dd><button class="chip" (click)="ui.open(t.parent)">{{ t.parent }}</button></dd>}
          @if (t.subtasks) {
            <dt>Split into</dt>
            <dd>@for (s of t.subtasks.split(', '); track s) {<button class="chip" [attr.data-state]="store.byId().get(s)?.state" (click)="ui.open(s)">{{ s }}</button>}</dd>
          }
        </dl>

        @if (t.criteria.length) {
          <section>
            <h3>Acceptance criteria <span class="muted small">{{ verified(t) }}</span></h3>
            <ul class="crit-list">
              @for (c of t.criteria; track c.n) {
                <li [attr.data-mark]="c.mark === 'x' ? 'verified' : c.mark === '~' ? 'skipped' : 'open'">
                  <p><span class="muted">#{{ c.n }}</span> {{ c.text }}</p>
                  @for (e of c.evidence; track $index) {<p class="evidence">{{ e }}</p>}
                  @if (c.mark === ' ' && t.status === 'in_progress' && t.owner === store.me()) {
                    <span class="btns small">
                      <button class="btn" (click)="ui.act('verify', { id: t.id, n: c.n }, 'Verify #' + c.n + ': ' + c.text)">Verify…</button>
                      <button class="btn ghost" (click)="ui.act('verify', { id: t.id, n: c.n, skip: true }, 'Skip #' + c.n + ': ' + c.text)">Can’t verify…</button>
                    </span>
                  }
                </li>
              }
            </ul>
          </section>
        }

        @if (t.assumptions.length) {
          <section>
            <h3>Assumptions</h3>
            <ul class="assume-list">
              @for (a of t.assumptions; track a.n) {
                <li [attr.data-review]="a.review || 'open'">
                  <p>{{ a.text }}</p>
                  <span class="muted">#{{ a.n }} · {{ a.who }} · {{ a.at }}</span>
                  @if (a.review) {
                    <span class="small"> · <b>{{ a.review }}</b> by {{ a.reviewed_by }}@if (a.note) {: {{ a.note }}}</span>
                  } @else {
                    <span class="btns small">
                      <button class="btn" (click)="ui.act('confirm', { id: t.id, n: a.n })">Confirm</button>
                      <button class="btn ghost" (click)="ui.act('reject', { id: t.id, n: a.n })">Reject…</button>
                    </span>
                  }
                </li>
              }
            </ul>
          </section>
        }

        <section>
          <h3>Task file <span class="muted mono small">tasks/{{ t.id }}.md</span></h3>
          <div class="md" [innerHTML]="body()"></div>
        </section>

        <section>
          <h3>Changes</h3>
          <ol class="timeline">
            @for (c of commits(); track c.commit) {
              <li><span class="muted" [title]="stamp(c.at)">{{ c.at | ago: store.now() }}</span> {{ c.text }}</li>
            } @empty { <li class="muted">Loading…</li> }
          </ol>
        </section>
      </aside>
    }
  `,
})
export class TaskDrawer {
  store = inject(Store);
  ui = inject(Ui);
  readonly commits = signal<Commit[]>([]);
  readonly stamp = stamp;
  readonly task = computed(() => (this.ui.task() ? this.store.byId().get(this.ui.task()!) ?? null : null));
  readonly body = computed(() => markdown(this.task()?.body ?? ''));
  readonly offers = computed(() => {
    const t = this.task();
    return t ? offersFor(t, this.store.me(), !!this.store.board()?.frozen) : { main: [], more: [] };
  });

  constructor() {
    let last = '';
    effect(() => {
      const t = this.task();
      const key = t ? `${t.id}:${this.store.board()?.head}` : '';
      if (!t || key === last) return;
      last = key;
      this.store.get<{ commits: Commit[] }>(`/api/tasks/${t.id}/history`).then((r) => this.commits.set(r.commits)).catch(() => {});
    });
  }

  label = stateLabel;

  verified(t: Task) {
    const x = t.criteria.filter((c) => c.mark === 'x').length;
    const skipped = t.criteria.filter((c) => c.mark === '~').length;
    return `${x}/${t.criteria.length} verified${skipped ? `, ${skipped} skipped` : ''}`;
  }

  actionLabel(k: string) {
    return ACTIONS[k]?.label ?? k;
  }

  tone(k: string) {
    return k === 'edit' || k === 'followup' ? 'normal' : ACTIONS[k]?.tone ?? 'normal';
  }

  time(t: Task) {
    const now = this.store.now();
    if (t.status === 'done' && t.claimed_at && t.done_at) return `took ${dur(t.done_at - t.claimed_at)}`;
    if (t.status === 'in_progress' && t.claimed_at) return `${dur(now - t.claimed_at)} so far`;
    return '';
  }

  meaning(t: Task): string {
    const by = this.store.byId();
    const me = this.store.me();
    switch (t.state) {
      case 'done':
        return 'Finished: its code is on main.';
      case 'dropped':
        return 'Not part of the plan any more.';
      case 'stale':
        return `${t.owner} stopped renewing its lease, probably a crashed or sleeping laptop. Anyone may take it over and continue from its saved work.`;
      case 'hold':
        return t.hold ? 'Agents leave it alone until a human takes it off hold.' : 'It depends on a dropped task, so it can never become ready as it is.';
      case 'in_progress':
        return t.owner === me ? 'You are working on it. Commit your work, then mark it done.' : `${t.owner} is working on it.`;
      default: {
        if (t.ready) return 'Nobody works on it, and everything it needs is done. The next free agent takes it.';
        const open = t.deps.filter((d) => by.get(d)?.status !== 'done');
        return `It waits until ${open.join(', ')} ${open.length === 1 ? 'is' : 'are'} done.`;
      }
    }
  }
}
