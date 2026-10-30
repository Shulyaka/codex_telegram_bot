import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import (
    CodexRunner,
    SessionRecord,
    SessionStore,
    reply_targets_recorded_bot_message,
    telegram_message_conversation_key,
)
from telegram_topic_history import response_fingerprint
from test_bot import build_config


class GuestReplyDedupStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sessions.json"
        self.store = SessionStore(self.path)
        self.key = "guest:-1001:42"
        self.store.set(self.key, "session-one", 100.0)
        self.bot = {"id": 99, "is_bot": True}

    def message(self, text: str = "Known answer", **reply_fields) -> dict:
        return {
            "guest_query_id": "guest-query",
            "message_id": 101,
            "message_thread_id": 42,
            "chat": {"id": -1001, "type": "supergroup"},
            "text": "Follow up",
            "reply_to_message": {
                "message_id": 100,
                "from": self.bot,
                "chat": {"id": -1001, "type": "supergroup"},
                "text": text,
                **reply_fields,
            },
        }

    def remember(self, text: str = "Known answer") -> str:
        fingerprint = response_fingerprint(text)
        self.assertIsNotNone(fingerprint)
        self.assertTrue(self.store.record_guest_response(self.key, "session-one", fingerprint))
        return fingerprint

    def test_latest_guest_response_survives_restart_without_a_numeric_bot_id(self) -> None:
        fingerprint = self.remember()
        record = SessionStore(self.path).get_record(self.key)
        self.assertEqual(record.last_guest_response_fingerprint, fingerprint)
        self.assertIsNone(record.last_bot_message_id)
        self.assertTrue(reply_targets_recorded_bot_message(self.message(), record, self.bot))

    def test_same_session_mutations_preserve_the_guest_marker(self) -> None:
        fingerprint = self.remember()
        operations = (
            lambda: self.store.set(self.key, "session-one", 110.0),
            lambda: self.store.touch(self.key, 120.0),
            lambda: self.store.mark_compacted(self.key, "session-one", 130.0),
            lambda: self.store.record_bot_message(self.key, "session-one", 100),
        )
        for operation in operations:
            operation()
            self.assertEqual(self.store.get_record(self.key).last_guest_response_fingerprint, fingerprint)
        record = self.store.get_record(self.key)
        self.assertEqual(record.last_message_at, 120.0)
        self.assertEqual(record.last_compacted_at, 130.0)

    def test_new_session_and_archival_reset_the_marker(self) -> None:
        self.remember()
        self.store.set(self.key, "session-two", 200.0)
        record = self.store.get_record(self.key)
        self.assertIsNone(record.last_guest_response_fingerprint)
        self.assertFalse(reply_targets_recorded_bot_message(self.message(), record, self.bot))
        self.assertTrue(self.store.remove(self.key, "session-two"))
        self.store.set(self.key, "session-three", 300.0)
        self.assertIsNone(self.store.get_record(self.key).last_guest_response_fingerprint)

    def test_delayed_delivery_cannot_mark_or_clear_a_replacement_session(self) -> None:
        old_fingerprint = self.remember()
        self.store.set(self.key, "session-two", 200.0)
        new_fingerprint = response_fingerprint("A different response")
        self.assertTrue(self.store.record_guest_response(self.key, "session-two", new_fingerprint))
        self.assertFalse(self.store.record_guest_response(self.key, "session-one", old_fingerprint))
        self.assertFalse(self.store.record_guest_response(self.key, "session-one", None))
        self.assertEqual(self.store.get_record(self.key).last_guest_response_fingerprint, new_fingerprint)

    def test_clearing_a_marker_preserves_activity_and_other_metadata(self) -> None:
        self.remember()
        self.store.mark_compacted(self.key, "session-one", 200.0)
        self.store.record_bot_message(self.key, "session-one", 100)
        self.assertTrue(self.store.record_guest_response(self.key, "session-one", None))
        record = self.store.get_record(self.key)
        self.assertIsNone(record.last_guest_response_fingerprint)
        self.assertEqual((record.last_message_at, record.last_compacted_at, record.last_bot_message_id), (100.0, 200.0, 100))

    def test_ordinary_or_missing_conversations_cannot_receive_guest_markers(self) -> None:
        self.store.set(-1001, "ordinary-session")
        fingerprint = response_fingerprint("Known answer")
        self.assertFalse(self.store.record_guest_response(-1001, "ordinary-session", fingerprint))
        self.assertFalse(self.store.record_guest_response("guest:-2002", "missing-session", fingerprint))
        self.assertIsNone(self.store.get_record(-1001).last_guest_response_fingerprint)

    def test_invalid_fingerprint_updates_are_rejected(self) -> None:
        original = self.remember()
        for value in ("", "short", "A" * 64, "z" * 64, 123, True, []):
            with self.subTest(value=value):
                self.assertFalse(self.store.record_guest_response(self.key, "session-one", value))
                self.assertEqual(self.store.get_record(self.key).last_guest_response_fingerprint, original)

    def test_version_four_without_optional_field_loads_without_forced_migration(self) -> None:
        payload = {"version": 4, "sessions": {self.key: {"session_id": "old-session", "last_message_at": 100.0}}, "response_errors": {}}
        original = json.dumps(payload)
        self.path.write_text(original, encoding="utf-8")
        with patch.object(SessionStore, "_save") as save:
            loaded = SessionStore(self.path)
        save.assert_not_called()
        self.assertIsNone(loaded.get_record(self.key).last_guest_response_fingerprint)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_invalid_persisted_fingerprints_are_startup_errors(self) -> None:
        self.remember()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for value in ("", "invalid", "A" * 64, 7, False):
            with self.subTest(value=value):
                payload["sessions"][self.key]["last_guest_response_fingerprint"] = value
                self.path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    SessionStore(self.path)

    def test_only_the_latest_response_fingerprint_matches(self) -> None:
        self.remember("Older answer")
        self.remember("Latest answer")
        record = self.store.get_record(self.key)
        self.assertFalse(reply_targets_recorded_bot_message(self.message("Older answer"), record, self.bot))
        self.assertTrue(reply_targets_recorded_bot_message(self.message("Latest answer"), record, self.bot))

    def test_guest_numeric_message_ids_never_trigger_ordinary_dedup(self) -> None:
        self.store.record_bot_message(self.key, "session-one", 100)
        record = self.store.get_record(self.key)
        self.assertFalse(reply_targets_recorded_bot_message(self.message(), record, self.bot))
        self.remember("Different answer")
        self.assertFalse(reply_targets_recorded_bot_message(self.message(), self.store.get_record(self.key), self.bot))

    def test_guest_reply_requires_current_bot_and_a_resumed_record(self) -> None:
        self.remember()
        record = self.store.get_record(self.key)
        self.assertFalse(reply_targets_recorded_bot_message(self.message(), None, self.bot))
        self.assertFalse(reply_targets_recorded_bot_message(self.message(), record))
        for sender in ({"id": 77, "is_bot": True}, {"id": 78}, {}, {"id": True}):
            with self.subTest(sender=sender):
                self.assertFalse(reply_targets_recorded_bot_message(self.message(**{"from": sender}), record, self.bot))

    def test_external_and_other_chat_replies_remain_context(self) -> None:
        self.remember()
        record = self.store.get_record(self.key)
        cross_chat = self.message(chat={"id": -2002})
        self.assertFalse(reply_targets_recorded_bot_message(cross_chat, record, self.bot))
        external = self.message()
        external["external_reply"] = {"origin": {"type": "user"}}
        self.assertFalse(reply_targets_recorded_bot_message(external, record, self.bot))
        external.pop("reply_to_message")
        self.assertFalse(reply_targets_recorded_bot_message(external, record, self.bot))

    def test_selected_quote_and_missing_body_do_not_match_the_full_response(self) -> None:
        self.remember("Known answer")
        record = self.store.get_record(self.key)
        message = self.message("A different full answer")
        message["quote"] = {"text": "Known answer"}
        self.assertFalse(reply_targets_recorded_bot_message(message, record, self.bot))
        message["reply_to_message"].pop("text")
        self.assertFalse(reply_targets_recorded_bot_message(message, record, self.bot))

    def test_rendered_math_and_code_use_the_existing_response_fingerprint(self) -> None:
        html = {"html": r"<p>Potential <tg-math>\varphi</tg-math> and <code>x_1</code>.</p>"}
        fingerprint = response_fingerprint(html)
        self.store.record_guest_response(self.key, "session-one", fingerprint)
        message = self.message("")
        message["reply_to_message"]["rich_message"] = {"blocks": [{
            "type": "paragraph", "text": [
                "Potential ", {"type": "mathematical_expression", "expression": r"\varphi"},
                " and ", {"type": "code", "text": "x_1"}, ".",
            ],
        }]}
        self.assertTrue(reply_targets_recorded_bot_message(message, self.store.get_record(self.key), self.bot))

    def test_fingerprint_failure_preserves_reply_context(self) -> None:
        self.remember()
        with patch("bot.response_fingerprint", side_effect=ValueError("conversion failed")):
            with self.assertLogs("bot", level="WARNING"):
                self.assertFalse(reply_targets_recorded_bot_message(self.message(), self.store.get_record(self.key), self.bot))

    def test_ordinary_two_argument_numeric_matching_is_unchanged(self) -> None:
        message = self.message()
        message.pop("guest_query_id")
        record = SessionRecord("ordinary-session", 100.0, last_bot_message_id=100)
        self.assertTrue(reply_targets_recorded_bot_message(message, record))
        self.assertTrue(reply_targets_recorded_bot_message(message, record, self.bot))

    def test_runner_wrapper_keeps_expected_session_guard(self) -> None:
        runner = CodexRunner(build_config(Path(self.directory.name)))
        runner.sessions.set(self.key, "session-one")
        fingerprint = response_fingerprint("Known answer")
        self.assertTrue(runner.record_guest_response(self.key, "session-one", fingerprint))
        runner.sessions.set(self.key, "session-two")
        self.assertFalse(runner.record_guest_response(self.key, "session-one", fingerprint))

    def test_private_guest_pair_uses_shared_marker_for_both_participants(self) -> None:
        forward = self.message()
        forward["from"] = {"id": 1001}
        forward["chat"] = {"id": 1002, "type": "private"}
        pair = telegram_message_conversation_key("guest_message", forward)
        self.store.set(pair, "private-session")
        self.store.record_guest_response(pair, "private-session", response_fingerprint("Known answer"))
        reverse = self.message()
        reverse["from"] = {"id": 1002}
        reverse["chat"] = {"id": 1001, "type": "private"}
        reverse["reply_to_message"]["chat"] = reverse["chat"]
        self.assertEqual(telegram_message_conversation_key("guest_message", reverse), pair)
        self.assertTrue(reply_targets_recorded_bot_message(reverse, self.store.get_record(pair), self.bot))


if __name__ == "__main__":
    unittest.main()
