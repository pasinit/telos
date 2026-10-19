"""Shared test helpers."""

from typing import Any

from telos.domain.tools.schema import input_class_of


async def dispatch(tool, args: dict[str, Any], ctx):
    """Mirror what ``_dispatch_tool_call`` does to a Tool: coerce the
    raw arg dict through the tool's Pydantic input model (discovered
    via ``input_class_of``), then call.

    Used by unit tests that exercise a single Tool directly without
    going through the full executor plumbing.
    """
    InputModel = input_class_of(type(tool))
    return await tool(InputModel.model_validate(args), ctx)
