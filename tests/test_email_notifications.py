"""Regression tests for the email body rendering helper.

Phase 7K vendored ``md_to_html`` out of the deleted ``telos/ui/markdown.py``
into this service module and inadvertently flipped its config: ``escape=True``
silently turned every Jobber-style HTML email into literal escaped text in the
recipient's inbox, and the HTML-pass-through guard was dropped. These tests
pin the pre-7K behaviour.
"""
from telos.services.email_notifications import _md_to_html


def test_markdown_image_renders_as_img_tag():
    out = _md_to_html("![a flat](https://example.com/photo.jpg)")
    assert "<img" in out
    assert 'src="https://example.com/photo.jpg"' in out


def test_html_input_passes_through_unmangled():
    src = "<h2>Daily Brief</h2>\n<p>One <strong>find</strong>.</p>"
    assert _md_to_html(src) == src


def test_html_input_is_not_escaped():
    src = "<h2>Title</h2>"
    out = _md_to_html(src)
    assert "&lt;h2&gt;" not in out
    assert "<h2>" in out


def test_markdown_table_renders():
    src = "| col | val |\n|---|---|\n| a | 1 |"
    out = _md_to_html(src)
    assert "<table>" in out
    assert "<td>a</td>" in out


def test_markdown_strikethrough_renders():
    out = _md_to_html("hello ~~world~~")
    assert "<del>world</del>" in out


def test_plain_markdown_still_converts():
    out = _md_to_html("# Heading\n\nSome **bold** text.")
    assert "<h1>Heading</h1>" in out
    assert "<strong>bold</strong>" in out
