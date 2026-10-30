import copy
import html
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import (
    Bot,
    CodexRunner,
    CodexTurnHandle,
    ConversationController,
    GUEST_RESPONSE_WARNING_MESSAGE,
    QueuedTelegramSticker,
    telegram_message_conversation_key,
)
from telegram_topic_history import response_fingerprint
from test_bot import build_config, prompt_context, prompt_user_message


class GuestReplyDedupIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bot = object.__new__(Bot)
        self.bot.config = build_config(Path(self.directory.name))
        self.bot.bot_user = {"id": 99, "is_bot": True, "first_name": "Codex"}
        self.bot.attachments = MagicMock()
        self.bot.attachments.prepare_message.return_value = (None, False)
        self.bot.telegram = MagicMock()
        self.bot.turn_executor = MagicMock()
        self.bot.update_executor = MagicMock()
        self.bot.codex = CodexRunner(self.bot.config, self.bot.telegram)
        self.bot.codex.git_puller = MagicMock()
        self.bot.codex.client = MagicMock()
        self.bot.codex.client.generation = 1
        self.bot.codex.client.start_thread.side_effect = ("thread-one", "thread-two", "thread-three")
        self.bot.codex.client.start_turn.side_effect = self._start_turn
        self.prompts = []
        self.controllers = {}
        self.next_turn = 0

    def _start_turn(self, session_id, prompt, *args):
        self.next_turn += 1
        self.prompts.append(prompt)
        return CodexTurnHandle(session_id, f"turn-{self.next_turn}", queue.Queue())

    def message(self, message_id=10, *, chat=None, sender=1001):
        return {
            "message_id": message_id,
            "date": time.time(),
            "guest_query_id": f"guest-{message_id}",
            "text": "Follow up",
            "chat": chat or {"id": -1001, "type": "supergroup"},
            "from": {"id": sender, "first_name": "User"},
        }

    def reply(self, body, message_id=20, **message_fields):
        message = self.message(message_id, **message_fields)
        message["reply_to_message"] = {
            "message_id": message_id - 1,
            "date": time.time(),
            "chat": message["chat"],
            "from": self.bot.bot_user,
            **copy.deepcopy(body),
        }
        return message

    def controller(self, message):
        key = telegram_message_conversation_key("guest_message", message)
        if key not in self.controllers:
            controller = ConversationController(self.bot, message["chat"]["id"], key)
            self.controllers[key] = controller
            self.addCleanup(controller.shutdown)
        return self.controllers[key]

    def begin(self, message):
        controller = self.controller(message)
        with patch("bot.threading.Timer"):
            controller._process_incoming("guest_message", message)
        self.assertIsNotNone(controller.active_turn)
        return controller

    def finish(self, controller, model_text, delivered_html):
        self.bot.telegram.answer_guest_query.return_value = {"html": delivered_html}
        running = controller.active_turn
        self.bot.codex.client.wait_turn.return_value = model_text
        self.bot.codex.wait_turn(running)
        controller._process_turn_completed(running, model_text, None)

    def prompt(self, message, *, record="current"):
        controller = self.controller(message)
        prepared = controller._prepare_input("guest_message", message)
        self.assertIsNotNone(prepared)
        if record == "current":
            record = self.bot.codex.sessions.get_record(controller.conversation_key)
        return prepared.prompt(record)

    def test_live_final_reply_is_omitted_on_same_session_for_text_formatting_and_math(self) -> None:
        cases = (
            ("Known answer.", "<p>Known answer.</p>", {"text": "Known answer."}),
            (
                "Use *italics* and `a_b **literal**`.",
                "<p>Use <i>italics</i> and <code>a_b **literal**</code>.</p>",
                {
                    "text": "Use italics and a_b **literal**.",
                    "entities": [
                        {"type": "italic", "offset": 4, "length": 7},
                        {"type": "code", "offset": 16, "length": 15},
                    ],
                },
            ),
            (
                r"Result \(\frac{a}{b}\).",
                r"<p>Result <tg-math>\frac{a}{b}</tg-math>.</p>",
                {"rich_message": {"blocks": [{"type": "paragraph", "text": [
                    "Result ", {"type": "mathematical_expression", "expression": r"\frac{a}{b}"}, ".",
                ]}]}},
            ),
        )
        for index, (model_text, delivered_html, body) in enumerate(cases):
            with self.subTest(index=index):
                controller = self.begin(self.message(100 + index * 10))
                session_id = controller.active_turn.session_id
                self.finish(controller, model_text, delivered_html)
                marker = self.bot.codex.sessions.get_record(controller.conversation_key)
                self.assertEqual(marker.session_id, session_id)
                self.assertIsNotNone(marker.last_guest_response_fingerprint)

                incoming = self.reply(body, 101 + index * 10)
                resumed = self.begin(incoming)

                self.assertEqual(resumed.active_turn.session_id, session_id)
                self.assertNotIn("reply_to_message", prompt_context(self.prompts[-1]))
                self.assertEqual(prompt_user_message(self.prompts[-1]), "Follow up")
                self.finish(resumed, model_text, delivered_html)

    def test_new_thread_changed_mapping_older_body_other_sender_and_mismatch_keep_quote(self) -> None:
        controller = self.begin(self.message())
        self.finish(controller, "Older answer.", "<p>Older answer.</p>")
        controller = self.begin(self.message(11))
        self.finish(controller, "Latest answer.", "<p>Latest answer.</p>")
        latest = self.reply({"text": "Latest answer."})

        for body in ({"text": "Older answer."}, {"text": "Different answer."}):
            with self.subTest(body=body):
                self.assertIn("reply_to_message", prompt_context(self.prompt(self.reply(body))))
        other_sender = copy.deepcopy(latest)
        other_sender["reply_to_message"]["from"] = {"id": 3003, "is_bot": False}
        self.assertIn("reply_to_message", prompt_context(self.prompt(other_sender)))
        self.assertIn("reply_to_message", prompt_context(self.prompt(latest, record=None)))

        self.bot.codex.sessions.set(controller.conversation_key, "replacement-thread")
        self.assertIn("reply_to_message", prompt_context(self.prompt(latest)))

    def test_only_actual_delivered_partial_payload_matches_the_latest_marker(self) -> None:
        controller = self.begin(self.message())
        self.finish(controller, "Sent first part.\n\nUnsent second part.", "<p>Sent first part.</p>")

        sent = self.prompt(self.reply({"text": "Sent first part."}))
        unsent = self.prompt(self.reply({"text": "Sent first part.\n\nUnsent second part."}, 30))

        self.assertNotIn("reply_to_message", prompt_context(sent))
        self.assertIn("reply_to_message", prompt_context(unsent))

    def test_successful_rich_failure_fallback_clears_model_response_marker(self) -> None:
        controller = self.begin(self.message())
        self.finish(controller, "Previous answer.", "<p>Previous answer.</p>")
        controller = self.begin(self.message(11))
        self.bot.telegram.answer_guest_query.side_effect = RuntimeError("rich delivery failed")

        with self.assertLogs("bot", level="ERROR"):
            self.finish(controller, "Unsent model answer.", "<p>Unsent model answer.</p>")

        record = self.bot.codex.sessions.get_record(controller.conversation_key)
        self.assertIsNone(record.last_guest_response_fingerprint)
        for text in ("Previous answer.", "Unsent model answer.", "rich delivery failed"):
            with self.subTest(text=text):
                self.assertIn("reply_to_message", prompt_context(self.prompt(self.reply({"text": text}))))

    def test_old_turn_completion_cannot_replace_a_new_session_marker(self) -> None:
        controller = self.begin(self.message())
        key = controller.conversation_key
        self.bot.codex.sessions.set(key, "replacement-thread")
        newer_marker = response_fingerprint({"html": "<p>New session answer.</p>"})
        self.assertTrue(self.bot.codex.record_guest_response(key, "replacement-thread", newer_marker))

        self.finish(controller, "Old session answer.", "<p>Old session answer.</p>")

        record = self.bot.codex.sessions.get_record(key)
        self.assertEqual(record.session_id, "replacement-thread")
        self.assertEqual(record.last_guest_response_fingerprint, newer_marker)

    def test_failed_rich_and_plain_delivery_preserve_the_last_successful_marker(self) -> None:
        controller = self.begin(self.message())
        self.finish(controller, "Previous answer.", "<p>Previous answer.</p>")
        previous = self.bot.codex.sessions.get_record(controller.conversation_key).last_guest_response_fingerprint
        controller = self.begin(self.message(11))
        self.bot.telegram.answer_guest_query.side_effect = RuntimeError("rich delivery failed")
        self.bot.telegram.answer_guest_query_with_plain_text.side_effect = RuntimeError("plain delivery failed")

        with self.assertLogs("bot", level="ERROR"):
            self.finish(controller, "Unsent model answer.", "<p>Unsent model answer.</p>")

        self.assertEqual(self.bot.codex.sessions.get_record(controller.conversation_key).last_guest_response_fingerprint, previous)
        self.assertNotIn("reply_to_message", prompt_context(self.prompt(self.reply({"text": "Previous answer."}))))

    def test_successful_sticker_only_response_clears_previous_text_marker(self) -> None:
        controller = self.begin(self.message())
        self.finish(controller, "Previous answer.", "<p>Previous answer.</p>")
        controller = self.begin(self.message(11))
        self.bot.codex.queued_guest_stickers[controller.active_turn.handle.turn_id] = [
            QueuedTelegramSticker("happy", Path(self.directory.name) / "happy.webp", 0),
        ]

        self.finish(controller, "", "")

        self.bot.telegram.answer_guest_query_with_sticker.assert_called_once()
        self.assertIsNone(self.bot.codex.sessions.get_record(controller.conversation_key).last_guest_response_fingerprint)

    def test_synthetic_model_execution_error_clears_previous_marker(self) -> None:
        controller = self.begin(self.message())
        self.finish(controller, "Previous answer.", "<p>Previous answer.</p>")
        controller = self.begin(self.message(11))
        running = controller.active_turn
        failure = RuntimeError("model execution failed")
        self.bot.codex.client.wait_turn.side_effect = failure
        with self.assertRaisesRegex(RuntimeError, "model execution failed"):
            self.bot.codex.wait_turn(running)
        self.bot.telegram.answer_guest_query.return_value = {"html": "<p>Execution failed.</p>"}

        controller._process_turn_completed(running, "Execution failed.", failure)

        self.assertIsNone(self.bot.codex.sessions.get_record(controller.conversation_key).last_guest_response_fingerprint)
        self.assertIn("reply_to_message", prompt_context(self.prompt(self.reply({"text": "Execution failed."}))))

    def test_delivered_deadline_warning_clears_marker_and_late_model_answer_does_not_restore_it(self) -> None:
        controller = self.begin(self.message())
        self.finish(controller, "Previous answer.", "<p>Previous answer.</p>")
        controller = self.begin(self.message(11))
        self.bot.telegram.answer_guest_query.return_value = {
            "html": f"<p>{html.escape(GUEST_RESPONSE_WARNING_MESSAGE)}</p>",
        }

        controller._process_guest_deadline(controller.target.generation)

        self.assertIsNone(self.bot.codex.sessions.get_record(controller.conversation_key).last_guest_response_fingerprint)
        self.finish(controller, "Late answer.", "<p>Late answer.</p>")
        self.assertIsNone(self.bot.codex.sessions.get_record(controller.conversation_key).last_guest_response_fingerprint)
        self.assertIn("reply_to_message", prompt_context(self.prompt(self.reply({"text": "Late answer."}))))

    def test_private_pair_reverse_direction_shares_marker_without_leaking_to_other_pairs(self) -> None:
        original = self.message(chat={"id": 2002, "type": "private"}, sender=1001)
        controller = self.begin(original)
        self.finish(controller, "Shared private answer.", "<p>Shared private answer.</p>")
        reverse = self.reply(
            {"text": "Shared private answer."},
            chat={"id": 1001, "type": "private"},
            sender=2002,
        )
        other_pair = self.reply(
            {"text": "Shared private answer."},
            chat={"id": 1001, "type": "private"},
            sender=3003,
        )

        self.assertIs(self.controller(reverse), controller)
        self.assertNotIn("reply_to_message", prompt_context(self.prompt(reverse)))
        self.assertIn("reply_to_message", prompt_context(self.prompt(other_pair)))


if __name__ == "__main__":
    unittest.main()
