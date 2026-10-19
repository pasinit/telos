<script lang="ts">
  /**
   * SPA profile page (PR 7D).
   *
   * Scope per plan §7D:
   *   - Read `/api/auth/me` + `/api/user/email` on mount.
   *   - Read `/api/user/google` to render the connection state.
   *   - "Connect Google" button → full-page nav to
   *     `/api/oauth/google/start`. The OAuth round-trip destroys SPA
   *     state, so we surface the outcome via the `?google=connected`
   *     / `?google=error` query the callback redirects to.
   *   - "Disconnect" → POST `/api/oauth/google/disconnect` (JSON,
   *     no navigation).
   *   - Sign out → `logout()` then redirect to `/login`.
   *
   * Things explicitly left for later PRs (display_name, timezone,
   * email_notifications, theme toggle) — the Jinja `profile.html` has
   * them; the SPA Profile keeps a minimal surface for 7D so we don't
   * scope-creep. Future PRs (or a 7K follow-up) expand it.
   *
   * OAuth gotchas codified in `/tmp/phase7-expertise/oauth-redirect-spa.md`:
   *   - "Connect Google" MUST be a full-page navigation, NOT a
   *     `<a use:link>` (the router would intercept and prevent the
   *     cross-origin redirect Google needs).
   *   - Strip the `?google=…` querystring after consuming it via
   *     `replace('/profile')`, otherwise a back-button / refresh
   *     re-toasts.
   */
  import { onMount } from 'svelte';
  import { querystring, replace } from 'svelte-spa-router';
  import { apiGet, apiPost, ApiError } from '../lib/api';
  import { currentUser, loadMe, logout } from '../lib/auth';
  import { showToast } from '../lib/toast';

  interface GoogleState {
    configured: boolean;
    email: string | null;
  }

  interface EmailPayload {
    email: string;
  }

  let userEmail = '';
  let emailDraft = '';
  let savingEmail = false;
  let google: GoogleState | null = null;
  let loading = true;
  let signingOut = false;

  $: dirtyEmail = emailDraft.trim() !== (userEmail ?? '').trim();

  // Consume the OAuth callback's querystring BEFORE the network calls so
  // the toast fires even if the user has a slow connection or the
  // /me probe is in-flight.
  function consumeGoogleQuery(): void {
    const qs = new URLSearchParams($querystring);
    const g = qs.get('google');
    if (g === 'connected') {
      showToast('Google account connected.', { kind: 'success' });
      replace('/profile');
    } else if (g === 'error') {
      showToast('Google connection failed — try again.', { kind: 'error' });
      replace('/profile');
    }
  }

  onMount(async () => {
    consumeGoogleQuery();

    const me = await loadMe();
    if (!me) {
      replace('/login');
      return;
    }

    try {
      const [emailRes, googleRes] = await Promise.all([
        apiGet<EmailPayload>('/user/email'),
        apiGet<GoogleState>('/user/google'),
      ]);
      userEmail = emailRes.email ?? '';
      emailDraft = userEmail;
      google = googleRes;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        replace('/login');
        return;
      }
      showToast('Failed to load profile.', { kind: 'error' });
    } finally {
      loading = false;
    }
  });

  async function saveEmail(): Promise<void> {
    if (savingEmail || !dirtyEmail) return;
    savingEmail = true;
    try {
      const res = await apiPost<{ ok: boolean; email: string }>('/user/email', {
        email: emailDraft.trim(),
      });
      userEmail = res.email;
      emailDraft = res.email;
      showToast('Email saved.', { kind: 'success' });
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        replace('/login');
        return;
      }
      showToast('Could not save email.', { kind: 'error' });
    } finally {
      savingEmail = false;
    }
  }

  function connectGoogle(): void {
    // Full-page navigation — Google's consent screen refuses to render
    // in an iframe / XHR. Don't use `push()` or `<a use:link>`.
    window.location.href = '/api/oauth/google/start';
  }

  async function disconnectGoogle(): Promise<void> {
    try {
      await apiPost('/oauth/google/disconnect');
      google = google ? { ...google, email: null } : google;
      showToast('Google account disconnected.', { kind: 'success' });
    } catch {
      showToast('Disconnect failed.', { kind: 'error' });
    }
  }

  async function signOut(): Promise<void> {
    if (signingOut) return;
    signingOut = true;
    await logout();
    replace('/login');
  }
</script>

<div class="w-full h-full overflow-y-auto">
  <div class="max-w-md mx-auto px-4 py-10 md:py-16">
    <div class="mb-8">
      <div class="flex items-center gap-3 mb-6">
        <img src="/icon-dark.svg" alt="Telos" class="w-8 h-8 rounded-lg" />
        <span class="text-xl font-semibold" style="font-family:'Cormorant Garamond',serif;color:#b8941f;">τέλος</span>
      </div>
      <h2 class="text-2xl font-bold text-center">Profile settings</h2>
    </div>

    <div
      class="bg-gray-100 dark:bg-gray-900 rounded-2xl p-8 shadow-xl space-y-6 border border-gray-200 dark:border-gray-800"
    >
      {#if loading}
        <p class="text-sm text-gray-500 dark:text-gray-400 text-center">Loading…</p>
      {:else}
        <div>
          <p class="text-sm font-medium text-gray-600 dark:text-gray-300 mb-1">Signed in as</p>
          <p class="text-base font-semibold text-gray-900 dark:text-gray-100">
            {$currentUser?.uid ?? '—'}
          </p>
        </div>

        <div>
          <label for="profile-email" class="block text-sm font-medium text-gray-600 dark:text-gray-300 mb-1.5">
            Email
          </label>
          <input
            id="profile-email"
            type="email"
            bind:value={emailDraft}
            placeholder="you@email.com"
            class="w-full bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand focus:border-transparent placeholder-gray-400 dark:placeholder-gray-600"
          />
          <p class="text-xs text-gray-400 dark:text-gray-500 mt-1">Used for routine result notifications</p>
          <button
            type="button"
            on:click={saveEmail}
            disabled={savingEmail || !dirtyEmail}
            class="mt-3 w-full bg-brand hover:bg-brand-dark transition-colors rounded-lg py-2.5 text-sm font-semibold text-white disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {savingEmail ? 'Saving…' : 'Save email'}
          </button>
        </div>

        <div class="pt-4 border-t border-gray-200 dark:border-gray-800">
          <p class="text-sm font-medium text-gray-600 dark:text-gray-300 mb-1">Google account</p>
          <p class="text-xs text-gray-400 dark:text-gray-500 mb-3">
            Lets your agents search your Drive and read/write your Docs and Sheets.
          </p>

          {#if google && !google.configured}
            <p class="text-xs text-gray-500 dark:text-gray-400 italic">
              Google integration is not configured on this Telos instance. Set
              <code>GOOGLE_OAUTH_CLIENT_ID</code>, <code>GOOGLE_OAUTH_CLIENT_SECRET</code>,
              and <code>GOOGLE_OAUTH_REDIRECT_URI</code> in <code>.env</code>.
            </p>
          {:else if google && google.email}
            <div class="flex items-center justify-between gap-3">
              <div class="text-sm text-gray-700 dark:text-gray-200 truncate">
                <span class="text-green-600 dark:text-green-400">✓</span>
                Connected as <span class="font-medium">{google.email}</span>
              </div>
              <button
                type="button"
                on:click={disconnectGoogle}
                class="text-xs px-3 py-1.5 rounded-lg bg-gray-200 dark:bg-gray-800 hover:bg-gray-300 dark:hover:bg-gray-700 text-gray-600 dark:text-gray-400 transition-colors"
              >
                Disconnect
              </button>
            </div>
          {:else if google}
            <button
              type="button"
              on:click={connectGoogle}
              class="inline-block bg-brand hover:bg-brand-dark transition-colors rounded-lg px-4 py-2 text-sm font-semibold text-white"
            >
              Connect Google
            </button>
          {/if}
        </div>

        <div class="flex items-center justify-end pt-2 border-t border-gray-200 dark:border-gray-800">
          <button
            type="button"
            on:click={signOut}
            disabled={signingOut}
            class="text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition-colors disabled:opacity-50"
          >
            {signingOut ? 'Signing out…' : 'Sign out'}
          </button>
        </div>
      {/if}
    </div>
  </div>
</div>
