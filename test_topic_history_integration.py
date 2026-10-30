import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import Bot, TelegramClient, build_codex_prompt, format_current_message_markdown
from telegram_topic_history import TelegramTopicHistory
from test_bot import build_config, prompt_context


class TopicHistoryIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sessions.topics.json"
        self.history = TelegramTopicHistory(self.path, format_current_message_markdown)
        self.bot = object.__new__(Bot)
        self.bot.config = build_config(Path(self.directory.name))
        self.bot.bot_user = {"id": 999}
        self.bot.topic_history = self.history
        self.bot.controllers = {}
        self.bot.controllers_lock = threading.Lock()
        self.bot.update_executor = MagicMock()
        self.bot.telegram = MagicMock()

    def message(self, message_id: int = 101, topic: int | None = None) -> dict:
        message = {
            "message_id": message_id,
            "chat": {"id": -1001, "type": "supergroup"},
            "from": {"id": 1001, "first_name": "User"},
            "text": "Question",
            "guest_query_id": f"guest-{message_id}",
        }
        if topic is not None:
            message["message_thread_id"] = topic
        return message

    def quoted_reply(self, text: str, message_id: int = 202) -> dict:
        message = self.message(message_id)
        message["reply_to_message"] = {
            "message_id": message_id - 1,
            "chat": message["chat"],
            "from": self.bot.bot_user,
            "text": text,
        }
        return message

    def test_constructor_loads_custom_session_topic_store_and_shares_it_with_transport(self) -> None:
        directory = Path(self.directory.name)
        config = replace(
            build_config(directory),
            session_store_path=directory / "customfilename.json",
        )
        topic_path = directory / "customfilename.topics.json"
        persisted = TelegramTopicHistory(topic_path)
        persisted.record_response("guest_message", self.message(topic=42), "Persisted answer.")

        with (
            patch("bot.TelegramClient") as transport,
            patch("bot.CodexRunner"),
            patch("bot.configured_writable_roots", return_value=[]),
        ):
            bot = Bot(config)
        self.addCleanup(bot.update_executor.shutdown, wait=True)
        self.addCleanup(bot.turn_executor.shutdown, wait=True)
        bot.bot_user = self.bot.bot_user

        self.assertEqual(bot.topic_history.path, topic_path)
        self.assertIs(transport.return_value.topic_history, bot.topic_history)
        recovered = bot.resolve_message_topic("guest_message", self.quoted_reply("Persisted answer."))
        self.assertEqual(recovered["message_thread_id"], 42)

    def test_actual_ordinary_delivery_topic_and_body_override_fallback_values(self) -> None:
        client = object.__new__(TelegramClient)
        client.topic_history = self.history
        client._api = MagicMock(return_value={
            "result": {
                "message_id": 501,
                "message_thread_id": 99,
                "is_topic_message": True,
                "text": "Actual delivered answer.",
            },
        })

        client.send_plain_message(-1001, "Fallback input answer.", None, 42)

        reloaded = TelegramTopicHistory(self.path, format_current_message_markdown)
        reply_by_id = self.message(601)
        reply_by_id["reply_to_message"] = {"message_id": 501, "chat": reply_by_id["chat"]}
        by_id = reloaded.resolve("message", reply_by_id, self.bot.bot_user)
        by_body = reloaded.resolve(
            "message", self.quoted_reply("Actual delivered answer.", 701), self.bot.bot_user,
        )
        fallback = reloaded.resolve(
            "message", self.quoted_reply("Fallback input answer.", 801), self.bot.bot_user,
        )

        self.assertEqual(by_id["message_thread_id"], 99)
        self.assertEqual(by_body["message_thread_id"], 99)
        self.assertNotIn("message_thread_id", fallback)

    def test_guest_html_and_received_formatting_recover_topic_without_altering_code(self) -> None:
        self.bot.telegram.answer_guest_query.return_value = {
            "html": "<p>Use <em>italics</em> and <code>a_b **literal**</code>.</p>",
        }
        delivery = self.bot.answer_guest_response(
            "guest-101", "Use *italics* and `a_b **literal**`.", self.message(topic=42), "response",
        )
        self.assertTrue(delivery.delivered)
        self.bot.topic_history = TelegramTopicHistory(self.path, format_current_message_markdown)
        received_text = "Use italics and a_b **literal**."
        message = self.quoted_reply(received_text)
        code = "a_b **literal**"
        message["reply_to_message"]["entities"] = [
            {"type": "italic", "offset": 4, "length": len("italics")},
            {"type": "code", "offset": received_text.index(code), "length": len(code)},
        ]

        self.bot.dispatch_update({"update_id": 2, "guest_message": message})

        controller = self.bot.controllers["guest:-1001:42"]
        self.assertEqual(controller.events[0][1][1]["message_thread_id"], 42)
        changed_text = "Use italics and a_b literal."
        changed = self.quoted_reply(changed_text, 301)
        changed_code = "a_b literal"
        changed["reply_to_message"]["entities"] = [
            {"type": "italic", "offset": 4, "length": len("italics")},
            {"type": "code", "offset": changed_text.index(changed_code), "length": len(changed_code)},
        ]
        unresolved = self.bot.resolve_message_topic("guest_message", changed)
        self.assertNotIn("message_thread_id", unresolved)

    def test_guest_creation_reply_recovers_topic_before_controller_and_prompt(self) -> None:
        message = self.message()
        message["reply_to_message"] = {
            "message_id": 42,
            "chat": message["chat"],
            "forum_topic_created": {"name": "Assistant", "icon_color": 16749490},
        }

        self.bot.dispatch_update({"update_id": 1, "guest_message": message})

        controller = self.bot.controllers["guest:-1001:42"]
        kind, normalized = controller.events[0][1]
        prompt = build_codex_prompt(kind, normalized, self.bot.bot_user)
        self.assertEqual(prompt_context(prompt)["message_thread_id"], 42)
        self.assertEqual(prompt[1], "Question")
        self.assertNotIn("message_thread_id", message)

    def test_live_topic_message_teaches_recovery_for_future_replies(self) -> None:
        unknown = self.quoted_reply("An old answer that was never observed.", 150)
        self.bot.dispatch_update({"update_id": 1, "guest_message": unknown})
        self.assertIn("guest:-1001", self.bot.controllers)

        message = self.message(151)
        message["reply_to_message"] = {
            "message_id": 42,
            "chat": message["chat"],
            "forum_topic_created": {"name": "Assistant"},
        }
        self.bot.dispatch_update({"update_id": 2, "guest_message": message})
        controller = self.bot.controllers["guest:-1001:42"]
        normalized = controller.events[0][1][1]
        self.bot.telegram.answer_guest_query.return_value = {"html": "<p>A new live answer.</p>"}
        self.bot.answer_guest_response("guest-151", "A new live answer.", normalized, "response")
        self.bot.topic_history = TelegramTopicHistory(self.path, format_current_message_markdown)

        self.bot.dispatch_update({
            "update_id": 3,
            "guest_message": self.quoted_reply("A new live answer.", 153),
        })

        kind, reply = controller.events[-1][1]
        self.assertEqual(reply["message_thread_id"], 42)
        self.assertEqual(prompt_context(build_codex_prompt(kind, reply, self.bot.bot_user))["message_thread_id"], 42)

    def test_guest_response_fingerprint_survives_restart_and_routes_reply(self) -> None:
        self.bot.telegram.answer_guest_query.return_value = {
            "html": "<p>A <b>distinctive</b> answer.</p>",
        }
        delivery = self.bot.answer_guest_response(
            "guest-101", "A **distinctive** answer.", self.message(topic=42), "response",
        )
        self.assertTrue(delivery.delivered)
        self.bot.topic_history = TelegramTopicHistory(self.path, format_current_message_markdown)

        self.bot.dispatch_update({
            "update_id": 2,
            "guest_message": self.quoted_reply("A distinctive answer."),
        })

        controller = self.bot.controllers["guest:-1001:42"]
        self.assertEqual(controller.events[0][1][1]["message_thread_id"], 42)

    def test_only_delivered_guest_content_is_indexed(self) -> None:
        self.bot.telegram.answer_guest_query.return_value = {"html": "<p>First part.</p>"}
        self.bot.answer_guest_response(
            "guest-101", "First part.\n\nUnsent second part.", self.message(topic=42), "response",
        )

        delivered = self.history.resolve(
            "guest_message", self.quoted_reply("First part."), self.bot.bot_user,
        )
        unsent = self.history.resolve(
            "guest_message", self.quoted_reply("First part.\n\nUnsent second part.", 301),
            self.bot.bot_user,
        )
        self.assertEqual(delivered["message_thread_id"], 42)
        self.assertNotIn("message_thread_id", unsent)

    def test_failed_guest_response_indexes_delivered_error_only(self) -> None:
        self.bot.telegram.answer_guest_query.side_effect = ValueError("delivery failed")
        with self.assertLogs("bot", level="ERROR"):
            delivery = self.bot.answer_guest_response(
                "guest-101", "Unsent answer.", self.message(topic=42), "response",
            )
        self.assertTrue(delivery.delivered)
        delivered = self.history.resolve(
            "guest_message", self.quoted_reply("delivery failed"), self.bot.bot_user,
        )
        unsent = self.history.resolve(
            "guest_message", self.quoted_reply("Unsent answer.", 301), self.bot.bot_user,
        )
        self.assertEqual(delivered["message_thread_id"], 42)
        self.assertNotIn("message_thread_id", unsent)

    def test_every_plain_response_chunk_id_is_saved(self) -> None:
        client = object.__new__(TelegramClient)
        client.topic_history = self.history
        client._api = MagicMock(side_effect=[
            {"result": {"message_id": 501, "text": "A" * 4096}},
            {"result": {"message_id": 502, "text": "A" * 10}},
        ])
        client.send_plain_message(-1001, "A" * 4106, None, 42)
        reloaded = TelegramTopicHistory(self.path, format_current_message_markdown)
        for message_id in (501, 502):
            with self.subTest(message_id=message_id):
                message = self.message(message_id + 100)
                message["reply_to_message"] = {"message_id": message_id, "chat": message["chat"]}
                normalized = reloaded.resolve("message", message, self.bot.bot_user)
                self.assertEqual(normalized["message_thread_id"], 42)

    def test_ordinary_rich_failure_indexes_only_the_actual_plain_delivery(self) -> None:
        client = object.__new__(TelegramClient)
        client.topic_history = self.history
        client.allowed_media_roots = ()

        def api(method: str, payload: dict) -> dict:
            if method == "sendRichMessage":
                raise ValueError("rich unavailable")
            return {"result": {"message_id": 501, "text": payload["text"]}}

        client._api = MagicMock(side_effect=api)
        self.bot.telegram = client
        with self.assertLogs("bot", level="ERROR"):
            self.bot.send_message_response(-1001, "Unsent answer.", 101, 42, "response", True)
        delivered = self.history.resolve(
            "message", self.quoted_reply("rich unavailable"), self.bot.bot_user,
        )
        unsent = self.history.resolve(
            "message", self.quoted_reply("Unsent answer.", 301), self.bot.bot_user,
        )
        self.assertEqual(delivered["message_thread_id"], 42)
        self.assertNotIn("message_thread_id", unsent)


if __name__ == "__main__":
    unittest.main()
