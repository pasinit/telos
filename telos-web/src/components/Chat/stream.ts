/**
 * Chat stream frame types and helpers.
 *
 * The server (`telos/ui/routes/chat.py`) emits one JSON object per SSE
 * `data:` line. Every shape we know about is captured below — the
 * components consume `ChatFrame` via the `streamSSE<ChatFrame>` helper
 * and dispatch on the top-level key.
 *
 * Keeping this as a discriminated set of optional fields (rather than a
 * tagged union) matches the wire format: the server uses keys for type
 * dispatch, never a `type` field on the chat frames themselves.
 */

export interface ToolCallEvent {
  name: string;
  input?: Record<string, unknown>;
  // `id` is present when present; some legacy frames omit it.
  id?: string;
}

export interface ToolResultEvent {
  name: string;
  output?: unknown;
  is_error?: boolean;
  // Delegation results carry an internal agent slug for label patching.
  _agent_slug?: string;
}

export interface UsagePayload {
  messages: number;
  total_messages: number;
  context_tokens: number;
  turn_cost?: number;
  cumulative_cost?: number | null;
}

export interface RecordatioEvent {
  type: string;
  units?: unknown[];
  entities?: unknown[];
  relations?: unknown[];
  query?: string;
  tool?: string;
  input?: { query?: string; url?: string };
  result?: string;
}

export interface PendingRoutineEvent {
  id: string;
  description: string;
  interval_seconds: number;
  time_of_day?: string | null;
  one_shot?: boolean;
  notify_email?: boolean;
}

export interface PendingAgentEvent {
  name: string;
  goal: string;
}

export interface DelegationTextEvent {
  agent: string;
  text: string;
}

export interface DelegationToolEvent {
  agent: string;
  tool_call: ToolCallEvent;
}

export interface DelegationToolResultEvent {
  agent: string;
  tool_result: ToolResultEvent;
}

/** Union of every key the chat SSE may carry on a single frame. */
export interface ChatFrame {
  text?: string;
  /**
   * Recordatio-path only: one extended-thinking delta. Drives the transient
   * "thinking" status line on the pending assistant bubble; never rendered
   * as message content and never persisted (see `thinking.ts`).
   */
  thinking?: string;
  tool_call?: ToolCallEvent;
  tool_result?: ToolResultEvent;
  pending_routine?: PendingRoutineEvent;
  pending_agent?: PendingAgentEvent;
  delegation_text?: DelegationTextEvent;
  delegation_tool?: DelegationToolEvent;
  delegation_tool_result?: DelegationToolResultEvent;
  recordatio?: RecordatioEvent;
  usage?: UsagePayload;
  turn_id?: { turn_id: string };
  done?: boolean;
  has_pending?: boolean;
}
