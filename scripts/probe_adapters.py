"""Probe each registered adapter against live URLs.

Run from the repo root with .venv active:
    python scripts/probe_adapters.py

Reports, per URL: did web_fetch succeed, did the adapter recognise it, did
enrichment produce data, and a snippet of what was extracted. The goal is
to surface adapters that pass unit tests but fail on real HTML (selector
drift, bot pages, redesigns).
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telos import adapters
from telos.agent import _web_fetch


PROBES: list[tuple[str, str, str]] = [
    # (label, url, expected_kind)
    ("rightmove search",  "https://www.rightmove.co.uk/property-for-sale/Brixton.html", "search_results"),
    ("rightmove listing", "https://www.rightmove.co.uk/properties/87971922", "listing"),
]


async def probe(label: str, url: str, expected_kind: str) -> dict:
    print(f"\n=== {label} ===")
    print(f"URL: {url}")
    adapter = adapters.find_for_url(url)
    print(f"adapter routed: {adapter.name if adapter else 'NONE'}")
    if adapter is None:
        return {"label": label, "ok": False, "reason": "no adapter for url"}

    try:
        fetched = await _web_fetch(url)
    except Exception as e:
        print(f"FETCH FAILED: {e}")
        return {"label": label, "ok": False, "reason": f"fetch failed: {e}"}

    text = fetched.get("text", "")
    print(f"fetched: {len(text)} chars of text")

    # _web_fetch already appends adapter enrichment to `text` if it found
    # something; check the marker.
    enriched_in_text = "\n[" in text and "] " in text and any(
        k in text for k in ("[listing]", "[search_results]", "[profile]", "[thread]")
    )

    # Re-run the adapter directly so we can inspect the structured payload.
    # We don't have raw html in fetched; re-fetch html ourselves so we can
    # call adapter.enrich(url, html). Use curl path for speed.
    from telos.agent import _fetch_with_curl
    try:
        html, _, _ = await _fetch_with_curl(url)
    except Exception as e:
        print(f"curl-only fetch for direct enrich failed: {e} — using web_fetch text only")
        html = ""

    if html:
        dump = Path("/tmp") / f"adapter_probe_{label.replace(' ', '_')}.html"
        dump.write_text(html)
        print(f"raw html: {len(html)} chars  → {dump}")
    direct = adapter.enrich(url, html) if html else None
    if direct is None:
        print("adapter.enrich returned None on real HTML")
        snippet = html[:400] if html else text[:400]
        print(f"snippet: {snippet!r}")
        return {"label": label, "ok": False, "reason": "enrich returned None",
                "fetched_chars": len(text), "appended": enriched_in_text}

    print(f"enrich kind={direct.kind} expected={expected_kind}")
    print(f"summary: {direct.summary}")
    payload = json.dumps(direct.data, indent=2)[:600]
    print(f"data (truncated):\n{payload}")
    ok = direct.kind == expected_kind
    return {"label": label, "ok": ok, "kind": direct.kind,
            "fetched_chars": len(text), "appended": enriched_in_text}


async def main():
    results = []
    for label, url, kind in PROBES:
        try:
            r = await probe(label, url, kind)
        except Exception as e:
            print(f"PROBE ERROR for {label}: {e}")
            r = {"label": label, "ok": False, "reason": f"probe error: {e}"}
        results.append(r)

    print("\n\n=== summary ===")
    for r in results:
        status = "OK" if r.get("ok") else "FAIL"
        print(f"  {status:4}  {r['label']:24}  {r.get('reason', r.get('kind', ''))}")


if __name__ == "__main__":
    asyncio.run(main())
