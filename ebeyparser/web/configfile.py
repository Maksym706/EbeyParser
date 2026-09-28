"""Comment-preserving edits of config.yaml and .env (used by the setup wizard, the
/setup and /settings pages and the search editor).

Everything is line-based so the user's comments and layout survive; each edit is
re-parsed and checked, and anything unusual falls back to a plain YAML re-dump
(data kept, comments lost) instead of writing a broken file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..config import SearchConfig, save_searches

ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLE_ENV = ROOT / ".env.example"
_MISSING = object()
# fields always written for a search, even when they have the default value (readability)
_ALWAYS_WRITTEN = ("name", "source", "enabled", "purpose")


class _YamlEditError(Exception):
    """The YAML layout is too unusual for a line-based edit (flow style, lists, tabs)."""


def yaml_scalar(value: Any) -> str:
    """Python scalar -> YAML text: True -> 'true', 40.0 -> '40', 'http://x' -> 'http://x'."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    text = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True, width=10_000).strip()
    return text.removesuffix("...").strip()


def _split_comment(rest: str) -> tuple[str, str]:
    """' value   # note\\n' -> (' value', '   # note')."""
    rest = rest.rstrip("\r\n")
    m = re.search(r"\s+#", rest)
    return (rest[: m.start()], rest[m.start():]) if m else (rest, "")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    body = line.strip()
    return bool(body) and not body.startswith("#")


def _yaml_set_line(lines: list[str], keys: list[str], value: Any) -> list[str]:
    """Set `a.b.c: value` in block-style YAML lines, keeping comments; insert missing keys."""
    start, end, parent_indent = 0, len(lines), -1
    for depth, key in enumerate(keys):
        child_indent: int | None = None
        found: int | None = None
        for i in range(start, end):
            if not _is_content(lines[i]):
                continue
            if "\t" in lines[i][: len(lines[i]) - len(lines[i].lstrip())]:
                raise _YamlEditError("tabs")
            if child_indent is None:
                child_indent = _indent_of(lines[i])
            if _indent_of(lines[i]) != child_indent:
                continue
            body = lines[i].strip()
            if body == "-" or body.startswith("- "):
                raise _YamlEditError("list where a mapping was expected")
            if re.match(rf"{re.escape(key)}\s*:(\s|$)", body):
                found = i
                break
        if found is None:  # add the missing key (and sub-keys) right under the parent
            if child_indent is not None:
                indent = child_indent
            else:
                indent = parent_indent + 2 if parent_indent >= 0 else 0
            new = []
            for d, k in enumerate(keys[depth:]):
                tail = f" {yaml_scalar(value)}" if depth + d == len(keys) - 1 else ""
                new.append(f"{' ' * (indent + 2 * d)}{k}:{tail}\n")
            if depth == 0:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                return lines + new
            return lines[:start] + new + lines[start:]
        assert child_indent is not None
        block_end = end
        for j in range(found + 1, end):
            if _is_content(lines[j]) and _indent_of(lines[j]) <= child_indent:
                block_end = j
                break
        inline, comment = _split_comment(lines[found].split(":", 1)[1])
        if depth == len(keys) - 1:
            if any(_is_content(ln) for ln in lines[found + 1:block_end]):
                raise _YamlEditError(f"{key} is a block, not a value")
            newline = "\n" if lines[found].endswith("\n") else ""
            lines[found] = f"{' ' * child_indent}{key}: {yaml_scalar(value)}{comment}{newline}"
            return lines
        if inline.strip():
            raise _YamlEditError(f"{key} is written inline")
        start, end, parent_indent = found + 1, block_end, child_indent
    return lines


def _dig(data: Any, keys: list[str]) -> Any:
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return _MISSING
        data = data[key]
    return data


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig") if path.is_file() else ""


def update_yaml_values(path: str | Path, updates: dict[str, Any]) -> None:
    """Set scalar values by dotted key ({"ai.model": "x", "general.interval_minutes": 20})."""
    path = Path(path)
    original = _read(path)
    try:
        lines = original.splitlines(keepends=True)
        for dotted, value in updates.items():
            lines = _yaml_set_line(lines, dotted.split("."), value)
        text = "".join(lines)
        data = yaml.safe_load(text) or {}
        ok = isinstance(data, dict) and all(_dig(data, k.split(".")) == v for k, v in updates.items())
    except (_YamlEditError, yaml.YAMLError):
        ok = False
    if not ok:
        data = yaml.safe_load(original) or {}
        for dotted, value in updates.items():
            node = data
            *parents, last = dotted.split(".")
            for key in parents:
                if not isinstance(node.get(key), dict):
                    node[key] = {}
                node = node[key]
            node[last] = value
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    path.write_text(text, encoding="utf-8")


def search_to_yaml(search: SearchConfig) -> dict[str, Any]:
    """Compact dict for YAML: non-default fields plus name/source/enabled/purpose."""
    data = search.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    full = search.model_dump(mode="json")
    head = {k: full[k] for k in _ALWAYS_WRITTEN if k in full}
    return {**head, **{k: v for k, v in data.items() if k not in head}}


def _searches_block(searches: Iterable[SearchConfig]) -> str:
    items = [search_to_yaml(s) for s in searches]
    if not items:
        return "searches: []\n"
    body = yaml.safe_dump(items, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120)
    lines = ["searches:\n"]
    for line in body.splitlines():
        if line.startswith("- ") and len(lines) > 1:
            lines.append("\n")  # blank line between searches
        lines.append(f"  {line}\n")
    return "".join(lines)


def save_searches_block(path: str | Path, searches: list[SearchConfig]) -> None:
    """Rewrite only the top-level `searches:` block; every other line (comments, ${ENV}
    placeholders) stays as written. Falls back to config.save_searches if the file is odd."""
    path = Path(path)
    original = _read(path)
    lines = original.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(r"^searches\s*:", ln)), None)
    block = _searches_block(searches)
    if start is None:
        prefix = original if not original or original.endswith("\n") else original + "\n"
        text = prefix + ("\n" if prefix.strip() else "") + block
    else:
        end = next((j for j in range(start + 1, len(lines))
                    if _is_content(lines[j]) and not lines[j][0].isspace()), len(lines))
        # column-0 comments / blank lines right before the next section belong to that section
        keep_from = end
        while keep_from > start + 1 and (not lines[keep_from - 1].strip()
                                         or lines[keep_from - 1].startswith("#")):
            keep_from -= 1
        tail = lines[keep_from:]
        text = "".join(lines[:start]) + block + ("\n" if tail and lines[keep_from].strip() else "") + "".join(tail)
    expected = [search_to_yaml(s) for s in searches]
    try:
        data = yaml.safe_load(text) or {}
        before = yaml.safe_load(original) or {}
        ok = (isinstance(data, dict) and isinstance(before, dict) and (data.get("searches") or []) == expected
              and {k: v for k, v in data.items() if k != "searches"}
              == {k: v for k, v in before.items() if k != "searches"})
    except yaml.YAMLError:
        ok = False
    if not ok:
        save_searches(path, searches)
        return
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------- .env
def read_env_file(path: str | Path) -> dict[str, str]:
    path = Path(path)
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip().removeprefix("export ").strip()] = value.strip().strip('"').strip("'")
    return out


def write_env_values(path: str | Path, values: dict[str, str], *, set_environ: bool = True) -> None:
    """Set KEY=value lines in .env (created from .env.example when missing); other lines kept.
    With `set_environ` the running process sees the new values too (for ${VAR} in config)."""
    path = Path(path)
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif EXAMPLE_ENV.is_file():
        lines = EXAMPLE_ENV.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    for key, value in values.items():
        value = str(value).replace("\n", " ").strip()
        new = f"{key}={value}"
        for i, line in enumerate(lines):
            if re.match(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=", line):
                lines[i] = new
                break
        else:
            lines.append(new)
        if set_environ:
            os.environ[key] = value
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = [
    "read_env_file",
    "save_searches_block",
    "search_to_yaml",
    "update_yaml_values",
    "write_env_values",
    "yaml_scalar",
]
