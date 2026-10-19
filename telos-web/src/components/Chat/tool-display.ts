/**
 * Pure-function helpers for deriving + updating the tool-display rows.
 * Kept separate from `ToolDisplay.svelte` so the logic is trivially
 * unit-testable and importable from the chat orchestrator.
 *
 * Ports the dispatch in `telos/ui/static/js/tool-display.js`.
 */

import type { ToolCallEvent, ToolResultEvent } from './stream';

export type ToolRowStatus = 'pending' | 'ok' | 'error';

export interface ToolRow {
  key: string;
  name: string;
  badge: string;
  preview: string;
  status: ToolRowStatus;
}

/**
 * Build a row from one `tool_call` SSE event. Mirrors the branching
 * `addToolCall` did in the SSR JS — same badge + preview for the same
 * tools (delegate_to, coordinate, web_search, browser_open, default).
 */
export function rowFromCall(event: ToolCallEvent): ToolRow {
  let badge: string;
  let preview: string;
  const input = (event.input ?? {}) as Record<string, unknown>;
  if (event.name === 'delegate_to') {
    const agentSlug = input.agent_slug;
    badge = 'delegate';
    preview = `Asking ${typeof agentSlug === 'string' ? agentSlug : 'specialist'}…`;
  } else if (event.name === 'coordinate') {
    const slugs = input.agent_slugs;
    const list = Array.isArray(slugs) ? slugs.join(', ') : '';
    badge = 'coordinate';
    preview = `Asking ${list}…`;
  } else if (event.name === 'web_search') {
    const q = input.query;
    badge = 'web_search';
    preview = `"${typeof q === 'string' ? q : ''}"`;
  } else if (event.name === 'browser_open') {
    const u = input.url;
    const url = typeof u === 'string' ? u : '';
    badge = 'browser_open';
    preview = url.length > 70 ? url.slice(0, 67) + '…' : url;
  } else {
    badge = event.name;
    preview = JSON.stringify(event.input ?? {});
  }
  return {
    key: `${event.name}:${JSON.stringify(event.input ?? {})}`,
    name: event.name,
    badge,
    preview,
    status: 'pending',
  };
}

/**
 * Patch the first matching pending row to reflect the matching tool
 * result. Returns a NEW array so Svelte reactivity fires; the input is
 * left unchanged.
 */
export function applyResult(current: ToolRow[], event: ToolResultEvent): ToolRow[] {
  const idx = current.findIndex((r) => r.name === event.name && r.status === 'pending');
  if (idx === -1) return current;
  const row = { ...current[idx] };
  row.status = event.is_error ? 'error' : 'ok';
  if (!event.is_error && (event.name === 'delegate_to' || event.name === 'coordinate')) {
    if (event.name === 'delegate_to') {
      row.preview = `${event._agent_slug ?? 'Specialist'} responded`;
    } else {
      row.preview = 'All specialists responded';
    }
  }
  const next = current.slice();
  next[idx] = row;
  return next;
}
