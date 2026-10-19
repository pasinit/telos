"""Thin synchronous wrappers around google-api-python-client.

Each function takes an authorised google.oauth2.credentials.Credentials
object and returns an agent-friendly string. The synchronous Google client
is run via loop.run_in_executor in the agent dispatcher to avoid blocking
the event loop.

Six tools are exposed (matching the schemas registered in tools.py):

  drive_search(creds, query, kind=None) -> str
  gdocs_read(creds, document_id) -> str
  gdocs_append(creds, document_id, text) -> str
  gsheets_read(creds, spreadsheet_id, range) -> str
  gsheets_append(creds, spreadsheet_id, range, values) -> str
  gsheets_update(creds, spreadsheet_id, range, values) -> str
"""
from typing import Any

from googleapiclient.discovery import build

_DOC_MIME = "application/vnd.google-apps.document"
_SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _drive(creds):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _docs(creds):
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def _sheets(creds):
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------
# Drive search
# ---------------------------------------------------------------------

def drive_search(creds, query: str, kind: str | None = None) -> str:
    """Find Drive files by name. kind ∈ {None, 'doc', 'sheet'} filters mime."""
    parts = [f"name contains '{_escape(query)}'", "trashed = false"]
    if kind == "doc":
        parts.append(f"mimeType = '{_DOC_MIME}'")
    elif kind == "sheet":
        parts.append(f"mimeType = '{_SHEET_MIME}'")
    elif kind is None:
        parts.append(
            f"(mimeType = '{_DOC_MIME}' or mimeType = '{_SHEET_MIME}')"
        )
    q = " and ".join(parts)
    resp = (
        _drive(creds).files()
        .list(
            q=q,
            fields="files(id, name, mimeType, modifiedTime)",
            pageSize=15,
            orderBy="modifiedTime desc",
        )
        .execute()
    )
    files = resp.get("files", [])
    if not files:
        return "No matching Docs/Sheets found."
    lines = []
    for f in files:
        type_label = "Doc" if f.get("mimeType") == _DOC_MIME else "Sheet"
        lines.append(
            f"- [{type_label}] {f.get('name', '')} "
            f"(id={f.get('id', '')}, modified {f.get('modifiedTime', '')})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------

def gdocs_read(creds, document_id: str, max_chars: int = 16000) -> str:
    """Return the document body as plain text."""
    doc = _docs(creds).documents().get(documentId=document_id).execute()
    body = doc.get("body", {}).get("content", [])
    out: list[str] = []
    for el in body:
        para = el.get("paragraph")
        if not para:
            continue
        for run in para.get("elements", []):
            tr = run.get("textRun")
            if tr and "content" in tr:
                out.append(tr["content"])
    text = "".join(out).rstrip()
    return (text[:max_chars] + ("\n…[truncated]" if len(text) > max_chars else "")) or "(empty document)"


def gdocs_append(creds, document_id: str, text: str) -> str:
    """Append text at the end of the document."""
    # Need the current end-of-body index.
    doc = _docs(creds).documents().get(documentId=document_id).execute()
    end_index = doc.get("body", {}).get("content", [{}])[-1].get("endIndex", 1)
    # Inserting at end_index would be past-the-end; insert just before the
    # final newline that Docs reserves at endIndex.
    insert_at = max(end_index - 1, 1)
    requests = [{
        "insertText": {
            "location": {"index": insert_at},
            "text": text if text.startswith("\n") else "\n" + text,
        }
    }]
    _docs(creds).documents().batchUpdate(
        documentId=document_id, body={"requests": requests}
    ).execute()
    return f"Appended {len(text)} characters to document {document_id}."


# ---------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------

def gsheets_list_tabs(creds, spreadsheet_id: str) -> str:
    """List all tabs (sheets) in a spreadsheet."""
    meta = _sheets(creds).spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    tabs = meta.get("sheets", [])
    if not tabs:
        return "(no tabs found)"
    lines = [f"- {s['properties']['title']}" for s in tabs]
    return "\n".join(lines)


def gsheets_read(creds, spreadsheet_id: str, range: str) -> str:
    """Read a range (A1 notation, e.g. 'Sheet1!A1:D10'). Returns CSV-style text."""
    resp = (
        _sheets(creds).spreadsheets().values()
        .get(spreadsheetId=spreadsheet_id, range=range)
        .execute()
    )
    values = resp.get("values", [])
    if not values:
        return f"(empty range {range})"
    return "\n".join(",".join(_csv_cell(c) for c in row) for row in values)


def gsheets_append(creds, spreadsheet_id: str, range: str, values: list[list[Any]]) -> str:
    """Append rows after the last non-empty row in the given range."""
    if not isinstance(values, list) or not values:
        return "[error] values must be a non-empty 2-D array of cells."
    # Allow callers to pass a flat list as a single row.
    if values and not isinstance(values[0], list):
        values = [values]
    resp = (
        _sheets(creds).spreadsheets().values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )
    updates = resp.get("updates", {})
    return (
        f"Appended {updates.get('updatedRows', 0)} row(s) "
        f"({updates.get('updatedCells', 0)} cells) to {updates.get('updatedRange', range)}."
    )


def gsheets_update(creds, spreadsheet_id: str, range: str, values: list[list[Any]]) -> str:
    """Overwrite cells in a range."""
    if not isinstance(values, list) or not values:
        return "[error] values must be a non-empty 2-D array of cells."
    if values and not isinstance(values[0], list):
        values = [values]
    resp = (
        _sheets(creds).spreadsheets().values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        )
        .execute()
    )
    return (
        f"Updated {resp.get('updatedCells', 0)} cell(s) "
        f"in {resp.get('updatedRange', range)}."
    )


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def _escape(s: str) -> str:
    """Escape a literal for use inside Drive's q parameter (single-quoted)."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _csv_cell(value: Any) -> str:
    """Render a cell value as a CSV field (quoted iff necessary)."""
    s = "" if value is None else str(value)
    if any(ch in s for ch in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s
