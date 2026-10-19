"""Bounded, agent-scoped evidence of what a recipient was actually sent."""

import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import mistune
from bs4 import BeautifulSoup

from telos.core.repos.execution_events import ExecutionEventRepo


def email_links(body: str) -> list[str]:
    """Read Markdown, HTML and bare links without discarding identity parameters."""
    html = mistune.create_markdown(escape=False, plugins=["url"])(body)
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for anchor in soup.find_all("a", href=True):
        try:
            url = urlsplit(anchor["href"])
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
                continue
            host = url.hostname.lower().removeprefix("www.")
            if url.port:
                host += f":{url.port}"
            query = [(key, value) for key, value in parse_qsl(url.query, keep_blank_values=True)
                     if not key.lower().startswith("utm_") and key.lower() not in ("gclid", "fbclid")]
            links.add(urlunsplit((url.scheme, host, url.path.rstrip("/") or "/", urlencode(query), "")))
        except ValueError:
            continue
    return sorted(links)


def delivery_history(events: ExecutionEventRepo, recipient: str, *, max_chars: int = 24000) -> dict:
    """Describe a limited history; absence is never proof of a new discovery.

    A URL may have appeared in an exclusion/correction section. It proves only
    a previous mention, not eligibility, availability or an application.
    """
    if max_chars < 2048:
        raise ValueError("Delivery history budget must be at least 2048 characters")
    deliveries = events.recent_email_deliveries(recipient, limit=31)
    evidence = {"deliveries_checked": min(len(deliveries), 30), "older_deliveries_omitted": len(deliveries) > 30,
                "links_truncated": False, "previously_mentioned_links": [], "latest_email": None}
    if not deliveries:
        return evidence
    latest = deliveries[0]
    body_limit = min(10000, max_chars // 4)
    evidence["latest_email"] = {"timestamp": latest["timestamp"], "subject": latest["subject"][:300],
                                "body": latest["body"][:body_limit], "body_truncated": len(latest["body"]) > body_limit}
    while len(json.dumps(evidence, ensure_ascii=False)) > max_chars:
        email = evidence["latest_email"]
        field = "body" if len(email["body"]) >= len(email["subject"]) else "subject"
        email[field] = email[field][:len(email[field]) // 2]
        email["body_truncated"] = True
    links = {}
    for email in deliveries[:30]:
        for url in email_links(email["body"]):
            record = links.setdefault(url, {"url": url, "first_mentioned_in_window": email["timestamp"],
                                           "last_mentioned": email["timestamp"], "email_count": 0})
            record["first_mentioned_in_window"] = email["timestamp"]
            record["email_count"] += 1
    # Dict insertion order keeps the most recently mentioned links first.
    for record in links.values():
        evidence["previously_mentioned_links"].append(record)
        if len(json.dumps(evidence, ensure_ascii=False)) > max_chars:
            evidence["previously_mentioned_links"].pop()
            evidence["links_truncated"] = True
            break
    return evidence
