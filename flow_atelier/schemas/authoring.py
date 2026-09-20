"""JSON Schema for a hand-written ``conduit.yaml``.

:meth:`Conduit.model_json_schema` describes the *normalized* model, but a
conduit file on disk may also use the two shorthands
``Conduit._normalize_tasks`` accepts: an input written as a plain
description string, and a task wrapped in a single-key mapping whose key
supplies its name. This module reshapes the generated document so both
forms validate. Every field still comes from the models — there is no
second catalogue to keep in step.
"""
from __future__ import annotations

from typing import Any

from flow_atelier.schemas.conduit import Conduit

DIALECT = "https://json-schema.org/draft/2020-12/schema"

_DESCRIPTION = (
    "Structure of a Flow Atelier conduit.yaml, generated from flow-atelier "
    "{version}; regenerate it after upgrading. Shape only: dependency "
    "references, duplicate task names, loop predicate meaning, templates and "
    "whether a tool is installed are checked by `atelier check <name>`."
)


def conduit_json_schema() -> dict[str, Any]:
    """Build the authoring schema for a conduit file.

    :returns: a self-contained JSON Schema object; every ``$ref`` in it
        points at a local ``#/$defs/`` entry.
    """
    from flow_atelier import __version__

    schema = Conduit.model_json_schema(by_alias=True)
    defs = schema["$defs"]

    # `- greet:` puts the name in the mapping key, so the body below it does
    # not repeat one. A body that does carry an explicit `name` still wins,
    # exactly as the loader has it, so `name` stays allowed — only required
    # drops. Everything else is the generated task, shared by reference.
    body = dict(defs["TaskDefinition"])
    body["title"] = "WrappedTaskBody"
    body["description"] = "A task whose name comes from the mapping key above it."
    body["required"] = [f for f in body["required"] if f != "name"]
    defs["WrappedTaskBody"] = body

    schema["properties"]["tasks"]["items"] = {
        "anyOf": [
            {"$ref": "#/$defs/TaskDefinition"},
            {
                "title": "WrappedTask",
                "description": "`- <name>:` with the task body indented under it.",
                "type": "object",
                "minProperties": 1,
                "maxProperties": 1,
                "additionalProperties": {"$ref": "#/$defs/WrappedTaskBody"},
            },
        ]
    }

    schema["properties"]["inputs"]["additionalProperties"] = {
        "anyOf": [
            {
                "type": "string",
                "title": "InputDescription",
                "description": "Shorthand for `{description: <this text>}`.",
            },
            {"$ref": "#/$defs/InputSpec"},
        ]
    }

    schema["title"] = "Flow Atelier conduit"
    schema["description"] = _DESCRIPTION.format(version=__version__)
    return {"$schema": DIALECT, **schema}
