"""Adapt standard LaTeX delimiters to Telegram Rich Markdown math syntax."""

import re


_CONTAINER = r"[ \t]*(?:>[ \t]*)*(?:(?:[-+*]|\d+[.)])[ \t]+)?"
_FENCE = re.compile(rf"(?m)^{_CONTAINER}(?P<fence>`{{3,}}|~{{3,}})[^\n]*(?:\n|$)")
_INDENTED = re.compile(r"(?m)^(?: {4}|\t)[^\n]*(?:\n|$)")
_BACKTICKS = re.compile(r"`+")
_AUTOLINK = re.compile(r"<[A-Za-z][A-Za-z0-9+.-]*:[^<>\s]*>")


def _escaped(text: str, index: int) -> bool:
    start = index
    while start and text[start - 1] == "\\":
        start -= 1
    return (index - start) % 2 == 1


def _code_end(text: str, index: int, *, indented: bool = True) -> int | None:
    fence = _FENCE.match(text, index)
    if fence:
        marker = fence["fence"]
        closing = re.compile(rf"(?m)^{_CONTAINER}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*(?:\n|$)")
        match = closing.search(text, fence.end())
        return match.end() if match else len(text)
    if indented:
        line = _INDENTED.match(text, index)
        if line:
            return line.end()
    if text[index] == "`" and not _escaped(text, index):
        opening = _BACKTICKS.match(text, index)
        for closing in _BACKTICKS.finditer(text, opening.end()):
            if len(closing[0]) == len(opening[0]):
                return closing.end()
    return None


def _dollar_end(text: str, index: int) -> int | None:
    if _escaped(text, index):
        return None
    delimiter = "$$" if text.startswith("$$", index) else "$"
    start = index + len(delimiter)
    if delimiter == "$" and (start == len(text) or text[start].isspace()):
        return None
    cursor = start
    while cursor < len(text):
        if delimiter == "$" and text[cursor] == "\n":
            return None
        if text.startswith(delimiter, cursor) and not _escaped(text, cursor):
            end = cursor + len(delimiter)
            if delimiter == "$" and (text[cursor - 1].isspace() or (end < len(text) and text[end].isdigit())):
                return None
            return end
        cursor += 1
    return None


def _math_end(text: str, start: int, closing: str, *, inline: bool) -> int | None:
    cursor = start
    while cursor < len(text):
        if inline and text[cursor] == "\n":
            return None
        if text.startswith(closing, cursor) and not _escaped(text, cursor):
            return cursor
        if _code_end(text, cursor, indented=False) is not None:
            return None
        cursor += 1
    return None


def _destination_end(text: str, index: int) -> int | None:
    if not text.startswith("](", index):
        return None
    depth = 1
    cursor = index + 2
    while cursor < len(text):
        if not _escaped(text, cursor):
            if text[cursor] == "(":
                depth += 1
            elif text[cursor] == ")":
                depth -= 1
                if not depth:
                    return cursor + 1
        cursor += 1
    return None


def normalize_math_delimiters(markdown: str) -> str:
    """Normalize single-line inline and display math while preserving TeX/code.

    Existing dollar math, code spans/fences, escaped delimiters, and unmatched
    delimiters stay unchanged. Inline boundary whitespace is trimmed because
    Telegram's Markdown parser does not recognize ``$ x $`` as inline math.
    """
    result: list[str] = []
    cursor = 0
    while cursor < len(markdown):
        protected_end = _code_end(markdown, cursor) or _destination_end(markdown, cursor)
        if protected_end is None and markdown[cursor] == "$":
            protected_end = _dollar_end(markdown, cursor)
        if protected_end is None and markdown[cursor] == "<":
            autolink = _AUTOLINK.match(markdown, cursor)
            protected_end = autolink.end() if autolink else None
        if protected_end is not None:
            result.append(markdown[cursor:protected_end])
            cursor = protected_end
            continue
        if markdown[cursor] == "\\" and (cursor == 0 or markdown[cursor - 1] != "\\"):
            opening = markdown[cursor:cursor + 2]
            if opening in {"\\(", "\\["}:
                inline = opening == "\\("
                closing = "\\)" if inline else "\\]"
                end = _math_end(markdown, cursor + 2, closing, inline=inline)
                if end is not None:
                    body = markdown[cursor + 2:end]
                    body = body.strip() if inline else body
                    if body.strip():
                        delimiter = "$" if inline else "$$"
                        result.append(f"{delimiter}{body}{delimiter}")
                        cursor = end + 2
                        continue
        result.append(markdown[cursor])
        cursor += 1
    return "".join(result)
