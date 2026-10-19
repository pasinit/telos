import { describe, expect, it } from 'vitest';
import { rowFromCall, applyResult } from './tool-display';

describe('rowFromCall', () => {
  it('formats delegate_to with the agent slug', () => {
    const row = rowFromCall({ name: 'delegate_to', input: { agent_slug: 'weather' } });
    expect(row.badge).toBe('delegate');
    expect(row.preview).toContain('weather');
    expect(row.status).toBe('pending');
  });

  it('formats coordinate with the slug list', () => {
    const row = rowFromCall({ name: 'coordinate', input: { agent_slugs: ['a', 'b'] } });
    expect(row.badge).toBe('coordinate');
    expect(row.preview).toContain('a, b');
  });

  it('formats web_search with the quoted query', () => {
    const row = rowFromCall({ name: 'web_search', input: { query: 'climate' } });
    expect(row.badge).toBe('web_search');
    expect(row.preview).toBe('"climate"');
  });

  it('truncates a long browser_open URL with an ellipsis', () => {
    const long = 'https://example.com/' + 'x'.repeat(200);
    const row = rowFromCall({ name: 'browser_open', input: { url: long } });
    expect(row.preview.endsWith('…')).toBe(true);
    expect(row.preview.length).toBeLessThanOrEqual(70);
  });

  it('falls back to the JSON-stringified input for unknown tools', () => {
    const row = rowFromCall({ name: 'custom_tool', input: { k: 1 } });
    expect(row.badge).toBe('custom_tool');
    expect(row.preview).toBe('{"k":1}');
  });

  it('generates a unique key per (name, input) pair', () => {
    const r1 = rowFromCall({ name: 'web_search', input: { query: 'a' } });
    const r2 = rowFromCall({ name: 'web_search', input: { query: 'b' } });
    expect(r1.key).not.toBe(r2.key);
  });
});

describe('applyResult', () => {
  it('flips the first matching pending row to ok', () => {
    const rows = [rowFromCall({ name: 'web_search', input: { query: 'hi' } })];
    const next = applyResult(rows, { name: 'web_search', output: 'done' });
    expect(next[0].status).toBe('ok');
    // Caller-supplied array unchanged.
    expect(rows[0].status).toBe('pending');
  });

  it('marks errors as error and leaves preview unchanged for non-delegation', () => {
    const rows = [rowFromCall({ name: 'web_search', input: { query: 'q' } })];
    const next = applyResult(rows, { name: 'web_search', is_error: true });
    expect(next[0].status).toBe('error');
    expect(next[0].preview).toBe(rows[0].preview);
  });

  it('updates the preview for a successful delegate_to', () => {
    const rows = [rowFromCall({ name: 'delegate_to', input: { agent_slug: 'cook' } })];
    const next = applyResult(rows, { name: 'delegate_to', _agent_slug: 'cook' });
    expect(next[0].preview).toBe('cook responded');
  });

  it('updates the preview for a successful coordinate', () => {
    const rows = [rowFromCall({ name: 'coordinate', input: { agent_slugs: ['x'] } })];
    const next = applyResult(rows, { name: 'coordinate' });
    expect(next[0].preview).toBe('All specialists responded');
  });

  it('returns the same array reference when no pending row matches', () => {
    const rows = [rowFromCall({ name: 'web_search', input: { query: 'a' } })];
    const ok = applyResult(rows, { name: 'web_search' });
    const noMatch = applyResult(ok, { name: 'web_search' });
    expect(noMatch).toBe(ok);
  });
});
