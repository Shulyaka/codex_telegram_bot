import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot import Bot, CodexRunner, CodexTurnHandle, RunningCodexTurn, telegram_message_conversation_key
from test_bot import build_config, prompt_context


class CapturingExecutor:
    def submit(self, function, *args):
        return MagicMock()


class PrivateGuestIntegrationTest(unittest.TestCase):
    @staticmethod
    def message(sender: int, peer: int, number: int) -> dict:
        return {
            "message_id": number,
            "date": int(time.time()),
            "guest_query_id": f"guest-{number}",
            "from": {"id": sender, "first_name": f"User {sender}"},
            "chat": {"id": peer, "type": "private"},
            "text": f"Question {number}",
        }

    def test_both_participants_steer_one_turn_and_answer_the_latest_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = object.__new__(Bot)
            bot.config = build_config(Path(directory))
            bot.bot_user = {"id": 77}
            bot.controllers = {}
            bot.controllers_lock = threading.Lock()
            bot.update_executor = CapturingExecutor()
            bot.turn_executor = CapturingExecutor()
            bot.attachments = MagicMock()
            bot.attachments.prepare_message.return_value = (None, False)
            bot.download_images = MagicMock(return_value=[])
            bot.download_files = MagicMock(return_value=[])
            bot.telegram = MagicMock()
            bot.telegram.answer_guest_query.return_value = {"html": "<p>Shared answer</p>"}
            bot.codex = MagicMock()
            bot.codex.previous_response_error.return_value = None
            bot.codex.take_queued_guest_stickers.return_value = ()
            bot.codex.take_queued_guest_files.return_value = ()
            bot.answer_guest_response = MagicMock(wraps=bot.answer_guest_response)
            first = self.message(1001, 1002, 10)
            second = self.message(1002, 1001, 11)
            key = "guest:private:1001:1002"
            running = RunningCodexTurn(key, "shared-session", CodexTurnHandle("shared-session", "turn-one", queue.Queue()))
            bot.codex.begin_turn.return_value = running

            bot.dispatch_update({"update_id": 1, "guest_message": first})
            controller = bot.controllers[key]
            self.addCleanup(controller.shutdown)
            controller._drain()
            bot.dispatch_update({"update_id": 2, "guest_message": second})
            controller._drain()

            self.assertEqual(list(bot.controllers), [key])
            bot.codex.begin_turn.assert_called_once()
            bot.codex.steer_turn.assert_called_once()
            self.assertEqual(bot.codex.begin_turn.call_args.args[0], key)
            self.assertIs(bot.codex.steer_turn.call_args.args[0], running)
            first_context = prompt_context(bot.codex.begin_turn.call_args.args[1](None))
            second_context = prompt_context(bot.codex.steer_turn.call_args.args[1](None))
            self.assertEqual((first_context["asking_user"]["id"], first_context["chat"]["id"]), (1001, 1002))
            self.assertEqual((second_context["asking_user"]["id"], second_context["chat"]["id"]), (1002, 1001))

            controller._process_turn_completed(running, "Shared answer", None)
            self.assertEqual(bot.answer_guest_response.call_args.args[0], "guest-11")
            self.assertEqual(bot.answer_guest_response.call_args.args[2]["chat"]["id"], 1001)
            bot.telegram.answer_guest_query.assert_called_once_with("guest-11", "Shared answer")

    def test_reverse_sender_resumes_the_same_session_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            first_key = telegram_message_conversation_key("guest_message", self.message(1001, 1002, 10))
            runner = CodexRunner(config)
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.git_puller = MagicMock()
            runner.client.start_thread.return_value = "shared-session"
            runner.client.start_turn.return_value = CodexTurnHandle("shared-session", "turn-one", queue.Queue())
            runner.client.wait_turn.return_value = "First answer"
            self.assertEqual(runner.wait_turn(runner.begin_turn(first_key, "First question")), "First answer")

            restarted = CodexRunner(config)
            restarted.client = MagicMock()
            restarted.client.generation = 1
            restarted.git_puller = MagicMock()
            restarted.client.start_turn.return_value = CodexTurnHandle("shared-session", "turn-two", queue.Queue())
            reverse_key = telegram_message_conversation_key("guest_message", self.message(1002, 1001, 11))
            running = restarted.begin_turn(reverse_key, "Second question")

            self.assertEqual(running.session_id, "shared-session")
            restarted.client.start_thread.assert_not_called()
            restarted.client.resume_thread.assert_called_once_with("shared-session")

    def test_old_one_user_histories_are_preserved_without_becoming_pair_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = CodexRunner(build_config(Path(directory)))
            runner.sessions.set("guest:1001", "old-first-session")
            runner.sessions.set("guest:1002", "old-second-session")
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.git_puller = MagicMock()
            runner.client.start_thread.return_value = "new-pair-session"
            runner.client.start_turn.return_value = CodexTurnHandle("new-pair-session", "turn-one", queue.Queue())
            key = telegram_message_conversation_key("guest_message", self.message(1001, 1002, 10))

            running = runner.begin_turn(key, "A shared question")

            self.assertEqual(running.session_id, "new-pair-session")
            runner.client.resume_thread.assert_not_called()
            self.assertEqual(runner.sessions.get("guest:1001"), "old-first-session")
            self.assertEqual(runner.sessions.get("guest:1002"), "old-second-session")
            self.assertEqual(runner.sessions.get(key), "new-pair-session")


if __name__ == "__main__":
    unittest.main()
