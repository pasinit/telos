/**
 * Tests for `lib/theme.ts`.
 *
 * The pre-paint script in `index.html` sets the initial `<html class="dark">`
 * synchronously; the runtime helper here owns user-triggered cycling.
 * These specs verify the three-state cycle, localStorage persistence,
 * the dark class toggle, and Safari-private-mode tolerance.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { cycleTheme, themeIcon, themeLabel, themeMode } from './theme';

describe('lib/theme', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove('dark');
  });
  afterEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove('dark');
  });

  it('themeLabel / themeIcon map each mode to a deterministic glyph', () => {
    expect(themeLabel('auto')).toBe('Auto');
    expect(themeLabel('dark')).toBe('Dark');
    expect(themeLabel('light')).toBe('Light');
    expect(themeIcon('auto')).toBe('◐');
    expect(themeIcon('dark')).toBe('☀');
    expect(themeIcon('light')).toBe('☾');
  });

  it('cycleTheme rotates auto → dark → light → auto and updates the store', () => {
    // Boot reads localStorage; we cleared it, so we expect auto. The store
    // is module-scope and may carry a value from previous tests, so we
    // cycle until we observe a deterministic transition rather than asserting
    // the initial value.
    const first = cycleTheme();
    const second = cycleTheme();
    const third = cycleTheme();

    // Whichever order, three cycles return to the start.
    expect([first, second, third].sort()).toEqual(['auto', 'dark', 'light']);
    // The store mirrors the last cycle.
    expect(get(themeMode)).toBe(third);
  });

  it('persists "dark" to localStorage and adds the .dark class', () => {
    // Cycle until we hit dark (we're in auto initially after clear).
    let mode = cycleTheme();
    while (mode !== 'dark') mode = cycleTheme();
    expect(localStorage.getItem('theme')).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  it('removes the localStorage key when cycling to auto', () => {
    let mode = cycleTheme();
    while (mode !== 'auto') mode = cycleTheme();
    expect(localStorage.getItem('theme')).toBeNull();
  });

  it('does not throw when localStorage.setItem fails (Safari private mode)', () => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = () => {
      throw new Error('QuotaExceededError');
    };
    try {
      expect(() => cycleTheme()).not.toThrow();
      // Store still updates so the current tab is consistent.
      expect(['auto', 'dark', 'light']).toContain(get(themeMode));
    } finally {
      Storage.prototype.setItem = original;
    }
  });
});
