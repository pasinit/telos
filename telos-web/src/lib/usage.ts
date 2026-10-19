/**
 * Presentational helpers for the Usage page.
 *
 * Ports the small aggregations the SSR `/usage` page does inline (total
 * call count across sources, average cost per call, the auto-open top-3
 * accordion behaviour) into pure functions so they can be unit-tested
 * without spinning up the DOM.
 *
 * The wire format mirrors `telos/services/cost_rollup.py::CostRollupService.rollup`
 * exactly — every field is what FastAPI emits, no rename.
 */

// ──────────────────────────────────────────────────────────────────────
// Wire types — match `CostRollupService.rollup` 1:1.
// ──────────────────────────────────────────────────────────────────────

export interface SourceTotals {
  total_usd: number;
  calls: number;
}

export interface ModelTotals {
  total_usd: number;
  calls: number;
}

export interface AgentRollup {
  slug: string;
  name: string;
  total_usd: number;
  by_source: Record<string, SourceTotals>;
  by_model: Record<string, ModelTotals>;
}

export interface UsageRollup {
  since: string | null;
  total_usd: number;
  by_agent: AgentRollup[];
}

// `/agents/{slug}/costs/recent` payload — used by the recent-events panel.
export interface CostEvent {
  ts: string;
  source: string;
  model: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  turn_id: string | null;
}

export interface RecentEventsResponse {
  slug: string;
  limit: number;
  events: CostEvent[];
}

// ──────────────────────────────────────────────────────────────────────
// Range tokens — must match `telos/services/cost_rollup.py::_USAGE_RANGES`
// plus the implicit "all" passthrough.
// ──────────────────────────────────────────────────────────────────────

export type RangeToken = '24h' | '7d' | '30d' | 'all';

export const RANGE_OPTIONS: { token: RangeToken; label: string }[] = [
  { token: '24h', label: '24h' },
  { token: '7d', label: '7d' },
  { token: '30d', label: '30d' },
  { token: 'all', label: 'All' },
];

const VALID_RANGES = new Set<string>(['24h', '7d', '30d', 'all']);

/**
 * Normalise an arbitrary `?since=` querystring into a known range token.
 *
 * Mirrors `resolve_since` server-side: unknown / missing values fall back
 * to `30d` (the default window the SSR page used). Returned value is
 * always a valid `RangeToken` so the UI can light up the right pill.
 */
export function normaliseRange(value: string | null | undefined): RangeToken {
  const token = (value ?? '').trim().toLowerCase();
  if (token === '' || token === 'all-time') return 'all';
  if (VALID_RANGES.has(token)) return token as RangeToken;
  return '30d';
}

// ──────────────────────────────────────────────────────────────────────
// Aggregations — ported from `usage.html` Jinja inline arithmetic.
// ──────────────────────────────────────────────────────────────────────

/**
 * Sum every `calls` count across every source on every agent.
 *
 * The SSR template walked `rollup.by_agent[*].by_source.*.calls`; we
 * preserve the same total so the "N calls" KPI matches bit-for-bit.
 */
export function totalCalls(rollup: UsageRollup): number {
  let n = 0;
  for (const agent of rollup.by_agent) {
    for (const src of Object.values(agent.by_source)) {
      n += Number(src.calls) || 0;
    }
  }
  return n;
}

/**
 * Average $ per call across the whole rollup. Returns 0 on an empty
 * window so the KPI block doesn't render `$NaN/call`.
 */
export function avgCostPerCall(rollup: UsageRollup): number {
  const calls = totalCalls(rollup);
  if (calls === 0) return 0;
  return rollup.total_usd / calls;
}

/**
 * Sum every `calls` count for one agent (sidebar copy of the inline
 * Jinja walk so the per-agent "N calls" suffix stays consistent).
 */
export function agentCallCount(agent: AgentRollup): number {
  let n = 0;
  for (const src of Object.values(agent.by_source)) {
    n += Number(src.calls) || 0;
  }
  return n;
}

/**
 * Decide which agent accordions default-open.
 *
 * The SSR rule (verbatim from `routes/costs.py::usage_page`): the top-3
 * spenders auto-open, plus an explicit `?open=<slug>` from the URL.
 * Agents with `total_usd <= 0` never auto-open even if they're in the
 * top-3 by virtue of being the only entries (empty windows).
 */
export function autoOpenSlugs(rollup: UsageRollup, openSlug?: string | null): Set<string> {
  const slugs = new Set<string>();
  for (const agent of rollup.by_agent.slice(0, 3)) {
    if (agent.total_usd > 0) slugs.add(agent.slug);
  }
  if (openSlug) slugs.add(openSlug);
  return slugs;
}

// ──────────────────────────────────────────────────────────────────────
// Source colours — mirror `_USAGE_SOURCE_COLORS` in cost_rollup.py.
// Kept in sync manually; the test below pins the mapping so a server
// change without a client update fails CI.
// ──────────────────────────────────────────────────────────────────────

export const SOURCE_COLORS: Record<string, string> = {
  chat: '#6366f1',
  improver_research: '#fbbf24',
  improver_replay: '#f59e0b',
  improver_judge: '#d97706',
  signal_classify: '#14b8a6',
  summary: '#8b5cf6',
  goal_gen: '#ec4899',
  recordatio_extraction: '#10b981',
};

export const FALLBACK_SOURCE_COLOR = '#9ca3af';

export function sourceColor(src: string): string {
  return SOURCE_COLORS[src] ?? FALLBACK_SOURCE_COLOR;
}

export interface SourceLegendEntry {
  src: string;
  color: string;
}

export function sourceLegend(): SourceLegendEntry[] {
  return Object.entries(SOURCE_COLORS).map(([src, color]) => ({ src, color }));
}

// ──────────────────────────────────────────────────────────────────────
// Per-agent spend bar — `sparkline` segments for the stacked colour bar.
// ──────────────────────────────────────────────────────────────────────

export interface SpendSegment {
  src: string;
  color: string;
  /** % of this agent's `total_usd` this source represents (0–100). */
  widthPct: number;
  total_usd: number;
  calls: number;
}

/**
 * Build the stacked-segment list for one agent.
 *
 * The bar's overall width is the agent's spend relative to the largest
 * spender — handled in the component. Each segment's width is the
 * source's share of THIS agent's spend, so the bar always sums to 100%.
 * Segments contributing 0% are dropped (the SSR template applied the
 * same filter to avoid invisible-but-present DOM nodes).
 */
export function spendSegments(agent: AgentRollup): SpendSegment[] {
  if (agent.total_usd <= 0) return [];
  const segments: SpendSegment[] = [];
  for (const [src, data] of Object.entries(agent.by_source)) {
    const total = Number(data.total_usd) || 0;
    if (total <= 0) continue;
    segments.push({
      src,
      color: sourceColor(src),
      widthPct: (total / agent.total_usd) * 100,
      total_usd: total,
      calls: Number(data.calls) || 0,
    });
  }
  // Sort largest → smallest so the bar reads predictably left-to-right.
  segments.sort((a, b) => b.total_usd - a.total_usd);
  return segments;
}

/**
 * Bar width % for one agent within the per-agent ranking.
 *
 * The SSR template uses `entry.total_usd / max_total * 100` where
 * `max_total = rollup.by_agent[0].total_usd` (top spender). With a 4px
 * `min-width` floor handled in CSS, the visual ratio matches the
 * numeric one.
 */
export function relativeWidthPct(agent: AgentRollup, maxTotal: number): number {
  if (maxTotal <= 0) return 0;
  return (agent.total_usd / maxTotal) * 100;
}

// ──────────────────────────────────────────────────────────────────────
// By-model / by-source sort helpers (largest first), used by the
// breakdown panels inside each agent accordion.
// ──────────────────────────────────────────────────────────────────────

export interface NamedTotals {
  key: string;
  total_usd: number;
  calls: number;
}

export function sortedSources(agent: AgentRollup): NamedTotals[] {
  return Object.entries(agent.by_source)
    .map(([key, data]) => ({ key, total_usd: Number(data.total_usd) || 0, calls: Number(data.calls) || 0 }))
    .sort((a, b) => b.total_usd - a.total_usd);
}

export function sortedModels(agent: AgentRollup): NamedTotals[] {
  return Object.entries(agent.by_model)
    .map(([key, data]) => ({ key, total_usd: Number(data.total_usd) || 0, calls: Number(data.calls) || 0 }))
    .sort((a, b) => b.total_usd - a.total_usd);
}

// ──────────────────────────────────────────────────────────────────────
// Formatting — kept in this module so component code stays declarative.
// ──────────────────────────────────────────────────────────────────────

/** `$0.0123` — 4 decimal places, the SSR per-agent default. */
export function formatUsd(amount: number, places = 4): string {
  return `$${amount.toFixed(places)}`;
}

/** Pluralise "call" — matches `{{ "" if n == 1 else "s" }}` Jinja idiom. */
export function pluraliseCalls(n: number): string {
  return `${n} call${n === 1 ? '' : 's'}`;
}
