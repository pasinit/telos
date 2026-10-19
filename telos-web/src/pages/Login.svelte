<script lang="ts">
  /**
   * SPA login page (PR 7D).
   *
   * Posts to `POST /api/auth/login` via `lib/auth.login()`, which stores
   * the returned bearer in `localStorage` and updates the `currentUser`
   * store. On success: navigates to `/` (the Housekeeper landing).
   *
   * If the user already has a valid bearer / cookie, mount silently
   * forwards them to `/` so they don't see an empty login form after a
   * page reload.
   */
  import { onMount } from 'svelte';
  import { push } from 'svelte-spa-router';
  import { ApiError } from '../lib/api';
  import { loadMe, login } from '../lib/auth';

  let username = '';
  let email = '';
  let submitting = false;
  let error = '';
  let usernameInput: HTMLInputElement | undefined;

  onMount(async () => {
    // Programmatic focus instead of the autofocus attribute so svelte-check
    // a11y rules don't complain. (Same UX as the legacy Jinja form.)
    usernameInput?.focus();

    // If a bearer / cookie is already valid, skip the login form.
    try {
      const me = await loadMe();
      if (me) push('/');
    } catch {
      // Network blip — let the user submit credentials manually.
    }
  });

  async function submit(): Promise<void> {
    if (submitting) return;
    const u = username.trim();
    if (!u) {
      error = 'Please enter your name.';
      return;
    }
    submitting = true;
    error = '';
    try {
      await login(u, email);
      push('/');
    } catch (e) {
      if (e instanceof ApiError) {
        error = e.message || `Sign-in failed (${e.status}).`;
      } else {
        error = 'Sign-in failed — check your connection.';
      }
    } finally {
      submitting = false;
    }
  }

  function onSubmit(event: Event): void {
    event.preventDefault();
    void submit();
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey && !submitting) {
      event.preventDefault();
      void submit();
    }
  }
</script>

<div class="flex items-center justify-center w-full h-full">
  <div class="w-full max-w-sm px-4">
    <div class="text-center mb-8">
      <div class="flex items-center justify-center gap-3">
        <img src="/icon-dark.svg" alt="Telos" class="w-12 h-12 rounded-xl" />
        <h1 class="text-4xl font-semibold" style="font-family:'Cormorant Garamond',serif;color:#b8941f;">τέλος</h1>
      </div>
      <p class="text-gray-500 dark:text-gray-400 mt-1 text-sm">your personal AI agents</p>
    </div>

    <form
      class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-8 shadow-xl space-y-5"
      on:submit={onSubmit}
      novalidate
    >
      <div>
        <label for="login-username" class="block text-sm font-medium text-gray-600 dark:text-gray-300 mb-1.5">
          Your name
        </label>
        <input
          id="login-username"
          name="username"
          type="text"
          autocomplete="username"
          required
          placeholder="e.g. tommaso"
          bind:value={username}
          bind:this={usernameInput}
          on:keydown={onKeydown}
          class="w-full bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand focus:border-transparent placeholder-gray-400 dark:placeholder-gray-600"
        />
      </div>

      <div>
        <label for="login-email" class="block text-sm font-medium text-gray-600 dark:text-gray-300 mb-1.5">
          Email <span class="text-gray-400 dark:text-gray-500 font-normal">(optional)</span>
        </label>
        <input
          id="login-email"
          name="email"
          type="email"
          autocomplete="email"
          placeholder="you@email.com"
          bind:value={email}
          on:keydown={onKeydown}
          class="w-full bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand focus:border-transparent placeholder-gray-400 dark:placeholder-gray-600"
        />
        <p class="text-xs text-gray-400 dark:text-gray-500 mt-1">For routine result notifications</p>
      </div>

      {#if error}
        <p class="text-sm text-red-600 dark:text-red-400" role="alert">{error}</p>
      {/if}

      <button
        type="submit"
        disabled={submitting}
        class="w-full bg-brand hover:bg-brand-dark transition-colors rounded-lg py-2.5 text-sm font-semibold text-white disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {submitting ? 'Signing in…' : 'Continue'}
      </button>
    </form>
  </div>
</div>
