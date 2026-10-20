import { Component, ElementRef, computed, effect, inject, signal, untracked, viewChild } from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ACTIONS, Field } from './actions';
import { Job, Store } from './store';
import { Ui } from './ui';

/** The most telling line of a CLI run: what happened, in the CLI's own words. */
export function outcome(job: Job): { line: string; good: boolean | null } {
  const lines = job.output.split('\n').map((l) => l.trim()).filter(Boolean);
  const bad = lines.find((l) => /^(REFUSED|LOST|CONFLICT|FROZEN|ON HOLD .*attempts failed)|could not|failed|Error|no such|not owned|exists/i.test(l));
  const good = lines.find((l) => /^(CLAIMED|DONE|RELEASED|ON HOLD|UNHELD|DROPPED|UNDROPPED|FROZEN|UNFROZEN|RECORDED|REDO|SPLIT|EXTENDED|CREATED|SAVED|started|asked|actions now|ok$)/.test(l));
  if (job.state === 'failed') return { line: bad || lines.at(-1) || `exit ${job.exit}`, good: false };
  if (job.state === 'ok') return { line: good || lines.at(-1) || 'Done.', good: true };
  return { line: job.state === 'queued' ? 'Waiting for the previous command…' : 'Running…', good: null };
}

@Component({
  selector: 'tf-dialog',
  imports: [FormsModule, NgTemplateOutlet],
  template: `
    <dialog #dlg class="dlg" (close)="closed()" (cancel)="$event.preventDefault(); close()">
      @if (def(); as d) {
        <form (ngSubmit)="submit()" class="dlg-body">
          <header>
            <h2>{{ heading() }}</h2>
            <button type="button" class="x" (click)="close()" aria-label="Close">×</button>
          </header>
          <p class="explain">{{ explain() }}</p>
          @if (!job()) {
            @for (f of basic(); track f.key) {
              <ng-container [ngTemplateOutlet]="field" [ngTemplateOutletContext]="{ f }" />
            }
            @if (advanced().length) {
              <details class="more-opts">
                <summary>More options</summary>
                @for (f of advanced(); track f.key) {
                  <ng-container [ngTemplateOutlet]="field" [ngTemplateOutletContext]="{ f }" />
                }
              </details>
            }
          }
          <div class="cli" title="The same, on the command line">
            <span class="prompt">$</span> {{ d.cli(params()) }}
          </div>
          @if (job(); as j) {
            <div class="result" [class.good]="res()?.good === true" [class.bad]="res()?.good === false">
              <b>{{ res()?.line }}</b>
            </div>
            @if (j.output) {
              <pre class="output">{{ j.output }}</pre>
            }
          }
          @if (error()) {
            <div class="result bad"><b>{{ error() }}</b></div>
          }
          <footer>
            @if (!job() || job()!.state === 'queued' || job()!.state === 'running') {
              <button type="button" class="btn ghost" (click)="close()">{{ job() ? 'Hide' : 'Cancel' }}</button>
              <button type="submit" class="btn" [class]="'tone-' + (d.tone || 'normal')" [disabled]="!valid() || !!job()">
                {{ job() ? 'Running…' : d.label }}
              </button>
            } @else {
              <button type="button" class="btn" (click)="close()">Close</button>
            }
          </footer>
        </form>
      }
    </dialog>

    <ng-template #field let-f="f">
      @switch (f.type) {
        @case ('check') {
          <label class="check">
            <input type="checkbox" [name]="f.key" [ngModel]="params()[f.key]" (ngModelChange)="set(f.key, $event)" />
            <span>{{ f.label }}@if (f.help) {<small>{{ f.help }}</small>}</span>
          </label>
        }
        @case ('textarea') {
          <label class="fld">
            <span>{{ f.label }}@if (f.required) {<i> required</i>}</span>
            <textarea rows="3" [name]="f.key" [placeholder]="f.placeholder || ''" [ngModel]="params()[f.key] ?? ''" (ngModelChange)="set(f.key, $event)"></textarea>
            @if (f.help) {<small>{{ f.help }}</small>}
          </label>
        }
        @default {
          <label class="fld">
            <span>{{ f.label }}@if (f.required) {<i> required</i>}</span>
            <input [type]="f.type === 'number' ? 'number' : 'text'" [name]="f.key" [min]="f.min ?? ''" [max]="f.max ?? ''"
              [placeholder]="f.placeholder || ''" [ngModel]="params()[f.key] ?? ''" (ngModelChange)="set(f.key, $event)" />
            @if (f.help) {<small>{{ f.help }}</small>}
          </label>
        }
      }
    </ng-template>
  `,
})
export class ActionDialog {
  private store = inject(Store);
  private ui = inject(Ui);
  private dlg = viewChild.required<ElementRef<HTMLDialogElement>>('dlg');

  readonly params = signal<Record<string, any>>({});
  readonly job = signal<Job | null>(null);
  readonly error = signal<string | null>(null);

  readonly def = computed(() => {
    const r = this.ui.dialog();
    return r ? ACTIONS[r.key] : null;
  });
  readonly basic = computed(() => (this.def()?.fields ?? []).filter((f) => !f.advanced && !this.hidden(f)));
  readonly advanced = computed(() => (this.def()?.fields ?? []).filter((f) => f.advanced && !this.hidden(f)));
  readonly heading = computed(() => {
    const r = this.ui.dialog();
    const d = this.def();
    if (!r || !d) return '';
    const id = r.preset?.['id'] || r.preset?.['name'];
    return r.title || (id ? `${d.label}: ${id}` : d.label);
  });
  readonly explain = computed(() => {
    const id = this.ui.dialog()?.preset?.['id'];
    return (this.def()?.explain ?? '').replace(/<id>/g, id || '<id>');
  });
  readonly valid = computed(() =>
    (this.def()?.fields ?? []).every((f) => !f.required || String(this.params()[f.key] ?? '').trim() !== ''),
  );
  readonly res = computed(() => {
    const j = this.job();
    if (!j) return null;
    const live = this.store.jobs().find((x) => x.id === j.id) ?? j;
    return outcome(live);
  });

  constructor() {
    effect(() => {
      const r = this.ui.dialog();
      const el = this.dlg().nativeElement;
      if (r) {
        // untracked: a poll replaces store.local(); it must not reset what the user typed since opening
        const defaults = untracked(() => (r.key === 'agents_start' ? { n: 2, host: this.store.local()?.host, first: this.nextAgent() } : {}));
        this.params.set({ ...defaults, ...(r.preset ?? {}) });
        this.job.set(null);
        this.error.set(null);
        if (!el.open) el.showModal();
      } else if (el.open) el.close();
    });
    // follow the job's output while it runs
    effect(() => {
      const j = this.job();
      if (!j) return;
      const live = this.store.jobs().find((x) => x.id === j.id);
      if (live && (live.output !== j.output || live.state !== j.state)) this.job.set(live);
    });
  }

  /** "Do it although it is not my task" only matters for someone else's task. */
  private hidden(f: Field) {
    const id = this.ui.dialog()?.preset?.['id'];
    if (f.key !== 'force' || !id) return false;
    const t = this.store.byId().get(id);
    return !!t && t.owner === this.store.me() && this.ui.dialog()?.key !== 'claim';
  }

  private nextAgent() {
    const ns = (this.store.local()?.agents ?? []).filter((a) => a.alive).map((a) => +a.name.replace('agent-', ''));
    return ns.length ? Math.max(...ns) + 1 : 1;
  }

  set(k: string, v: any) {
    this.params.update((p) => ({ ...p, [k]: v }));
  }

  async submit() {
    const d = this.def();
    if (!d || !this.valid() || this.job()) return;
    this.error.set(null);
    try {
      const { job, done } = await this.store.run(d.action, { ...this.params(), ...(d.fixed ?? {}) });
      this.job.set(job);
      this.job.set(await done);
    } catch (e) {
      this.error.set((e as Error).message);
    }
  }

  close() {
    this.ui.dialog.set(null);
  }

  closed() {
    if (this.ui.dialog()) this.ui.dialog.set(null);
  }
}
