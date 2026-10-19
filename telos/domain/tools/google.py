"""Google-family tools — Drive + Docs + Sheets.

All seven tools share a :class:`GoogleCredsResolver`, constructed once
with the deployment-level OAuth client id/secret and the user-registry,
and injected into every Google tool's ``__init__``. The resolver
encapsulates the three-step gate (deployment config → per-user registry
context → cached / freshly-refreshed ``google.oauth2.credentials.Credentials``)
so tools don't reach for ``ctx`` for any of those bits at call time.
"""

import asyncio
import logging
from typing import Any

from pydantic import BaseModel, Field

from telos.core.ports import AgentRegistry

from telos import google_api
from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for
from telos.google_auth import get_credentials

log = logging.getLogger(__name__)


_NOT_CONFIGURED = ToolResult(
    text="Google integration is not configured on this Telos instance.",
    is_error=True,
)
_NO_USER_CONTEXT = ToolResult(
    text="Cannot resolve user context for Google credentials.",
    is_error=True,
)
_NOT_CONNECTED = ToolResult(
    text=(
        "Google account not connected. The user can connect it "
        "at /profile to enable Docs and Sheets tools."
    ),
    is_error=True,
)


class GoogleCredsResolver:
    """Resolves per-user Google ``Credentials``.

    Constructed once per agent with the deployment-level OAuth client
    id/secret + the user registry. Every Google tool gets a reference
    via ``__init__`` and calls :meth:`resolve` per request.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        registry: AgentRegistry | None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._registry = registry

    def resolve(self, user_id: str) -> tuple[Any | None, ToolResult | None]:
        if not (self._client_id and self._client_secret):
            return None, _NOT_CONFIGURED
        if self._registry is None or not user_id:
            return None, _NO_USER_CONTEXT
        creds = get_credentials(
            user_id, self._registry, self._client_id, self._client_secret,
        )
        if creds is None:
            return None, _NOT_CONNECTED
        return creds, None


async def _run_google_op(fn, creds, /, **kwargs) -> ToolResult:
    """Offload a synchronous ``google_api`` call to a worker thread."""
    try:
        text = await asyncio.to_thread(fn, creds, **kwargs)
        return ToolResult(text=text, is_error=False)
    except Exception as e:
        log.warning("Google tool %s failed: %s", getattr(fn, "__name__", repr(fn)), e)
        return ToolResult(text=f"[google error] {e}", is_error=True)


class _GoogleToolBase:
    """Shared scaffolding — every Google tool resolves creds the same way."""

    name: str
    description: str

    def __init__(self, creds: GoogleCredsResolver) -> None:
        self._creds = creds

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)


# ──────────────────────────────────────────────────────────────────────
# Drive
# ──────────────────────────────────────────────────────────────────────


class DriveSearchInput(BaseModel):
    query: str = Field(description="Drive search query — names / fullText:'...' / mimeType filters.")
    kind: str | None = Field(
        default=None,
        description="Optional kind filter: 'document', 'spreadsheet', or 'folder'.",
    )


class DriveSearchTool(_GoogleToolBase):
    name = "drive_search"
    description = "Search the user's Google Drive for documents, sheets, or folders."

    async def __call__(self, inp: DriveSearchInput, ctx: ToolContext) -> ToolResult:
        creds, err = self._creds.resolve(ctx.user_id)
        if err is not None:
            return err
        return await _run_google_op(
            google_api.drive_search, creds, query=inp.query, kind=inp.kind,
        )


# ──────────────────────────────────────────────────────────────────────
# Docs
# ──────────────────────────────────────────────────────────────────────


class GDocsReadInput(BaseModel):
    document_id: str = Field(description="Google Docs document ID (from the doc URL).")


class GDocsReadTool(_GoogleToolBase):
    name = "gdocs_read"
    description = "Fetch a Google Doc's plain-text content."

    async def __call__(self, inp: GDocsReadInput, ctx: ToolContext) -> ToolResult:
        creds, err = self._creds.resolve(ctx.user_id)
        if err is not None:
            return err
        return await _run_google_op(
            google_api.gdocs_read, creds, document_id=inp.document_id,
        )


class GDocsAppendInput(BaseModel):
    document_id: str = Field(description="Google Docs document ID.")
    text: str = Field(description="Text to append at the end of the doc.")


class GDocsAppendTool(_GoogleToolBase):
    name = "gdocs_append"
    description = "Append text to the end of a Google Doc."

    async def __call__(self, inp: GDocsAppendInput, ctx: ToolContext) -> ToolResult:
        creds, err = self._creds.resolve(ctx.user_id)
        if err is not None:
            return err
        return await _run_google_op(
            google_api.gdocs_append, creds, document_id=inp.document_id, text=inp.text,
        )


# ──────────────────────────────────────────────────────────────────────
# Sheets
# ──────────────────────────────────────────────────────────────────────


class GSheetsListTabsInput(BaseModel):
    spreadsheet_id: str = Field(description="Google Sheets spreadsheet ID.")


class GSheetsListTabsTool(_GoogleToolBase):
    name = "gsheets_list_tabs"
    description = "List the tabs (worksheets) of a Google Sheets spreadsheet."

    async def __call__(self, inp: GSheetsListTabsInput, ctx: ToolContext) -> ToolResult:
        creds, err = self._creds.resolve(ctx.user_id)
        if err is not None:
            return err
        return await _run_google_op(
            google_api.gsheets_list_tabs, creds, spreadsheet_id=inp.spreadsheet_id,
        )


class GSheetsReadInput(BaseModel):
    spreadsheet_id: str = Field(description="Google Sheets spreadsheet ID.")
    range: str = Field(description="A1 notation range, e.g. 'Sheet1!A1:C10'.")


class GSheetsReadTool(_GoogleToolBase):
    name = "gsheets_read"
    description = "Read a range from a Google Sheets spreadsheet."

    async def __call__(self, inp: GSheetsReadInput, ctx: ToolContext) -> ToolResult:
        creds, err = self._creds.resolve(ctx.user_id)
        if err is not None:
            return err
        return await _run_google_op(
            google_api.gsheets_read, creds,
            spreadsheet_id=inp.spreadsheet_id, range=inp.range,
        )


class GSheetsAppendInput(BaseModel):
    spreadsheet_id: str = Field(description="Google Sheets spreadsheet ID.")
    range: str = Field(description="A1 notation range to append after, e.g. 'Sheet1!A1'.")
    values: list[list[Any]] = Field(description="Rows of cell values to append.")


class GSheetsAppendTool(_GoogleToolBase):
    name = "gsheets_append"
    description = "Append rows to a Google Sheets spreadsheet."

    async def __call__(self, inp: GSheetsAppendInput, ctx: ToolContext) -> ToolResult:
        creds, err = self._creds.resolve(ctx.user_id)
        if err is not None:
            return err
        return await _run_google_op(
            google_api.gsheets_append, creds,
            spreadsheet_id=inp.spreadsheet_id, range=inp.range, values=inp.values,
        )


class GSheetsUpdateInput(BaseModel):
    spreadsheet_id: str = Field(description="Google Sheets spreadsheet ID.")
    range: str = Field(description="A1 notation range to overwrite, e.g. 'Sheet1!A1:B2'.")
    values: list[list[Any]] = Field(description="Rows of cell values to write.")


class GSheetsUpdateTool(_GoogleToolBase):
    name = "gsheets_update"
    description = "Overwrite cells in a Google Sheets spreadsheet range."

    async def __call__(self, inp: GSheetsUpdateInput, ctx: ToolContext) -> ToolResult:
        creds, err = self._creds.resolve(ctx.user_id)
        if err is not None:
            return err
        return await _run_google_op(
            google_api.gsheets_update, creds,
            spreadsheet_id=inp.spreadsheet_id, range=inp.range, values=inp.values,
        )
