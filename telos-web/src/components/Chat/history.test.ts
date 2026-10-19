import { describe, expect, it } from 'vitest';
import { reconcileHistoryWithLiveTurn } from './history';

const baseLiveTurn = {
  turnId: 'turn-2',
  streaming: true,
  waiting: false,
  empty: false,
};

const turns = [
  { turn_id: 'turn-1', assistant_response: 'older response' },
  { turn_id: 'turn-2', assistant_response: '' },
];

describe('reconcileHistoryWithLiveTurn', () => {
  it('keeps an active live turn out of history when persistence has only the empty shell', () => {
    const result = reconcileHistoryWithLiveTurn(turns, baseLiveTurn);

    expect(result.history).toEqual([{ turn_id: 'turn-1', assistant_response: 'older response' }]);
    expect(result.liveTurn).toBe(baseLiveTurn);
  });

  it('clears the live turn once the completed persisted response is available', () => {
    const liveTurn = { ...baseLiveTurn, streaming: false };
    const result = reconcileHistoryWithLiveTurn(
      [
        turns[0],
        { turn_id: 'turn-2', assistant_response: 'new response' },
      ],
      liveTurn,
    );

    expect(result.history).toEqual([
      { turn_id: 'turn-1', assistant_response: 'older response' },
      { turn_id: 'turn-2', assistant_response: 'new response' },
    ]);
    expect(result.liveTurn).toBeNull();
  });

  it('keeps a completed live turn visible until its persisted row arrives', () => {
    const liveTurn = { ...baseLiveTurn, streaming: false };
    const result = reconcileHistoryWithLiveTurn([turns[0]], liveTurn);

    expect(result.history).toEqual([turns[0]]);
    expect(result.liveTurn).toBe(liveTurn);
  });

  it('does not filter history when there is no live turn id yet', () => {
    const liveTurn = { ...baseLiveTurn, turnId: null };
    const result = reconcileHistoryWithLiveTurn(turns, liveTurn);

    expect(result.history).toBe(turns);
    expect(result.liveTurn).toBe(liveTurn);
  });
});
