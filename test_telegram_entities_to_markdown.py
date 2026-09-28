import unittest

from telegram_entities_to_markdown import entities_to_markdown


class TelegramEntitiesToMarkdownTest(unittest.TestCase):
    def test_documented_formatting_entities_become_readable_markdown(self) -> None:
        text = (
            "Bold, italic, bold italic, underline, strikethrough, inline code.\n"
            "This is a blockquote.\n"
            "It can span multiple paragraphs.\n"
            "Select serno, rrn\n"
            "From authorizations\n"
            "Where response_code = '00';\n"
            "OpenAI, Denis, and secret."
        )
        quote = "This is a blockquote.\nIt can span multiple paragraphs."
        code = (
            "Select serno, rrn\n"
            "From authorizations\n"
            "Where response_code = '00';"
        )
        entities = [
            entity(text, "Bold", "bold"),
            entity(text, "italic", "italic"),
            entity(text, "bold italic", "bold"),
            entity(text, "bold italic", "italic"),
            entity(text, "underline", "underline"),
            entity(text, "strikethrough", "strikethrough"),
            entity(text, "inline code", "code"),
            entity(text, quote, "blockquote"),
            entity(text, code, "pre", language="sql"),
            entity(text, "OpenAI", "text_link", url="https://openai.com/"),
            entity(text, "Denis", "text_mention", user={"id": 42}),
            entity(text, "secret", "spoiler"),
        ]

        self.assertEqual(
            entities_to_markdown(text, entities),
            """**Bold**, *italic*, ***bold italic***, __underline__, ~~strikethrough~~, `inline code`.
> This is a blockquote.
> It can span multiple paragraphs.
```sql
Select serno, rrn
From authorizations
Where response_code = '00';
```
[OpenAI](https://openai.com/), [Denis](tg://user?id=42), and ||secret||.""",
        )

    def test_utf16_offsets_handle_non_bmp_characters(self) -> None:
        text = "🙂 Bold and italic"

        self.assertEqual(
            entities_to_markdown(
                text,
                [
                    entity(text, "Bold", "bold"),
                    entity(text, "italic", "italic"),
                ],
            ),
            "🙂 **Bold** and *italic*",
        )

    def test_nested_entities_and_same_range_styles_are_supported(self) -> None:
        text = "bold italic and both"
        entities = [
            entity(text, "bold italic", "bold"),
            entity(text, "italic", "italic"),
            entity(text, "both", "bold"),
            entity(text, "both", "italic"),
        ]

        self.assertEqual(
            entities_to_markdown(text, entities),
            "**bold *italic*** and ***both***",
        )

    def test_plain_punctuation_is_not_markdownv2_escaped(self) -> None:
        text = "Hello, world! Version 1.2 (stable) - yes + no = fine."

        self.assertEqual(entities_to_markdown(text), text)

    def test_literal_markdown_delimiters_are_escaped_minimally(self) -> None:
        text = "literal *stars*, _underscores_, [brackets], ~tilde~, and `ticks`"

        self.assertEqual(
            entities_to_markdown(text),
            r"literal \*stars\*, \_underscores\_, \[brackets\], \~tilde\~, and \`ticks\`",
        )

    def test_inline_and_fenced_code_choose_safe_backtick_delimiters(self) -> None:
        inline_text = "Use a`b now"
        pre_text = "before\nx = ```value```\nafter"

        self.assertEqual(
            entities_to_markdown(inline_text, [entity(inline_text, "a`b", "code")]),
            "Use ``a`b`` now",
        )
        self.assertEqual(
            entities_to_markdown(pre_text, [entity(pre_text, pre_text, "pre", language="py")]),
            "````py\nbefore\nx = ```value```\nafter\n````",
        )

    def test_expandable_blockquote_is_preserved_as_a_quote(self) -> None:
        text = "Visible\n\nHidden"

        self.assertEqual(
            entities_to_markdown(
                text,
                [entity(text, text, "expandable_blockquote")],
            ),
            "> Visible\n>\n> Hidden",
        )

    def test_visible_text_entities_are_preserved_without_telegram_links(self) -> None:
        samples = [
            ("@user", "mention"),
            ("#topic", "hashtag"),
            ("$USD", "cashtag"),
            ("/start", "bot_command"),
            ("https://example.com/a_b", "url"),
            ("a_b@example.com", "email"),
            ("+1-212-555-0123", "phone_number"),
            ("🙂", "custom_emoji"),
        ]

        for text, entity_type in samples:
            with self.subTest(entity_type=entity_type):
                self.assertEqual(
                    entities_to_markdown(text, [entity(text, text, entity_type)]),
                    text,
                )

    def test_date_time_preserves_telegram_timestamp_metadata(self) -> None:
        text = "Today is the day."

        self.assertEqual(
            entities_to_markdown(
                text,
                [
                    entity(
                        text,
                        "Today",
                        "date_time",
                        unix_time=1787130000,
                        date_time_format="wDT",
                    ),
                ],
            ),
            "![Today](tg://time?unix=1787130000&format=wDT) is the day.",
        )

    def test_style_nested_inside_an_automatic_url_is_preserved(self) -> None:
        text = "https://example.com/path"

        self.assertEqual(
            entities_to_markdown(
                text,
                [
                    entity(text, text, "url"),
                    entity(text, "path", "bold"),
                ],
            ),
            "https://example.com/**path**",
        )

    def test_invalid_ranges_and_crossing_entities_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "surrogate pair"):
            entities_to_markdown(
                "🙂",
                [{"type": "bold", "offset": 1, "length": 1}],
            )

        with self.assertRaisesRegex(ValueError, "without being nested"):
            entities_to_markdown(
                "abcde",
                [
                    {"type": "bold", "offset": 0, "length": 3},
                    {"type": "italic", "offset": 2, "length": 3},
                ],
            )


def entity(
    text: str,
    selected_text: str,
    entity_type: str,
    **fields: object,
) -> dict[str, object]:
    start = text.index(selected_text)
    return {
        "type": entity_type,
        "offset": utf16_length(text[:start]),
        "length": utf16_length(selected_text),
        **fields,
    }


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
