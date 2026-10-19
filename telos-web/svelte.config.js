import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

export default {
  preprocess: vitePreprocess(),
  compilerOptions: {
    // Phase 7 locked decision: classic Svelte syntax, no runes.
    // `export let prop` and reactive `$:` blocks remain idiomatic.
    runes: false,
  },
};
