<script lang="ts">
  import { link } from 'svelte-spa-router';
</script>

<div class="flex items-center justify-center w-full h-full">
  <div class="text-center">
    <h1 class="text-2xl font-semibold mb-2">Not found</h1>
    <p class="text-gray-500 dark:text-gray-400 mb-4">No such page in this SPA.</p>
    <a href="/" use:link class="text-brand hover:text-brand-dark underline">Back to start</a>
  </div>
</div>
