"""Convert Telegram Bot API RichMessage objects to readable Markdown.

The converter targets CommonMark with GitHub-flavored tables, task lists, and
strikethrough. Telegram constructs without a native Markdown representation use
small HTML fragments or a readable textual fallback so that content is not
silently discarded.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from typing import Any


RichObject = Mapping[str, Any]

_INLINE_MARKDOWN_PATTERN = re.compile(r"([\\`*_[\]~])")
_ORDERED_LABEL_PATTERN = re.compile(r"^(?:\d+|[A-Za-z]+|[ivxlcdmIVXLCDM]+)[.)]$")
_CHECKBOX_PREFIX_PATTERN = re.compile(r"^(✅|☑|☐)\s+")
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9_+.-]+$")


def rich_message_to_markdown(rich_message: RichObject) -> str:
    """Convert a Telegram ``RichMessage`` mapping to Markdown."""

    markdown = rich_blocks_to_markdown(rich_message.get("blocks"))
    if not rich_message.get("is_rtl"):
        return markdown
    if not markdown:
        return '<div dir="rtl"></div>'
    return f'<div dir="rtl">\n\n{markdown}\n\n</div>'


def rich_blocks_to_markdown(blocks: Any) -> str:
    """Convert an array of Telegram ``RichBlock`` objects to Markdown."""

    if not _is_sequence(blocks):
        return ""
    rendered = [_render_block(block) for block in blocks]
    return "\n\n".join(part.strip("\n") for part in rendered if part.strip("\n"))


def rich_text_to_markdown(rich_text: Any) -> str:
    """Convert a Telegram ``RichText`` value to inline Markdown."""

    if rich_text is None:
        return ""
    if isinstance(rich_text, str):
        return _escape_inline_markdown(rich_text)
    if _is_sequence(rich_text):
        return "".join(rich_text_to_markdown(part) for part in rich_text)
    if not isinstance(rich_text, Mapping):
        return _escape_inline_markdown(str(rich_text))

    text_type = rich_text.get("type")
    text = rich_text_to_markdown(rich_text.get("text"))

    if text_type == "bold":
        return _wrap_emphasis("**", text)
    if text_type == "italic":
        return _wrap_emphasis("*", text)
    if text_type == "underline":
        return _wrap_html("u", text)
    if text_type == "strikethrough":
        return _wrap_emphasis("~~", text)
    if text_type == "spoiler":
        return _wrap_emphasis("||", text)
    if text_type == "date_time":
        return text
    if text_type == "text_mention":
        user = rich_text.get("user")
        user_id = user.get("id") if isinstance(user, Mapping) else None
        return _markdown_link(text, f"tg://user?id={user_id}") if user_id is not None else text
    if text_type == "subscript":
        return _wrap_html("sub", text)
    if text_type == "superscript":
        return _wrap_html("sup", text)
    if text_type == "marked":
        return _wrap_html("mark", text)
    if text_type == "code":
        return _inline_code(_rich_text_to_plain(rich_text.get("text")))
    if text_type == "custom_emoji":
        return _escape_inline_markdown(str(rich_text.get("alternative_text") or ""))
    if text_type == "mathematical_expression":
        return f"${rich_text.get('expression') or ''}$"
    if text_type == "url":
        return _markdown_link(text, rich_text.get("url"))
    if text_type == "email_address":
        address = str(rich_text.get("email_address") or "")
        return _markdown_link(text or _escape_inline_markdown(address), f"mailto:{address}")
    if text_type == "phone_number":
        number = str(rich_text.get("phone_number") or "")
        return _markdown_link(text or _escape_inline_markdown(number), f"tel:{number}")
    if text_type == "bank_card_number":
        return text or _escape_inline_markdown(str(rich_text.get("bank_card_number") or ""))
    if text_type == "mention":
        username = str(rich_text.get("username") or "").lstrip("@")
        return text or (f"@{username}" if username else "")
    if text_type == "hashtag":
        hashtag = str(rich_text.get("hashtag") or "").lstrip("#")
        return text or (f"#{hashtag}" if hashtag else "")
    if text_type == "cashtag":
        cashtag = str(rich_text.get("cashtag") or "").lstrip("$")
        return text or (f"${cashtag}" if cashtag else "")
    if text_type == "bot_command":
        command = str(rich_text.get("bot_command") or "").lstrip("/")
        return text or (f"/{command}" if command else "")
    if text_type == "anchor":
        name = html.escape(str(rich_text.get("name") or ""), quote=True)
        return f'<a id="{name}"></a>' if name else ""
    if text_type == "anchor_link":
        name = str(rich_text.get("anchor_name") or "")
        return _markdown_link(text, f"#{name}")
    if text_type == "reference":
        name = html.escape(str(rich_text.get("name") or ""), quote=True)
        return f'<span id="ref-{name}">{text}</span>' if name else text
    if text_type == "reference_link":
        name = str(rich_text.get("reference_name") or "")
        return _markdown_link(text, f"#ref-{name}")

    if text:
        return text
    type_name = _escape_inline_markdown(str(text_type or "unknown"))
    return f"[Unsupported Telegram rich text: {type_name}]"


def _render_block(block: Any) -> str:
    if not isinstance(block, Mapping):
        return ""

    block_type = block.get("type")
    if block_type == "paragraph":
        return rich_text_to_markdown(block.get("text"))
    if block_type == "heading":
        size = block.get("size")
        level = size if isinstance(size, int) and not isinstance(size, bool) else 1
        level = max(1, min(level, 6))
        return f"{'#' * level} {rich_text_to_markdown(block.get('text'))}".rstrip()
    if block_type == "pre":
        return _fenced_code(
            _rich_text_to_plain(block.get("text")),
            block.get("language"),
        )
    if block_type == "footer":
        return _wrap_emphasis("_", rich_text_to_markdown(block.get("text")))
    if block_type == "divider":
        return "---"
    if block_type == "mathematical_expression":
        expression = str(block.get("expression") or "")
        return f"$$\n{expression}\n$$"
    if block_type == "anchor":
        name = html.escape(str(block.get("name") or ""), quote=True)
        return f'<a id="{name}"></a>' if name else ""
    if block_type == "list":
        return _render_list(block.get("items"))
    if block_type == "blockquote":
        content = rich_blocks_to_markdown(block.get("blocks"))
        return _render_quote(content, block.get("credit"))
    if block_type == "pullquote":
        content = rich_text_to_markdown(block.get("text"))
        return _render_quote(content, block.get("credit"))
    if block_type in {"collage", "slideshow"}:
        content = rich_blocks_to_markdown(block.get("blocks"))
        caption = _render_caption(block.get("caption"))
        return _join_nonempty(content, caption)
    if block_type == "table":
        return _render_table(block)
    if block_type == "details":
        return _render_details(block)
    if block_type == "map":
        return _render_map(block)
    if block_type in {"animation", "audio", "photo", "video", "voice_note"}:
        return _render_media(block_type, block)
    if block_type == "thinking":
        return _render_quote(rich_text_to_markdown(block.get("text")), None)

    text = rich_text_to_markdown(block.get("text"))
    nested = rich_blocks_to_markdown(block.get("blocks"))
    caption = _render_caption(block.get("caption"))
    fallback = _join_nonempty(text, nested, caption)
    if fallback:
        return fallback
    type_name = str(block_type or "unknown")
    return f"[Unsupported Telegram rich block: {type_name}]"


def _render_list(items: Any) -> str:
    if not _is_sequence(items):
        return ""

    rendered_items = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        body = _render_list_item_blocks(item.get("blocks"))
        marker, body = _list_marker_and_body(item, body)
        lines = body.splitlines() or [""]
        continuation = " " * (len(marker) + 1)
        rendered = [f"{marker} {lines[0]}".rstrip()]
        rendered.extend(
            "" if not line else f"{continuation}{line}"
            for line in lines[1:]
        )
        rendered_items.append("\n".join(rendered))
    return "\n".join(rendered_items)


def _render_list_item_blocks(blocks: Any) -> str:
    if not _is_sequence(blocks):
        return ""

    parts: list[tuple[str | None, str]] = []
    for block in blocks:
        block_type = block.get("type") if isinstance(block, Mapping) else None
        rendered = _render_block(block).strip("\n")
        if rendered:
            parts.append((block_type, rendered))

    result = ""
    previous_type = None
    for block_type, rendered in parts:
        if result:
            separator = "\n" if "list" in {previous_type, block_type} else "\n\n"
            result += separator
        result += rendered
        previous_type = block_type
    return result


def _list_marker_and_body(item: RichObject, body: str) -> tuple[str, str]:
    if item.get("has_checkbox"):
        return ("- [x]" if item.get("is_checked") else "- [ ]"), body

    checkbox_prefix = _CHECKBOX_PREFIX_PATTERN.match(body)
    if checkbox_prefix:
        checked = checkbox_prefix.group(1) == "✅"
        return ("- [x]" if checked else "- [ ]"), body[checkbox_prefix.end() :]

    label = str(item.get("label") or "").strip()
    if item.get("type") or item.get("value") is not None or _ORDERED_LABEL_PATTERN.match(label):
        return label or f"{item.get('value', 1)}.", body
    return "-", body


def _render_quote(content: str, credit: Any) -> str:
    credit_text = rich_text_to_markdown(credit)
    if credit_text:
        content = _join_nonempty(content, f"— {credit_text}")
    if not content:
        return ">"
    return "\n".join(">" if not line else f"> {line}" for line in content.splitlines())


def _render_table(block: RichObject) -> str:
    rows = block.get("cells")
    if not _is_sequence(rows) or not rows:
        return _render_table_caption(block.get("caption"))
    if _table_has_spans(rows):
        table = _render_html_table(rows)
    else:
        table = _render_gfm_table(rows)
    return _join_nonempty(_render_table_caption(block.get("caption")), table)


def _render_gfm_table(rows: Sequence[Any]) -> str:
    normalized_rows = [row for row in rows if _is_sequence(row)]
    if not normalized_rows:
        return ""
    width = max(len(row) for row in normalized_rows)
    rendered_rows = [
        [_table_cell_markdown(row[index] if index < len(row) else {}) for index in range(width)]
        for row in normalized_rows
    ]

    first_row_has_header = any(
        isinstance(cell, Mapping) and cell.get("is_header")
        for cell in normalized_rows[0]
    )
    lines = []
    if not first_row_has_header:
        lines.append(_markdown_table_row([""] * width))
        lines.append(_markdown_table_divider(width))
    for index, row in enumerate(rendered_rows):
        lines.append(_markdown_table_row(row))
        if index == 0 and first_row_has_header:
            lines.append(_markdown_table_divider(width))
    return "\n".join(lines)


def _render_html_table(rows: Sequence[Any]) -> str:
    lines = ["<table>"]
    for row in rows:
        if not _is_sequence(row):
            continue
        lines.append("  <tr>")
        for cell in row:
            cell = cell if isinstance(cell, Mapping) else {}
            tag = "th" if cell.get("is_header") else "td"
            attributes = []
            for name in ("colspan", "rowspan", "align", "valign"):
                value = cell.get(name)
                if value is not None:
                    attributes.append(f'{name}="{html.escape(str(value), quote=True)}"')
            attribute_text = f" {' '.join(attributes)}" if attributes else ""
            content = rich_text_to_markdown(cell.get("text")).replace("\n", "<br>")
            lines.append(f"    <{tag}{attribute_text}>{content}</{tag}>")
        lines.append("  </tr>")
    lines.append("</table>")
    return "\n".join(lines)


def _render_details(block: RichObject) -> str:
    summary = rich_text_to_markdown(block.get("summary"))
    content = rich_blocks_to_markdown(block.get("blocks"))
    open_attribute = " open" if block.get("is_open") else ""
    return (
        f"<details{open_attribute}>\n"
        f"<summary>{summary}</summary>\n\n"
        f"{content}\n\n"
        "</details>"
    )


def _render_map(block: RichObject) -> str:
    location = block.get("location")
    latitude = location.get("latitude") if isinstance(location, Mapping) else None
    longitude = location.get("longitude") if isinstance(location, Mapping) else None
    zoom = block.get("zoom")
    if latitude is None or longitude is None:
        map_text = "[Map]"
    else:
        label = f"Map: {latitude}, {longitude}"
        zoom_value = zoom if isinstance(zoom, int) and not isinstance(zoom, bool) else 15
        url = (
            "https://www.openstreetmap.org/"
            f"?mlat={latitude}&mlon={longitude}#map={zoom_value}/{latitude}/{longitude}"
        )
        map_text = _markdown_link(label, url)
    return _join_nonempty(map_text, _render_caption(block.get("caption")))


def _render_media(block_type: str, block: RichObject) -> str:
    media = block.get(block_type)
    if block_type == "voice_note" and media is None:
        media = block.get("voice")
    label = block_type.replace("_", " ").title()
    details = _media_details(block_type, media)
    spoiler = "spoiler" if block.get("has_spoiler") else ""
    qualifiers = ", ".join(part for part in (details, spoiler) if part)
    media_text = f"**{label}**" + (f" ({qualifiers})" if qualifiers else "")
    return _join_nonempty(media_text, _render_caption(block.get("caption")))


def _media_details(block_type: str, media: Any) -> str:
    if block_type == "photo" and _is_sequence(media):
        photos = [photo for photo in media if isinstance(photo, Mapping)]
        if not photos:
            return ""
        largest = max(
            photos,
            key=lambda photo: (
                photo.get("file_size") or 0,
                photo.get("width") or 0,
                photo.get("height") or 0,
            ),
        )
        return _dimensions(largest)
    if not isinstance(media, Mapping):
        return ""

    parts = []
    file_name = media.get("file_name")
    if file_name:
        parts.append(str(file_name))
    title = media.get("title")
    performer = media.get("performer")
    if title:
        parts.append(f"{performer} — {title}" if performer else str(title))
    dimensions = _dimensions(media)
    if dimensions:
        parts.append(dimensions)
    duration = media.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        parts.append(_format_duration(duration))
    return ", ".join(parts)


def _render_caption(caption: Any) -> str:
    if not isinstance(caption, Mapping):
        return rich_text_to_markdown(caption)
    text = rich_text_to_markdown(caption.get("text"))
    credit = rich_text_to_markdown(caption.get("credit"))
    return _join_nonempty(text, f"— {credit}" if credit else "")


def _render_table_caption(caption: Any) -> str:
    text = rich_text_to_markdown(caption)
    return _wrap_emphasis("_", text) if text else ""


def _table_has_spans(rows: Sequence[Any]) -> bool:
    return any(
        isinstance(cell, Mapping) and (cell.get("colspan", 1) != 1 or cell.get("rowspan", 1) != 1)
        for row in rows
        if _is_sequence(row)
        for cell in row
    )


def _table_cell_markdown(cell: Any) -> str:
    if not isinstance(cell, Mapping):
        return ""
    return (
        rich_text_to_markdown(cell.get("text"))
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
    )


def _markdown_table_row(cells: Sequence[str]) -> str:
    return f"| {' | '.join(cells)} |"


def _markdown_table_divider(width: int) -> str:
    return f"|{'|'.join(['---'] * width)}|"


def _fenced_code(text: str, language: Any) -> str:
    fence = "`" * max(3, _longest_run(text, "`") + 1)
    language_text = str(language or "")
    if not _LANGUAGE_PATTERN.fullmatch(language_text):
        language_text = ""
    return f"{fence}{language_text}\n{text}\n{fence}"


def _inline_code(text: str) -> str:
    text = text.replace("\r\n", " ").replace("\n", " ")
    delimiter = "`" * max(1, _longest_run(text, "`") + 1)
    if text.startswith((" ", "`")) or text.endswith((" ", "`")):
        text = f" {text} "
    return f"{delimiter}{text}{delimiter}"


def _rich_text_to_plain(rich_text: Any) -> str:
    if rich_text is None:
        return ""
    if isinstance(rich_text, str):
        return rich_text
    if _is_sequence(rich_text):
        return "".join(_rich_text_to_plain(part) for part in rich_text)
    if not isinstance(rich_text, Mapping):
        return str(rich_text)
    if rich_text.get("type") == "custom_emoji":
        return str(rich_text.get("alternative_text") or "")
    if rich_text.get("type") == "mathematical_expression":
        return str(rich_text.get("expression") or "")
    return _rich_text_to_plain(rich_text.get("text"))


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


def _wrap_html(tag: str, text: str) -> str:
    return f"<{tag}>{text}</{tag}>" if text else ""


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


def _escape_inline_markdown(text: str) -> str:
    return _INLINE_MARKDOWN_PATTERN.sub(r"\\\1", text)


def _dimensions(media: RichObject) -> str:
    width = media.get("width")
    height = media.get("height")
    if isinstance(width, int) and isinstance(height, int):
        return f"{width}×{height}"
    return ""


def _format_duration(seconds: int | float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes}:{remaining_seconds:02d}"


def _longest_run(text: str, character: str) -> int:
    return max((len(match.group(0)) for match in re.finditer(re.escape(character) + "+", text)), default=0)


def _join_nonempty(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
