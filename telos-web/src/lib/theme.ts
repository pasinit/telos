/**
 * Theme cycling — port of `cycleTheme()` from the SSR `base.html`.
 *
 * Three states: auto (no localStorage entry, follow system) → dark →
 * light → auto. Mirrors the legacy behaviour so installed PWAs see the
 * same UX after the cutover.
 *
 * The pre-paint script in `index.html` decides initial dark/light to
 * avoid a flash. This module owns user-triggered changes after first
 * paint.
 */

import { writable, type Readable } from 'svelte/store';

export type ThemeMode = 'auto' | 'dark' | 'light';

function readMode(): ThemeMode {
  try {
    if (typeof localStorage === 'undefined') return 'auto';
    const saved = localStorage.getItem('theme');
    if (saved === 'dark' || saved === 'light') return saved;
    return 'auto';
  } catch {
    return 'auto';
  }
}

const _mode = writable<ThemeMode>(readMode());

export const themeMode: Readable<ThemeMode> = {
  subscribe: _mode.subscribe,
};

function systemPrefersDark(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function applyMode(mode: ThemeMode): void {
  if (typeof document === 'undefined') return;
  const isDark = mode === 'dark' || (mode === 'auto' && systemPrefersDark());
  document.documentElement.classList.toggle('dark', isDark);
  // Swap favicon — matches the SSR behaviour.
  const favicon = document.getElementById('favicon') as HTMLLinkElement | null;
  if (favicon) favicon.href = isDark ? '/icon-light.svg' : '/icon-dark.svg';
}

/**
 * Cycle: auto → dark → light → auto.
 *
 * Persists to localStorage (or removes the key for `auto`), updates
 * the `<html class="dark">` toggle, swaps the favicon, and updates the
 * `themeMode` store so any subscribed UI re-renders.
 */
export function cycleTheme(): ThemeMode {
  const current = readMode();
  const next: ThemeMode = current === 'auto' ? 'dark' : current === 'dark' ? 'light' : 'auto';
  try {
    if (typeof localStorage !== 'undefined') {
      if (next === 'auto') localStorage.removeItem('theme');
      else localStorage.setItem('theme', next);
    }
  } catch {
    // localStorage may throw in Safari private mode; the in-memory
    // store still updates so the current tab is consistent.
  }
  applyMode(next);
  _mode.set(next);
  return next;
}

/** Human-readable label for the current mode. */
export function themeLabel(mode: ThemeMode): string {
  switch (mode) {
    case 'dark':
      return 'Dark';
    case 'light':
      return 'Light';
    default:
      return 'Auto';
  }
}

/** Glyph icon for the current mode (matches SSR `base.html`). */
export function themeIcon(mode: ThemeMode): string {
  switch (mode) {
    case 'dark':
      return '☀';
    case 'light':
      return '☾';
    default:
      return '◐';
  }
}
