import { describe, expect, it, vi } from 'vitest';
import {
  appendThinking,
  collapseThinking,
  hasThinking,
  initialThinkingState,
  recallThinking,
  rememberThinking,
} from './thinking';
import { reconcileHistoryWithLiveTurn } from './history';

describe('appendThinking', () => {
  it('accumulates the FULL thinking text (not a tail)', () => {
    let s = initialThinkingState();
    s = appendThinking(s, 'The user wants ', 1000);
    s = appendThinking(s, 'a summary.', 1010);
    expect(s.text).toBe('The user wants a summary.');
  });

  it('retains very long thinking verbatim (no truncation)', () => {
    let s = initialThinkingState();
    let expected = '';
    for (let i = 0; i < 200; i++) {
      const chunk = `chunk-${i} `;
      s = appendThinking(s, chunk, 1000 + i);
      expected += chunk;
    }
    expect(s.text).toBe(expected);
    expect(s.text.length).toBeGreaterThan(1000);
  });

  it('stamps startedAt on the first delta and keeps it on later deltas', () => {
    let s = initialThinkingState();
    expect(s.startedAt).toBeNull();
    s = appendThinking(s, 'a', 5000);
    expect(s.startedAt).toBe(5000);
    s = appendThinking(s, 'b', 9000);
    expect(s.startedAt).toBe(5000);
  });

  it('anchors startedAt to an earlier message start when supplied', () => {
    let s = initialThinkingState();
    s = appendThinking(s, 'a', 5000, 4200);
    expect(s.startedAt).toBe(4200);
  });

  it('ignores a message start that is later than the first delta', () => {
    let s = initialThinkingState();
    s = appendThinking(s, 'a', 5000, 6000);
    expect(s.startedAt).toBe(5000);
  });
});

describe('collapseThinking', () => {
  it('computes duration from first-thinking to first-answer, rounded', () => {
    let s = initialThinkingState();
    s = appendThinking(s, 'reasoning', 1000);
    s = collapseThinking(s, 4600); // 3.6s → 4
    expect(s.durationSec).toBe(4);
  });

  it('rounds sub-second durations to 0', () => {
    let s = initialThinkingState();
    s = appendThinking(s, 'x', 1000);
    s = collapseThinking(s, 1300);
    expect(s.durationSec).toBe(0);
  });

  it('is a no-op when no thinking ever streamed', () => {
    const s = collapseThinking(initialThinkingState(), 9999);
    expect(s.durationSec).toBeNull();
    expect(s.startedAt).toBeNull();
  });

  it('does not recompute once collapsed', () => {
    let s = initialThinkingState();
    s = appendThinking(s, 'x', 1000);
    s = collapseThinking(s, 3000); // 2s
    s = collapseThinking(s, 9000); // would be 8s if recomputed
    expect(s.durationSec).toBe(2);
  });
});

describe('hasThinking', () => {
  it('is false for a fresh state and a no-thinking turn', () => {
    expect(hasThinking(initialThinkingState())).toBe(false);
  });

  it('is true once any delta has arrived', () => {
    const s = appendThinking(initialThinkingState(), 'x', 1000);
    expect(hasThinking(s)).toBe(true);
  });
});

describe('session thinking memory (rememberThinking / recallThinking)', () => {
  function collapsedState(text = 'deep reasoning'): ReturnType<typeof initialThinkingState> {
    let s = initialThinkingState();
    s = appendThinking(s, text, 1000);
    return collapseThinking(s, 4000); // 3s
  }

  it('chip data is recallable after the done frame pins it', () => {
    rememberThinking('turn-done', collapsedState());
    const recalled = recallThinking('turn-done');
    expect(recalled).not.toBeNull();
    expect(recalled!.text).toBe('deep reasoning');
    expect(recalled!.durationSec).toBe(3);
  });

  it('survives a history refetch that drops the live turn (reconcile → liveTurn null)', () => {
    // The exact post-turn sequence: chip pinned on done, THEN
    // refreshHistory swaps the live turn for its persisted row.
    rememberThinking('turn-2', collapsedState('thought hard'));
    const result = reconcileHistoryWithLiveTurn(
      [{ turn_id: 'turn-2', assistant_response: 'final answer' }],
      { turnId: 'turn-2', streaming: false, waiting: false, empty: false },
    );
    expect(result.liveTurn).toBeNull(); // the old chip carrier is gone…
    expect(recallThinking('turn-2')?.text).toBe('thought hard'); // …the chip is not.
  });

  it('keeps thinking out of persisted turn content (side channel only)', () => {
    const persisted = { turn_id: 'turn-3', assistant_response: 'answer' };
    rememberThinking('turn-3', collapsedState('secret reasoning'));
    const result = reconcileHistoryWithLiveTurn(
      [persisted],
      { turnId: 'turn-3', streaming: false, waiting: false, empty: false },
    );
    // The persisted row is returned untouched — no thinking field grafted on.
    expect(result.history[0]).toBe(persisted);
    expect(Object.keys(result.history[0])).toEqual(['turn_id', 'assistant_response']);
    expect(result.history[0].assistant_response).not.toContain('secret reasoning');
  });

  it('refuses to pin a turn that streamed no thinking', () => {
    rememberThinking('turn-no-thinking', initialThinkingState());
    expect(recallThinking('turn-no-thinking')).toBeNull();
  });

  it('refuses to pin a pre-collapse state (no half-built chip after an interrupted stream)', () => {
    const s = appendThinking(initialThinkingState(), 'partial', 1000); // durationSec still null
    rememberThinking('turn-interrupted', s);
    expect(recallThinking('turn-interrupted')).toBeNull();
  });

  it('overwrites with the latest pinned state (idempotent per-frame pinning)', () => {
    rememberThinking('turn-twice', collapsedState('first'));
    rememberThinking('turn-twice', collapsedState('first plus more'));
    expect(recallThinking('turn-twice')?.text).toBe('first plus more');
  });

  it('is empty on a fresh page load (module re-evaluation clears the map)', async () => {
    rememberThinking('turn-reload', collapsedState());
    expect(recallThinking('turn-reload')).not.toBeNull();
    vi.resetModules(); // simulate the new-page-load module graph
    const fresh = await import('./thinking');
    expect(fresh.recallThinking('turn-reload')).toBeNull();
  });
});
