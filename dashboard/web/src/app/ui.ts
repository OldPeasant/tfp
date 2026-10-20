import { Injectable, signal } from '@angular/core';

export type View = 'board' | 'graph' | 'agents' | 'activity' | 'assumptions' | 'repos';

export interface DialogReq {
  key: string;
  preset?: Record<string, any>;
  title?: string;
}

export interface EditorReq {
  mode: 'new' | 'edit';
  id?: string;
  deps?: string[];
}

/** Which view, drawer and dialog are open. One place, so any component can open any of them. */
@Injectable({ providedIn: 'root' })
export class Ui {
  readonly view = signal<View>(this.initialView());
  readonly task = signal<string | null>(null);
  readonly dialog = signal<DialogReq | null>(null);
  readonly editor = signal<EditorReq | null>(null);
  readonly split = signal<string | null>(null);
  readonly console = signal(false);

  private initialView(): View {
    const h = location.hash.replace('#', '') as View;
    return ['board', 'graph', 'agents', 'activity', 'assumptions', 'repos'].includes(h) ? h : 'board';
  }

  go(v: View) {
    this.view.set(v);
    history.replaceState(null, '', '#' + v);
  }

  open(id: string) {
    this.task.set(id);
  }

  act(key: string, preset?: Record<string, any>, title?: string) {
    if (key === 'edit') return this.editor.set({ mode: 'edit', id: preset?.['id'] ?? this.task() ?? undefined });
    if (key === 'followup') return this.editor.set({ mode: 'new', deps: [preset?.['id'] ?? this.task()!] });
    if (key === 'new') return this.editor.set({ mode: 'new', deps: preset?.['deps'] });
    if (key === 'split') return this.split.set(preset?.['id'] ?? this.task());
    this.dialog.set({ key, preset, title });
  }
}
