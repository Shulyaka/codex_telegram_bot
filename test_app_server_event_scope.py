import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import CodexAppServerClient, CodexRunner
from test_bot import build_config


class AppServerEventScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notifications = MagicMock()
        self.client = CodexAppServerClient(
            build_config(Path("/repo")),
            notification_handler=self.notifications,
            server_request_handler=MagicMock(),
            thread_is_owned=lambda thread_id: thread_id == "owned-thread",
        )
        self.client._write_started = MagicMock()

    @staticmethod
    def completed_message(thread_id: str, turn_id: str = "turn-one") -> dict:
        return {
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {"type": "agentMessage", "phase": "final_answer", "text": "A thread's private response"},
            },
        }

    def test_foreign_progress_is_filtered_before_summaries_logs_and_state(self) -> None:
        events = [
            self.completed_message("foreign-thread", "foreign-turn"),
            {"method": "item/agentMessage/delta", "params": {"threadId": "foreign-thread", "delta": "Private partial response"}},
            {"method": "item/reasoning/summaryTextDelta", "params": {"threadId": "foreign-thread", "delta": "Private summary"}},
            {"method": "item/started", "params": {"threadId": "foreign-thread", "item": {"type": "commandExecution", "command": "private-command"}}},
        ]
        with patch("bot.summarize_app_server_event") as summarize, patch("bot.logger") as logger:
            for event in events:
                self.client._route_message(event)
        summarize.assert_not_called()
        self.assertEqual(logger.mock_calls, [])
        self.notifications.assert_not_called()
        self.assertEqual(self.client.non_commentary_agent_message_turns, set())

    def test_persisted_owned_thread_logs_without_an_active_queue(self) -> None:
        event = self.completed_message("owned-thread")
        with self.assertLogs("bot", level="INFO") as logs:
            self.client._route_message(event)
        self.assertTrue(any("owned-thread" in line and "private response" in line for line in logs.output))
        self.notifications.assert_called_once_with(event)
        self.assertEqual(self.client.non_commentary_agent_message_turns, {"turn-one"})

    def test_active_maintenance_queue_is_owned_without_a_persisted_mapping(self) -> None:
        event_queue = queue.Queue()
        self.client.notification_queues["maintenance-thread"] = {event_queue}
        event = self.completed_message("maintenance-thread")
        with self.assertLogs("bot", level="INFO"):
            self.client._route_message(event)
        self.assertIs(event_queue.get_nowait(), event)
        self.notifications.assert_called_once_with(event)

    def test_nested_thread_started_identity_is_scoped(self) -> None:
        owned = {"method": "thread/started", "params": {"thread": {"id": "owned-thread"}}}
        foreign = {"method": "thread/started", "params": {"thread": {"id": "foreign-thread"}}}
        with self.assertLogs("bot", level="INFO") as logs:
            self.client._route_message(foreign)
            self.client._route_message(owned)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("codex progress for thread owned-thread", logs.output[0])
        self.assertNotIn("foreign-thread", logs.output[0])
        self.notifications.assert_called_once_with(owned)

    def test_unidentified_thread_content_is_not_logged_or_forwarded(self) -> None:
        events = [
            {"method": "item/completed", "params": {"turnId": "foreign-turn", "item": {"type": "agentMessage", "text": "Private response"}}},
            {"method": "turn/completed", "params": {"turn": {"id": "foreign-turn", "status": "completed"}}},
            {"method": "thread/started", "params": {"thread": {"id": None}}},
            {"method": "item/reasoning/summaryTextDelta", "params": {"delta": "Private summary"}},
        ]
        with patch("bot.logger") as logger:
            for event in events:
                self.client._route_message(event)
        self.assertEqual(logger.mock_calls, [])
        self.notifications.assert_not_called()
        self.assertEqual(self.client.non_commentary_agent_message_turns, set())

    def test_global_warnings_and_errors_remain_visible(self) -> None:
        events = [{"method": method, "params": {"message": "Global diagnostic"}} for method in ("warning", "configWarning", "error")]
        with self.assertLogs("bot", level="INFO") as logs:
            for event in events:
                self.client._route_message(event)
        self.assertEqual(len(logs.output), 3)
        self.assertTrue(all("Global diagnostic" in line for line in logs.output))
        self.assertEqual(self.notifications.call_count, 3)

    def test_foreign_dynamic_calls_are_not_acknowledged_or_executed(self) -> None:
        request = {"id": 17, "method": "item/tool/call", "params": {"threadId": "foreign-thread", "turnId": "foreign-turn", "tool": "send_sticker", "arguments": {"sticker": "happy"}}}
        with patch("bot.threading.Thread") as thread, patch("bot.logger") as logger:
            self.client._route_message(request)
        thread.assert_not_called()
        self.client._write_started.assert_not_called()
        self.client.server_request_handler.assert_not_called()
        self.assertEqual(logger.mock_calls, [])
        self.assertEqual(self.client.successful_dynamic_tool_turns, set())

    def test_foreign_user_input_and_other_thread_requests_are_not_rejected(self) -> None:
        requests = [
            {"id": 17, "method": "item/tool/requestUserInput", "params": {"threadId": "foreign-thread"}},
            {"id": 18, "method": "item/commandExecution/requestApproval", "params": {"threadId": "foreign-thread"}},
            {"id": 19, "method": "item/tool/requestUserInput", "params": {"thread": {"id": "foreign-thread"}}},
        ]
        with patch("bot.logger") as logger:
            for request in requests:
                self.client._route_message(request)
        self.client._write_started.assert_not_called()
        self.assertEqual(logger.mock_calls, [])

    def test_unidentified_thread_requests_are_left_for_the_rightful_client(self) -> None:
        with patch("bot.threading.Thread") as thread:
            self.client._route_message({"id": 17, "method": "item/tool/call", "params": {"tool": "send_sticker"}})
            self.client._route_message({"id": 18, "method": "item/tool/requestUserInput", "params": {}})
        thread.assert_not_called()
        self.client._write_started.assert_not_called()

    def test_owned_dynamic_call_still_reaches_background_execution(self) -> None:
        request = {"id": 17, "method": "item/tool/call", "params": {"threadId": "owned-thread", "tool": "send_sticker"}}
        with patch("bot.threading.Thread") as thread:
            self.client._route_message(request)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["target"], self.client._handle_server_request)
        self.assertEqual(thread.call_args.kwargs["args"], (17, request["params"]))
        thread.return_value.start.assert_called_once_with()

    def test_owned_user_input_request_retains_telegram_rejection(self) -> None:
        with self.assertLogs("bot", level="INFO"):
            self.client._route_message({"id": 17, "method": "item/tool/requestUserInput", "params": {"threadId": "owned-thread"}})
        result = self.client._write_started.call_args.args[0]
        self.assertEqual(result["id"], 17)
        self.assertIn("Interactive user input is unavailable", result["error"]["message"])

    def test_background_tool_execution_rechecks_ownership(self) -> None:
        self.client.thread_is_owned = lambda thread_id: False
        with patch("bot.logger") as logger:
            self.client._handle_server_request(
                17,
                {"threadId": "owned-thread", "tool": "send_sticker", "turnId": "turn-one"},
            )
        self.client._write_started.assert_not_called()
        self.client.server_request_handler.assert_not_called()
        self.assertEqual(self.client.successful_dynamic_tool_turns, set())
        self.assertEqual(logger.mock_calls, [])

    def test_rpc_responses_route_before_thread_ownership_checks(self) -> None:
        response_queue = queue.Queue()
        self.client.pending_requests[17] = response_queue
        response = {"id": 17, "result": {"thread": {"id": "newly-created-thread"}}}
        self.client._route_message(response)
        self.assertIs(response_queue.get_nowait(), response)

    def test_runner_ownership_preserves_external_archive_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = CodexRunner(build_config(Path(directory)))
            runner.sessions.set(-1001, "owned-thread")
            with self.assertLogs("bot", level="INFO") as logs:
                runner.client._route_message({"method": "thread/archived", "params": {"threadId": "foreign-thread"}})
                self.assertEqual(runner.sessions.get(-1001), "owned-thread")
                runner.client._route_message({"method": "thread/archived", "params": {"threadId": "owned-thread"}})
            self.assertIsNone(runner.sessions.get(-1001))
            self.assertFalse(any("foreign-thread" in line for line in logs.output))
            self.assertTrue(any("removed Telegram session mapping" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
