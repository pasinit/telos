<script lang="ts">
  /**
   * Grouped tool picker. The "Save tools" button is only enabled when
   * the current selection differs from the saved baseline — matches
   * the SSR widget. ``onSave`` resolves once the backend has
   * acknowledged the new set; the component then updates its baseline
   * so the button disables again.
   */
  import { onMount } from 'svelte';
  import type { ToolGroup } from './types';

  export let groups: ToolGroup[];
  export let initialSelected: string[];
  export let onSave: (tools: string[]) => Promise<void>;

  // Saved baseline — the source of truth for "has the user changed anything".
  let baseline: Set<string> = new Set(initialSelected);
  let selected: Set<string> = new Set(initialSelected);
  let saving = false;
  let saved = false;

  // Whenever the parent re-fetches the payload and passes a new
  // ``initialSelected``, reset the baseline + the current selection
  // so the picker stays in sync with backend truth.
  $: {
    baseline = new Set(initialSelected);
    if (!saving) selected = new Set(initialSelected);
  }

  function toggle(name: string, checked: boolean): void {
    if (checked) selected.add(name);
    else selected.delete(name);
    selected = new Set(selected); // reassign so Svelte reacts
  }

  function setsEqual(a: Set<string>, b: Set<string>): boolean {
    if (a.size !== b.size) return false;
    for (const v of a) if (!b.has(v)) return false;
    return true;
  }

  $: dirty = !setsEqual(selected, baseline);

  async function save(): Promise<void> {
    saving = true;
    saved = false;
    try {
      const list = [...selected];
      await onSave(list);
      baseline = new Set(list);
      saved = true;
      setTimeout(() => { saved = false; }, 1500);
    } finally {
      saving = false;
    }
  }

  let saveLabel = 'Save tools';
  $: saveLabel = saving ? 'Saving…' : saved ? 'Saved!' : 'Save tools';

  // Suppress unused-warning during initial render before the
  // reactive baseline assignment fires.
  onMount(() => {
    baseline = new Set(initialSelected);
    selected = new Set(initialSelected);
  });
</script>

<div class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-6 border border-gray-200 dark:border-gray-800 space-y-4">
  <h2 class="font-semibold text-sm text-gray-600 dark:text-gray-300 uppercase tracking-wide">Tools</h2>

  <div class="space-y-5">
    {#each groups as group (group.category)}
      <div class="space-y-1">
        <h3 class="text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400 px-1">{group.category}</h3>
        {#each group.tools as tool (tool.name)}
          <label class="flex items-center gap-3 py-2 px-3 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors cursor-pointer">
            <input
              type="checkbox"
              class="accent-indigo-500"
              checked={selected.has(tool.name)}
              on:change={(e) => toggle(tool.name, (e.target as HTMLInputElement).checked)}
            />
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2">
                <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{tool.display_name}</span>
                {#if tool.display_name !== tool.name}
                  <span class="text-[10px] font-mono text-gray-400 dark:text-gray-600">{tool.name}</span>
                {/if}
                <span class="text-[10px] px-1.5 py-px rounded uppercase tracking-wide {tool.type === 'builtin' ? 'bg-gray-200 text-gray-600 dark:bg-gray-800 dark:text-gray-500' : 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-400'}">
                  {tool.type}
                </span>
              </div>
              {#if tool.description}
                <p class="text-xs text-gray-600 dark:text-gray-500 truncate">{tool.description}</p>
              {/if}
            </div>
          </label>
        {/each}
      </div>
    {/each}
  </div>

  <button
    on:click={save}
    disabled={!dirty || saving}
    class="bg-brand hover:bg-brand-dark transition-colors rounded-lg px-4 py-2 text-sm font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
  >
    {saveLabel}
  </button>
</div>
