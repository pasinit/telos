<script lang="ts">
  /**
   * Housekeeper landing page (`/`). Thin wrapper around the shared
   * `<Chat>` component pinned to the reserved housekeeper slug.
   *
   * Composes inside `<Shell>` with the housekeeper pin highlighted.
   * Settings + Clear-history are specialist-only affordances, so the
   * `sidebarFooter` slot is left empty here. `mobileHeader` carries
   * the agent name (filled from `Chat`'s `bind:agentName`).
   */
  import Shell from '../components/Shell.svelte';
  import Chat from '../components/Chat/Chat.svelte';

  // Reserved slug — matches `telos.housekeeper.HOUSEKEEPER_SLUG`.
  const HOUSEKEEPER_SLUG = '_housekeeper';

  let agentName = '';
</script>

<Shell isHousekeeper={true}>
  <svelte:fragment slot="mobileHeader">
    {#if agentName}
      <h1 class="font-semibold text-gray-800 dark:text-gray-100 truncate">{agentName}</h1>
    {/if}
  </svelte:fragment>

  <Chat slug={HOUSEKEEPER_SLUG} bind:agentName />
</Shell>
