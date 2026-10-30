import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_topic_history import TelegramTopicHistory, normalize_topic_response, private_guest_chat_key

class TelegramTopicHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "sessions.topics.json"
        self.history = TelegramTopicHistory(self.path)
        self.bot = {"id": 99, "is_bot": True}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def message(self, message_id: int = 100, topic: int | None = None, **fields) -> dict:
        message = {"message_id": message_id, "chat": {"id": -1001, "type": "supergroup"}, "text": "Question"}
        if topic is not None:
            message["message_thread_id"] = topic
        message.update(fields)
        return message

    def reply(self, message_id: int = 99, text: str = "Answer", **fields) -> dict:
        reply = {"message_id": message_id, "from": self.bot, "text": text}
        reply.update(fields)
        return reply

    def private_message(self, sender: int, peer: int, message_id: int = 100, topic: int | None = None, **fields) -> dict:
        message = self.message(message_id, topic, chat={"id": peer, "type": "private"})
        message["from"] = {"id": sender}
        message.update(fields)
        return message

    def test_private_guest_chat_key_is_shared_by_both_participants(self) -> None:
        self.assertEqual(private_guest_chat_key(self.private_message(1001, 2002)), "guest:private:1001:2002")
        self.assertEqual(private_guest_chat_key(self.private_message(2002, 1001)), "guest:private:1001:2002")
        invalid_messages = (
            self.message(**{"from": {"id": 1001}}),
            self.private_message(1001, 1001),
            self.private_message(True, 2002),
            self.private_message(1001, True),
            self.private_message(0, 2002),
            self.private_message(1001, -2002),
            self.private_message("1001", 2002),
            {"chat": {"id": 2002, "type": "private"}, "from": {"username": "sender"}, "reply_to_message": {"from": {"id": 1001}}},
            {"from": {"id": 1001}},
        )
        for message in invalid_messages:
            with self.subTest(message=message):
                self.assertIsNone(private_guest_chat_key(message))

    def test_private_guest_senders_sharing_a_peer_have_separate_persisted_evidence(self) -> None:
        for sender, topic in ((1001, 42), (3003, 81)):
            message = self.private_message(sender, 2002, topic=topic)
            self.history.resolve("guest_message", message, self.bot)
            self.history.record_response("guest_message", message, "Same answer")
        reloaded = TelegramTopicHistory(self.path)

        for sender, topic in ((1001, 42), (3003, 81)):
            with self.subTest(sender=sender):
                by_id = reloaded.resolve(
                    "guest_message", self.private_message(sender, 2002, 101, reply_to_message=self.reply(100)), self.bot,
                )
                by_body = reloaded.resolve(
                    "guest_message", self.private_message(sender, 2002, 201, reply_to_message=self.reply(200, "Same answer")), self.bot,
                )
                self.assertEqual(by_id["message_thread_id"], topic)
                self.assertEqual(by_body["message_thread_id"], topic)
        self.assertEqual(
            set(json.loads(self.path.read_text(encoding="utf-8"))["scopes"]),
            {"guest:private:1001:2002:view:2002", "guest:private:2002:3003:view:2002"},
        )

    def test_private_guest_reverse_views_keep_numeric_ids_and_fingerprints_separate(self) -> None:
        for sender, peer, topic in ((1001, 2002, 42), (2002, 1001, 81)):
            message = self.private_message(sender, peer, topic=topic)
            self.history.resolve("guest_message", message, self.bot)
            self.history.record_response("guest_message", message, "Same answer")
        reloaded = TelegramTopicHistory(self.path)

        for sender, peer, topic in ((1001, 2002, 42), (2002, 1001, 81)):
            with self.subTest(sender=sender):
                by_id = reloaded.resolve(
                    "guest_message", self.private_message(sender, peer, 101, reply_to_message=self.reply(100)), self.bot,
                )
                by_body = reloaded.resolve(
                    "guest_message", self.private_message(sender, peer, 201, reply_to_message=self.reply(200, "Same answer")), self.bot,
                )
                self.assertEqual(by_id["message_thread_id"], topic)
                self.assertEqual(by_body["message_thread_id"], topic)

    def test_private_guest_pair_has_no_legacy_scope_alias_and_other_scopes_stay_unchanged(self) -> None:
        legacy = {"message_id": 100, "chat": {"id": 2002, "type": "private"}, "message_thread_id": 42}
        self.history.record_response("guest_message", legacy, "Known answer", 99)
        ordinary = self.private_message(1001, 2002, topic=81)
        self.history.record_response("message", ordinary, "Known answer", 99)
        self.history.record_response("guest_message", self.message(topic=123), "Known answer", 99)
        reloaded = TelegramTopicHistory(self.path)

        paired = reloaded.resolve(
            "guest_message", self.private_message(1001, 2002, reply_to_message=self.reply()), self.bot,
        )
        self.assertNotIn("message_thread_id", paired)
        without_sender = reloaded.resolve(
            "guest_message", {"chat": legacy["chat"], "reply_to_message": self.reply()}, self.bot,
        )
        self.assertEqual(without_sender["message_thread_id"], 42)
        ordinary_reply = reloaded.resolve(
            "message", self.private_message(1001, 2002, reply_to_message=self.reply()), self.bot,
        )
        self.assertEqual(ordinary_reply["message_thread_id"], 81)
        group_reply = reloaded.resolve("guest_message", self.message(reply_to_message=self.reply()), self.bot)
        self.assertEqual(group_reply["message_thread_id"], 123)

    def test_invalid_private_guest_persistence_scope_is_rejected(self) -> None:
        for scope in (
            "guest:private:2002:1001:view:2002",
            "guest:private:1001:1001:view:1001",
            "guest:private:0:2002:view:2002",
            "guest:private:1001:2002:view:3003",
        ):
            with self.subTest(scope=scope):
                self.path.write_text(json.dumps({"version": 1, "scopes": {scope: {"messages": {}, "responses": {}}}}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    TelegramTopicHistory(self.path)

    def test_explicit_topic_is_copied_and_recovered_after_restart(self) -> None:
        original = self.message(topic=42)
        normalized = self.history.resolve("message", original, self.bot)
        normalized["chat"]["title"] = "A copied chat"
        self.assertNotIn("title", original["chat"])
        reloaded = TelegramTopicHistory(self.path)
        self.assertEqual(reloaded.resolve("message", self.message(), self.bot)["message_thread_id"], 42)

    def test_known_reply_message_recovers_topic_after_restart(self) -> None:
        self.history.resolve("message", self.message(100, 42), self.bot)
        reloaded = TelegramTopicHistory(self.path)
        result = reloaded.resolve("message", self.message(101, reply_to_message=self.reply(100)), self.bot)
        self.assertEqual(result["message_thread_id"], 42)

    def test_reply_topic_and_creation_root_are_supported(self) -> None:
        for reply in (
            self.reply(42, message_thread_id=42),
            self.reply(42, forum_topic_created={"name": "Topic"}),
        ):
            with self.subTest(reply=reply):
                result = self.history.resolve("message", self.message(reply_to_message=reply), self.bot)
                self.assertEqual(result["message_thread_id"], 42)
        self.assertEqual(self.history.resolve("message", self.message(81, forum_topic_created={"name": "Another"}), self.bot)["message_thread_id"], 81)

    def test_explicit_false_drops_ordinary_thread_ids(self) -> None:
        original = self.message(topic=700, is_topic_message=False, reply_to_message=self.reply(99, message_thread_id=701))
        result = self.history.resolve("message", original, self.bot)
        self.assertNotIn("message_thread_id", result)
        self.assertEqual(original["message_thread_id"], 700)

    def test_explicit_false_vetoes_creation_or_known_response_inference(self) -> None:
        message = self.message(topic=700, is_topic_message=False, reply_to_message=self.reply(42, forum_topic_created={"name": "Topic"}))
        self.assertNotIn("message_thread_id", self.history.resolve("message", message, self.bot))
        self.history.record_response("message", self.message(topic=42), "Known answer")
        message = self.message(topic=700, is_topic_message=False, reply_to_message=self.reply(text="Known answer"))
        self.assertNotIn("message_thread_id", self.history.resolve("message", message, self.bot))

    def test_explicit_topic_wins_over_other_evidence(self) -> None:
        result = self.history.resolve("message", self.message(topic=42, reply_to_message=self.reply(message_thread_id=81)), self.bot)
        self.assertEqual(result["message_thread_id"], 42)
        later = self.history.resolve("message", self.message(101, reply_to_message=self.reply(99)), self.bot)
        self.assertEqual(later["message_thread_id"], 81)

    def test_conflicting_strong_evidence_does_not_guess(self) -> None:
        self.history.resolve("message", self.message(100, 42), self.bot)
        result = self.history.resolve("message", self.message(100, reply_to_message=self.reply(99, message_thread_id=81)), self.bot)
        self.assertNotIn("message_thread_id", result)

    def test_cross_chat_and_external_reply_are_ignored(self) -> None:
        for fields in (
            {"reply_to_message": self.reply(message_thread_id=42, chat={"id": -2002})},
            {"external_reply": self.reply(message_thread_id=42)},
        ):
            with self.subTest(fields=fields):
                self.assertNotIn("message_thread_id", self.history.resolve("message", self.message(**fields), self.bot))

    def test_current_bot_full_response_hash_recovers_topic(self) -> None:
        answer = "**Known answer** with `an_identifier` &amp; details"
        self.history.record_response("message", self.message(topic=42), answer)
        reloaded = TelegramTopicHistory(self.path)
        quoted = "Known **answer with** `an_identifier` & details"
        result = reloaded.resolve("message", self.message(reply_to_message=self.reply(text=quoted)), self.bot)
        self.assertEqual(result["message_thread_id"], 42)
        self.assertEqual(reloaded.resolve("message", self.message(101, reply_to_message=self.reply(99, "Edited text")), self.bot)["message_thread_id"], 42)

    def test_another_bots_or_users_text_does_not_match(self) -> None:
        self.history.record_response("message", self.message(topic=42), "Known answer")
        for sender in ({"id": 51, "is_bot": True}, {"id": 52, "is_bot": False}, {}):
            with self.subTest(sender=sender):
                result = self.history.resolve("message", self.message(reply_to_message=self.reply(text="Known answer", **{"from": sender})), self.bot)
                self.assertNotIn("message_thread_id", result)

    def test_selected_quote_is_not_treated_as_a_full_bot_answer(self) -> None:
        self.history.record_response("message", self.message(topic=42), "Known answer")
        result = self.history.resolve("message", self.message(reply_to_message=self.reply(text="A different message including Known answer"), quote={"text": "Known answer"}), self.bot)
        self.assertNotIn("message_thread_id", result)

    def test_response_collisions_are_ambiguous_in_both_namespaces(self) -> None:
        for kind in ("message", "guest_message"):
            with self.subTest(kind=kind):
                self.history.record_response(kind, self.message(topic=42), "Same answer")
                self.history.record_response(kind, self.message(topic=81), "Same **answer**")
                reloaded = TelegramTopicHistory(self.path)
                result = reloaded.resolve(kind, self.message(reply_to_message=self.reply(text="Same answer")), self.bot)
                self.assertNotIn("message_thread_id", result)

    def test_numeric_and_response_indexes_are_scoped_by_chat_and_namespace(self) -> None:
        self.history.record_response("guest_message", self.message(topic=42), "Known answer", 99)
        ordinary = self.history.resolve("message", self.message(reply_to_message=self.reply(text="Known answer")), self.bot)
        another_chat = self.history.resolve("guest_message", self.message(chat={"id": -2002}, reply_to_message=self.reply(text="Known answer")), self.bot)
        self.assertNotIn("message_thread_id", ordinary)
        self.assertNotIn("message_thread_id", another_chat)

    def test_empty_document_response_still_records_numeric_message_id(self) -> None:
        self.history.record_response("message", self.message(topic=42), "", 99)
        result = self.history.resolve("message", self.message(reply_to_message=self.reply(text="")), self.bot)
        self.assertEqual(result["message_thread_id"], 42)

    def test_persistence_contains_only_hashes_and_ids_and_is_private(self) -> None:
        secret = "Source body must not be persisted"
        self.history.record_response("message", self.message(topic=42), secret, 99)
        payload = self.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, payload)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(payload)["version"], 1)

    def test_persistence_error_retains_live_index_and_does_not_fail_message(self) -> None:
        with patch("telegram_topic_history.os.replace", side_effect=OSError("disk full")):
            with self.assertLogs("telegram_topic_history", level="WARNING"):
                result = self.history.resolve("message", self.message(topic=42), self.bot)
        self.assertEqual(result["message_thread_id"], 42)
        result = self.history.resolve("message", self.message(101, reply_to_message=self.reply(100)), self.bot)
        self.assertEqual(result["message_thread_id"], 42)
        self.assertTrue(self.path.exists())
        self.assertEqual(list(self.root.glob(".sessions.topics.json.*")), [])

    def test_corrupt_existing_history_is_an_explicit_startup_error(self) -> None:
        for payload in ("broken", '{"version": 2, "scopes": {}}', '{"version": 1, "scopes": {"guest:-1": {"messages": {}, "responses": {"invalid": [1]}}}}'):
            with self.subTest(payload=payload):
                self.path.write_text(payload, encoding="utf-8")
                with self.assertRaises(ValueError):
                    TelegramTopicHistory(self.path)

    def test_html_rich_and_markdown_normalize_to_the_same_response(self) -> None:
        markdown = "# Heading\n\n**Known** `value` and [documentation](https://example.test?a=1&b=2)"
        rich = {"html": '<h1>Heading</h1><p><strong>Known</strong> <code>value</code> and <a href="https://example.test?a=1&amp;b=2">documentation</a></p>'}
        self.assertEqual(normalize_topic_response(markdown), normalize_topic_response(rich))
        self.history.record_response("message", self.message(topic=42), rich)
        result = self.history.resolve("message", self.message(reply_to_message=self.reply(text=markdown)), self.bot)
        self.assertEqual(result["message_thread_id"], 42)

    def test_received_rich_and_entities_use_existing_converters(self) -> None:
        self.history.record_response("message", self.message(topic=42), "A **known** answer")
        for reply in (
            self.reply(text="A known answer", entities=[{"type": "bold", "offset": 2, "length": 5}]),
            self.reply(text="", rich_message={"blocks": [{"type": "paragraph", "text": "A known answer"}]}),
        ):
            with self.subTest(reply=reply):
                self.assertEqual(self.history.resolve("message", self.message(reply_to_message=reply), self.bot)["message_thread_id"], 42)

    def test_chat_wide_or_unknown_response_blocks_a_unique_topic_hash_after_restart(self) -> None:
        for kind in ("message", "guest_message"):
            with self.subTest(kind=kind):
                self.history.record_response(kind, self.message(topic=42), "Complete.")
                self.history.record_response(kind, self.message(200), "Complete.")
                reloaded = TelegramTopicHistory(self.path)
                result = reloaded.resolve(kind, self.message(201, reply_to_message=self.reply(199, "Complete.")), self.bot)
                self.assertNotIn("message_thread_id", result)

    def test_formatting_parity_between_html_and_received_entities_and_rich(self) -> None:
        for entity_type, tag in (("bold", "strong"), ("italic", "em"), ("underline", "u"), ("strikethrough", "s"), ("spoiler", "tg-spoiler")):
            with self.subTest(entity_type=entity_type):
                history = TelegramTopicHistory(self.root / f"{entity_type}.json")
                history.record_response("message", self.message(topic=42), {"html": f"<p>Use <{tag}>formatting</{tag}> carefully.</p>"})
                reply = self.reply(text="Use formatting carefully.", entities=[{"type": entity_type, "offset": 4, "length": 10}])
                self.assertEqual(history.resolve("message", self.message(100, reply_to_message=reply), self.bot)["message_thread_id"], 42)
                rich_reply = self.reply(200, text="", rich_message={"blocks": [{"type": "paragraph", "text": ["Use ", {"type": entity_type, "text": "formatting"}, " carefully."]}]})
                result = history.resolve("message", self.message(201, reply_to_message=rich_reply), self.bot)
                if entity_type == "underline":
                    self.assertNotIn("message_thread_id", result)
                else:
                    self.assertEqual(result["message_thread_id"], 42)

    def test_literal_format_markers_in_code_are_preserved(self) -> None:
        html = {"html": '<p>Use <code>**literal**</code> carefully.</p>'}
        self.assertEqual(normalize_topic_response(html), normalize_topic_response("Use `**literal**` carefully."))
        self.assertNotEqual(normalize_topic_response(html), normalize_topic_response("Use `literal` carefully."))

    def test_literal_html_tags_in_code_remain_literal(self) -> None:
        html = {"html": "<p>Use <code>&lt;u&gt;literal&lt;/u&gt;</code> carefully.</p>"}
        self.assertEqual(normalize_topic_response(html), normalize_topic_response("Use `<u>literal</u>` carefully."))
        self.assertNotEqual(normalize_topic_response(html), normalize_topic_response("Use `literal` carefully."))

    def test_parent_explicit_false_is_not_used_as_topic_evidence(self) -> None:
        self.history.record_response("message", self.message(topic=42), "Known answer", 99)
        for index, fields in enumerate(({}, {"forum_topic_created": {"name": "Topic"}}, {"message_thread_id": 42})):
            with self.subTest(fields=fields):
                reply = self.reply(99, "Known answer", is_topic_message=False, **fields)
                result = self.history.resolve("message", self.message(100 + index * 2, reply_to_message=reply), self.bot)
                self.assertNotIn("message_thread_id", result)
                result = self.history.resolve("message", self.message(101 + index * 2, topic=81, reply_to_message=reply), self.bot)
                self.assertEqual(result["message_thread_id"], 81)

    def test_escaped_literal_format_markers_are_not_erased(self) -> None:
        self.assertNotEqual(normalize_topic_response(r"Pay \*literal\* fee"), normalize_topic_response("Pay literal fee"))
        self.assertEqual(normalize_topic_response({"html": "<p>Pay *literal* fee</p>"}), normalize_topic_response(r"Pay \*literal\* fee"))
        self.assertNotEqual(normalize_topic_response({"html": "<p>Pay *literal* fee</p>"}), normalize_topic_response({"html": "<p>Pay <em>literal</em> fee</p>"}))

    def test_html_code_with_backticks_matches_received_entity_callback(self) -> None:
        from telegram_entities_to_markdown import entities_to_markdown

        history = TelegramTopicHistory(self.path, message_text=lambda message: entities_to_markdown(message.get("text", ""), message.get("entities")))
        literal = "`tick` and **stars**"
        history.record_response("message", self.message(topic=42), {"html": f"<p>Use <code>{literal}</code> carefully.</p>"})
        reply = self.reply(text=f"Use {literal} carefully.", entities=[{"type": "code", "offset": 4, "length": len(literal)}])
        result = history.resolve("message", self.message(reply_to_message=reply), self.bot)
        self.assertEqual(result["message_thread_id"], 42)

    def test_long_code_fences_preserve_embedded_fences(self) -> None:
        from telegram_entities_to_markdown import entities_to_markdown

        code = 'before\n```\n"**literal**"\nafter'
        markdown = entities_to_markdown(code, [{"type": "pre", "offset": 0, "length": len(code), "language": "text"}])
        html = {"html": f"<pre><code>{code}</code></pre>"}
        self.assertEqual(normalize_topic_response(html), normalize_topic_response(markdown))

    def test_html_preformatted_and_markdown_fence_match(self) -> None:
        html = {"html": '<pre><code class="language-python">result = "**literal**"\nprint(result)</code></pre>'}
        markdown = '```python\nresult = "**literal**"\nprint(result)\n```'
        self.assertEqual(normalize_topic_response(html), normalize_topic_response(markdown))

if __name__ == "__main__":
    unittest.main()
