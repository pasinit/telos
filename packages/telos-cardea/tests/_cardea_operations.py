"""Test-only copy of Cardea's descriptor validation, for environments without Cardea.

Copied from pasinit/cardea ``apps/api/src/cardea/service/operations.py`` at
b15119ba1ea512356a2522e1e682768c2ac35705, keeping
``Binding``, ``OperationDescriptor`` and the argument/output checks of
``Registry``. Cardea's ``Record``, ``Right`` and ``OperationName`` are inlined.
``conftest.py`` prefers the real Cardea when it is installed
(``uv sync --group cardea``); CI runs without it.
"""

import json
from typing import Annotated, Any, Literal, Self, get_args

import jsonpointer
import mcp.types
import rfc8785
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, model_validator

SOURCE_COMMIT = "b15119ba1ea512356a2522e1e682768c2ac35705"

Right = Literal[
    "objects.read", "objects.write", "tasks.inspect", "tasks.cancel", "tasks.resume", "events.read", "tasks.submit",
    "host.sha256", "host.copy", "models.invoke", "models.disclose", "tools.incidents", "inputs.append", "tasks.delegate",
]
OperationName = Annotated[str, Field(strict=True, max_length=128, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")]
KERNEL_RIGHTS: frozenset[str] = frozenset(get_args(Right))
RESERVED_PREFIXES = ("objects.", "tasks.", "events.", "host.", "models.", "tools.", "inputs.")
MAX_OUTPUT_BYTES = 262144
_NAME: TypeAdapter[str] = TypeAdapter(OperationName)
HandlerName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
Pointer = Annotated[str, Field(pattern=r"^(/([^~/]|~[01])*)+$", max_length=256)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Binding(Record):
    version: Annotated[int, Field(strict=True, ge=1)]
    effect: Literal["read", "effect"]
    handler: HandlerName
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=600)] = 30
    max_output_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_OUTPUT_BYTES)] = 65536
    requires: tuple[Right, ...] = ()
    idempotency_scope: Annotated[tuple[Pointer, ...], Field(max_length=16)] | None = None
    reconciler: HandlerName | None = None

    @model_validator(mode="after")
    def effect_only(self) -> Self:
        if self.effect == "read" and (self.idempotency_scope is not None or self.reconciler):
            raise ValueError("Read operations have no effect scope or reconciler")
        return self


class OperationDescriptor(Record):
    tool: dict[str, JsonValue]
    cardea: Binding

    @model_validator(mode="after")
    def standard_tool(self) -> Self:
        try:
            tool = mcp.types.Tool.model_validate(self.tool)
        except ValidationError as error:
            raise ValueError("Descriptor tool is not a valid MCP Tool") from error
        _NAME.validate_python(tool.name)
        if tool.name in KERNEL_RIGHTS or tool.name.startswith(RESERVED_PREFIXES):
            raise ValueError("Operation name is reserved for kernel rights")
        if tool.output_schema is None:
            raise ValueError("Installed operations require an output schema for verification")
        for schema in (tool.input_schema, tool.output_schema):
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as error:
                raise ValueError("Operation schema is not valid JSON Schema 2020-12") from error
            if schema.get("type") != "object":
                raise ValueError("Operation input and output schemas must describe objects")
        return self

    @property
    def name(self) -> str:
        return str(self.tool["name"])

    @property
    def input_schema(self) -> dict[str, Any]:
        return mcp.types.Tool.model_validate(self.tool).input_schema

    @property
    def output_schema(self) -> dict[str, Any]:
        schema = mcp.types.Tool.model_validate(self.tool).output_schema
        assert schema is not None
        return schema


def scope_values(descriptor: OperationDescriptor, arguments: dict[str, Any]) -> list[Any]:
    """The argument values Cardea's ``Registry.scope_key`` digests."""
    assert descriptor.cardea.idempotency_scope is not None
    return [jsonpointer.resolve_pointer(arguments, pointer) for pointer in descriptor.cardea.idempotency_scope]


def valid_arguments(descriptor: OperationDescriptor, arguments: dict[str, Any]) -> bool:
    return Draft202012Validator(descriptor.input_schema).is_valid(arguments)


def check_output(descriptor: OperationDescriptor, output: object) -> bytes | None:
    """Same rule as ``Registry.check_output``: schema-valid and within ``max_output_bytes``."""
    try:
        content = rfc8785.dumps(json.loads(json.dumps(output)))
    except (TypeError, ValueError):
        return None
    if len(content) > descriptor.cardea.max_output_bytes:
        return None
    if not Draft202012Validator(descriptor.output_schema).is_valid(output):
        return None
    return content
