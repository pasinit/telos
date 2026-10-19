/**
 * Agents domain store.
 *
 * Backs:
 * - `pages/AgentsIndex.svelte` — the user-managed specialist list.
 * - `components/Shell.svelte` sidebar (when populated by PR 7D).
 * - `components/Chat/*` agent-picker (PR 7E may also read this).
 *
 * The list returned by `GET /api/agents` includes the housekeeper
 * row (it wraps `RegistryRepo.list_for_user`); UIs that want the
 * specialist-only view filter on `slug`. See
 * `telos/ui/routes/agents.py::agents_list_json`.
 *
 * Cache strategy: `ensureAgentsLoaded()` fetches once and subsequent
 * callers reuse the in-memory list. After a mutation (`createAgent`,
 * `deleteAgent`) we re-fetch — cheap and avoids drift with backend
 * touch / last_active fields.
 */
import { writable, type Writable } from 'svelte/store';
import { apiDelete, apiGet, apiPost } from '../api';

export interface Agent {
  slug: string;
  name: string;
  description: string;
  created_at: string | null;
  last_active: string | null;
}

export interface CreatedAgent {
  slug: string;
  name: string;
}

export const agents: Writable<Agent[]> = writable([]);

let loaded = false;
let inflight: Promise<void> | null = null;

async function fetchAndSet(): Promise<void> {
  const list = await apiGet<Agent[]>('/agents');
  agents.set(list);
  loaded = true;
}

/**
 * Fetch agents once; subsequent calls return immediately. Concurrent
 * callers share the same in-flight promise so mount races don't issue
 * duplicate requests.
 */
export async function ensureAgentsLoaded(): Promise<void> {
  if (loaded) return;
  if (inflight) return inflight;
  inflight = fetchAndSet().finally(() => {
    inflight = null;
  });
  return inflight;
}

/** Force a re-fetch — call after a mutation, or on explicit refresh. */
export async function refreshAgents(): Promise<void> {
  await fetchAndSet();
}

/** Reset cached state (sign-out, user switch). */
export function resetAgents(): void {
  agents.set([]);
  loaded = false;
  inflight = null;
}

/**
 * Create a specialist agent. Returns `{slug, name}` on success.
 * Refreshes the store so subscribers see the new row.
 */
export async function createAgent(name: string, description: string): Promise<CreatedAgent> {
  const created = await apiPost<CreatedAgent>('/agents', { name, description });
  await refreshAgents();
  return created;
}

/**
 * Delete a specialist by slug. Refreshes the store on success.
 *
 * Hits `DELETE /api/agents/{slug}` — the canonical SPA mirror of the
 * legacy `DELETE /agents/{slug}` SSR route. Both call the same
 * handler (see `telos/ui/routes/agents.py::delete_agent`); the
 * `/api/` registration exists so the wrapper here doesn't have to
 * bypass the `/api` prefix convention.
 */
export async function deleteAgent(slug: string): Promise<void> {
  await apiDelete(`/agents/${encodeURIComponent(slug)}`);
  await refreshAgents();
}
