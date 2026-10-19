<script lang="ts">
  /**
   * Pending-candidate banner — shown when ``has_pending && candidate``.
   *
   * Two leaf actions: Deploy (approve) and Discard (reject). Both call
   * back into the parent so the parent can refresh the page payload
   * after the mutation; this component is presentation-only.
   */
  import type { PendingCandidate } from './types';

  export let candidate: PendingCandidate;
  export let onApprove: () => void;
  export let onReject: () => void;
  export let submitting: boolean = false;
</script>

<div class="bg-indigo-900/30 border border-indigo-700/50 rounded-2xl p-6 space-y-4">
  <div class="flex items-center gap-2">
    <span class="text-indigo-400 font-semibold">✦ Prompt improvement ready</span>
    <span class="text-xs text-indigo-600 font-mono">{candidate.sha}</span>
  </div>
  <p class="text-sm text-gray-300">{candidate.rationale}</p>
  <p class="text-xs text-indigo-400">
    Score delta: +{Math.round(candidate.score_delta * 100)}%
  </p>
  <div class="flex gap-3">
    <button
      class="bg-indigo-600 hover:bg-indigo-500 transition-colors rounded-lg px-4 py-2 text-sm font-semibold disabled:opacity-50"
      disabled={submitting}
      on:click={onApprove}
    >
      Deploy
    </button>
    <button
      class="bg-gray-800 hover:bg-gray-700 transition-colors rounded-lg px-4 py-2 text-sm disabled:opacity-50"
      disabled={submitting}
      on:click={onReject}
    >
      Discard
    </button>
  </div>
</div>
