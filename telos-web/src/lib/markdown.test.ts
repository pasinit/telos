import { describe, expect, it } from 'vitest';
import { render } from './markdown';

describe('render', () => {
  it('returns an empty string for empty input', () => {
    expect(render('')).toBe('');
  });

  it('renders simple markdown to HTML', () => {
    const html = render('**bold**');
    expect(html).toContain('<strong>bold</strong>');
  });

  it('renders bullet lists', () => {
    const html = render('- one\n- two');
    expect(html).toContain('<ul>');
    expect(html).toContain('<li>one</li>');
    expect(html).toContain('<li>two</li>');
  });

  it('renders tables (GFM)', () => {
    const html = render('| a | b |\n|---|---|\n| 1 | 2 |');
    expect(html).toContain('<table>');
    expect(html).toContain('<th>a</th>');
  });

  it('renders code blocks', () => {
    const html = render('```\nprint("hi")\n```');
    expect(html).toContain('<pre>');
    expect(html).toContain('<code>');
  });

  it('applies the punctuation fix (sentence-boundary spacing)', () => {
    // Mirrors the SSR `chat.js` fixer: "Done.Next" → "Done. Next".
    const html = render('Done.Next');
    // Cheap assertion that doesn't depend on the exact wrapping tag.
    expect(html.replace(/<[^>]*>/g, '')).toContain('Done. Next');
  });

  it('sanitises a script tag injected via markdown', () => {
    const html = render('<script>alert(1)</script>\n\nhello');
    expect(html).not.toContain('<script>');
    expect(html).toContain('hello');
  });
});
