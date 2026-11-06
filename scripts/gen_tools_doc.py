"""Generate docs/tools.md from the tool definitions the server actually registers.

    python scripts/gen_tools_doc.py            # rewrite docs/tools.md
    python scripts/gen_tools_doc.py --check    # exit 1 if docs/tools.md is out of date

The tests run the --check logic, so the reference can't drift from the code.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "tools.md"

HEADER = """# Tool reference

Every tool `search-console-mcp` exposes, with its parameters. You don't call tools by name: ask Claude in plain
language and it picks the right one. The grouped overview is in the [README](../README.md#what-it-can-do).

_This file is generated from the server's tool definitions by `scripts/gen_tools_doc.py`. Don't edit it by hand._

Conventions used by most tools:

- **`site`** is a Search Console property: `sc-domain:example.com`, a URL-prefix property such as
  `https://www.example.com/`, or just `example.com`. Use `list_properties` to see the exact values.
- **Dates** are `YYYY-MM-DD` in Pacific Time, as in Search Console. `days` counts back from yesterday and is ignored
  when `start_date` is given. Comparisons end on the latest complete day.
- **Access.** Nothing here can change Search Console. Tools marked *saves locally* also write results to your own
  computer (the local database, CSV or Markdown files).
"""


def _range(schema: dict) -> str:
    lo, hi = schema.get("minimum"), schema.get("maximum")
    xlo, xhi = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
    if lo is not None and hi is not None:
        return f"{lo:g}–{hi:g}"
    if xlo is not None and xhi is not None:
        return f"{xlo:g}–{xhi:g}, exclusive"
    if lo is not None:
        return f"≥ {lo:g}"
    if hi is not None:
        return f"≤ {hi:g}"
    if xlo is not None:
        return f"> {xlo:g}"
    if xhi is not None:
        return f"< {xhi:g}"
    return ""


def type_of(schema: dict) -> str:
    """Readable type for a JSON-schema fragment (None-able unions are shown without the null)."""
    if "anyOf" in schema:
        options = [o for o in schema["anyOf"] if o.get("type") != "null"]
        return " or ".join(type_of(o) for o in options)
    if "enum" in schema:
        return " / ".join(f"`{v}`" for v in schema["enum"])
    kind = schema.get("type", "any")
    if kind == "array":
        return f"list of {type_of(schema.get('items', {}))}"
    if kind in ("integer", "number"):
        bounds = _range(schema)
        return f"{kind} ({bounds})" if bounds else kind
    return kind


def default_of(name: str, schema: dict, required: bool) -> str:
    if required:
        return "**required**"
    value = schema.get("default")
    if value is None:
        return "–"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    return f"`{value}`"


def cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def render_tool(tool) -> str:
    props = tool.input_schema.get("properties", {})
    required = set(tool.input_schema.get("required", []))
    saves = tool.annotations is not None and tool.annotations.read_only_hint is False
    access = ("Reads data; also *saves locally* (database or files). Changes nothing in Search Console."
              if saves else "Reads data. Changes nothing in Search Console.")
    lines = [f"### `{tool.name}`", "", cell(tool.description or ""), "", f"_{access}_", ""]
    if not props:
        lines += ["No parameters.", ""]
        return "\n".join(lines)
    lines += ["| Parameter | Type | Default | Description |", "| --- | --- | --- | --- |"]
    for name, schema in props.items():
        lines.append(f"| `{name}` | {cell(type_of(schema))} | {default_of(name, schema, name in required)} "
                     f"| {cell(schema.get('description', ''))} |")
    lines.append("")
    return "\n".join(lines)


def render() -> str:
    from search_console_mcp import server

    tools = sorted(asyncio.run(server.mcp.list_tools()), key=lambda t: t.name)
    index = ", ".join(f"[`{t.name}`](#{t.name})" for t in tools)
    parts = [HEADER, f"**{len(tools)} tools:** {index}\n", "---\n"]
    parts += [render_tool(t) for t in tools]
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != text:
            print(f"{OUT} is out of date. Run: python scripts/gen_tools_doc.py", file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
