import { Pipe, PipeTransform } from '@angular/core';
import { Task } from './store';

export function ago(at: number | null | undefined, now: number): string {
  if (!at) return '–';
  const s = Math.max(0, Math.floor(now - at));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function dur(s: number): string {
  s = Math.max(0, Math.floor(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  return `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')} min`;
}

export function stamp(at: number | null | undefined): string {
  return at ? new Date(at * 1000).toLocaleString() : '';
}

/** What a task's state means, in words a newcomer understands. */
export function stateLabel(t: Task): string {
  switch (t.state) {
    case 'hold':
      return t.hold ? 'needs a human' : 'waits on a dropped task';
    case 'stale':
      return 'stale: owner went quiet';
    case 'in_progress':
      return 'in progress';
    case 'todo':
      return t.ready ? 'ready to claim' : 'waiting on dependencies';
    default:
      return t.state;
  }
}

export function plural(n: number, word: string, many = word + 's') {
  return `${n} ${n === 1 ? word : many}`;
}

/** Host and agent name from an owner like "lapA/agent-2"; humans have no host. */
export function splitOwner(owner: string): { host: string; name: string } {
  const i = owner.lastIndexOf('/');
  return i < 0 ? { host: '', name: owner } : { host: owner.slice(0, i), name: owner.slice(i + 1) };
}

const esc = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function inline(s: string): string {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
}

/** A small Markdown renderer for task files: headings, lists, checkboxes, code blocks, inline code,
 * bold. Everything is escaped first, so a task file can't inject markup. */
export function markdown(src: string): string {
  const out: string[] = [];
  let list = false;
  let code: string[] | null = null;
  const close = () => {
    if (list) out.push('</ul>');
    list = false;
  };
  for (const raw of src.replace(/\r\n/g, '\n').split('\n')) {
    if (code) {
      if (raw.trim().startsWith('```')) {
        out.push(`<pre>${esc(code.join('\n'))}</pre>`);
        code = null;
      } else code.push(raw);
      continue;
    }
    if (raw.trim().startsWith('```')) {
      close();
      code = [];
      continue;
    }
    const h = raw.match(/^(#{1,4})\s+(.*)$/);
    const li = raw.match(/^(\s*)[-*]\s+(\[( |x|X|~)\]\s*)?(.*)$/);
    if (h) {
      close();
      out.push(`<h${h[1].length + 2}>${inline(h[2])}</h${h[1].length + 2}>`);
    } else if (li) {
      if (!list) out.push('<ul>');
      list = true;
      const state = li[3] === ' ' ? 'open' : li[3] === '~' ? 'skipped' : 'done';
      const box = li[2] ? `<span class="box ${state === 'done' ? 'on' : state === 'skipped' ? 'skip' : ''}" aria-label="${state}"></span>` : '';
      const cls = [li[2] ? 'check' : '', li[1].length >= 2 ? 'sub' : ''].filter(Boolean).join(' ');
      out.push(`<li class="${cls}">${box}${inline(li[4])}</li>`);
    } else if (!raw.trim()) {
      close();
    } else {
      close();
      out.push(`<p>${inline(raw)}</p>`);
    }
  }
  if (code) out.push(`<pre>${esc(code.join('\n'))}</pre>`);
  close();
  return out.join('');
}

@Pipe({ name: 'ago' })
export class AgoPipe implements PipeTransform {
  transform(at: number | null | undefined, now: number) {
    return ago(at, now);
  }
}

@Pipe({ name: 'dur' })
export class DurPipe implements PipeTransform {
  transform(s: number) {
    return dur(s);
  }
}
