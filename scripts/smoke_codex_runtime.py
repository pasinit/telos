"""Run authenticated text + host-tool/structured-output checks before cutover."""
import asyncio

from pontifex.codex import CodexRunConfig, CodexSession

from telos.api.settings import Settings
from telos.services.nightly.tool_definitions import ToolDefinition, ToolServer, codex_tools


async def main():
    settings = Settings()
    session = CodexSession()
    result = await session.run(config=CodexRunConfig(model=settings.codex_model),
                               system_prompt="Reply with exactly TELOS_CODEX_OK.", prompt="Run the smoke check.")
    if result.is_error or (result.result_text or "").strip() != "TELOS_CODEX_OK":
        raise RuntimeError(result.error or f"Unexpected text: {result.result_text!r}")
    called = []

    async def echo(args):
        called.append(args)
        return {"content": [{"type": "text", "text": args["value"]}]}

    schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}
    server = ToolServer("nightly", [ToolDefinition("echo_probe", "Echo the test value.", schema, echo)])
    specs, handlers = codex_tools({"nightly": server}, ["mcp__nightly__echo_probe"])
    config = CodexRunConfig(model=settings.codex_model, output_schema=schema, dynamic_tools=specs,
                            handlers=handlers, max_tool_calls=2)
    result = await session.run(config=config, system_prompt="Call host__nightly__echo_probe once, then return its value in the output schema.",
                               prompt="Echo TELOS_TOOL_OK.")
    if result.is_error or called != [{"value": "TELOS_TOOL_OK"}] or result.structured_output != {"value": "TELOS_TOOL_OK"}:
        raise RuntimeError(result.error or "Tool/structured-output smoke failed")
    print(f"Codex text, host tool, and structured output verified (model={result.model}, billing={result.billing_mode}).")


if __name__ == "__main__":
    asyncio.run(main())
