export interface PersistedTurn {
  turn_id: string;
  assistant_response: string;
}

export interface LiveTurnState {
  turnId: string | null;
  streaming: boolean;
  waiting: boolean;
  empty: boolean;
}

export function reconcileHistoryWithLiveTurn<TTurn extends PersistedTurn, TLive extends LiveTurnState>(
  turns: TTurn[],
  liveTurn: TLive | null,
): { history: TTurn[]; liveTurn: TLive | null } {
  if (!liveTurn?.turnId) {
    return { history: turns, liveTurn };
  }

  const persisted = turns.find((turn) => turn.turn_id === liveTurn.turnId);
  if (!persisted) {
    return { history: turns, liveTurn };
  }

  const liveTurnComplete = !liveTurn.streaming && !liveTurn.waiting;
  const persistedResponseComplete = persisted.assistant_response.trim().length > 0;
  if (liveTurnComplete && (persistedResponseComplete || liveTurn.empty)) {
    return { history: turns, liveTurn: null };
  }

  return {
    history: turns.filter((turn) => turn.turn_id !== liveTurn.turnId),
    liveTurn,
  };
}
