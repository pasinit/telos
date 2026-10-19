import { describe, expect, it } from 'vitest';
import {
  type AgentRollup,
  type UsageRollup,
  agentCallCount,
  autoOpenSlugs,
  avgCostPerCall,
  FALLBACK_SOURCE_COLOR,
  formatUsd,
  normaliseRange,
  pluraliseCalls,
  relativeWidthPct,
  SOURCE_COLORS,
  sortedModels,
  sortedSources,
  sourceColor,
  spendSegments,
  totalCalls,
} from './usage';

function mkAgent(
  slug: string,
  total: number,
  bySource: Record<string, { total_usd: number; calls: number }> = {},
  byModel: Record<string, { total_usd: number; calls: number }> = {},
): AgentRollup {
  return { slug, name: slug, total_usd: total, by_source: bySource, by_model: byModel };
}

function mkRollup(agents: AgentRollup[], since: string | null = null): UsageRollup {
  const total = agents.reduce((acc, a) => acc + a.total_usd, 0);
  return { since, total_usd: total, by_agent: agents };
}

describe('normaliseRange', () => {
  it('passes through known tokens', () => {
    expect(normaliseRange('24h')).toBe('24h');
    expect(normaliseRange('7d')).toBe('7d');
    expect(normaliseRange('30d')).toBe('30d');
    expect(normaliseRange('all')).toBe('all');
  });
  it('coerces empty + all-time to "all"', () => {
    expect(normaliseRange('')).toBe('all');
    expect(normaliseRange(null)).toBe('all');
    expect(normaliseRange(undefined)).toBe('all');
    expect(normaliseRange('all-time')).toBe('all');
  });
  it('falls back to 30d on unknown tokens', () => {
    expect(normaliseRange('forever')).toBe('30d');
    expect(normaliseRange('1m')).toBe('30d');
  });
  it('is case-insensitive', () => {
    expect(normaliseRange('24H')).toBe('24h');
    expect(normaliseRange(' 7D ')).toBe('7d');
  });
});

describe('totalCalls / avgCostPerCall', () => {
  it('sums calls across every source on every agent', () => {
    const rollup = mkRollup([
      mkAgent('a', 1.0, { chat: { total_usd: 0.8, calls: 4 }, summary: { total_usd: 0.2, calls: 2 } }),
      mkAgent('b', 0.5, { chat: { total_usd: 0.5, calls: 3 } }),
    ]);
    expect(totalCalls(rollup)).toBe(9);
  });
  it('returns 0 for an empty rollup', () => {
    expect(totalCalls(mkRollup([]))).toBe(0);
    expect(totalCalls(mkRollup([mkAgent('a', 0)]))).toBe(0);
  });
  it('avgCostPerCall returns 0 instead of NaN for empty windows', () => {
    expect(avgCostPerCall(mkRollup([]))).toBe(0);
    expect(avgCostPerCall(mkRollup([mkAgent('a', 0)]))).toBe(0);
  });
  it('avgCostPerCall divides total by call count', () => {
    const rollup = mkRollup([
      mkAgent('a', 1.0, { chat: { total_usd: 1.0, calls: 4 } }),
    ]);
    expect(avgCostPerCall(rollup)).toBeCloseTo(0.25, 6);
  });
});

describe('agentCallCount', () => {
  it('sums calls across all sources on one agent', () => {
    const a = mkAgent('a', 1.0, {
      chat: { total_usd: 0.5, calls: 3 },
      summary: { total_usd: 0.5, calls: 2 },
    });
    expect(agentCallCount(a)).toBe(5);
  });
  it('returns 0 for an agent with no events', () => {
    expect(agentCallCount(mkAgent('a', 0))).toBe(0);
  });
});

describe('autoOpenSlugs', () => {
  it('auto-opens the top-3 spending agents', () => {
    const rollup = mkRollup([
      mkAgent('big', 1.5),
      mkAgent('mid', 0.5),
      mkAgent('tiny', 0.05),
      mkAgent('zero', 0),
    ]);
    const open = autoOpenSlugs(rollup);
    expect(open.has('big')).toBe(true);
    expect(open.has('mid')).toBe(true);
    expect(open.has('tiny')).toBe(true);
    expect(open.has('zero')).toBe(false);
  });
  it('skips agents at $0 even within the top-3 window', () => {
    const rollup = mkRollup([
      mkAgent('big', 1.5),
      mkAgent('zero1', 0),
      mkAgent('zero2', 0),
      mkAgent('zero3', 0),
    ]);
    const open = autoOpenSlugs(rollup);
    expect(open.size).toBe(1);
    expect(open.has('big')).toBe(true);
  });
  it('always honours an explicit ?open=<slug>, even outside the top-3', () => {
    const rollup = mkRollup([
      mkAgent('big', 1.5),
      mkAgent('mid', 0.5),
      mkAgent('tiny', 0.05),
      mkAgent('extra', 0.01),
    ]);
    const open = autoOpenSlugs(rollup, 'extra');
    expect(open.has('extra')).toBe(true);
    expect(open.has('big')).toBe(true);
  });
  it('empty rollup yields an empty set', () => {
    expect(autoOpenSlugs(mkRollup([])).size).toBe(0);
  });
});

describe('sourceColor', () => {
  it('returns the documented colour for each known source', () => {
    // Pin a few; if these mismatch the server constants someone needs to update both.
    expect(sourceColor('chat')).toBe('#6366f1');
    expect(sourceColor('summary')).toBe('#8b5cf6');
    expect(sourceColor('recordatio_extraction')).toBe('#10b981');
    expect(SOURCE_COLORS.improver_judge).toBe('#d97706');
  });
  it('returns the fallback for unknown sources', () => {
    expect(sourceColor('not-a-real-source')).toBe(FALLBACK_SOURCE_COLOR);
  });
});

describe('spendSegments', () => {
  it('returns segments summing to 100% sorted largest-first', () => {
    const a = mkAgent('a', 1.0, {
      chat: { total_usd: 0.25, calls: 1 },
      summary: { total_usd: 0.75, calls: 3 },
    });
    const segs = spendSegments(a);
    expect(segs).toHaveLength(2);
    expect(segs[0].src).toBe('summary'); // bigger first
    expect(segs[0].widthPct).toBeCloseTo(75, 4);
    expect(segs[1].src).toBe('chat');
    expect(segs[1].widthPct).toBeCloseTo(25, 4);
    const sum = segs.reduce((acc, s) => acc + s.widthPct, 0);
    expect(sum).toBeCloseTo(100, 4);
  });
  it('drops 0-spend segments', () => {
    const a = mkAgent('a', 1.0, {
      chat: { total_usd: 1.0, calls: 1 },
      summary: { total_usd: 0, calls: 0 },
    });
    expect(spendSegments(a).map((s) => s.src)).toEqual(['chat']);
  });
  it('returns [] for a 0-spend agent', () => {
    expect(spendSegments(mkAgent('a', 0))).toEqual([]);
  });
  it('attaches the right colour to each segment', () => {
    const a = mkAgent('a', 1.0, { chat: { total_usd: 1.0, calls: 1 } });
    expect(spendSegments(a)[0].color).toBe('#6366f1');
  });
});

describe('relativeWidthPct', () => {
  it('returns 100% for the top spender', () => {
    expect(relativeWidthPct(mkAgent('a', 1.0), 1.0)).toBeCloseTo(100, 4);
  });
  it('scales smaller spenders proportionally', () => {
    expect(relativeWidthPct(mkAgent('a', 0.25), 1.0)).toBeCloseTo(25, 4);
  });
  it('returns 0 when the max is 0 (empty window)', () => {
    expect(relativeWidthPct(mkAgent('a', 0), 0)).toBe(0);
  });
});

describe('sortedSources / sortedModels', () => {
  it('sortedSources orders largest-first', () => {
    const a = mkAgent('a', 1.0, {
      summary: { total_usd: 0.2, calls: 1 },
      chat: { total_usd: 0.8, calls: 4 },
    });
    expect(sortedSources(a).map((s) => s.key)).toEqual(['chat', 'summary']);
  });
  it('sortedModels orders largest-first', () => {
    const a = mkAgent('a', 1.0, {}, {
      'claude-haiku-4-5': { total_usd: 0.1, calls: 2 },
      'claude-sonnet-4-6': { total_usd: 0.9, calls: 4 },
    });
    expect(sortedModels(a).map((m) => m.key)).toEqual(['claude-sonnet-4-6', 'claude-haiku-4-5']);
  });
});

describe('formatters', () => {
  it('formatUsd renders 4 decimals by default', () => {
    expect(formatUsd(0.01234)).toBe('$0.0123');
    expect(formatUsd(1)).toBe('$1.0000');
  });
  it('formatUsd respects an explicit places arg', () => {
    expect(formatUsd(0.12345, 2)).toBe('$0.12');
    expect(formatUsd(1234.5, 0)).toBe('$1235');
  });
  it('pluraliseCalls only adds the s for !=1', () => {
    expect(pluraliseCalls(0)).toBe('0 calls');
    expect(pluraliseCalls(1)).toBe('1 call');
    expect(pluraliseCalls(2)).toBe('2 calls');
  });
});
