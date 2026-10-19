/**
 * Improvements store — polls ``GET /api/agents/{slug}/improvements`` so a
 * new run from a background improver shows up without a manual refresh.
 *
 * The SSR page rendered the snapshot once and required reload. The SPA
 * page keeps the data live by refetching every 15 s by default. Each
 * store instance is bound to a single agent slug; pages create one in
 * ``onMount`` and ``stop()`` it in ``onDestroy``.
 *
 * Decision filter changes go through ``setDecision()`` so the in-flight
 * request can be aborted before issuing the new one — avoids a stale
 * response clobbering the freshly filtered list.
 */
import { writable, type Readable } from 'svelte/store';

import { ApiError, apiGet } from '../api';

export const DECISIONS = [
  'pending',
  'approved',
  'rejected',
  'no_improvement',
  'error',
] as const;
export type Decision = (typeof DECISIONS)[number];

export interface ImprovementRun {
  id: number;
  agent_slug: string;
  started_at: string;
  finished_at: string | null;
  model: string | null;
  decision: Decision | string | null;
  current_sha: string | null;
  proposed_sha: string | null;
  rationale: string | null;
  proposed_diff: string | null;
  score_delta: number | null;
  judge_summary: string | null;
  error: string | null;
}

export interface ImprovementsPayload {
  slug: string;
  limit: number;
  offset: number;
  runs: ImprovementRun[];
}

export interface ImprovementsState {
  data: ImprovementsPayload | null;
  loading: boolean;
  error: Error | null;
  lastFetched: number | null;
}

export interface ImprovementsStore extends Readable<ImprovementsState> {
  refresh(): Promise<void>;
  start(intervalMs?: number): void;
  stop(): void;
  setDecision(decision: Decision | null): void;
  getDecision(): Decision | null;
}

/** Default poll interval. Matches the chat status poller's cadence. */
export const DEFAULT_POLL_INTERVAL_MS = 15_000;

export function createImprovementsStore(
  slug: string,
  initialDecision: Decision | null = null,
): ImprovementsStore {
  const store = writable<ImprovementsState>({
    data: null,
    loading: false,
    error: null,
    lastFetched: null,
  });

  let decision: Decision | null = initialDecision;
  let timer: ReturnType<typeof setInterval> | null = null;
  let aborter: AbortController | null = null;

  function buildPath(): string {
    const qs = new URLSearchParams();
    if (decision) qs.set('decision', decision);
    qs.set('limit', '200');
    return `/agents/${encodeURIComponent(slug)}/improvements?${qs.toString()}`;
  }

  async function refresh(): Promise<void> {
    aborter?.abort();
    aborter = new AbortController();
    const signal = aborter.signal;
    store.update((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await apiGet<ImprovementsPayload>(buildPath(), { signal });
      if (signal.aborted) return;
      store.update((s) => ({
        ...s,
        data,
        loading: false,
        error: null,
        lastFetched: Date.now(),
      }));
    } catch (e) {
      // Treat aborts as no-ops — they happen on filter changes / unmount.
      if (signal.aborted) return;
      if (e instanceof DOMException && e.name === 'AbortError') return;
      const err = e instanceof Error ? e : new ApiError(0, null, String(e));
      store.update((s) => ({ ...s, loading: false, error: err }));
    }
  }

  function start(intervalMs: number = DEFAULT_POLL_INTERVAL_MS): void {
    if (timer !== null) return;
    timer = setInterval(() => {
      // Fire-and-forget; refresh() captures its own errors.
      void refresh();
    }, intervalMs);
  }

  function stop(): void {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
    aborter?.abort();
    aborter = null;
  }

  function setDecision(value: Decision | null): void {
    decision = value;
    void refresh();
  }

  function getDecision(): Decision | null {
    return decision;
  }

  return {
    subscribe: store.subscribe,
    refresh,
    start,
    stop,
    setDecision,
    getDecision,
  };
}
