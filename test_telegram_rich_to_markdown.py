import unittest

from telegram_rich_to_markdown import (
    rich_message_to_markdown,
    rich_text_to_markdown,
)


class TelegramRichMessageExampleTest(unittest.TestCase):
    def test_documented_example_is_converted_to_markdown(self) -> None:
        rich_message = {
            "blocks": [
                {"type": "heading", "text": "Markdown Demo", "size": 1},
                {"type": "heading", "text": "Text styles", "size": 2},
                {
                    "type": "paragraph",
                    "text": [
                        {"type": "bold", "text": "Bold"},
                        ", ",
                        {"type": "italic", "text": "italic"},
                        ", ",
                        {
                            "type": "bold",
                            "text": {"type": "italic", "text": "bold italic"},
                        },
                        ", ",
                        {"type": "strikethrough", "text": "strikethrough"},
                        ", and ",
                        {"type": "code", "text": "inline code"},
                        ".",
                    ],
                },
                {
                    "type": "blockquote",
                    "blocks": [
                        {"type": "paragraph", "text": "This is a blockquote."},
                        {
                            "type": "paragraph",
                            "text": "It can span multiple paragraphs.",
                        },
                    ],
                },
                {"type": "heading", "text": "Lists", "size": 2},
                {
                    "type": "list",
                    "items": [
                        {
                            "label": "•",
                            "blocks": [{"type": "paragraph", "text": "First item"}],
                        },
                        {
                            "label": "•",
                            "blocks": [
                                {"type": "paragraph", "text": "Second item"},
                                {
                                    "type": "list",
                                    "items": [
                                        {
                                            "label": "•",
                                            "blocks": [
                                                {
                                                    "type": "paragraph",
                                                    "text": "Nested item",
                                                },
                                            ],
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                },
                {
                    "type": "list",
                    "items": [
                        {
                            "label": "1.",
                            "blocks": [{"type": "paragraph", "text": "First step"}],
                            "type": "1",
                            "value": 1,
                        },
                        {
                            "label": "2.",
                            "blocks": [{"type": "paragraph", "text": "Second step"}],
                            "type": "1",
                            "value": 2,
                        },
                        {
                            "label": "3.",
                            "blocks": [{"type": "paragraph", "text": "Third step"}],
                            "type": "1",
                            "value": 3,
                        },
                    ],
                },
                {
                    "type": "list",
                    "items": [
                        {
                            "label": "•",
                            "blocks": [
                                {"type": "paragraph", "text": "✅ Completed task"},
                            ],
                        },
                        {
                            "label": "•",
                            "blocks": [
                                {"type": "paragraph", "text": "☑ Pending task"},
                            ],
                        },
                    ],
                },
                {"type": "heading", "text": "Code", "size": 2},
                {
                    "type": "pre",
                    "text": (
                        "Select serno, rrn\n"
                        "From authorizations\n"
                        "Where response_code = '00';"
                    ),
                    "language": "sql",
                },
                {"type": "heading", "text": "Table", "size": 2},
                {
                    "type": "table",
                    "cells": [
                        [
                            {
                                "text": "Field",
                                "is_header": True,
                                "align": "center",
                                "valign": "middle",
                            },
                            {
                                "text": "Meaning",
                                "is_header": True,
                                "align": "center",
                                "valign": "middle",
                            },
                        ],
                        [
                            {"text": "DE22", "align": "left", "valign": "middle"},
                            {
                                "text": "POS entry mode",
                                "align": "left",
                                "valign": "middle",
                            },
                        ],
                        [
                            {"text": "DE39", "align": "left", "valign": "middle"},
                            {
                                "text": "Response code",
                                "align": "left",
                                "valign": "middle",
                            },
                        ],
                        [
                            {"text": "DE102", "align": "left", "valign": "middle"},
                            {
                                "text": "Account identification",
                                "align": "left",
                                "valign": "middle",
                            },
                        ],
                    ],
                },
                {"type": "heading", "text": "Link", "size": 2},
                {
                    "type": "paragraph",
                    "text": {
                        "type": "url",
                        "text": "OpenAI",
                        "url": "https://openai.com/",
                    },
                },
                {"type": "divider"},
                {
                    "type": "paragraph",
                    "text": "That line above is a horizontal rule.",
                },
            ],
        }
        expected = """# Markdown Demo

## Text styles

**Bold**, *italic*, ***bold italic***, ~~strikethrough~~, and `inline code`.

> This is a blockquote.
>
> It can span multiple paragraphs.

## Lists

- First item
- Second item
  - Nested item

1. First step
2. Second step
3. Third step

- [x] Completed task
- [ ] Pending task

## Code

```sql
Select serno, rrn
From authorizations
Where response_code = '00';
```

## Table

| Field | Meaning |
|---|---|
| DE22 | POS entry mode |
| DE39 | Response code |
| DE102 | Account identification |

## Link

[OpenAI](https://openai.com/)

---

That line above is a horizontal rule."""

        self.assertEqual(rich_message_to_markdown(rich_message), expected)


class TelegramRichTextTest(unittest.TestCase):
    def test_every_rich_text_variant_has_a_markdown_representation(self) -> None:
        values = [
            {"type": "bold", "text": "bold"},
            {"type": "italic", "text": "italic"},
            {"type": "underline", "text": "underlined"},
            {"type": "strikethrough", "text": "deleted"},
            {"type": "spoiler", "text": "secret"},
            {
                "type": "date_time",
                "text": "tomorrow",
                "unix_time": 2_000_000_000,
                "date_time_format": "day_month_year",
            },
            {
                "type": "text_mention",
                "text": "Denis",
                "user": {"id": 42, "first_name": "Denis"},
            },
            {"type": "subscript", "text": "sub"},
            {"type": "superscript", "text": "sup"},
            {"type": "marked", "text": "marked"},
            {"type": "code", "text": "a`b"},
            {
                "type": "custom_emoji",
                "custom_emoji_id": "emoji-id",
                "alternative_text": "🙂",
            },
            {"type": "mathematical_expression", "expression": "x^2"},
            {"type": "url", "text": "site", "url": "https://example.com/a_(b)"},
            {
                "type": "email_address",
                "text": "mail",
                "email_address": "a@example.com",
            },
            {"type": "phone_number", "text": "call", "phone_number": "+123"},
            {
                "type": "bank_card_number",
                "text": "masked card",
                "bank_card_number": "000000******0000",
            },
            {"type": "mention", "text": "@user", "username": "user"},
            {"type": "hashtag", "text": "#topic", "hashtag": "topic"},
            {"type": "cashtag", "text": "$ABC", "cashtag": "ABC"},
            {"type": "bot_command", "text": "/start", "bot_command": "start"},
            {"type": "anchor", "name": "top"},
            {"type": "anchor_link", "text": "top", "anchor_name": "top"},
            {"type": "reference", "text": "source", "name": "one"},
            {
                "type": "reference_link",
                "text": "see source",
                "reference_name": "one",
            },
        ]

        rendered = " ".join(rich_text_to_markdown(value) for value in values)

        for expected in (
            "**bold**",
            "*italic*",
            "<u>underlined</u>",
            "~~deleted~~",
            "||secret||",
            "[Denis](tg://user?id=42)",
            "<sub>sub</sub>",
            "<sup>sup</sup>",
            "<mark>marked</mark>",
            "``a`b``",
            "🙂",
            "$x^2$",
            "[site](https://example.com/a_%28b%29)",
            "[mail](mailto:a@example.com)",
            "[call](tel:+123)",
            '<a id="top"></a>',
            "[top](#top)",
            '<span id="ref-one">source</span>',
            "[see source](#ref-one)",
        ):
            self.assertIn(expected, rendered)

    def test_plain_markdown_metacharacters_and_code_delimiters_are_safe(self) -> None:
        rendered = rich_text_to_markdown(
            [
                "literal *stars* and [brackets] ",
                {"type": "code", "text": "` edge `"},
            ],
        )

        self.assertEqual(
            rendered,
            r"literal \*stars\* and \[brackets\] `` ` edge ` ``",
        )


class TelegramRichBlockTest(unittest.TestCase):
    def test_additional_documented_block_types_are_preserved(self) -> None:
        rich_message = {
            "is_rtl": True,
            "blocks": [
                {"type": "footer", "text": "Footer"},
                {"type": "mathematical_expression", "expression": "x = y + 1"},
                {"type": "anchor", "name": "section"},
                {"type": "pullquote", "text": "Quoted", "credit": "Author"},
                {
                    "type": "collage",
                    "blocks": [{"type": "photo", "photo": [{"width": 10, "height": 20}]}],
                    "caption": {"text": "Collage caption", "credit": "Photographer"},
                },
                {
                    "type": "slideshow",
                    "blocks": [{"type": "video", "video": {"duration": 65}}],
                    "caption": {"text": "Slides"},
                },
                {
                    "type": "table",
                    "caption": "Spanning table",
                    "cells": [
                        [{"text": "Wide", "is_header": True, "colspan": 2}],
                        [{"text": "Tall", "rowspan": 2}, {"text": "Value"}],
                    ],
                },
                {
                    "type": "details",
                    "summary": "More",
                    "blocks": [{"type": "paragraph", "text": "Hidden details"}],
                    "is_open": True,
                },
                {
                    "type": "map",
                    "location": {"latitude": 33.3152, "longitude": 44.3661},
                    "zoom": 14,
                    "caption": {"text": "Baghdad"},
                },
                {
                    "type": "animation",
                    "animation": {"file_name": "demo.gif", "duration": 3},
                    "has_spoiler": True,
                    "caption": {"text": "Animation caption"},
                },
                {
                    "type": "audio",
                    "audio": {"performer": "Artist", "title": "Song", "duration": 125},
                },
                {
                    "type": "photo",
                    "photo": [{"width": 320, "height": 200, "file_size": 100}],
                },
                {
                    "type": "video",
                    "video": {"width": 1920, "height": 1080, "duration": 65},
                },
                {"type": "voice_note", "voice_note": {"duration": 7}},
                {"type": "thinking", "text": "Thinking…"},
            ],
        }

        rendered = rich_message_to_markdown(rich_message)

        for expected in (
            '<div dir="rtl">',
            "_Footer_",
            "$$\nx = y + 1\n$$",
            '<a id="section"></a>',
            "> Quoted\n>\n> — Author",
            "**Photo** (10×20)",
            "Collage caption\n\n— Photographer",
            "**Video** (1:05)",
            "_Spanning table_",
            '<th colspan="2">Wide</th>',
            '<td rowspan="2">Tall</td>',
            "<details open>",
            "<summary>More</summary>",
            "Hidden details",
            "[Map: 33.3152, 44.3661]",
            "Baghdad",
            "**Animation** (demo.gif, 0:03, spoiler)",
            "Animation caption",
            "**Audio** (Artist — Song, 2:05)",
            "**Photo** (320×200)",
            "**Video** (1920×1080, 1:05)",
            "**Voice Note** (0:07)",
            "> Thinking…",
            "</div>",
        ):
            self.assertIn(expected, rendered)

    def test_official_checkboxes_and_non_decimal_ordered_labels_are_supported(self) -> None:
        rich_message = {
            "blocks": [
                {
                    "type": "list",
                    "items": [
                        {
                            "label": "•",
                            "has_checkbox": True,
                            "is_checked": True,
                            "blocks": [{"type": "paragraph", "text": "Done"}],
                        },
                        {
                            "label": "a.",
                            "type": "a",
                            "value": 1,
                            "blocks": [{"type": "paragraph", "text": "Alpha"}],
                        },
                    ],
                },
            ],
        }

        self.assertEqual(rich_message_to_markdown(rich_message), "- [x] Done\na. Alpha")


if __name__ == "__main__":
    unittest.main()
