<script lang="ts">
  /**
   * Shared chat component used by both `Housekeeper.svelte` and
   * `ChatPage.svelte`. Owns:
   *
   * - Initial hydration: `GET /api/agents/{slug}` (meta + has_pending +
   *   cumulative usage) + `GET /api/agents/{slug}/turns?limit=50`.
   * - Streaming a new turn over `POST /agents/{slug}/chat` via
   *   `lib/sse.ts`, dispatching frames into per-turn state.
   * - PDF upload via `POST /agents/{slug}/upload` (image goes inline as
   *   a base64 data URI in the chat body).
   * - Stream-interruption fallback: poll `/agents/{slug}/status` until
   *   the last turn is recorded, then refresh history.
   * - Tab-visibility fallback: re-poll on `visibilitychange` when the
   *   tab was hidden mid-stream.
   * - Periodic /status poll: surfaces the "improvement is ready" banner
   *   and refreshes history when a background routine produces a turn.
   *
   * Chat-latency invariant (Phase 5): nothing here may await improvement
   * work or block the SSE stream. The improvement banner is purely a
   * read of the `has_pending` flag; status polling is a side effect.
   */
  import { onDestroy, onMount } from 'svelte';
  import { link, replace } from 'svelte-spa-router';
  import MessageBubble from './MessageBubble.svelte';
  import UsageBar from './UsageBar.svelte';
  import RecalledDrawer from './RecalledDrawer.svelte';
  import { apiGet, ApiError } from '../../lib/api';
  import { getBearerToken } from '../../lib/auth';
  import { traceIdFromSpan, withSpan } from '../../lib/observability';
  import { streamSSE } from '../../lib/sse';
  import { rowFromCall, applyResult, type ToolRow } from './tool-display';
  import { isBriefingEvent } from './recordatio-briefing';
  import ThinkingDisclosure from './ThinkingDisclosure.svelte';
  import {
    appendThinking,
    collapseThinking,
    hasThinking,
    initialThinkingState,
    recallThinking,
    rememberThinking,
    THINKING_FLUSH_MS,
    type ThinkingState,
  } from './thinking';
  import { reconcileHistoryWithLiveTurn } from './history';
  import type {
    ChatFrame,
    UsagePayload,
    PendingRoutineEvent,
    PendingAgentEvent,
  } from './stream';

  export let slug: string;
  /** Agent display name, exposed via `bind:agentName` so the page
   *  wrapper can render it inside Shell's `mobileHeader` slot. */
  export let agentName: string = '';
  /** True for the housekeeper slug — drives "is this the home page?"
   *  affordances and is exposed via `bind:` to the page wrapper. */
  export let isHousekeeper: boolean = false;

  // ─── Hydration ────────────────────────────────────────────────────

  interface AgentMeta {
    slug: string;
    name: string;
    [k: string]: unknown;
  }

  interface MetaBundle {
    agent: AgentMeta;
    has_pending: boolean;
    usage: UsagePayload | null;
    is_housekeeper: boolean;
  }

  interface StoredTurn {
    turn_id: string;
    has_request_id: boolean;
    user_message: string;
    routine_label: string;
    routine_description: string;
    assistant_response: string;
    timestamp: string;
    is_routine: boolean;
    cost_usd: number;
  }

  let meta: MetaBundle | null = null;
  let history: StoredTurn[] = [];
  let usage: UsagePayload | null = null;
  let traceId: string | null = null;
  let hasPending = false;
  let loadError: string | null = null;
  let initialLoaded = false;

  // ─── Streaming state for the in-flight turn ───────────────────────

  /** A single rendered live turn (user prompt + assistant streaming bubble). */
  interface LiveTurn {
    userMessage: string;
    attachedFilename: string | null;
    raw: string;
    toolRows: ToolRow[];
    pendingRoutines: PendingRoutineEvent[];
    pendingAgents: PendingAgentEvent[];
    turnId: string | null;
    hasBriefing: boolean;
    /** Streaming → false once `done` arrives or the stream errors. */
    streaming: boolean;
    /** True when `done` fires with no accumulated text. */
    empty: boolean;
    /** Set when stream errored — replaces bubble with "Waiting for response…" */
    waiting: boolean;
    /** Delegation sub-agent currently emitting text (for inline attribution). */
    delegationAgent: string | null;
    /** Cost of this turn's chat call, set when the final `usage` frame arrives. */
    turnCost: number | null;
    /**
     * Ephemeral extended-thinking state (recordatio path): the FULL
     * accumulated thinking text + timing, used to render the collapsible
     * "Thought for Ns" disclosure ABOVE the answer balloon. Carried only
     * on this in-memory turn for the session — never persisted (gone on
     * reload). `null` for turns with no thinking.
     */
    thinking: ThinkingState | null;
  }
  let liveTurn: LiveTurn | null = null;

  // Throttle state for the streaming "Thinking…" row. Deltas can arrive
  // ≥10/s but the row carries no per-delta info (just a spinner); we
  // commit the accumulated buffer to `liveTurn` at most every
  // THINKING_FLUSH_MS via a leading-edge throttle with a trailing flush.
  let thinkingBuffer: ThinkingState = initialThinkingState();
  let lastThinkingFlush = 0;
  let thinkingFlushTimer: ReturnType<typeof setTimeout> | null = null;
  let turnStartedAt = 0;

  function clearThinking(): void {
    thinkingBuffer = initialThinkingState();
    lastThinkingFlush = 0;
    if (thinkingFlushTimer !== null) {
      clearTimeout(thinkingFlushTimer);
      thinkingFlushTimer = null;
    }
  }

  // ─── UI state ─────────────────────────────────────────────────────

  let messageInput = '';
  let textareaEl: HTMLTextAreaElement | null = null;
  let sending = false;
  let userScrolledUp = false;
  let isStreaming = false;
  let streamWasActive = false;

  // PDF / image attachment
  let fileInputEl: HTMLInputElement | null = null;
  let attachedFile: File | null = null;
  let attachedImageDataUrl: string | null = null;

  // Recalled-memories drawer
  let drawerOpen = false;
  let drawerTurnId: string = '';

  // Abort controller for the in-flight SSE stream
  let abortController: AbortController | null = null;

  // Status polling for improvement banner + background-routine turn detection
  let statusPollerId: ReturnType<typeof setInterval> | null = null;
  let knownTurnCount = 0;
  let lastTurnComplete = true;

  // ─── Lifecycle ────────────────────────────────────────────────────

  onMount(() => {
    void hydrate();
  });

  onDestroy(() => {
    clearThinking();
    abortController?.abort();
    if (statusPollerId !== null) {
      clearInterval(statusPollerId);
      statusPollerId = null;
    }
    document.removeEventListener('visibilitychange', onVisibilityChange);
  });

  async function hydrate(): Promise<void> {
    try {
      const [metaResp, turnsResp] = await Promise.all([
        apiGet<MetaBundle>(`/agents/${encodeURIComponent(slug)}`),
        apiGet<{ turns: StoredTurn[] }>(`/agents/${encodeURIComponent(slug)}/turns?limit=50`),
      ]);
      meta = metaResp;
      // Expose meta fields to the page wrapper via `bind:`.
      agentName = metaResp.agent.name;
      isHousekeeper = metaResp.is_housekeeper;
      usage = metaResp.usage;
      hasPending = metaResp.has_pending;
      history = turnsResp.turns ?? [];
      knownTurnCount = history.length;
      initialLoaded = true;
      // Background poll for improvement + routine results — start AFTER
      // initial hydration so we never race the first /status call.
      startStatusPolling();
      document.addEventListener('visibilitychange', onVisibilityChange);
      // After paint, scroll the messages container to the bottom.
      requestAnimationFrame(() => scrollToBottom(true));
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 401) {
          replace('/login');
          return;
        }
        if (err.status === 404) {
          loadError = 'Agent not found.';
          return;
        }
      }
      loadError = err instanceof Error ? err.message : 'Failed to load chat.';
    }
  }

  // ─── Scroll handling ──────────────────────────────────────────────

  let messagesEl: HTMLDivElement | null = null;

  function scrollToBottom(force = false): void {
    if (!messagesEl) return;
    if (force || !userScrolledUp) {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  }

  function onMessagesScroll(): void {
    if (!messagesEl) return;
    const dist = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
    if (dist > 150) {
      userScrolledUp = true;
    } else if (dist < 30 && !isStreaming) {
      userScrolledUp = false;
    }
  }

  // ─── Textarea auto-resize + Enter-to-submit ───────────────────────

  let prevInputLen = 0;
  function onTextareaInput(): void {
    if (!textareaEl) return;
    const len = messageInput.length;
    if (len < prevInputLen) {
      textareaEl.style.height = 'auto';
      textareaEl.style.height = Math.min(textareaEl.scrollHeight, 160) + 'px';
    } else if (textareaEl.scrollHeight > textareaEl.clientHeight) {
      textareaEl.style.height = Math.min(textareaEl.scrollHeight, 160) + 'px';
    }
    prevInputLen = len;
  }

  function onTextareaKeyDown(e: KeyboardEvent): void {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  }

  // ─── File attachment ──────────────────────────────────────────────

  function onFileChange(): void {
    const file = fileInputEl?.files?.[0] ?? null;
    if (!file) return;
    attachedFile = file;
    attachedImageDataUrl = null;
    if (file.type.startsWith('image/')) {
      const reader = new FileReader();
      reader.onload = (e) => {
        attachedImageDataUrl = typeof e.target?.result === 'string' ? e.target.result : null;
      };
      reader.readAsDataURL(file);
    }
  }

  function removeAttachment(): void {
    if (fileInputEl) fileInputEl.value = '';
    attachedFile = null;
    attachedImageDataUrl = null;
  }

  async function uploadPdf(file: File): Promise<{ filename: string; text: string; pages: number }> {
    const fd = new FormData();
    fd.append('file', file);
    // The upload endpoint is NOT under /api — it's at /agents/{slug}/upload.
    // Bypass lib/api.ts (which assumes /api prefix) and attach the
    // Bearer header manually so dual-auth still works.
    const headers: Record<string, string> = {};
    const token = getBearerToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(`/agents/${encodeURIComponent(slug)}/upload`, {
      method: 'POST',
      headers,
      credentials: 'same-origin',
      body: fd,
    });
    if (!res.ok) {
      let detail = 'Upload failed';
      try {
        const body = await res.json();
        if (body && typeof body.detail === 'string') detail = body.detail;
      } catch {
        /* non-JSON error */
      }
      throw new Error(detail);
    }
    return (await res.json()) as { filename: string; text: string; pages: number };
  }

  // ─── Submit (the main turn loop) ──────────────────────────────────

  async function submit(): Promise<void> {
    if (sending) return;
    let message = messageInput.trim();
    const filename = attachedFile?.name ?? null;
    const displayMessage = message || (filename ? `📄 ${filename}` : '');
    if (!displayMessage) return;

    // Reset input + per-turn state.
    messageInput = '';
    if (textareaEl) textareaEl.style.height = 'auto';
    prevInputLen = 0;

    sending = true;
    isStreaming = true;
    streamWasActive = true;
    userScrolledUp = false;
    traceId = null;
    clearThinking();
    turnStartedAt = Date.now();

    // PDF: upload + prepend extracted text to the message body.
    let imageDataUrl: string | null = null;
    let attachedFilenameForBubble = filename;
    if (attachedFile) {
      if (attachedFile.type.startsWith('image/')) {
        imageDataUrl = attachedImageDataUrl;
      } else {
        try {
          const result = await uploadPdf(attachedFile);
          message =
            `[PDF: ${result.filename}, ${result.pages} page${result.pages === 1 ? '' : 's'}]` +
            `\n\n${result.text}\n\n---\n\n` +
            message;
        } catch (err) {
          // Show the upload error as a fake user-bubble + abort the turn.
          liveTurn = makeLiveTurn(
            `[Upload error: ${err instanceof Error ? err.message : String(err)}]`,
            null,
          );
          liveTurn.streaming = false;
          liveTurn.empty = true;
          sending = false;
          isStreaming = false;
          return;
        }
      }
      removeAttachment();
    }

    liveTurn = makeLiveTurn(displayMessage, attachedFilenameForBubble);
    requestAnimationFrame(() => scrollToBottom(true));

    abortController?.abort();
    abortController = new AbortController();

    try {
      await withSpan(
        'ui.chat.turn',
        {
          'agent.slug': slug,
          'message.chars': message.length,
          has_image: Boolean(imageDataUrl),
        },
        (sp) => {
          traceId = traceIdFromSpan(sp);
          return streamSSE<ChatFrame>(`/agents/${encodeURIComponent(slug)}/chat`, {
            body: imageDataUrl ? { message, image: imageDataUrl } : { message },
            signal: abortController!.signal,
            onFrame: handleFrame,
          });
        },
      );
      await refreshHistory();
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') {
        // user-initiated abort — silent.
      } else {
        // Stream interrupted; flip the bubble to a waiting state and
        // poll the server until the turn is persisted, then refresh
        // history (the persisted turn shows up there).
        clearThinking();
        if (liveTurn) {
          liveTurn = { ...liveTurn, streaming: false, waiting: true, thinking: null };
        }
        void waitForResponse();
      }
    } finally {
      sending = false;
      isStreaming = false;
      streamWasActive = false;
      // Re-focus the textarea for rapid follow-up.
      requestAnimationFrame(() => textareaEl?.focus());
    }
  }

  function makeLiveTurn(userMessage: string, attachedFilename: string | null): LiveTurn {
    return {
      userMessage,
      attachedFilename,
      raw: '',
      toolRows: [],
      pendingRoutines: [],
      pendingAgents: [],
      turnId: null,
      hasBriefing: false,
      streaming: true,
      empty: false,
      waiting: false,
      delegationAgent: null,
      turnCost: null,
      thinking: null,
    };
  }

  // ─── SSE frame dispatch ───────────────────────────────────────────

  // Collapse the streaming thinking row the instant the first answer token
  // arrives: compute the duration and pin the FULL thinking text on the
  // turn so the "Thought for Ns" disclosure can expand it. Cancels any
  // pending flush. Returns the turn with `thinking` set (or unchanged when
  // no thinking ever streamed). Idempotent.
  function collapseThinkingOnFirstAnswer(turn: LiveTurn): LiveTurn {
    if (thinkingFlushTimer !== null) {
      clearTimeout(thinkingFlushTimer);
      thinkingFlushTimer = null;
    }
    if (!hasThinking(thinkingBuffer)) return turn;
    if (turn.thinking?.durationSec != null) return turn;
    thinkingBuffer = collapseThinking(thinkingBuffer);
    return { ...turn, thinking: thinkingBuffer };
  }

  function handleFrame(frame: ChatFrame): void {
    if (!liveTurn) return;
    let next = liveTurn;
    if (frame.thinking && !next.raw) {
      // Pre-text phase only: accumulate the FULL thinking text and commit
      // the buffer to the turn at most every THINKING_FLUSH_MS (leading
      // edge now, trailing flush via timer) so the "Thinking…" row — which
      // carries no per-delta content — doesn't repaint per token.
      thinkingBuffer = appendThinking(thinkingBuffer, frame.thinking, Date.now(), turnStartedAt);
      const now = Date.now();
      if (now - lastThinkingFlush >= THINKING_FLUSH_MS) {
        lastThinkingFlush = now;
        next = { ...next, thinking: thinkingBuffer };
      } else if (thinkingFlushTimer === null) {
        thinkingFlushTimer = setTimeout(() => {
          thinkingFlushTimer = null;
          lastThinkingFlush = Date.now();
          // Re-check: the first answer token may have landed meanwhile (it
          // owns the collapse), so only flush while still pre-text.
          if (liveTurn?.streaming && !liveTurn.raw) {
            liveTurn = { ...liveTurn, thinking: thinkingBuffer };
          }
        }, THINKING_FLUSH_MS - (now - lastThinkingFlush));
      }
    }
    if (frame.text) {
      // First real text token collapses the thinking row for good (keeping
      // the full text on `thinking` so the chip can still expand it).
      if (!next.raw) next = collapseThinkingOnFirstAnswer(next);
      next = { ...next, raw: next.raw + frame.text };
    }
    if (frame.tool_call) {
      next = { ...next, toolRows: [...next.toolRows, rowFromCall(frame.tool_call)] };
    }
    if (frame.tool_result) {
      next = { ...next, toolRows: applyResult(next.toolRows, frame.tool_result) };
    }
    if (frame.pending_routine) {
      next = { ...next, pendingRoutines: [...next.pendingRoutines, frame.pending_routine] };
    }
    if (frame.pending_agent) {
      next = { ...next, pendingAgents: [...next.pendingAgents, frame.pending_agent] };
    }
    if (frame.delegation_text) {
      const d = frame.delegation_text;
      let raw = next.raw;
      let delegationAgent = next.delegationAgent;
      if (delegationAgent !== d.agent) {
        delegationAgent = d.agent;
        raw = raw + `\n\n**via ${d.agent}:**\n`;
      }
      // Delegation text is visible output too — collapse the thinking row.
      if (!next.raw) next = collapseThinkingOnFirstAnswer(next);
      next = { ...next, raw: raw + d.text, delegationAgent };
    }
    if (frame.delegation_tool) {
      const dt = frame.delegation_tool;
      next = {
        ...next,
        toolRows: [
          ...next.toolRows,
          rowFromCall({ ...dt.tool_call, name: `${dt.agent}/${dt.tool_call.name}` }),
        ],
      };
    }
    if (frame.delegation_tool_result) {
      const dtr = frame.delegation_tool_result;
      next = {
        ...next,
        toolRows: applyResult(next.toolRows, {
          ...dtr.tool_result,
          name: `${dtr.agent}/${dtr.tool_result.name}`,
        }),
      };
    }
    if (frame.recordatio && isBriefingEvent(frame.recordatio)) {
      // The briefing frame renders NOTHING during streaming (its panel
      // content is only meaningful after the turn; the backend persists
      // its request_id on the turn row independently). It only flags the
      // turn so the bottom "View recalled memories" control can appear
      // once streaming ends — same affordance the refetched history row
      // gets from `has_request_id`.
      next = { ...next, hasBriefing: true };
    }
    if (frame.usage) {
      usage = frame.usage;
      if (frame.usage.turn_cost != null) {
        next = { ...next, turnCost: frame.usage.turn_cost };
      }
    }
    if (frame.turn_id) {
      next = { ...next, turnId: frame.turn_id.turn_id };
    }
    if (frame.done) {
      // A turn that thought but never emitted answer text still collapses
      // its thinking into a chip (so the duration is fixed and the full
      // text stays expandable). Then drop the throttle scratchpad — the
      // FULL text now lives on `next.thinking`, which `clearThinking`
      // must not touch.
      next = collapseThinkingOnFirstAnswer(next);
      const thinking = next.thinking;
      clearThinking();
      next = {
        ...next,
        streaming: false,
        empty: !next.raw,
        thinking,
      };
      if (frame.has_pending) hasPending = true;
    }
    // Once the chip is final (collapsed) and the turn id is known, pin the
    // thinking state in the session map keyed by turn_id. The live turn is
    // dropped as soon as `refreshHistory` sees the persisted row
    // (`reconcileHistoryWithLiveTurn` → `liveTurn: null`), so this map is
    // what lets the disclosure re-attach to the refetched history row and
    // persist for the rest of the session. Runs on every frame to be
    // robust to `turn_id` / `done` ordering; `rememberThinking` is a cheap
    // idempotent set.
    if (next.turnId && next.thinking) {
      rememberThinking(next.turnId, next.thinking);
    }
    liveTurn = next;
    scrollToBottom();
  }

  // ─── Polling fallbacks ────────────────────────────────────────────

  let waiting = false;
  async function waitForResponse(): Promise<void> {
    if (waiting) return;
    waiting = true;
    const start = Date.now();
    const tick = async (): Promise<void> => {
      try {
        const status = await apiGet<{ last_turn_complete: boolean }>(
          `/agents/${encodeURIComponent(slug)}/status`,
        );
        if (status.last_turn_complete) {
          waiting = false;
          await refreshHistory();
          return;
        }
      } catch {
        /* ignore — try again */
      }
      // Routine-style turns run 10+ minutes; giving up earlier strands the
      // live bubble until a manual reload.
      if (Date.now() - start > 1_800_000) {
        waiting = false;
        return;
      }
      setTimeout(() => void tick(), 2000);
    };
    void tick();
  }

  function startStatusPolling(): void {
    if (statusPollerId !== null) return;
    statusPollerId = setInterval(async () => {
      try {
        const status = await apiGet<{
          has_pending: boolean;
          turn_count: number;
          last_turn_complete: boolean;
        }>(`/agents/${encodeURIComponent(slug)}/status`);
        if (status.has_pending) hasPending = true;
        const userBusy =
          userScrolledUp ||
          (textareaEl && document.activeElement === textareaEl) ||
          messageInput.trim().length > 0;
        // The turn row is written when the turn STARTS, so completion
        // doesn't change turn_count — also refresh when the last turn
        // flips to complete (e.g. after a dropped stream).
        const finished = status.last_turn_complete && !lastTurnComplete;
        lastTurnComplete = status.last_turn_complete;
        if (!isStreaming && (status.turn_count > knownTurnCount || finished) && !userBusy) {
          knownTurnCount = status.turn_count;
          await refreshHistory();
        }
      } catch {
        /* tolerated — try again next interval */
      }
    }, 15_000);
  }

  function onVisibilityChange(): void {
    if (document.visibilityState === 'visible' && streamWasActive) {
      streamWasActive = false;
      void waitForResponse();
    }
  }

  async function refreshHistory(): Promise<void> {
    try {
      const turnsResp = await apiGet<{ turns: StoredTurn[] }>(
        `/agents/${encodeURIComponent(slug)}/turns?limit=50`,
      );
      const reconciled = reconcileHistoryWithLiveTurn(turnsResp.turns ?? [], liveTurn);
      history = reconciled.history;
      liveTurn = reconciled.liveTurn;
      knownTurnCount = history.length;
      requestAnimationFrame(() => scrollToBottom(true));
    } catch {
      /* tolerated */
    }
  }

  // ─── Recalled drawer ──────────────────────────────────────────────

  function openRecalledDrawer(turnId: string): void {
    drawerTurnId = turnId;
    drawerOpen = true;
  }

  function closeRecalledDrawer(): void {
    drawerOpen = false;
  }

  // ─── Clear history (specialist only) ──────────────────────────────
  //
  // Exposed as a public method via `bind:this={chatRef}` so the page
  // wrapper's `sidebarFooter` slot can call `chatRef.clearHistory()`.
  // The two-click confirm UX moves to the page (its "Clear history"
  // button is the only consumer); Chat just runs the destructive op.

  export async function clearHistory(): Promise<void> {
    try {
      // The legacy endpoint returns a 303 RedirectResponse; with
      // `redirect: 'manual'` we'd get an opaque response. Easiest is to
      // accept the redirect, then refresh state explicitly.
      const token = getBearerToken();
      await fetch(`/agents/${encodeURIComponent(slug)}/clear`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      });
      await refreshHistory();
      usage = null;
      hasPending = false;
    } catch {
      /* surface via toast if available */
    }
  }
</script>

{#if loadError}
  <div class="flex-1 flex items-center justify-center p-6 text-sm text-red-400">{loadError}</div>
{:else if !initialLoaded || !meta}
  <div class="flex-1 flex items-center justify-center p-6 text-sm text-gray-500 italic">Loading…</div>
{:else}
  <!-- Desktop top-bar with the agent name. The mobile drawer + the
       mobile header (agent name on mobile) live in Shell via the
       `mobileHeader` slot; Settings + Clear live in `sidebarFooter`.
       This header is desktop-only so the agent-name affordance is
       always visible to keyboard / wide-viewport users. -->
  <header
    class="md:flex hidden items-center justify-between px-4 md:px-6 py-3.5 border-b border-gray-200 dark:border-gray-800 shrink-0"
  >
    <h1 class="font-semibold text-gray-800 dark:text-gray-100">{meta.agent.name}</h1>
  </header>

  <!-- Improvement banner -->
  {#if hasPending}
    <div
      class="bg-indigo-900/40 border-b border-indigo-700/50 px-6 py-2.5 text-sm flex items-center justify-between shrink-0"
    >
      <span class="text-indigo-300">✦ A prompt improvement is ready for review.</span>
      <a
        href="/agents/{slug}/settings"
        use:link
        class="text-indigo-400 hover:text-indigo-200 font-medium transition-colors">Review →</a
      >
    </div>
  {/if}

  <!-- Messages -->
  <div
    id="messages"
    bind:this={messagesEl}
    on:scroll={onMessagesScroll}
    class="flex-1 overflow-y-auto px-3 md:px-6 py-4 md:py-6 space-y-6"
  >
    {#each history as turn (turn.turn_id)}
      {#if turn.is_routine}
        <MessageBubble
          role="user"
          raw={turn.user_message}
          timestamp={turn.timestamp}
          routineLabel={turn.routine_label}
          routineDescription={turn.routine_description}
        />
      {:else}
        <MessageBubble
          role="user"
          raw={turn.user_message}
          timestamp={turn.timestamp}
        />
      {/if}
      <!-- Session-remembered "Thought for Ns" chip: persisted turns carry
           no thinking (never written to the transcript), so a turn that
           streamed thinking THIS session re-attaches its chip from the
           module-scoped map in thinking.ts. Gone on page reload — the
           accepted persistence boundary. -->
      {@const sessionThinking = recallThinking(turn.turn_id)}
      {#if sessionThinking}
        <ThinkingDisclosure
          text={sessionThinking.text}
          durationSec={sessionThinking.durationSec}
        />
      {/if}
      <MessageBubble
        role="assistant"
        raw={turn.assistant_response}
        timestamp={turn.timestamp}
        {slug}
        turnId={turn.turn_id}
        hasBriefing={turn.has_request_id}
        turnCost={turn.cost_usd}
        onOpenRecalled={openRecalledDrawer}
      />
    {/each}

    {#if liveTurn}
      <MessageBubble
        role="user"
        raw={liveTurn.userMessage}
        attachedFilename={liveTurn.attachedFilename}
      />
      <!-- Collapsible "Thought for Ns" disclosure — a SEPARATE element
           above the answer balloon. The balloon itself never carries
           thinking text and never resizes because of it. -->
      {#if liveTurn.thinking && !liveTurn.waiting}
        <ThinkingDisclosure
          text={liveTurn.thinking.text}
          durationSec={liveTurn.thinking.durationSec}
        />
      {/if}
      <!-- Pre-answer thinking phase: the answer balloon does not exist
           yet (only the "Thinking…" row above is shown). Otherwise render
           the balloon exactly as before — byte-identical for the legacy /
           no-thinking path. -->
      {#if !(liveTurn.streaming && !liveTurn.raw && !liveTurn.waiting && liveTurn.thinking && liveTurn.thinking.durationSec === null)}
        <MessageBubble
          role="assistant"
          raw={liveTurn.waiting ? '' : liveTurn.raw}
          html={liveTurn.waiting
            ? '<span class="text-gray-500 dark:text-gray-400 italic text-xs">Waiting for response…</span>'
            : null}
          isStreaming={liveTurn.streaming}
          isEmpty={liveTurn.empty}
          toolRows={liveTurn.toolRows}
          pendingRoutines={liveTurn.pendingRoutines}
          pendingAgents={liveTurn.pendingAgents}
          {slug}
          turnId={liveTurn.turnId}
          hasBriefing={liveTurn.hasBriefing}
          turnCost={liveTurn.turnCost}
          onOpenRecalled={openRecalledDrawer}
        />
      {/if}
    {/if}
  </div>

  <!-- Input -->
  <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 px-3 md:px-6 py-3 md:py-4">
    {#if attachedFile}
      <div
        class="mb-2 flex items-center gap-2 px-3 py-1.5 rounded-lg bg-gray-900 border border-gray-700 text-xs text-gray-400 w-fit"
      >
        <span>📄</span>
        <span class="truncate max-w-xs">{attachedFile.name}</span>
        <button
          type="button"
          on:click={removeAttachment}
          class="ml-1 text-gray-600 hover:text-red-400 transition-colors">✕</button
        >
      </div>
    {/if}
    <UsageBar {usage} {traceId} />
    <form
      class="flex gap-3 items-end"
      on:submit|preventDefault={submit}
    >
      <label
        class="shrink-0 cursor-pointer text-gray-500 hover:text-gray-300 transition-colors py-3"
        title="Attach file"
      >
        <span>📎</span>
        <input
          bind:this={fileInputEl}
          type="file"
          accept=".pdf,image/*"
          class="hidden"
          on:change={onFileChange}
        />
      </label>
      <textarea
        bind:this={textareaEl}
        bind:value={messageInput}
        rows="1"
        placeholder="Message {meta.agent.name}…"
        class="flex-1 bg-gray-100 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-brand focus:border-transparent placeholder-gray-400 dark:placeholder-gray-600 resize-none"
        style="max-height: 160px; overflow-y: auto;"
        on:input={onTextareaInput}
        on:keydown={onTextareaKeyDown}
      ></textarea>
      <button
        type="submit"
        disabled={sending}
        aria-label="Send"
        title="Send"
        class="bg-brand hover:bg-brand-dark transition-colors rounded-xl w-11 h-11 flex items-center justify-center shrink-0 text-white disabled:opacity-50"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          class="w-5 h-5"
        >
          <path d="M3.4 20.4 21 12 3.4 3.6 3 10l13 2-13 2 .4 6.4Z" />
        </svg>
      </button>
    </form>
  </div>

  <RecalledDrawer
    open={drawerOpen}
    slug={slug}
    turnId={drawerTurnId}
    onClose={closeRecalledDrawer}
  />
{/if}
