/**
 * Briefing-frame detection for the recordatio path.
 *
 * The `agentmem.briefing` SSE frame arrives BEFORE any answer text, but
 * its panel content is only meaningful once the turn completes — so the
 * SPA renders nothing for it during streaming. The frame is still
 * consumed: it flags the in-flight turn as "has recalled memories" so
 * the bottom-of-message "View recalled memories" control can appear as
 * soon as the turn finishes (the backend persists the briefing's
 * `request_id` on the turn row independently — see `Agent.astream` and
 * `/api/agents/{slug}/turns/{turn_id}/memories`).
 *
 * Lives in its own file so the logic stays trivially unit-testable —
 * the same split `thinking.ts` uses.
 */

import type { RecordatioEvent } from './stream';

/**
 * True when the event is a briefing frame. Deliberately ignores the
 * recalled counts: the backend persists `has_request_id` whenever a
 * briefing fired, even with zero recalled units, so the post-completion
 * control must mirror that.
 */
export function isBriefingEvent(event: RecordatioEvent): boolean {
  return event.type === 'agentmem.briefing';
}
