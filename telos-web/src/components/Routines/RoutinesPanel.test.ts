/**
 * Behavioural smoke tests for RoutinesPanel.svelte.
 *
 * Same pattern as Shell.test.ts — no @testing-library DOM rendering;
 * tests pin the pure-data behaviours the panel relies on (interval
 * preset list, time-of-day null semantics, display-name fallback).
 *
 * The component itself is exercised by manual smoke (settings page
 * loads + a routine round-trip).
 */
import { describe, expect, it } from 'vitest';
import type { RoutineRow } from '../Settings/types';

function makeRoutine(over: Partial<RoutineRow> = {}): RoutineRow {
  return {
    id: 'r-1',
    user_id: 'tommaso',
    agent_slug: 'cooking',
    name: '',
    description: 'Daily recipe roundup',
    interval_seconds: 86400,
    time_of_day: '09:00',
    one_shot: false,
    notify_email: false,
    enabled: true,
    last_run: null,
    created_at: '2026-05-17T12:00:00Z',
    next_run: '2026-05-18 09:00',
    ...over,
  };
}

// Mirror the constant in the component — keep in sync.
const INTERVAL_PRESETS = [300, 1800, 3600, 21600, 43200, 86400, 604800];

describe('RoutinesPanel pure-data behaviour', () => {
  it('preset interval is rendered as-is, no custom row added', () => {
    const r = makeRoutine({ interval_seconds: 86400 });
    const opts = INTERVAL_PRESETS.includes(r.interval_seconds)
      ? INTERVAL_PRESETS
      : [...INTERVAL_PRESETS, r.interval_seconds];
    expect(opts).toEqual(INTERVAL_PRESETS);
  });

  it('non-preset interval surfaces a custom option so the select keeps the value', () => {
    const r = makeRoutine({ interval_seconds: 4242 });
    const opts = INTERVAL_PRESETS.includes(r.interval_seconds)
      ? INTERVAL_PRESETS
      : [...INTERVAL_PRESETS, r.interval_seconds];
    expect(opts).toContain(4242);
    expect(opts[opts.length - 1]).toBe(4242);
  });

  it('clearing time_of_day sends null, not empty string', () => {
    // Mirror the onTimeOfDay handler — empty string input → null body.
    const input = '';
    const body = { time_of_day: input || null };
    expect(body.time_of_day).toBeNull();
  });

  it('setting a time_of_day passes through verbatim', () => {
    const input = '07:30';
    const body = { time_of_day: input || null };
    expect(body.time_of_day).toBe('07:30');
  });

  it('display name prefers explicit name over description', () => {
    const r = makeRoutine({ name: 'Morning roundup', description: 'Daily recipe roundup\nbla bla' });
    const display = r.name || r.description.split('\n')[0].slice(0, 60);
    expect(display).toBe('Morning roundup');
  });

  it('display name falls back to first line of description truncated to 60', () => {
    const r = makeRoutine({
      name: '',
      description:
        'This is a really long routine description that should be truncated at sixty chars not full body.',
    });
    const display = r.name || r.description.split('\n')[0].slice(0, 60);
    expect(display).toHaveLength(60);
    expect(display.startsWith('This is a really long routine description')).toBe(true);
  });

  it('display name uses only the first line of a multi-line description', () => {
    const r = makeRoutine({
      name: '',
      description: 'Line one of the description\nLine two should not appear',
    });
    const display = r.name || r.description.split('\n')[0].slice(0, 60);
    expect(display).toBe('Line one of the description');
    expect(display).not.toContain('Line two');
  });

  it('notify_email PATCH body is boolean, not "on"/"off"', () => {
    // Browser checkbox `event.target.checked` is boolean; we forward
    // verbatim. The legacy SSR sent {notify_email: true/false} too —
    // backend expects boolean.
    const body = { notify_email: true };
    expect(typeof body.notify_email).toBe('boolean');
  });

  it('pending rows get Activate/Dismiss, active rows get Delete', () => {
    // Mirrors the action-cell branch: r.enabled selects the control set.
    // Pending proposals (enabled=false rows the agent staged) must be
    // resolvable from the panel — chat pills from past turns are gone.
    const actions = (r: RoutineRow) => (r.enabled ? ['Delete'] : ['Activate', 'Dismiss']);
    expect(actions(makeRoutine({ enabled: false, next_run: null }))).toEqual([
      'Activate',
      'Dismiss',
    ]);
    expect(actions(makeRoutine({ enabled: true }))).toEqual(['Delete']);
  });

  it('activate flips the row to enabled in place; dismiss removes it', () => {
    // Mirrors the optimistic updates in onActivate / onDismiss.
    const rows = [
      makeRoutine({ id: 'a', enabled: false, next_run: null }),
      makeRoutine({ id: 'b', enabled: false, next_run: null }),
    ];
    const afterActivate = rows.map((r) => (r.id === 'a' ? { ...r, enabled: true } : r));
    expect(afterActivate.find((r) => r.id === 'a')?.enabled).toBe(true);
    expect(afterActivate.find((r) => r.id === 'b')?.enabled).toBe(false);

    const afterDismiss = afterActivate.filter((r) => r.id !== 'b');
    expect(afterDismiss.map((r) => r.id)).toEqual(['a']);
  });

  it('activate body forwards the row notify_email, defaulting false', () => {
    // Mirrors onActivate: the inline checkbox state rides along so
    // activation does not silently reset email delivery.
    const row = makeRoutine({ enabled: false, notify_email: true });
    expect({ notify_email: row?.notify_email ?? false }).toEqual({ notify_email: true });
    const missing = undefined as RoutineRow | undefined;
    expect({ notify_email: missing?.notify_email ?? false }).toEqual({ notify_email: false });
  });
});
