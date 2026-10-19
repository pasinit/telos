"""Pydantic → OpenAI function-call schema derivation.

Tool inputs are Pydantic ``BaseModel`` classes; the LLM-facing JSON
schema is *derived* from the model rather than hand-rolled. Same source
of truth for both runtime validation and the schema the LLM sees, so
they can't drift apart.

The Input class is discovered by introspecting the tool's ``__call__``
signature — no need for the tool to also declare it as a class
attribute. One source of truth per tool.
"""

import inspect
from typing import Any

from pydantic import BaseModel


def input_class_of(tool_cls: type) -> type[BaseModel]:
    """Read the Pydantic input class off the tool class's ``__call__``
    signature. Raises ``TypeError`` if the annotation isn't a
    ``BaseModel`` subclass.
    """
    sig = inspect.signature(tool_cls.__call__)
    if "inp" not in sig.parameters:
        raise TypeError(f"{tool_cls.__name__}.__call__ has no `inp` parameter")
    ann = sig.parameters["inp"].annotation
    if not (isinstance(ann, type) and issubclass(ann, BaseModel)):
        raise TypeError(
            f"{tool_cls.__name__}.__call__'s `inp` must be annotated as a "
            f"Pydantic BaseModel subclass; got {ann!r}"
        )
    return ann


def schema_from_pydantic(
    InputModel: type[BaseModel], *, name: str, description: str,
) -> dict[str, Any]:
    """Return the OpenAI function-call schema for a Pydantic-typed tool."""
    params = InputModel.model_json_schema()
    # Pydantic adds ``title`` and ``$defs`` blocks that OAI doesn't need.
    params.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": params,
        },
    }


def oai_schema_for(tool_cls: type, *, name: str, description: str) -> dict[str, Any]:
    """Convenience — derive the schema directly from the Tool *class*.

    Used by :mod:`telos.tools` (the catalogue) which has classes but not
    instances; tools themselves use this in their ``oai_schema()`` method.
    """
    return schema_from_pydantic(input_class_of(tool_cls), name=name, description=description)


def coerce_args(InputModel: type[BaseModel], raw_args: dict[str, Any]) -> BaseModel:
    """Validate the LLM-supplied JSON ``raw_args`` against the input model.

    Pydantic raises :class:`pydantic.ValidationError` on mismatch — the
    executor catches this and surfaces a tool error to the model so it
    can correct itself.
    """
    return InputModel.model_validate(raw_args)
