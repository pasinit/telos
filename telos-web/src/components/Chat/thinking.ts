/**
 * Pure-function helpers for the collapsible "Thought for Ns" disclosure
 * shown for a recordatio-path turn that streamed extended-thinking deltas
 * (see `telos/services/agent_runtime_v2.py`).
 *
 * Design contract (replaces the PR #169 throttled-tail line):
 * - While thinking deltas stream and no answer text has arrived yet, a
 *   small "✶ Thinking…" row renders on its OWN line (NOT inside the
 *   answer balloon, which does not exist yet).
 * - The instant the first real answer token arrives, that row collapses
 *   into a compact chip "› Thought for {N}s" sitting just ABOVE the
 *   answer balloon. N = seconds from the first thinking delta (or message
 *   start, whichever is earlier) to the first answer token, rounded.
 * - The chip expands a muted, scrollable panel with the FULL accumulated
 *   thinking text. Collapsed by default.
 * - A turn with no thinking deltas shows no chip at all.
 *
 * The FULL thinking text is accumulated here (unlike #169, which kept
 * only an 80-char tail) so "expand" can show everything. It lives only
 * on the in-memory message for the session — never persisted to the
 * transcript / sqlite log / full_response / model context.
 *
 * Lives in its own file so `Chat.svelte` stays a thin consumer and the
 * accumulation / duration arithmetic is trivially unit-testable — the
 * same split `recordatio-briefing.ts` uses.
 */

/**
 * Minimum ms between DOM updates of the streaming "Thinking…" row. Deltas
 * can arrive at ≥10/s; the row carries no per-delta information (just a
 * spinner), so we repaint at most ~5/s.
 */
export const THINKING_FLUSH_MS = 200;

/**
 * Ephemeral thinking state carried on the in-flight message for one turn,
 * then pinned in the session map below (keyed by turn_id) so the chip
 * survives the live turn being swapped for its persisted history row.
 * Never persisted; gone on reload — unlike the briefing chip, which is
 * backed by the persisted `has_request_id` flag and so survives reloads.
 */
export interface ThinkingState {
  /** FULL accumulated thinking text (for the expand panel). */
  text: string;
  /**
   * Timestamp (ms) of the first thinking delta, or the message start if
   * that was recorded earlier. `null` until the first delta arrives.
   */
  startedAt: number | null;
  /**
   * Duration in whole seconds from `startedAt` to the first answer token.
   * `null` until the first answer token collapses the row.
   */
  durationSec: number | null;
}

/** A fresh thinking-state scratchpad for a new turn. */
export function initialThinkingState(): ThinkingState {
  return { text: '', startedAt: null, durationSec: null };
}

/**
 * Append one thinking delta, retaining the FULL accumulated text and
 * stamping the start time on the first delta. `now`/`messageStart` are
 * injected for testability (default to `Date.now()` / undefined).
 *
 * `messageStart`, when supplied and earlier than the first delta, is used
 * as the duration anchor so N reflects the whole silent pre-text phase.
 */
export function appendThinking(
  state: ThinkingState,
  delta: string,
  now: number = Date.now(),
  messageStart?: number,
): ThinkingState {
  let startedAt = state.startedAt;
  if (startedAt === null) {
    startedAt = messageStart !== undefined && messageStart < now ? messageStart : now;
  }
  return { ...state, text: state.text + delta, startedAt };
}

/**
 * Collapse the thinking row the moment the first answer token arrives:
 * compute the rounded-seconds duration from `startedAt` to `now`. A no-op
 * (returns the state unchanged) when no thinking ever streamed or the
 * duration was already computed.
 */
export function collapseThinking(state: ThinkingState, now: number = Date.now()): ThinkingState {
  if (state.startedAt === null || state.durationSec !== null) return state;
  const durationSec = Math.max(0, Math.round((now - state.startedAt) / 1000));
  return { ...state, durationSec };
}

/** True when this turn streamed at least one thinking delta. */
export function hasThinking(state: ThinkingState): boolean {
  return state.startedAt !== null;
}

// ─── Session-scoped thinking memory ──────────────────────────────────
//
// The live turn that accumulates `ThinkingState` is DROPPED once the
// persisted row arrives from `/turns` (see `history.ts`:
// `reconcileHistoryWithLiveTurn` returns `liveTurn: null`), and persisted
// turns deliberately carry no thinking (never written to the transcript /
// sqlite log / full_response). Without a side channel the chip would
// unmount the moment the answer finishes printing.
//
// This module-scoped map IS that side channel: collapsed thinking state
// keyed by the stable `turn_id`, so the disclosure re-attaches to the
// refetched history row. Module scope means it survives stream
// completion, post-turn history refetches, and Chat component
// remounts (switching to another agent's chat and back within the SPA
// session) — and is wiped by a page reload, the accepted persistence
// boundary (thinking text + duration are session-ephemeral).

const sessionThinkingByTurn = new Map<string, ThinkingState>();

/**
 * Pin a turn's thinking state for the rest of the browser session. A
 * no-op unless the state is a finished chip — at least one delta streamed
 * AND the duration was computed — so an interrupted pre-collapse stream
 * never leaves a half-built "Thinking…" row attached to history.
 */
export function rememberThinking(turnId: string, state: ThinkingState): void {
  if (!hasThinking(state) || state.durationSec === null) return;
  sessionThinkingByTurn.set(turnId, state);
}

/**
 * The remembered thinking state for a persisted turn, or `null` for turns
 * that streamed no thinking (or finished in a previous page load).
 */
export function recallThinking(turnId: string): ThinkingState | null {
  return sessionThinkingByTurn.get(turnId) ?? null;
}
