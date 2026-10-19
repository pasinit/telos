<script lang="ts">
  import type { UsagePayload } from './stream';
  export let usage: UsagePayload | null = null;
  /** Optional W3C trace_id (32-hex) for the most recent turn. Rendered
   *  alongside usage so the user can quote it when reporting issues. */
  export let traceId: string | null = null;

  function fmtUsd(n: number): string {
    return `$${n.toFixed(4)}`;
  }

  function fmtTokens(n: number): string {
    return n >= 1000 ? (n / 1000).toFixed(1) + 'K' : String(n);
  }

  $: parts = usage
    ? [
        `${usage.messages} / ${usage.total_messages} messages`,
        `~${fmtTokens(usage.context_tokens)} tokens`,
        usage.cumulative_cost != null ? `${fmtUsd(usage.cumulative_cost)} total` : null,
        traceId ? `trace ${traceId.slice(0, 8)}` : null,
      ].filter(Boolean)
    : traceId
      ? [`trace ${traceId.slice(0, 8)}`]
      : [];
</script>

{#if usage || traceId}
  <div class="mb-2 text-[10px] text-gray-400 dark:text-gray-500"
       title={traceId ? `trace_id: ${traceId}` : undefined}>
    {parts.join(' · ')}
  </div>
{/if}
