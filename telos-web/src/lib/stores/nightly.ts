/**
 * Nightly-feedback store — fetches ``GET /api/agents/{slug}/nightly``: the
 * most recent nightly-analysis feedback rows for one agent.
 *
 * Unlike the improvements store this does not poll — the nightly loop runs
 * once a day, so a page mount fetches once. Each instance is bound to one
 * slug; pages create it reactively on slug change and ``stop()`` it on
 * destroy (aborts any in-flight request).
 */
import { writable, type Readable } from 'svelte/store';

import { apiGet } from '../api';

/** One classified recall miss (mirrors the analyst's RecallMiss schema). */
export interface RecallMiss {
  turn_id: string;
  query_used: string;
  classification: 'extraction_gap' | 'ranking_gap' | 'budget_gap' | 'usage_gap' | string;
  detail: string;
}

export interface TurnFailure {
  turn_id: string;
  issue: string;
  evidence: string;
}

/** The analyst's per-agent verdict (mirrors AgentFeedback). */
export interface AgentFeedback {
  score: number;
  summary: string;
  failures: TurnFailure[];
  prompt_suggestions: string[];
  tool_use_issues: string[];
  recall_misses: RecallMiss[];
}

export interface NightlyFeedbackRow {
  id: number;
  run_id: number;
  user_id: string;
  slug: string;
  status: string;
  feedback: AgentFeedback | null;
  error: string | null;
}

export interface NightlyPayload {
  slug: string;
  feedback: NightlyFeedbackRow[];
}

export interface NightlyState {
  data: NightlyPayload | null;
  loading: boolean;
  error: Error | null;
  lastFetched: number | null;
}

export interface NightlyStore extends Readable<NightlyState> {
  refresh(): Promise<void>;
  stop(): void;
}

export function createNightlyStore(slug: string): NightlyStore {
  const store = writable<NightlyState>({ data: null, loading: false, error: null, lastFetched: null });
  let controller: AbortController | null = null;

  async function refresh(): Promise<void> {
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    store.update((s) => ({ ...s, loading: true }));
    try {
      const data = await apiGet<NightlyPayload>(`/agents/${encodeURIComponent(slug)}/nightly`, { signal });
      if (signal.aborted) return;
      store.set({ data, loading: false, error: null, lastFetched: Date.now() });
    } catch (err) {
      if (signal.aborted) return;
      store.update((s) => ({ ...s, loading: false, error: err as Error }));
    }
  }

  function stop(): void {
    controller?.abort();
    controller = null;
  }

  return { subscribe: store.subscribe, refresh, stop };
}
