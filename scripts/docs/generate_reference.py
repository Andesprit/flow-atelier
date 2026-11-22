"""Generate the CLI page and the public HTTP reference from the installed app."""

import argparse
import json
from pathlib import Path

from cli_reference import render_cli_reference
from http_reference import export_openapi, render_http_reference

GENERATED_PAGE = "---\ntemplate: generated-reference.html\n---\n\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    output_dir = parser.parse_args().output_dir

    cli = render_cli_reference()
    schema = export_openapi()
    http = render_http_reference(schema)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "cli.md").write_text(GENERATED_PAGE + cli, encoding="utf-8")
    (output_dir / "http-api.md").write_text(GENERATED_PAGE + http, encoding="utf-8")
    (output_dir / "openapi.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
