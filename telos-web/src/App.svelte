<script lang="ts">
  import Router from 'svelte-spa-router';
  import { routes } from './routes';
  import Toast from './components/common/Toast.svelte';
</script>

<!--
  Root SPA shell. Owned by setup-impl during 7A-7C; ownership transfers
  to page-1-login after PR 7D. Per-page PRs may add routes to
  `routes.ts` and wrap their page in <Shell> as they land.

  `restoreScrollState` restores window scroll on browser back/forward.
  Inner scrollable containers (e.g. chat #messages) must wire their own
  scroll preservation; that's handled inside the relevant components.

  <Toast /> is mounted once globally (PR 7D) so any page can call
  `showToast(...)` from `lib/toast.ts` and have it render.
-->
<Router {routes} restoreScrollState={true} />
<Toast />
