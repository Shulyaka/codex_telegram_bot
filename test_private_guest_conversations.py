import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot import (
    AttachmentStore,
    ConversationController,
    SessionStore,
    conversation_chat_id,
    conversation_message_thread_id,
    guest_conversation_key,
    is_guest_conversation_key,
    parse_conversation_storage_key,
    parsed_conversation_key,
    telegram_message_conversation_key,
    telegram_thread_key,
    valid_conversation_storage_key,
)


class PrivateGuestConversationTests(unittest.TestCase):
    @staticmethod
    def message(sender: int = 1001, recipient: int = 1002, topic: int | None = None) -> dict:
        message = {
            "message_id": 100,
            "guest_query_id": "guest-query",
            "text": "Question",
            "from": {"id": sender, "username": f"user{sender}"},
            "chat": {"id": recipient, "type": "private", "username": f"user{recipient}"},
        }
        if topic is not None:
            message["message_thread_id"] = topic
        return message

    def test_private_guest_key_is_the_same_in_both_directions(self) -> None:
        forward = telegram_message_conversation_key("guest_message", self.message())
        reverse = telegram_message_conversation_key("guest_message", self.message(1002, 1001))
        self.assertEqual(forward, "guest:private:1001:1002")
        self.assertEqual(reverse, forward)

    def test_different_people_with_the_same_recipient_have_separate_keys(self) -> None:
        first = telegram_message_conversation_key("guest_message", self.message(1001, 1002))
        second = telegram_message_conversation_key("guest_message", self.message(1003, 1002))
        self.assertNotEqual(first, second)
        self.assertEqual(second, "guest:private:1002:1003")

    def test_names_and_usernames_do_not_change_private_identity(self) -> None:
        message = self.message()
        message["from"]["username"] = "renamed"
        message["chat"]["username"] = "also_renamed"
        self.assertEqual(telegram_message_conversation_key("guest_message", message), "guest:private:1001:1002")

    def test_ordinary_and_group_conversation_keys_remain_unchanged(self) -> None:
        self.assertEqual(telegram_message_conversation_key("message", self.message()), 1002)
        self.assertEqual(telegram_message_conversation_key("message", self.message(topic=42)), "topic:1002:42")
        group = self.message(topic=42)
        group["chat"] = {"id": -10099, "type": "supergroup"}
        self.assertEqual(telegram_message_conversation_key("guest_message", group), "guest:-10099:42")
        self.assertEqual(telegram_message_conversation_key("message", group), "topic:-10099:42")

    def test_topics_remain_separate_and_reverse_direction_uses_same_topic(self) -> None:
        first = telegram_message_conversation_key("guest_message", self.message(topic=42))
        reverse = telegram_message_conversation_key("guest_message", self.message(1002, 1001, 42))
        second = telegram_message_conversation_key("guest_message", self.message(topic=81))
        self.assertEqual(first, "guest:private:1001:1002:42")
        self.assertEqual(reverse, first)
        self.assertNotEqual(first, second)

    def test_missing_equal_or_invalid_sender_falls_back_without_guessing(self) -> None:
        for sender in (None, {}, {"id": None}, {"id": True}, {"id": -1}, {"id": "1001"}, {"id": 1002}):
            with self.subTest(sender=sender):
                message = self.message(topic=42)
                if sender is None:
                    message.pop("from")
                else:
                    message["from"] = sender
                self.assertEqual(telegram_message_conversation_key("guest_message", message), "guest:1002:42")

    def test_invalid_topic_is_not_added_to_private_key(self) -> None:
        for topic in (0, -1, True, "42"):
            with self.subTest(topic=topic):
                self.assertEqual(telegram_message_conversation_key("guest_message", self.message(topic=topic)), "guest:private:1001:1002")

    def test_parser_and_storage_validation_support_canonical_private_pairs(self) -> None:
        self.assertEqual(parsed_conversation_key("guest:private:1001:1002"), (1001, None, True))
        self.assertEqual(parsed_conversation_key("guest:private:1001:1002:42"), (1001, 42, True))
        self.assertEqual(conversation_chat_id("guest:private:1001:1002"), 1001)
        self.assertEqual(conversation_message_thread_id("guest:private:1001:1002:42"), 42)
        self.assertTrue(is_guest_conversation_key("guest:private:1001:1002"))
        self.assertTrue(valid_conversation_storage_key("guest:private:1001:1002:42"))
        self.assertEqual(parse_conversation_storage_key("guest:private:1001:1002"), "guest:private:1001:1002")

    def test_noncanonical_or_invalid_private_pair_storage_keys_are_rejected(self) -> None:
        keys = (
            "guest:private:1001", "guest:private:1002:1001", "guest:private:1001:1001",
            "guest:private:0:1002", "guest:private:-1:1002", "guest:private:a:1002",
            "guest:private:01001:1002", "guest:private:+1001:1002", "guest:private:1001:1002:0",
            "guest:private:1001:1002:-1", "guest:private:1001:1002:True", "guest:private:1001:1002:",
            "guest:private:1001:1002:42:43",
        )
        for key in keys:
            with self.subTest(key=key):
                self.assertFalse(valid_conversation_storage_key(key))
                with self.assertRaises(ValueError):
                    parsed_conversation_key(key)

    def test_session_pair_survives_restart_and_legacy_histories_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            store = SessionStore(path)
            store.set(guest_conversation_key(1001), "legacy-first")
            store.set(guest_conversation_key(1002), "legacy-second")
            pair = telegram_message_conversation_key("guest_message", self.message())
            self.assertIsNone(store.get(pair))
            store.set(pair, "shared-pair-session")
            reloaded = SessionStore(path)
            reverse = telegram_message_conversation_key("guest_message", self.message(1002, 1001))
            self.assertEqual(reloaded.get(reverse), "shared-pair-session")
            self.assertEqual(reloaded.get(guest_conversation_key(1001)), "legacy-first")
            self.assertEqual(reloaded.get(guest_conversation_key(1002)), "legacy-second")

    def test_private_guest_attachment_cache_is_shared_in_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AttachmentStore(Path(directory))
            forward, _ = store.prepare_message(self.message(), True)
            reverse, _ = store.prepare_message(self.message(1002, 1001), True)
            different_pair, _ = store.prepare_message(self.message(1003, 1002), True)
            self.assertEqual(forward, reverse)
            self.assertNotEqual(forward, different_pair)
            self.assertEqual(forward.parent.name, "guest-private-1001-1002-thread-main")

    def test_attachment_cache_topics_and_existing_ordinary_paths_remain_separate(self) -> None:
        self.assertNotEqual(telegram_thread_key(self.message(topic=42)), telegram_thread_key(self.message(topic=81)))
        ordinary = self.message()
        ordinary.pop("guest_query_id")
        self.assertEqual(telegram_thread_key(ordinary), "chat-1002-thread-main")
        group = self.message(topic=42)
        group["chat"] = {"id": -10099, "type": "supergroup"}
        self.assertEqual(telegram_thread_key(group), "chat--10099-thread-42")

    def test_controller_keeps_pair_identity_and_tracks_the_actual_guest_chat(self) -> None:
        pair = telegram_message_conversation_key("guest_message", self.message())
        controller = ConversationController(MagicMock(), 1002, pair)
        controller._prepare_input = MagicMock(return_value=None)
        controller._process_incoming("guest_message", self.message(1002, 1001))
        self.assertEqual(controller.conversation_key, pair)
        self.assertEqual(controller.chat_id, 1001)


if __name__ == "__main__":
    unittest.main()
