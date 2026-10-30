import html
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot import TelegramClient, format_current_message_markdown, prepare_telegram_rich_parts
from telegram_topic_history import TelegramTopicHistory


class TelegramMathIntegrationTest(unittest.TestCase):
    markdown = (
        r"Потенциал \(\varphi\), затем градиент:"
        "\n\n"
        r"\["
        "\n"
        r"\mathbf E=-\nabla\varphi."
        "\n"
        r"\]"
    )

    def client(self) -> TelegramClient:
        client = object.__new__(TelegramClient)
        client.allowed_media_roots = ()
        client.topic_history = None
        client._api = MagicMock(return_value={"result": {"message_id": 3001}})
        client.delete_guest_upload_messages = MagicMock()
        return client

    def assert_math(self, rendered: str) -> None:
        self.assertIn(r"<tg-math>\varphi</tg-math>", rendered)
        expression = rendered.split("<tg-math-block>", 1)[1].split("</tg-math-block>", 1)[0]
        self.assertEqual(html.unescape(expression).strip(), r"\mathbf E=-\nabla\varphi.")

    def test_codex_math_is_sent_as_native_rich_math_in_an_ordinary_topic(self) -> None:
        client = self.client()
        client.send_rich_message(-1001, self.markdown, 2397, 42)

        method, payload = client._api.call_args.args
        self.assertEqual(method, "sendRichMessage")
        self.assertEqual(payload["message_thread_id"], 42)
        self.assertEqual(payload["reply_parameters"], {"message_id": 2397})
        self.assert_math(payload["rich_message"]["html"])

    def test_guest_math_uses_the_same_native_rich_format(self) -> None:
        client = self.client()
        sent = client.answer_guest_query("guest-query", self.markdown)

        method, payload = client._api.call_args.args
        self.assertEqual(method, "answerGuestQuery")
        self.assertEqual(payload["guest_query_id"], "guest-query")
        content = payload["result"]["input_message_content"]["rich_message"]
        self.assertEqual(content, sent)
        self.assert_math(content["html"])

    def test_aligned_equations_keep_tex_commands_and_row_separators(self) -> None:
        expression = (
            r"\begin{aligned}"
            "\n"
            r"\nabla\cdot\mathbf D&=0,\\"
            "\n"
            r"\mathbf D&=\boldsymbol\varepsilon^{S}\mathbf E+\mathbf e:\mathbf S."
            "\n"
            r"\end{aligned}"
        )
        parts = prepare_telegram_rich_parts("\\[\n" + expression + "\n\\]")
        rendered = html.unescape(parts[0].rich_message["html"])
        body = rendered.split("<tg-math-block>", 1)[1].split("</tg-math-block>", 1)[0]
        self.assertEqual(body.strip(), expression)

    def test_math_and_literal_code_examples_can_coexist(self) -> None:
        markdown = (
            self.markdown
            + "\n\nInline example: `\\(\\varphi\\)`\n\n"
            + "~~~python\nexpression = r\"\\[x^2\\]\"\n~~~"
        )
        rendered = prepare_telegram_rich_parts(markdown)[0].rich_message["html"]
        self.assert_math(rendered)
        self.assertIn(r"<code>\(\varphi\)</code>", rendered)
        self.assertIn(r'\[x^2\]', rendered)
        self.assertEqual(rendered.count("<tg-math>"), 1)
        self.assertEqual(rendered.count("<tg-math-block>"), 1)

    def test_native_math_reply_still_matches_topic_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = TelegramTopicHistory(Path(directory) / "topics.json", format_current_message_markdown)
            original = {"chat": {"id": -1001, "type": "supergroup"}, "message_thread_id": 42}
            sent = prepare_telegram_rich_parts(self.markdown)[0].rich_message
            history.record_response("guest_message", original, sent)
            message = {
                "chat": original["chat"],
                "message_id": 302,
                "text": "Explain this formula",
                "reply_to_message": {
                    "message_id": 301,
                    "from": {"id": 99},
                    "rich_message": {"blocks": [
                        {"type": "paragraph", "text": [
                            {"type": "plain", "text": "Потенциал "},
                            {"type": "mathematical_expression", "expression": r"\varphi"},
                            {"type": "plain", "text": ", затем градиент:"},
                        ]},
                        {"type": "mathematical_expression", "expression": r"\mathbf E=-\nabla\varphi."},
                    ]},
                },
            }
            self.assertEqual(history.resolve("guest_message", message, {"id": 99})["message_thread_id"], 42)


if __name__ == "__main__":
    unittest.main()
