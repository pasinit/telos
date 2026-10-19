/**
 * Markdown → HTML helper. Wraps `marked` with the same feature set the
 * SSR app shipped via `mistune` (tables, strikethrough, GFM-ish).
 *
 * One source of truth for chat rendering: every place that needs to
 * render agent markdown goes through `render`. Inputs are agent-
 * generated, NOT user-typed, so we trust them by default; pass the
 * result through DOMPurify for belt-and-braces against future
 * surprises (e.g. an agent that includes a user-supplied snippet).
 */

import { marked } from 'marked';
import DOMPurify from 'dompurify';

// The legacy `chat.js` applied a small punctuation fixer before
// rendering on page load. Keeping it preserves visual parity with the
// SSR output for older stored turns.
function applyPunctuationFix(input: string): string {
  return input.replace(/([.!?])([A-Z])/g, '$1 $2');
}

// Configure marked once at module load. GFM + breaks match the
// behaviour expected by chat content (e.g. line breaks inside lists).
marked.setOptions({
  gfm: true,
  breaks: false,
});

/**
 * Render raw markdown to sanitized HTML.
 *
 * Sync because callers (streaming render) re-call this on every text
 * delta and an extra microtask per delta is wasted work.
 */
export function render(raw: string): string {
  if (!raw) return '';
  const html = marked.parse(applyPunctuationFix(raw), { async: false }) as string;
  return DOMPurify.sanitize(html, {
    ADD_ATTR: ['target'],
  });
}
