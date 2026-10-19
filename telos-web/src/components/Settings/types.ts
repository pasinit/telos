/**
 * Shared types for the settings page (PR 7G).
 *
 * Mirrors the JSON returned by ``GET /api/agents/{slug}/settings``
 * (see ``telos/ui/routes/settings_page.py::settings_api``). Keep this
 * file in sync if you add a field on the backend.
 */

export interface AgentMeta {
  user_id?: string;
  slug: string;
  name: string;
  description: string;
  system_prompt: string;
  tools: string;
  tool_descriptions: string;
  created_at: string;
  last_active: string | null;
}

export interface PromptHistoryEntry {
  sha: string;
  created_at: string;
  deployed_at: string | null;
  retired_at: string | null;
}

export interface SignalSummary {
  follow_up_rate: number;
  correction_rate: number;
  ignored_rate?: number;
  total_turns: number;
}

export interface PendingCandidate {
  sha: string;
  content: string;
  rationale: string;
  score_delta: number;
  run_id: number | null;
}

export interface ToolOption {
  name: string;
  description: string;
  type: string;
  display_name: string;
}

export interface ToolGroup {
  category: string;
  tools: ToolOption[];
}

export interface RoutineRow {
  id: string;
  user_id: string;
  agent_slug: string;
  name: string;
  description: string;
  interval_seconds: number;
  time_of_day: string | null;
  one_shot: boolean;
  notify_email: boolean;
  enabled: boolean;
  last_run: string | null;
  created_at: string;
  next_run: string | null;
}

export interface SettingsPayload {
  user_id: string;
  agent: AgentMeta;
  current_prompt: string;
  prompt_history: PromptHistoryEntry[];
  signal_summary: SignalSummary;
  has_pending: boolean;
  candidate: PendingCandidate | null;
  agent_tools: string[];
  tool_groups: ToolGroup[];
  routines: RoutineRow[];
}
