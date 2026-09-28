"""Convert Telegram Bot API message entities to readable Markdown.

The converter targets CommonMark with GitHub Flavored Markdown extensions.
Telegram entities without a native GFM representation use concise Telegram
MarkdownV2 constructs or preserve their visible text. Entity offsets are
interpreted as UTF-16 code units, as required by the Telegram Bot API.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


EntityObject = Mapping[str, Any]

_INLINE_MARKDOWN_PATTERN = re.compile(r"([\\`*_[\]~])")
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9_+.-]+$")
_PLAIN_ENTITY_TYPES = {
    "mention",
    "hashtag",
    "cashtag",
    "bot_command",
    "url",
    "email",
    "phone_number",
    "custom_emoji",
}
_OUTER_ENTITY_PRIORITY = {
    "blockquote": 0,
    "expandable_blockquote": 0,
    "pre": 1,
    "text_link": 2,
    "text_mention": 2,
    "bold": 3,
    "italic": 4,
    "underline": 5,
    "strikethrough": 6,
    "spoiler": 7,
    "code": 8,
}


@dataclass
class _EntityNode:
    entity: EntityObject
    start: int
    end: int
    order: int
    children: list[_EntityNode] = field(default_factory=list)


def entities_to_markdown(text: str, entities: Any = None) -> str:
    """Convert Telegram text and its ``MessageEntity`` array to Markdown."""

    if not isinstance(text, str):
        raise TypeError("Telegram message text must be a string")
    if entities is None:
        entities = []
    if not _is_sequence(entities):
        raise TypeError("Telegram message entities must be a list")

    utf16_boundaries = _utf16_boundaries(text)
    nodes = [
        node
        for order, entity in enumerate(entities)
        if (node := _entity_node(entity, order, utf16_boundaries)) is not None
    ]
    roots = _build_entity_tree(nodes)
    return _render_span(text, 0, len(text), roots)


def _entity_node(
    entity: Any,
    order: int,
    utf16_boundaries: Mapping[int, int],
) -> _EntityNode | None:
    if not isinstance(entity, Mapping):
        raise TypeError("Telegram message entity must be an object")

    entity_type = entity.get("type")
    offset = entity.get("offset")
    length = entity.get("length")
    if not isinstance(entity_type, str) or not entity_type:
        raise ValueError("Telegram message entity type must be a non-empty string")
    if not _is_nonnegative_int(offset) or not _is_nonnegative_int(length):
        raise ValueError("Telegram message entity offset and length must be non-negative integers")
    if length == 0:
        return None

    end_offset = offset + length
    try:
        start = utf16_boundaries[offset]
        end = utf16_boundaries[end_offset]
    except KeyError as err:
        raise ValueError(
            "Telegram message entity range is outside the text or splits a UTF-16 surrogate pair",
        ) from err
    return _EntityNode(entity=entity, start=start, end=end, order=order)


def _build_entity_tree(nodes: list[_EntityNode]) -> list[_EntityNode]:
    ordered = sorted(
        nodes,
        key=lambda node: (
            node.start,
            -node.end,
            _OUTER_ENTITY_PRIORITY.get(str(node.entity.get("type")), 50),
            node.order,
        ),
    )
    roots: list[_EntityNode] = []
    stack: list[_EntityNode] = []

    for node in ordered:
        while stack and node.start >= stack[-1].end:
            stack.pop()

        if stack:
            parent = stack[-1]
            if node.end > parent.end:
                raise ValueError("Telegram message entities overlap without being nested")
            parent.children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    return roots


def _render_span(
    text: str,
    start: int,
    end: int,
    nodes: Sequence[_EntityNode],
    *,
    escape_plain: bool = True,
) -> str:
    rendered: list[str] = []
    position = start
    render_plain = _escape_inline_markdown if escape_plain else str
    for node in nodes:
        rendered.append(render_plain(text[position : node.start]))
        rendered.append(_render_entity(text, node))
        position = node.end
    rendered.append(render_plain(text[position:end]))
    return "".join(rendered)


def _render_entity(text: str, node: _EntityNode) -> str:
    entity_type = str(node.entity.get("type"))
    raw_text = text[node.start : node.end]

    if entity_type == "code":
        return _inline_code(raw_text)
    if entity_type == "pre":
        return _fenced_code(raw_text, node.entity.get("language"))
    if entity_type in _PLAIN_ENTITY_TYPES:
        if not node.children:
            return raw_text
        return _render_span(text, node.start, node.end, node.children, escape_plain=False)

    content = _render_span(text, node.start, node.end, node.children)
    if entity_type == "bold":
        return _wrap_emphasis("**", content)
    if entity_type == "italic":
        return _wrap_emphasis("*", content)
    if entity_type == "underline":
        return _wrap_emphasis("__", content)
    if entity_type == "strikethrough":
        return _wrap_emphasis("~~", content)
    if entity_type == "spoiler":
        return _wrap_emphasis("||", content)
    if entity_type == "date_time":
        return _date_time(content, node.entity)
    if entity_type in {"blockquote", "expandable_blockquote"}:
        return _blockquote(content)
    if entity_type == "text_link":
        return _markdown_link(content, node.entity.get("url"))
    if entity_type == "text_mention":
        user = node.entity.get("user")
        user_id = user.get("id") if isinstance(user, Mapping) else None
        return _markdown_link(content, f"tg://user?id={user_id}") if user_id is not None else content
    return content


def _utf16_boundaries(text: str) -> dict[int, int]:
    boundaries = {0: 0}
    utf16_offset = 0
    for index, character in enumerate(text, start=1):
        utf16_offset += len(character.encode("utf-16-le")) // 2
        boundaries[utf16_offset] = index
    return boundaries


def _escape_inline_markdown(text: str) -> str:
    return _INLINE_MARKDOWN_PATTERN.sub(r"\\\1", text)


def _wrap_emphasis(marker: str, text: str) -> str:
    if not text:
        return ""
    leading_length = len(text) - len(text.lstrip())
    trailing_length = len(text) - len(text.rstrip())
    leading = text[:leading_length]
    trailing = text[len(text) - trailing_length :] if trailing_length else ""
    end = len(text) - trailing_length if trailing_length else len(text)
    content = text[leading_length:end]
    return f"{leading}{marker}{content}{marker}{trailing}" if content else text


def _blockquote(text: str) -> str:
    if not text:
        return ">"
    return "\n".join(">" if not line else f"> {line}" for line in text.splitlines())


def _markdown_link(text: str, destination: Any) -> str:
    destination_text = str(destination or "")
    if not destination_text:
        return text
    escaped_destination = (
        destination_text.replace("\\", "%5C")
        .replace(" ", "%20")
        .replace("(", "%28")
        .replace(")", "%29")
    )
    return f"[{text}]({escaped_destination})"


def _date_time(text: str, entity: EntityObject) -> str:
    unix_time = entity.get("unix_time")
    if not isinstance(unix_time, int) or isinstance(unix_time, bool):
        return text

    destination = f"tg://time?unix={unix_time}"
    date_time_format = entity.get("date_time_format")
    if isinstance(date_time_format, str) and date_time_format:
        destination += f"&format={date_time_format}"
    return f"!{_markdown_link(text, destination)}"


def _inline_code(text: str) -> str:
    text = text.replace("\r\n", " ").replace("\n", " ")
    delimiter = "`" * max(1, _longest_run(text, "`") + 1)
    if text.startswith((" ", "`")) or text.endswith((" ", "`")):
        text = f" {text} "
    return f"{delimiter}{text}{delimiter}"


def _fenced_code(text: str, language: Any) -> str:
    fence = "`" * max(3, _longest_run(text, "`") + 1)
    language_text = str(language or "")
    if not _LANGUAGE_PATTERN.fullmatch(language_text):
        language_text = ""
    return f"{fence}{language_text}\n{text}\n{fence}"


def _longest_run(text: str, character: str) -> int:
    return max(
        (len(match.group(0)) for match in re.finditer(re.escape(character) + "+", text)),
        default=0,
    )


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
