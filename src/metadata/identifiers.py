"""Helpers for identifiers emitted by database and MCP catalogs."""

from __future__ import annotations

from typing import List


def split_qualified_identifier(value: str) -> List[str]:
    """Split a possibly quoted qualified identifier into unquoted components.

    Catalog providers may emit the same table as ``dimdate``,
    ``public.dimdate`` or ``"public"."dimdate"``. Dots inside quoted
    identifiers are preserved.
    """
    text = (value or "").strip()
    if not text:
        return []

    parts: List[str] = []
    current: List[str] = []
    quote: str | None = None
    bracketed = False
    index = 0

    while index < len(text):
        char = text[index]

        if bracketed:
            if char == "]":
                if index + 1 < len(text) and text[index + 1] == "]":
                    current.append("]")
                    index += 2
                    continue
                bracketed = False
            else:
                current.append(char)
            index += 1
            continue

        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    current.append(quote)
                    index += 2
                    continue
                quote = None
            else:
                current.append(char)
            index += 1
            continue

        if char == "[":
            bracketed = True
        elif char in {'"', "`"}:
            quote = char
        elif char == ".":
            component = "".join(current).strip()
            if component:
                parts.append(component)
            current = []
        else:
            current.append(char)
        index += 1

    component = "".join(current).strip()
    if component:
        parts.append(component)
    return parts


def table_name_from_identifier(value: str) -> str:
    """Return the unqualified, lower-cased table component."""
    parts = split_qualified_identifier(value)
    return parts[-1].lower() if parts else ""


def table_column_from_identifier(value: str) -> tuple[str, str]:
    """Return lower-cased ``(table, column)`` from a qualified column."""
    parts = split_qualified_identifier(value)
    if len(parts) < 2:
        return "", ""
    return parts[-2].lower(), parts[-1].lower()
