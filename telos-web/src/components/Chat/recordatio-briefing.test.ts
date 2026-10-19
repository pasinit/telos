import { describe, expect, it } from 'vitest';
import * as recordatioBriefing from './recordatio-briefing';
import { isBriefingEvent } from './recordatio-briefing';

describe('isBriefingEvent', () => {
  it('detects a briefing frame (drives the post-completion control only)', () => {
    expect(
      isBriefingEvent({ type: 'agentmem.briefing', units: [{}, {}], entities: [{}] }),
    ).toBe(true);
  });

  it('flags a briefing even when the recalled count is zero (mirrors has_request_id)', () => {
    expect(
      isBriefingEvent({ type: 'agentmem.briefing', units: [], entities: [], relations: [] }),
    ).toBe(true);
  });

  it('ignores non-briefing recordatio events', () => {
    expect(isBriefingEvent({ type: 'agentmem.tool_call', tool: 'web_search' })).toBe(false);
    expect(isBriefingEvent({ type: 'agentmem.memory_search', query: 'q' })).toBe(false);
    expect(isBriefingEvent({ type: 'agentmem.something_new' })).toBe(false);
  });

  it('exposes no chip derivation — the live chip is gone for good', () => {
    // A briefing frame during streaming must render nothing: the module
    // deliberately has no chip-producing API left to call.
    expect(recordatioBriefing).not.toHaveProperty('chipFromRecordatioEvent');
    expect(Object.keys(recordatioBriefing)).toEqual(['isBriefingEvent']);
  });
});
