"""Export and render the public REST API from FastAPI's OpenAPI document."""

import re
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.api.app import FastApiServer

# Browser sessions, dashboard chat, mobile launches, Web Push and SPA assets.
DASHBOARD_PATHS = {"session", "chat", "mobile", "push", "favicon.svg", "{full_path}"}
SCHEMA_REF = "#/components/schemas/"
SPHINX_FIELDS = re.compile(r"\n\s*:(?:param|returns?|raises?|rtype|yields?)\b")


def schema_references(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        if value.get("$ref", "").startswith(SCHEMA_REF):
            yield value["$ref"].removeprefix(SCHEMA_REF)
        for item in value.values():
            yield from schema_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from schema_references(item)


def export_openapi() -> dict:
    with TemporaryDirectory(prefix="atelier-openapi-") as tmp:
        root = Path(tmp)
        settings = AtelierSettings(
            atelier_dir=root / "project", global_atelier_dir=root / "global", _env_file=None
        )
        schema = FastApiServer().create_app(Atelier(settings=settings)).openapi()

    schema["paths"] = {
        path: methods
        for path, methods in schema["paths"].items()
        if path.split("/", 2)[1] not in DASHBOARD_PATHS
    }

    # Keep the transitive model references for the remaining operations.
    models = schema["components"]["schemas"]
    pending = set(schema_references(schema["paths"]))
    used: set[str] = set()
    while pending:
        name = pending.pop()
        used.add(name)
        pending.update(set(schema_references(models[name])) - used)
    schema["components"]["schemas"] = {
        name: model for name, model in models.items() if name in used
    }
    return schema


def schema_type(schema: dict) -> str:
    if "$ref" in schema:
        return schema["$ref"].removeprefix(SCHEMA_REF)
    if schema.get("type") == "array":
        return f"array of {schema_type(schema.get('items', {}))}"
    if "anyOf" in schema:
        return " or ".join(schema_type(item) for item in schema["anyOf"])
    return schema.get("type", "any")


def content_types(content: dict) -> str:
    return ", ".join(
        f"`{schema_type(media.get('schema', {}))}` (`{mime}`)" for mime, media in content.items()
    )


def render_http_reference(schema: dict) -> str:
    lines = [
        "# HTTP API reference",
        "",
        "`atelier serve` exposes the API at `http://127.0.0.1:8000` by default.",
        "Download the [OpenAPI schema](openapi.json) for full request and response models.",
        "",
        "## Authentication",
        "",
        "On loopback, the API accepts requests without a token by default. Non-loopback binds",
        "require `ATELIER_API_TOKEN`. Whenever the server has a token configured, send",
        "`Authorization: Bearer <token>` with HTTP requests. Missing or invalid credentials",
        "return `401 Unauthorized`. The dashboard can also authenticate with a session cookie.",
        "",
        "Status codes under **Declared responses** come from the OpenAPI schema. Operation",
        "descriptions cover additional behavior implemented in the handlers.",
        "",
    ]
    groups = defaultdict(list)
    for path, methods in sorted(schema["paths"].items()):
        for method, operation in sorted(methods.items()):
            tag = (operation.get("tags") or [path.split("/", 2)[1]])[0]
            groups[tag].append((method, path, operation))

    for tag, operations in sorted(groups.items()):
        lines.extend([f"## {tag.title()}", ""])
        for method, path, operation in operations:
            lines.extend([f"### `{method.upper()} {path}`", ""])
            description = SPHINX_FIELDS.split(operation.get("description", ""), maxsplit=1)[0]
            lines.extend([description.strip() or operation["summary"], ""])

            if parameters := operation.get("parameters"):
                lines.extend(["**Parameters**", ""])
                for param in parameters:
                    required = "required" if param.get("required") else "optional"
                    kind = schema_type(param["schema"])
                    line = f"- `{param['name']}` ({param['in']}, `{kind}`, {required})"
                    if detail := param.get("description"):
                        line += f" — {detail}"
                    lines.append(line)
                lines.append("")

            if body := operation.get("requestBody"):
                required = "required" if body.get("required") else "optional"
                content = content_types(body["content"])
                lines.extend([f"**Request body** ({required}) {content}", ""])

            lines.extend(["**Declared responses**", ""])
            for status, response in operation["responses"].items():
                line = f"- `{status}` — {response['description']}"
                if content := response.get("content"):
                    line += f". {content_types(content)}"
                lines.append(line)
            lines.append("")

    return "\n".join(lines)
