/**
 * Toast notification queue.
 *
 * Added in PR 7D to surface "Google connected"/"Google failed" feedback
 * after the OAuth round-trip strips the SPA's in-memory state. The
 * primer in `/tmp/phase7-expertise/oauth-redirect-spa.md` mandates a
 * toast affordance for this flow.
 *
 * Minimal API: `showToast(message, { kind?, timeoutMs? })`. The actual
 * rendering lives in `components/common/Toast.svelte`, which subscribes
 * to the store and pops items after `timeoutMs`.
 *
 * Used by ≥2 pages → lives in `lib/` (per CONVENTIONS §3).
 */

import { writable, type Readable } from 'svelte/store';

export type ToastKind = 'success' | 'error' | 'info';

export interface Toast {
  id: number;
  message: string;
  kind: ToastKind;
  timeoutMs: number;
}

export interface ShowToastOptions {
  kind?: ToastKind;
  /** Auto-dismiss timeout in ms. Default 4000. Set to 0 to keep until dismissed. */
  timeoutMs?: number;
}

const _toasts = writable<Toast[]>([]);

export const toasts: Readable<Toast[]> = {
  subscribe: _toasts.subscribe,
};

let _nextId = 1;

export function showToast(message: string, opts: ShowToastOptions = {}): number {
  const id = _nextId++;
  const toast: Toast = {
    id,
    message,
    kind: opts.kind ?? 'info',
    timeoutMs: opts.timeoutMs ?? 4000,
  };
  _toasts.update((list) => [...list, toast]);
  return id;
}

export function dismissToast(id: number): void {
  _toasts.update((list) => list.filter((t) => t.id !== id));
}

/** Test helper — clear the queue between specs. */
export function _clearToastsForTesting(): void {
  _toasts.set([]);
  _nextId = 1;
}
