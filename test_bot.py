import asyncio
import json
import logging
import os
import queue
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
from telegram import Update
from telegram.error import BadRequest, NetworkError, RetryAfter

from bot import (  # noqa: E402
    ATTACHMENT_CACHE_DIRECTORY,
    ATTACHMENT_FREE_MESSAGE_RETENTION,
    DEFAULT_ATTACHMENT_RETENTION_SECONDS,
    GIT_PULL_WARNING_MESSAGE,
    GUEST_DEADLINE_RESPONSE_ERROR,
    GUEST_RESPONSE_WARNING_MESSAGE,
    TELEGRAM_CLEAR_REACTION_TOOL_NAME,
    TELEGRAM_REACTION_EMOJIS,
    TELEGRAM_REACTION_TOOL_NAME,
    TELEGRAM_STICKER_TOOL_NAME,
    AsyncGitPuller,
    AttachmentStore,
    Bot,
    CodexAgentMessage,
    CodexGeneratedImage,
    CodexAppServerError,
    CodexAppServerClient,
    CodexRunner,
    CodexTurnHandle,
    CodexTurnInactiveError,
    Config,
    ConversationController,
    GitPullError,
    GuestResponseDelivery,
    QueuedTelegramSticker,
    RunningCodexTurn,
    SessionRecord,
    SessionStore,
    TelegramClient,
    TelegramFile,
    TelegramImage,
    TELEGRAM_MEDIA_PLACEHOLDER_PREFIX,
    TELEGRAM_PHOTO_URI_PREFIX,
    build_codex_prompt,
    collect_message_files,
    collect_message_images,
    configure_logging,
    conversation_chat_id,
    conversation_message_thread_id,
    configured_telegram_stickers,
    configured_writable_roots,
    guest_conversation_key,
    guest_response_warning_delay,
    git_repository_root,
    is_unavailable_thread_error,
    parse_chat_ids,
    prepend_queued_sticker_images,
    prepare_telegram_rich_parts,
    pull_configured_git_repositories,
    pull_current_git_branch,
    safe_log_text,
    summarize_app_server_event,
    telegram_conversation_key,
    telegram_clear_reaction_tool_spec,
    telegram_dynamic_tool_specs,
    telegram_reaction_tool_spec,
    telegram_sticker_tool_spec,
    telegram_thread_name,
    telegram_uploaded_photo_file_id,
)


class ConfigurationTest(unittest.TestCase):
    def test_http_transport_request_logs_are_suppressed(self) -> None:
        httpx_logger = MagicMock()
        httpcore_logger = MagicMock()

        with (
            patch("bot.logging.basicConfig") as basic_config,
            patch(
                "bot.logging.getLogger",
                side_effect=[httpx_logger, httpcore_logger],
            ) as get_logger,
        ):
            configure_logging()

        basic_config.assert_called_once()
        self.assertEqual(
            get_logger.call_args_list,
            [call("httpx"), call("httpcore")],
        )
        httpx_logger.setLevel.assert_called_once_with(logging.WARNING)
        httpcore_logger.setLevel.assert_called_once_with(logging.WARNING)

    def test_parse_multiple_chat_ids(self) -> None:
        self.assertEqual(parse_chat_ids("TEST_IDS", "-1001, -1002"), frozenset({-1001, -1002}))

    def test_repository_uses_process_working_directory_unless_overridden(self) -> None:
        previous_dir = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            working_dir = root / "working"
            override_dir = root / "configured"
            working_dir.mkdir()
            override_dir.mkdir()
            try:
                os.chdir(working_dir)
                for configured_repo, expected_repo in (
                    (None, working_dir),
                    ("", working_dir),
                    (str(override_dir), override_dir),
                    ("../configured", override_dir),
                ):
                    with self.subTest(configured_repo=configured_repo):
                        environment = {
                            "TELEGRAM_BOT_TOKEN": "test-token",
                            "TELEGRAM_ALLOWED_CHAT_IDS": "-1001",
                            "PWD": str(root / "incorrect-pwd"),
                        }
                        if configured_repo is not None:
                            environment["CODEX_TELEGRAM_REPO_DIR"] = configured_repo
                        with patch.dict(os.environ, environment, clear=True):
                            config = Config.from_env()

                        self.assertEqual(config.repo_dir, expected_repo)
                        self.assertEqual(
                            config.session_store_path,
                            expected_repo / "sessions.json",
                        )
            finally:
                os.chdir(previous_dir)

    def test_instructions_file_is_optional(self) -> None:
        for value in (None, ""):
            with self.subTest(value=value):
                environment = {
                    "TELEGRAM_BOT_TOKEN": "test-token",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-1001",
                }
                if value is not None:
                    environment["CODEX_TELEGRAM_INSTRUCTIONS_FILE"] = value
                with patch.dict(os.environ, environment, clear=True):
                    config = Config.from_env()

                self.assertIsNone(config.instructions_file)

    def test_instructions_file_resolves_relative_absolute_and_home_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            repo_dir = root / "repo"
            environment = {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_ALLOWED_CHAT_IDS": "-1001",
                "CODEX_TELEGRAM_REPO_DIR": str(repo_dir),
            }
            with patch.dict(os.environ, environment, clear=True):
                for value, expected in (
                    ("instructions.md", repo_dir / "instructions.md"),
                    ("../shared/instructions.md", root / "shared" / "instructions.md"),
                    (str(root / "external.md"), root / "external.md"),
                    ("~/telegram-instructions.md", Path.home() / "telegram-instructions.md"),
                ):
                    with self.subTest(value=value):
                        os.environ["CODEX_TELEGRAM_INSTRUCTIONS_FILE"] = value

                        config = Config.from_env()

                        self.assertEqual(config.instructions_file, expected.resolve())

    def test_read_only_defaults_to_true(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_CHAT_IDS": "-1001",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_env()

        self.assertTrue(config.read_only)

    def test_read_only_can_be_configured(self) -> None:
        for value, expected in (
            ("true", True),
            ("1", True),
            ("yes", True),
            ("on", True),
            ("false", False),
            ("0", False),
            ("no", False),
            ("off", False),
            (" FALSE ", False),
        ):
            with self.subTest(value=value):
                environment = {
                    "TELEGRAM_BOT_TOKEN": "test-token",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-1001",
                    "CODEX_TELEGRAM_READ_ONLY": value,
                }
                with patch.dict(os.environ, environment, clear=True):
                    config = Config.from_env()

                self.assertIs(config.read_only, expected)

    def test_read_only_rejects_invalid_value(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_CHAT_IDS": "-1001",
            "CODEX_TELEGRAM_READ_ONLY": "sometimes",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(
                ValueError,
                "CODEX_TELEGRAM_READ_ONLY must be a boolean",
            ):
                Config.from_env()

    def test_legacy_single_chat_variable_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_ALLOWED_CHAT_ID": "-1001",
                "CODEX_TELEGRAM_SESSION_STORE": str(Path(temp_dir) / "sessions.json"),
            }
            with patch.dict(os.environ, environment, clear=True):
                config = Config.from_env()

        self.assertEqual(config.allowed_chat_ids, frozenset({-1001}))
        self.assertEqual(config.guest_response_warning_seconds, 290)
        self.assertEqual(
            config.attachment_retention_seconds,
            DEFAULT_ATTACHMENT_RETENTION_SECONDS,
        )

    def test_guest_warning_must_be_before_telegram_deadline(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_CHAT_ID": "-1001",
            "CODEX_TELEGRAM_GUEST_WARNING_SECONDS": "300",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "must be less than 300"):
                Config.from_env()

    def test_relative_sticker_directory_is_resolved_against_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir).resolve()
            environment = {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_ALLOWED_CHAT_ID": "-1001",
                "CODEX_TELEGRAM_REPO_DIR": str(repo_dir),
                "CODEX_TELEGRAM_STICKER_DIR": "tools/codex_telegram_bot/stickers",
            }
            with patch.dict(os.environ, environment, clear=True):
                config = Config.from_env()

        self.assertEqual(
            config.sticker_dir,
            repo_dir / "tools" / "codex_telegram_bot" / "stickers",
        )

    def test_guest_warning_accounts_for_time_spent_in_update_queue(self) -> None:
        self.assertEqual(
            guest_response_warning_delay(
                {"date": 1_000.0},
                270,
                now=1_030.0,
            ),
            240.0,
        )
        self.assertEqual(
            guest_response_warning_delay(
                {"date": 1_000.0},
                270,
                now=1_300.0,
            ),
            0.0,
        )


class TelegramReplyTest(unittest.TestCase):
    def test_get_me_uses_native_ptb_method(self) -> None:
        client = TelegramClient("test-token")
        client.bot = MagicMock()
        client.bot.get_me = AsyncMock(
            return_value=MagicMock(to_dict=MagicMock(return_value={"id": 99})),
        )
        client._submit = lambda coroutine: asyncio.run(coroutine)

        self.assertEqual(client.get_me(), {"id": 99})

        client.bot.get_me.assert_awaited_once_with()
        client.bot.do_api_request.assert_not_called()

    def test_get_webhook_info_uses_native_ptb_method(self) -> None:
        client = TelegramClient("test-token")
        client.bot = MagicMock()
        client.bot.get_webhook_info = AsyncMock(
            return_value=MagicMock(
                to_dict=MagicMock(return_value={"url": "", "pending_update_count": 0}),
            ),
        )
        client._submit = lambda coroutine: asyncio.run(coroutine)

        self.assertEqual(
            client.get_webhook_info(),
            {"url": "", "pending_update_count": 0},
        )

        client.bot.get_webhook_info.assert_awaited_once_with()
        client.bot.do_api_request.assert_not_called()

    def test_media_placeholder_is_longer_than_final_telegram_uri(self) -> None:
        media_id = "image_0123456789abcdef01234567"

        self.assertGreaterEqual(
            len(f"{TELEGRAM_MEDIA_PLACEHOLDER_PREFIX}{media_id}".encode("utf-8")),
            len(f"{TELEGRAM_PHOTO_URI_PREFIX}{media_id}".encode("utf-8")),
        )

    def test_local_markdown_image_becomes_uploaded_rich_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            image_path = root / "generated.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test image")

            parts = prepare_telegram_rich_parts(
                f"Here it is.\n\n![result]({image_path.as_uri()})",
                (root,),
            )

        self.assertEqual(len(parts), 1)
        rich_message = parts[0].rich_message
        self.assertIn('src="tg://photo?id=image_', rich_message["html"])
        self.assertNotIn("codex-telegram.invalid", rich_message["html"])
        self.assertEqual(len(rich_message["media"]), 1)
        media = rich_message["media"][0]
        self.assertEqual(media["media"]["type"], "photo")
        self.assertEqual(media["media"]["media"], f"attach://{media['id']}")
        self.assertEqual(parts[0].uploads[media["id"]], image_path)

    def test_local_markdown_image_outside_safe_roots_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir).resolve() / "generated.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test image")

            with self.assertRaisesRegex(ValueError, "outside the configured safe roots"):
                prepare_telegram_rich_parts(f"![result]({image_path.as_uri()})")

    def test_missing_local_markdown_image_becomes_a_text_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            missing_image = root / "purged result.png"

            with self.assertLogs("bot", level="WARNING") as logs:
                parts = prepare_telegram_rich_parts(
                    f"Before. ![generated result]({missing_image.as_uri()}) After.",
                    (root,),
                )

        self.assertEqual(len(parts), 1)
        rich_message = parts[0].rich_message
        self.assertIn("[Image unavailable: generated result]", rich_message["html"])
        self.assertNotIn("<img", rich_message["html"])
        self.assertNotIn("media", rich_message)
        self.assertFalse(parts[0].uploads)
        self.assertIn("embedded local image is unavailable", "\n".join(logs.output))

    def test_missing_local_markdown_image_without_alt_uses_generic_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            missing_image = root / "purged.png"

            parts = prepare_telegram_rich_parts(
                f"![]({missing_image.as_uri()})",
                (root,),
            )

        self.assertIn("[Image unavailable]", parts[0].rich_message["html"])
        self.assertFalse(parts[0].uploads)

    def test_generated_image_path_is_allowed_outside_safe_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir).resolve() / "generated.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test image")

            parts = prepare_telegram_rich_parts(
                f"![result]({image_path.as_uri()})",
                generated_image_paths=frozenset({image_path}),
            )

        self.assertEqual(len(parts[0].uploads), 1)

    def test_remote_markdown_image_remains_native_for_regular_messages(self) -> None:
        parts = prepare_telegram_rich_parts("![result](https://example.com/result.png)")

        self.assertIn('src="https://example.com/result.png"', parts[0].rich_message["html"])
        self.assertNotIn("media", parts[0].rich_message)
        self.assertFalse(parts[0].uploads)

    def test_file_image_examples_inside_code_are_not_processed(self) -> None:
        markdown = (
            "`![inline](file:///does/not/exist.png)`\n\n"
            "```markdown\n![fenced](file:///does/not/exist.png)\n```"
        )

        parts = prepare_telegram_rich_parts(markdown)

        self.assertNotIn("media", parts[0].rich_message)
        self.assertFalse(parts[0].uploads)

    def test_typing_action_uses_native_ptb_method(self) -> None:
        client = TelegramClient("test-token")
        client.bot = MagicMock()
        pending_request = object()
        client.bot.send_chat_action.return_value = pending_request
        client._submit = MagicMock()

        client.send_typing_action(-1001, 42)

        client.bot.send_chat_action.assert_called_once_with(
            chat_id=-1001,
            action="typing",
            message_thread_id=42,
        )
        client._submit.assert_called_once_with(pending_request)

    def test_ptb_api_retries_network_error_once(self) -> None:
        client = TelegramClient("test-token")
        operation = AsyncMock(
            side_effect=[NetworkError("temporary failure"), {"message_id": 3001}],
        )

        with patch("bot.asyncio.sleep", new=AsyncMock()) as sleep:
            result = asyncio.run(client._call_ptb_api("sendMessage", operation))

        self.assertEqual(result, {"message_id": 3001})
        self.assertEqual(operation.await_count, 2)
        sleep.assert_awaited_once_with(0.5)

    def test_ptb_api_stops_after_three_network_retries(self) -> None:
        client = TelegramClient("test-token")
        operation = AsyncMock(side_effect=NetworkError("persistent failure"))

        with patch("bot.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(RuntimeError, "persistent failure"):
                asyncio.run(client._call_ptb_api("sendMessage", operation))

        self.assertEqual(operation.await_count, 4)
        self.assertEqual(sleep.await_count, 3)
        sleep.assert_has_awaits([call(0.5), call(0.5), call(0.5)])

    def test_ptb_api_uses_retry_after_delay(self) -> None:
        client = TelegramClient("test-token")
        operation = AsyncMock(
            side_effect=[RetryAfter(7), {"message_id": 3001}],
        )

        with patch("bot.asyncio.sleep", new=AsyncMock()) as sleep:
            result = asyncio.run(client._call_ptb_api("sendMessage", operation))

        self.assertEqual(result, {"message_id": 3001})
        self.assertEqual(operation.await_count, 2)
        sleep.assert_awaited_once_with(7.0)

    def test_ptb_api_does_not_retry_bad_request(self) -> None:
        client = TelegramClient("test-token")
        operation = AsyncMock(side_effect=BadRequest("invalid request"))

        with patch("bot.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(RuntimeError, "invalid request"):
                asyncio.run(client._call_ptb_api("sendMessage", operation))

        self.assertEqual(operation.await_count, 1)
        sleep.assert_not_awaited()

    def test_file_download_retries_transport_error_once(self) -> None:
        client = TelegramClient("test-token")
        request = httpx.Request("GET", "https://example.com/file")
        client._download_file_once = AsyncMock(
            side_effect=[httpx.ConnectError("temporary failure", request=request), None],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "download.part"
            with patch("bot.asyncio.sleep", new=AsyncMock()) as sleep:
                asyncio.run(client._download_file("documents/file.txt", destination))

        self.assertEqual(client._download_file_once.await_count, 2)
        sleep.assert_awaited_once_with(0.5)

    def test_file_download_does_not_retry_non_transient_http_status(self) -> None:
        client = TelegramClient("test-token")
        request = httpx.Request("GET", "https://example.com/file")
        response = httpx.Response(404, request=request)
        client._download_file_once = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "not found",
                request=request,
                response=response,
            ),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "download.part"
            with patch("bot.asyncio.sleep", new=AsyncMock()) as sleep:
                with self.assertRaisesRegex(RuntimeError, "HTTPStatusError"):
                    asyncio.run(client._download_file("documents/file.txt", destination))

        self.assertEqual(client._download_file_once.await_count, 1)
        sleep.assert_not_awaited()

    def test_multipart_api_retries_transport_error_once(self) -> None:
        client = TelegramClient("test-token")
        request = httpx.Request("POST", "https://example.com/sendRichMessage")
        response = httpx.Response(
            200,
            request=request,
            json={"ok": True, "result": {"message_id": 3001}},
        )
        client._post_multipart_api = AsyncMock(
            side_effect=[httpx.ReadTimeout("temporary failure", request=request), response],
        )

        with patch("bot.asyncio.sleep", new=AsyncMock()) as sleep:
            result = asyncio.run(
                client._call_multipart_api(
                    "sendRichMessage",
                    {"chat_id": -1001},
                    {"image_1": Path("generated.png")},
                ),
            )

        self.assertEqual(result["result"]["message_id"], 3001)
        self.assertEqual(client._post_multipart_api.await_count, 2)
        sleep.assert_awaited_once_with(0.5)

    def test_multipart_api_does_not_retry_bad_request(self) -> None:
        client = TelegramClient("test-token")
        request = httpx.Request("POST", "https://example.com/sendRichMessage")
        response = httpx.Response(
            400,
            request=request,
            json={"ok": False, "description": "invalid rich message"},
        )
        client._post_multipart_api = AsyncMock(return_value=response)

        with patch("bot.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(RuntimeError, "invalid rich message"):
                asyncio.run(
                    client._call_multipart_api(
                        "sendRichMessage",
                        {"chat_id": -1001},
                        {"image_1": Path("generated.png")},
                    ),
                )

        self.assertEqual(client._post_multipart_api.await_count, 1)
        sleep.assert_not_awaited()

    def test_reaction_uses_set_message_reaction_for_the_selected_message(self) -> None:
        client = TelegramClient("test-token")
        client._api = MagicMock(return_value={"result": True})

        client.set_message_reaction(-1001, 2397, "👍")

        client._api.assert_called_once_with(
            "setMessageReaction",
            {
                "chat_id": -1001,
                "message_id": 2397,
                "reaction": [{"type": "emoji", "emoji": "👍"}],
            },
        )

    def test_clear_reaction_sends_an_empty_reaction_list(self) -> None:
        client = TelegramClient("test-token")
        client._api = MagicMock(return_value={"result": True})

        client.clear_message_reaction(-1001, 2397)

        client._api.assert_called_once_with(
            "setMessageReaction",
            {
                "chat_id": -1001,
                "message_id": 2397,
                "reaction": [],
            },
        )

    def test_sticker_is_uploaded_with_send_sticker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = TelegramClient("test-token")
            sticker_path = Path(temp_dir) / "happy.webp"
            sticker_path.write_bytes(b"webp")
            client._api_multipart = MagicMock(
                return_value={
                    "result": {
                        "message_id": 3001,
                        "sticker": {"file_id": "sticker-file-id"},
                    },
                },
            )
            client._api = MagicMock(
                return_value={
                    "result": {
                        "message_id": 3002,
                        "sticker": {"file_id": "sticker-file-id"},
                    },
                },
            )

            self.assertEqual(client.send_sticker(-1001, sticker_path), 3001)
            self.assertEqual(client.send_sticker(-1001, sticker_path), 3002)

        client._api_multipart.assert_called_once_with(
            "sendSticker",
            {
                "chat_id": -1001,
                "sticker": "attach://sticker",
            },
            {"sticker": sticker_path},
        )
        client._api.assert_called_once_with(
            "sendSticker",
            {"chat_id": -1001, "sticker": "sticker-file-id"},
        )

    def test_sticker_can_be_sent_to_a_forum_topic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = TelegramClient("test-token")
            sticker_path = Path(temp_dir) / "happy.webp"
            sticker_path.write_bytes(b"webp")
            client._api_multipart = MagicMock(
                return_value={
                    "result": {
                        "message_id": 3001,
                        "sticker": {"file_id": "sticker-file-id"},
                    },
                },
            )

            client.send_sticker(-1001, sticker_path, 42)

        client._api_multipart.assert_called_once_with(
            "sendSticker",
            {
                "chat_id": -1001,
                "message_thread_id": 42,
                "sticker": "attach://sticker",
            },
            {"sticker": sticker_path},
        )

    def test_guest_sticker_is_uploaded_once_cached_and_temporary_message_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = TelegramClient("test-token", media_upload_chat_id=-10099)
            sticker_path = Path(temp_dir) / "happy.webp"
            sticker_path.write_bytes(b"webp")
            client._api_multipart = MagicMock(
                return_value={
                    "result": {
                        "message_id": 9001,
                        "sticker": {"file_id": "sticker-file-id"},
                    },
                },
            )
            client._api = MagicMock(return_value={"result": True})
            client.delete_guest_upload_messages = MagicMock()

            client.answer_guest_query_with_sticker("guest-one", sticker_path)
            client.answer_guest_query_with_sticker("guest-two", sticker_path)

        client._api_multipart.assert_called_once_with(
            "sendSticker",
            {
                "chat_id": -10099,
                "sticker": "attach://sticker",
            },
            {"sticker": sticker_path},
        )
        self.assertEqual(client._api.call_count, 2)
        for api_call, guest_query_id in zip(
            client._api.call_args_list,
            ("guest-one", "guest-two"),
        ):
            self.assertEqual(api_call.args[0], "answerGuestQuery")
            payload = api_call.args[1]
            self.assertEqual(payload["guest_query_id"], guest_query_id)
            self.assertEqual(payload["result"]["type"], "sticker")
            self.assertEqual(payload["result"]["sticker_file_id"], "sticker-file-id")
        client.delete_guest_upload_messages.assert_called_once_with([9001])

    def test_async_telegram_transport_preserves_rich_message_payloads(self) -> None:
        client = TelegramClient("test-token")
        raw_update = {
            "update_id": 1,
            "message": {
                "message_id": 2,
                "date": 1,
                "chat": {"id": -1001, "type": "supergroup"},
                "rich_message": {
                    "blocks": [{"type": "paragraph", "text": "formatted"}],
                },
            },
        }

        converted = Update.de_json(raw_update, client.bot).to_dict()

        self.assertEqual(
            converted["message"]["rich_message"],
            raw_update["message"]["rich_message"],
        )

    def test_long_poll_does_not_block_other_telegram_requests(self) -> None:
        poll_started = threading.Event()
        finish_poll = threading.Event()
        get_updates_kwargs: dict[str, object] = {}

        class FakeAsyncBot:
            async def initialize(self) -> None:
                pass

            async def shutdown(self) -> None:
                pass

            async def get_updates(self, **kwargs: object) -> tuple[object, ...]:
                get_updates_kwargs.update(kwargs)
                poll_started.set()
                while not finish_poll.is_set():
                    await asyncio.sleep(0.01)
                return ()

            async def get_me(self) -> dict[str, object]:
                return {"id": 99, "username": "bot"}

            async def do_api_request(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"id": 99, "username": "bot"}

        client = TelegramClient("test-token")
        client.bot = FakeAsyncBot()
        poll_thread = threading.Thread(target=client.get_updates, args=(None, 30))
        try:
            poll_thread.start()
            self.assertTrue(poll_started.wait(1))
            self.assertEqual(client.get_me()["id"], 99)
        finally:
            finish_poll.set()
            poll_thread.join(timeout=1)
            client.close()

        self.assertEqual(
            get_updates_kwargs["allowed_updates"],
            ["message", "guest_message", "message_reaction"],
        )

        self.assertFalse(poll_thread.is_alive())

    def test_empty_guest_rich_conversion_is_reported_to_delivery_fallback(self) -> None:
        client = TelegramClient("test-token")
        client.answer_guest_query_with_plain_text = MagicMock()

        with patch("bot.telegramify_rich", return_value=[]):
            with self.assertRaisesRegex(ValueError, "conversion returned no items"):
                client.answer_guest_query("guest-query", "answer")

        client.answer_guest_query_with_plain_text.assert_not_called()

    def test_only_first_rich_item_replies_to_original_message(self) -> None:
        client = TelegramClient("test-token")
        rich_items = [{"text": "first"}, {"text": "second"}]

        with (
            patch("bot.telegramify_rich", return_value=rich_items),
            patch.object(
                client,
                "_api",
                side_effect=[
                    {"result": {"message_id": 3001}},
                    {"result": {"message_id": 3002}},
                ],
            ) as api,
        ):
            sent_message_id = client.send_rich_message(-1001, "response", 2397, 42)

        payloads = [call.args[1] for call in api.call_args_list]
        self.assertEqual(payloads[0]["reply_parameters"], {"message_id": 2397})
        self.assertNotIn("reply_parameters", payloads[1])
        self.assertEqual([payload["message_thread_id"] for payload in payloads], [42, 42])
        self.assertEqual(sent_message_id, 3002)

    def test_local_image_part_uses_multipart_delivery(self) -> None:
        client = TelegramClient("test-token", allowed_media_roots=(Path("/repo"),))
        part = MagicMock()
        part.rich_message = {"html": '<p><img src="tg://photo?id=image_1"/></p>'}
        part.uploads = {"image_1": Path("/repo/generated.png")}
        client._api_multipart = MagicMock(return_value={"result": {"message_id": 3001}})

        with patch("bot.prepare_telegram_rich_parts", return_value=[part]):
            sent_message_id = client.send_rich_message(-1001, "response", 2397, None)

        self.assertEqual(sent_message_id, 3001)
        client._api_multipart.assert_called_once_with(
            "sendRichMessage",
            {
                "chat_id": -1001,
                "rich_message": part.rich_message,
                "reply_parameters": {"message_id": 2397},
            },
            part.uploads,
        )

    def test_queued_sticker_can_be_prepared_as_an_allowed_rich_photo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sticker_path = Path(temp_dir) / "happy.webp"
            sticker_path.write_bytes(b"RIFF\x04\x00\x00\x00WEBP")
            sticker = QueuedTelegramSticker("happy", sticker_path, 0)
            markdown = prepend_queued_sticker_images("Final text.", (sticker,))

            parts = prepare_telegram_rich_parts(
                markdown,
                generated_image_paths=frozenset({sticker_path.resolve()}),
            )

        self.assertEqual(len(parts), 1)
        self.assertEqual(list(parts[0].uploads.values()), [sticker_path.resolve()])

    def test_guest_image_is_preuploaded_and_referenced_by_file_id(self) -> None:
        client = TelegramClient("test-token", media_upload_chat_id=-10099)
        client.bot = MagicMock()
        client.bot.send_photo = AsyncMock(
            return_value={
                "message_id": 9001,
                "photo": [{"file_id": "small"}, {"file_id": "largest"}],
            },
        )
        client._submit = lambda coroutine: asyncio.run(coroutine)
        client._api = MagicMock(return_value={"result": True})

        client.answer_guest_query(
            "guest-query",
            "![result](https://example.com/result.png)",
        )

        client.bot.send_photo.assert_called_once_with(
            chat_id=-10099,
            photo="https://example.com/result.png",
        )
        answer_call = next(
            api_call
            for api_call in client._api.call_args_list
            if api_call.args[0] == "answerGuestQuery"
        )
        result = answer_call.args[1]["result"]
        rich_message = result["input_message_content"]["rich_message"]
        self.assertIn('src="tg://photo?id=image_', rich_message["html"])
        self.assertEqual(rich_message["media"][0]["media"]["media"], "largest")
        self.assertEqual(
            client._api.call_args_list[-1],
            call(
                "deleteMessages",
                {"chat_id": -10099, "message_ids": [9001]},
            ),
        )

    def test_guest_upload_is_deleted_when_guest_response_fails(self) -> None:
        client = TelegramClient("test-token", media_upload_chat_id=-10099)
        client.bot = MagicMock()
        client.bot.send_photo = AsyncMock(
            return_value={"message_id": 9001, "photo": [{"file_id": "photo-id"}]},
        )
        client._submit = lambda coroutine: asyncio.run(coroutine)
        client._api = MagicMock(
            side_effect=[RuntimeError("guest response rejected"), {"result": True}],
        )

        with self.assertRaisesRegex(RuntimeError, "guest response rejected"):
            client.answer_guest_query(
                "guest-query",
                "![result](https://example.com/result.png)",
            )

        self.assertEqual(
            client._api.call_args_list[-1],
            call(
                "deleteMessages",
                {"chat_id": -10099, "message_ids": [9001]},
            ),
        )

    def test_guest_upload_cleanup_failure_does_not_mask_successful_response(self) -> None:
        client = TelegramClient("test-token", media_upload_chat_id=-10099)
        client.bot = MagicMock()
        client.bot.send_photo = AsyncMock(
            return_value={"message_id": 9001, "photo": [{"file_id": "photo-id"}]},
        )
        client._submit = lambda coroutine: asyncio.run(coroutine)
        client._api = MagicMock(
            side_effect=[{"result": True}, RuntimeError("cleanup rejected")],
        )

        with self.assertLogs("bot", level="WARNING") as logs:
            client.answer_guest_query(
                "guest-query",
                "![result](https://example.com/result.png)",
            )

        self.assertIn("failed to delete 1 guest media upload message", "\n".join(logs.output))

    def test_uploaded_photo_file_id_uses_largest_photo_size(self) -> None:
        self.assertEqual(
            telegram_uploaded_photo_file_id(
                {"photo": [{"file_id": "small"}, {"file_id": "largest"}]},
            ),
            "largest",
        )

    def test_only_first_plain_chunk_replies_to_original_message(self) -> None:
        client = TelegramClient("test-token")

        with patch.object(
            client,
            "_api",
            side_effect=[
                {"result": {"message_id": 3001}},
                {"result": {"message_id": 3002}},
            ],
        ) as api:
            sent_message_id = client.send_plain_message(-1001, "x" * 5000, 2397, None)

        payloads = [call.args[1] for call in api.call_args_list]
        self.assertEqual(payloads[0]["reply_parameters"], {"message_id": 2397})
        self.assertNotIn("reply_parameters", payloads[1])
        self.assertEqual(sent_message_id, 3002)

    def test_rich_delivery_failure_sends_error_instead_of_unformatted_answer(self) -> None:
        bot = object.__new__(Bot)
        bot.telegram = MagicMock()
        bot.telegram.send_rich_message.side_effect = RuntimeError("invalid rich block")
        bot.telegram.send_plain_message.return_value = 3001

        with self.assertLogs("bot", level="INFO"):
            sent_message_id = bot.send_message_response(
                -1001,
                "Original formatted answer",
                2397,
                None,
                "response",
                True,
            )

        fallback_text = bot.telegram.send_plain_message.call_args.args[1]
        self.assertEqual(sent_message_id, 3001)
        self.assertEqual(fallback_text, "invalid rich block")

    def test_session_store_defaults_to_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir).resolve()
            environment = {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_ALLOWED_CHAT_IDS": "-1001,-1002",
                "CODEX_TELEGRAM_REPO_DIR": str(repo_dir),
            }
            with patch.dict(os.environ, environment, clear=True):
                config = Config.from_env()

        self.assertEqual(config.session_store_path, repo_dir / "sessions.json")

    def test_shared_app_server_socket_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "shared-app-server.sock"
            environment = {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_ALLOWED_CHAT_IDS": "-1001",
                "CODEX_TELEGRAM_APP_SERVER_SOCKET": str(socket_path),
            }
            with patch.dict(os.environ, environment, clear=True):
                config = Config.from_env()

        self.assertEqual(config.app_server_socket_path, socket_path.resolve())

    def test_guest_media_upload_chat_can_be_configured(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_CHAT_ID": "-1001",
            "CODEX_TELEGRAM_MEDIA_UPLOAD_CHAT_ID": "-1009876543210",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_env()

        self.assertEqual(config.media_upload_chat_id, -1009876543210)


class CodexPromptTest(unittest.TestCase):
    def test_current_user_text_only_appears_in_user_message(self) -> None:
        user_text = "Unique request text for prompt deduplication"
        message = {
            "message_id": 42,
            "date": 1785000000,
            "text": user_text,
            "chat": {"id": -1001, "type": "supergroup"},
            "from": {"id": 1001, "first_name": "Test"},
        }

        prompt = build_codex_prompt("message", message, {})
        context = prompt_context(prompt)

        self.assertEqual(len(prompt), 2)
        self.assertTrue(prompt[0].startswith("<telegram_context>\n"))
        self.assertTrue(prompt[0].endswith("\n</telegram_context>\n"))
        self.assertEqual(prompt[1], user_text)
        self.assertNotIn("<user_message>", prompt_text(prompt))
        self.assertNotIn("</user_message>", prompt_text(prompt))
        self.assertNotIn("text", context["current_message"])
        self.assertNotIn("raw_payload", context["current_message"])
        self.assertNotIn("attached_images", context)
        self.assertNotIn("attached_files", context)
        self.assertEqual(context["attachments"], [])
        self.assertEqual(sum(part.count(user_text) for part in prompt), 1)

    def test_reaction_is_user_text_without_current_message_or_quotation(self) -> None:
        message = {
            "message_id": 3001,
            "date": 1785000000,
            "text": "👍",
            "chat": {"id": -1001, "type": "supergroup", "title": "Test"},
            "from": {"id": 1001, "first_name": "Reactor"},
            "reply_to_message": {
                "message_id": 3001,
                "chat": {"id": -1001, "type": "supergroup", "title": "Test"},
                "from": {"id": 99, "is_bot": True, "first_name": "Codex"},
            },
        }

        prompt = build_codex_prompt(
            "message_reaction",
            message,
            {"id": 99, "is_bot": True, "first_name": "Codex"},
        )
        context = prompt_context(prompt)

        self.assertEqual(context["message_kind"], "message_reaction")
        self.assertNotIn("current_message", context)
        self.assertEqual(context["asking_user"]["display_name"], "Reactor")
        self.assertEqual(context["reply_to_message"]["message_id"], 3001)
        self.assertNotIn("raw_payload", context["reply_to_message"])
        self.assertEqual(prompt_user_message(prompt), "👍")
        self.assertNotIn(">", prompt_user_message(prompt))

    def test_previous_response_error_is_added_to_telegram_context(self) -> None:
        prompt = build_codex_prompt(
            "guest_message",
            {
                "message_id": 43,
                "text": "ping",
                "chat": {"id": -1001, "type": "supergroup"},
                "from": {"id": 1001, "first_name": "Test"},
            },
            {},
            previous_response_error=(
                "Telegram answerGuestQuery failed: query is too old"
            ),
        )

        self.assertEqual(
            prompt_context(prompt)["previous_response_error"],
            "Telegram answerGuestQuery failed: query is too old",
        )
        self.assertEqual(prompt_user_message(prompt), "ping")

    def test_current_caption_only_appears_in_user_message(self) -> None:
        caption = "Unique caption request"
        message = {
            "message_id": 43,
            "date": 1785000001,
            "caption": caption,
            "caption_entities": [],
            "document": {
                "file_id": "document-id",
                "file_name": "report.txt",
                "mime_type": "text/plain",
            },
            "chat": {"id": -1001, "type": "supergroup"},
            "from": {"id": 1001, "first_name": "Test"},
        }

        prompt = build_codex_prompt("message", message, {})
        context = prompt_context(prompt)

        self.assertNotIn("caption", context["current_message"])
        self.assertEqual(prompt_user_message(prompt), caption)
        self.assertEqual(sum(part.count(caption) for part in prompt), 1)

    def test_rich_reply_is_quoted_and_removed_from_raw_payload(self) -> None:
        message = {
            "message_id": 43,
            "text": "Follow up",
            "chat": {"id": -1001, "type": "supergroup"},
            "reply_to_message": {
                "message_id": 42,
                "rich_message": {"blocks": [{"type": "heading", "text": "Answer"}]},
                "from": {"id": 99, "is_bot": True},
            },
        }

        with patch("bot.rich_message_to_markdown", return_value="# Answer"):
            prompt = build_codex_prompt("message", message, {"id": 99})

        context = prompt_context(prompt)
        self.assertEqual(
            prompt_user_message(prompt),
            "> # Answer\n\nFollow up",
        )
        self.assertNotIn("raw_payload", context["reply_to_message"])
        self.assertNotIn("note", context["reply_to_message"])

    def test_user_rich_reply_is_not_repeated_as_raw_json(self) -> None:
        replied_text = (
            "What if I send you a picture or other media using rich message, "
            "would you get it?"
        )
        message = {
            "message_id": 239,
            "text": "What about now?",
            "chat": {"id": 114122164, "type": "private"},
            "reply_to_message": {
                "message_id": 236,
                "rich_message": {
                    "blocks": [
                        {"type": "paragraph", "text": replied_text},
                        {
                            "type": "photo",
                            "photo": [{"file_id": "photo-id", "width": 752, "height": 255}],
                            "caption": {"text": "terminal"},
                        },
                    ],
                },
                "from": {"id": 114122164, "first_name": "Denis"},
            },
        }

        prompt = build_codex_prompt("message", message, {})

        self.assertEqual(
            prompt_user_message(prompt),
            (
                f"> {replied_text}\n>\n> **Photo** (752×255)\n>\n> terminal"
                "\n\nWhat about now?"
            ),
        )
        self.assertEqual(prompt_user_message(prompt).count(replied_text), 1)
        self.assertNotIn('> {"rich', prompt_user_message(prompt))
        self.assertNotIn(
            "raw_payload",
            prompt_context(prompt)["reply_to_message"],
        )

    def test_entity_reply_is_quoted_and_duplicate_original_content_is_removed(self) -> None:
        reply_text = "Bold reply"
        message = {
            "message_id": 44,
            "text": "Follow up",
            "entities": [],
            "chat": {"id": -1001, "type": "supergroup"},
            "reply_to_message": {
                "message_id": 43,
                "text": reply_text,
                "entities": [{"offset": 0, "length": 4, "type": "bold"}],
                "message_thread_id": 7,
                "from": {"id": 1001, "first_name": "Other"},
            },
        }

        prompt = build_codex_prompt("message", message, {})

        context = prompt_context(prompt)
        self.assertEqual(
            prompt_user_message(prompt),
            "> **Bold** reply\n\nFollow up",
        )
        self.assertNotIn("quoted_original_message", context["reply_to_message"])
        self.assertEqual(
            context["reply_to_message"]["raw_payload"],
            {"message_thread_id": 7},
        )

    def test_distinct_original_content_becomes_a_second_quote_and_is_removed(self) -> None:
        message = {
            "message_id": 45,
            "text": "Follow up",
            "chat": {"id": -1001, "type": "supergroup"},
            "reply_to_message": {
                "message_id": 44,
                "text": "Main reply",
                "from": {"id": 1001, "first_name": "Other"},
            },
        }

        with patch("bot.best_effort_message_content", return_value="Additional original"):
            prompt = build_codex_prompt("message", message, {})

        context = prompt_context(prompt)
        self.assertEqual(
            prompt_user_message(prompt),
            "> Main reply\n\n> Additional original\n\nFollow up",
        )
        self.assertNotIn("quoted_original_message", context["reply_to_message"])
        self.assertNotIn("raw_payload", context["reply_to_message"])

    def test_current_message_formatting_is_preserved_without_quoting(self) -> None:
        message = {
            "message_id": 46,
            "text": "Bold request",
            "entities": [{"offset": 0, "length": 4, "type": "bold"}],
            "chat": {"id": -1001, "type": "supergroup"},
        }

        prompt = build_codex_prompt("message", message, {})

        self.assertEqual(prompt_user_message(prompt), "**Bold** request")

    def test_real_entity_converter_is_used_for_current_message(self) -> None:
        message = {
            "message_id": 47,
            "text": "Bold, italic.",
            "entities": [
                {"offset": 0, "length": 4, "type": "bold"},
                {"offset": 6, "length": 6, "type": "italic"},
            ],
            "chat": {"id": -1001, "type": "supergroup"},
        }

        prompt = build_codex_prompt("message", message, {})

        self.assertEqual(prompt_user_message(prompt), "**Bold**, *italic*.")

    def test_markdownv2_converter_is_used_when_local_entity_conversion_fails(self) -> None:
        message = {
            "message_id": 48,
            "text": "Bold request.",
            "entities": [{"offset": 0, "length": 4, "type": "bold"}],
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with (
            patch("bot.entities_to_markdown", side_effect=ValueError("bad local conversion")),
            patch("bot.entities_to_markdownv2", return_value="*Bold* request\\.") as fallback,
            self.assertLogs("bot", level="WARNING"),
        ):
            prompt = build_codex_prompt("message", message, {})

        self.assertEqual(prompt_user_message(prompt), "*Bold* request\\.")
        fallback.assert_called_once()

    def test_current_rich_message_is_converted_without_quoting(self) -> None:
        rich_message = {"blocks": [{"type": "heading", "text": "Request"}]}
        message = {
            "message_id": 47,
            "rich_message": rich_message,
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with patch("bot.rich_message_to_markdown", return_value="# Request"):
            prompt = build_codex_prompt("message", message, {})

        self.assertEqual(prompt_user_message(prompt), "# Request")

    def test_conversion_failures_use_the_requested_fallbacks(self) -> None:
        entity_message = {
            "message_id": 48,
            "text": "Plain request",
            "entities": [{"offset": 0, "length": 5, "type": "bold"}],
            "chat": {"id": -1001, "type": "supergroup"},
            "reply_to_message": {
                "message_id": 47,
                "text": "Plain reply",
                "entities": [{"offset": 0, "length": 5, "type": "bold"}],
                "from": {"id": 1001, "first_name": "Other"},
            },
        }
        with (
            patch("bot.entities_to_markdown", side_effect=ValueError("bad entity")),
            patch("bot.entities_to_markdownv2", side_effect=ValueError("bad fallback")),
            self.assertLogs("bot", level="WARNING"),
        ):
            entity_prompt = build_codex_prompt("message", entity_message, {})

        self.assertEqual(
            prompt_user_message(entity_prompt),
            "> Plain reply\n\nPlain request",
        )

        rich_payload = {"blocks": [{"type": "paragraph", "text": "Request"}]}
        rich_message = {
            "message_id": 49,
            "rich_message": rich_payload,
            "chat": {"id": -1001, "type": "supergroup"},
            "reply_to_message": {
                "message_id": 48,
                "rich_message": {"blocks": []},
                "from": {"id": 99, "is_bot": True},
            },
        }
        with (
            patch("bot.rich_message_to_markdown", side_effect=ValueError("bad rich text")),
            self.assertLogs("bot", level="WARNING"),
        ):
            rich_prompt = build_codex_prompt("message", rich_message, {"id": 99})

        self.assertFalse(prompt_user_message(rich_prompt).startswith(">"))
        self.assertIn('"rich_message"', prompt_user_message(rich_prompt))
        self.assertNotIn(
            "raw_payload",
            prompt_context(rich_prompt)["reply_to_message"],
        )

    def test_recorded_bot_reply_can_be_omitted_from_context(self) -> None:
        message = {
            "message_id": 43,
            "text": "follow up",
            "chat": {"id": -1001, "type": "supergroup"},
            "reply_to_message": {
                "message_id": 3001,
                "text": "previous bot response",
                "from": {"id": 99, "is_bot": True},
            },
        }

        prompt = build_codex_prompt(
            "message",
            message,
            {"id": 99},
            include_reply_to_message=False,
        )

        self.assertNotIn("reply_to_message", prompt_context(prompt))
        self.assertNotIn("previous bot response", prompt_text(prompt))

    def test_captionless_text_file_is_passed_by_readable_path_only(self) -> None:
        file_contents = "large file contents must not be embedded in the prompt"
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir).resolve()
            bot = object.__new__(Bot)
            bot.config = build_config(repo_dir)
            bot.attachments = AttachmentStore(repo_dir)
            bot.telegram = MagicMock()
            bot.telegram.get_file.return_value = {"file_path": "documents/report.txt"}

            def download_file(file_path: str, destination: Path) -> None:
                destination.write_text(file_contents, encoding="utf-8")
                destination.chmod(0o600)

            bot.telegram.download_file.side_effect = download_file
            bot.codex = MagicMock()
            bot.bot_user = {}
            downloaded_path: Path | None = None

            def run_codex(
                chat_id: int,
                prompt: list[str] | Callable[[SessionRecord | None], list[str]],
                image_paths: list[Path],
                agent_message_handler: Callable[[str], None],
                thread_name: str | None = None,
            ) -> str:
                nonlocal downloaded_path
                if callable(prompt):
                    prompt = prompt(None)
                context = prompt_context(prompt)
                downloaded_path = Path(context["attachments"][0]["local_path"])
                self.assertTrue(downloaded_path.is_relative_to(repo_dir))
                self.assertTrue(os.access(downloaded_path, os.R_OK))
                self.assertEqual(downloaded_path.read_text(encoding="utf-8"), file_contents)
                self.assertNotIn(file_contents, prompt_text(prompt))
                self.assertEqual(prompt_user_message(prompt), "Please inspect the attached file.")
                self.assertEqual(image_paths, [])
                return "done"

            bot.codex.run.side_effect = run_codex
            message = {
                "message_id": 43,
                "date": 1785000000,
                "document": {
                    "file_id": "text-file-id",
                    "file_name": "report.txt",
                    "mime_type": "text/plain",
                },
                "chat": {"id": -1001, "type": "supergroup"},
                "from": {"id": 1001, "first_name": "Test"},
            }

            with self.assertLogs("bot", level="INFO"):
                bot.handle_message(message)

            self.assertIsNotNone(downloaded_path)
            self.assertTrue(downloaded_path.exists())
            bot.codex.run.assert_called_once()

    def test_generic_files_include_pdfs_and_replies_but_exclude_images(self) -> None:
        message = {
            "document": {
                "file_id": "text-id",
                "file_name": "notes.txt",
                "mime_type": "text/plain",
            },
            "reply_to_message": {
                "document": {
                    "file_id": "archive-id",
                    "file_name": "data.zip",
                    "mime_type": "application/zip",
                },
            },
        }

        files = collect_message_files(message)

        self.assertEqual(
            files,
            [
                TelegramFile("text-id", "current_message", "notes.txt", "text/plain"),
                TelegramFile("archive-id", "reply_to_message", "data.zip", "application/zip"),
            ],
        )
        self.assertEqual(
            collect_message_files(
                {
                    "document": {
                        "file_id": "image-id",
                        "file_name": "image.png",
                        "mime_type": "image/png",
                    },
                },
            ),
            [],
        )
        self.assertEqual(
            collect_message_files(
                {
                    "document": {
                        "file_id": "pdf-id",
                        "file_name": "report.pdf",
                        "mime_type": "application/pdf",
                    },
                },
            ),
            [
                TelegramFile(
                    "pdf-id",
                    "current_message",
                    "report.pdf",
                    "application/pdf",
                ),
            ],
        )

        pdf_message = {
            "document": {
                "file_id": "pdf-id",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
            },
            "chat": {"id": -1001, "type": "supergroup"},
        }
        prompt = build_codex_prompt(
            "message",
            pdf_message,
            {},
            [Path("/repo/attachments/report.pdf")],
        )
        context = prompt_context(prompt)
        self.assertNotIn("attached_pdfs", context)
        self.assertEqual(
            context["attachments"][0]["local_path"],
            "/repo/attachments/report.pdf",
        )

    def test_non_document_media_are_collected_as_generic_files(self) -> None:
        files = collect_message_files(
            {
                "audio": {
                    "file_id": "audio-id",
                    "file_name": "recording.mp3",
                    "mime_type": "audio/mpeg",
                },
                "video_note": {
                    "file_id": "video-note-id",
                    "mime_type": "video/mp4",
                },
            },
        )

        self.assertEqual(
            files,
            [
                TelegramFile(
                    "audio-id",
                    "current_message",
                    "recording.mp3",
                    "audio/mpeg",
                    "audio",
                ),
                TelegramFile(
                    "video-note-id",
                    "current_message",
                    None,
                    "video/mp4",
                    "video_note",
                ),
            ],
        )

    def test_nested_rich_media_are_collected_and_top_level_duplicates_are_removed(self) -> None:
        message = {
            "photo": [
                {"file_id": "small-photo", "file_size": 10},
                {"file_id": "photo-id", "file_size": 20},
            ],
            "rich_message": {
                "blocks": [
                    {
                        "type": "collage",
                        "blocks": [
                            {
                                "type": "photo",
                                "photo": [
                                    {"file_id": "small-photo", "file_size": 10},
                                    {"file_id": "photo-id", "file_size": 20},
                                ],
                            },
                            {
                                "type": "animation",
                                "animation": {
                                    "file_id": "animation-id",
                                    "file_name": "demo.gif",
                                    "mime_type": "video/mp4",
                                },
                            },
                        ],
                    },
                    {
                        "type": "list",
                        "items": [
                            {
                                "blocks": [
                                    {
                                        "type": "audio",
                                        "audio": {
                                            "file_id": "audio-id",
                                            "file_name": "song.mp3",
                                            "mime_type": "audio/mpeg",
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "type": "details",
                        "blocks": [
                            {
                                "type": "voice_note",
                                "voice_note": {
                                    "file_id": "voice-id",
                                    "mime_type": "audio/ogg",
                                },
                            },
                            {
                                "type": "video",
                                "video": {
                                    "file_id": "video-id",
                                    "file_name": "clip.mp4",
                                    "mime_type": "video/mp4",
                                },
                            },
                        ],
                    },
                ],
            },
        }

        self.assertEqual(
            collect_message_images(message),
            [TelegramImage("photo-id", "current_message")],
        )
        self.assertEqual(
            collect_message_files(message),
            [
                TelegramFile(
                    "animation-id",
                    "current_message.rich_message",
                    "demo.gif",
                    "video/mp4",
                    "animation",
                ),
                TelegramFile(
                    "audio-id",
                    "current_message.rich_message",
                    "song.mp3",
                    "audio/mpeg",
                    "audio",
                ),
                TelegramFile(
                    "voice-id",
                    "current_message.rich_message",
                    None,
                    "audio/ogg",
                    "voice_note",
                ),
                TelegramFile(
                    "video-id",
                    "current_message.rich_message",
                    "clip.mp4",
                    "video/mp4",
                    "video",
                ),
            ],
        )

    def test_replied_to_rich_media_are_collected(self) -> None:
        message = {
            "reply_to_message": {
                "rich_message": {
                    "blocks": [
                        {
                            "type": "slideshow",
                            "blocks": [
                                {
                                    "type": "photo",
                                    "photo": [{"file_id": "reply-photo-id"}],
                                },
                                {
                                    "type": "audio",
                                    "audio": {"file_id": "reply-audio-id"},
                                },
                            ],
                        },
                    ],
                },
            },
        }

        self.assertEqual(
            collect_message_images(message),
            [TelegramImage("reply-photo-id", "reply_to_message.rich_message")],
        )
        self.assertEqual(
            collect_message_files(message),
            [
                TelegramFile(
                    "reply-audio-id",
                    "reply_to_message.rich_message",
                    None,
                    None,
                    "audio",
                ),
            ],
        )

    def test_file_download_is_streamed_and_owner_readable(self) -> None:
        client = TelegramClient("test-token")

        async def download(file_path: str, destination: Path) -> None:
            self.assertEqual(file_path, "documents/attachment.bin")
            destination.write_bytes(b"firstsecond")

        client._download_file = download
        client._submit = lambda coroutine: asyncio.run(coroutine)

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "attachment.bin"
            client.download_file("documents/attachment.bin", destination)

            self.assertEqual(destination.read_bytes(), b"firstsecond")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertTrue(os.access(destination, os.R_OK))

    def test_repeated_image_file_id_reuses_thread_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AttachmentStore(Path(temp_dir))
            first_cache_dir, _ = store.prepare_message(
                {"message_id": 1, "chat": {"id": -1001}},
                True,
            )
            second_cache_dir, _ = store.prepare_message(
                {"message_id": 2, "chat": {"id": -1001}},
                True,
            )
            self.assertEqual(first_cache_dir, second_cache_dir)

            bot = object.__new__(Bot)
            bot.telegram = MagicMock()
            bot.telegram.get_file.return_value = {"file_path": "photos/image.jpg"}
            bot.telegram.download_file.side_effect = (
                lambda file_path, destination: destination.write_bytes(b"jpeg")
            )

            first_paths = bot.download_images(
                [TelegramImage("same-image-id", "current_message")],
                first_cache_dir,
            )
            second_paths = bot.download_images(
                [TelegramImage("same-image-id", "reply_to_message")],
                second_cache_dir,
            )

            self.assertEqual(first_paths, second_paths)
            self.assertEqual(first_paths[0].read_bytes(), b"jpeg")
            bot.telegram.get_file.assert_called_once_with("same-image-id")
            bot.telegram.download_file.assert_called_once()

    def test_repeated_generic_file_id_reuses_thread_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AttachmentStore(Path(temp_dir))
            cache_dir, _ = store.prepare_message(
                {"message_id": 1, "chat": {"id": -1001}},
                True,
            )
            bot = object.__new__(Bot)
            bot.telegram = MagicMock()
            bot.telegram.get_file.return_value = {"file_path": "documents/report.pdf"}
            bot.telegram.download_file.side_effect = (
                lambda file_path, destination: destination.write_bytes(b"pdf")
            )

            first_paths = bot.download_files(
                [TelegramFile("same-file-id", "current_message", "report.pdf")],
                cache_dir,
            )
            second_paths = bot.download_files(
                [TelegramFile("same-file-id", "reply_to_message", "report.pdf")],
                cache_dir,
            )

            self.assertEqual(first_paths, second_paths)
            self.assertEqual(first_paths[0].read_bytes(), b"pdf")
            bot.telegram.get_file.assert_called_once_with("same-file-id")
            bot.telegram.download_file.assert_called_once()

    def test_failed_download_does_not_leave_a_reusable_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AttachmentStore(Path(temp_dir))
            cache_dir, _ = store.prepare_message(
                {"message_id": 1, "chat": {"id": -1001}},
                True,
            )
            bot = object.__new__(Bot)
            bot.telegram = MagicMock()
            bot.telegram.get_file.return_value = {"file_path": "documents/report.pdf"}

            def fail_download(file_path: str, destination: Path) -> None:
                destination.write_bytes(b"partial")
                raise RuntimeError("download interrupted")

            bot.telegram.download_file.side_effect = fail_download

            with self.assertRaisesRegex(RuntimeError, "download interrupted"):
                bot.download_files(
                    [TelegramFile("failed-file-id", "current_message", "report.pdf")],
                    cache_dir,
                )

            self.assertEqual(list(cache_dir.iterdir()), [])

    def test_captionless_generic_file_is_supported_in_guest_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = object.__new__(Bot)
            repo_dir = Path(temp_dir).resolve()
            bot.config = build_config(repo_dir)
            bot.attachments = AttachmentStore(repo_dir)
            bot.telegram = MagicMock()
            bot.telegram.get_file.return_value = {"file_path": "documents/data.csv"}
            bot.telegram.download_file.side_effect = (
                lambda file_path, destination: destination.write_bytes(b"a,b\n1,2\n")
            )
            bot.codex = MagicMock()
            bot.codex.run.return_value = "done"
            bot.bot_user = {}
            message = {
                "message_id": 44,
                "guest_query_id": "guest-query",
                "document": {
                    "file_id": "csv-file-id",
                    "file_name": "data.csv",
                    "mime_type": "text/csv",
                },
                "chat": {"id": -1001, "type": "supergroup"},
                "from": {"id": 1001, "first_name": "Test"},
            }

            with self.assertLogs("bot", level="INFO"):
                bot.handle_guest_message(message)

            prompt_factory = bot.codex.run.call_args.args[1]
            prompt = prompt_factory(None)
            self.assertIn('"attachments": [', prompt[0])
            self.assertNotIn('"attached_images"', prompt[0])
            self.assertNotIn('"attached_files"', prompt[0])
            self.assertIn('"local_path":', prompt[0])
            self.assertNotIn("a,b\n1,2", prompt_text(prompt))
            bot.telegram.answer_guest_query.assert_called_once_with("guest-query", "done")


class AttachmentStoreTest(unittest.TestCase):
    def test_startup_removes_only_expired_files_left_by_previous_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            old_cache = repo_dir / ATTACHMENT_CACHE_DIRECTORY
            old_cache.mkdir()
            old_file = old_cache / "old-attachment.txt"
            old_file.write_text("old", encoding="utf-8")
            recent_file = old_cache / "recent-attachment.txt"
            recent_file.write_text("recent", encoding="utf-8")
            expired_at = time.time() - 61
            os.utime(old_file, (expired_at, expired_at))

            store = AttachmentStore(repo_dir, retention_seconds=60)

            self.assertFalse(old_file.exists())
            self.assertTrue(recent_file.exists())
            self.assertTrue(store.root.is_dir())

    def test_thread_files_are_removed_after_fifth_attachment_free_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AttachmentStore(Path(temp_dir), retention_seconds=60)
            attachment_message = {
                "message_id": 1,
                "message_thread_id": 42,
                "chat": {"id": -1001},
            }
            attachment_dir, clear_thread = store.prepare_message(attachment_message, True)
            self.assertIsNotNone(attachment_dir)
            self.assertFalse(clear_thread)
            attachment_path = attachment_dir / "report.pdf"
            attachment_path.write_bytes(b"pdf")
            expired_at = time.time() - 61
            os.utime(attachment_path, (expired_at, expired_at))

            other_thread_message = {
                "message_id": 1,
                "message_thread_id": 99,
                "chat": {"id": -1001},
            }
            other_thread_dir, _ = store.prepare_message(other_thread_message, True)
            other_thread_path = other_thread_dir / "notes.txt"
            other_thread_path.write_text("keep", encoding="utf-8")

            for message_number in range(2, ATTACHMENT_FREE_MESSAGE_RETENTION + 1):
                follow_up = {
                    "message_id": message_number,
                    "message_thread_id": 42,
                    "chat": {"id": -1001},
                }
                _, clear_thread = store.prepare_message(follow_up, False)
                self.assertFalse(clear_thread)
                store.finish_message(follow_up, clear_thread)
                self.assertTrue(attachment_path.exists())

            fifth_follow_up = {
                "message_id": ATTACHMENT_FREE_MESSAGE_RETENTION + 1,
                "message_thread_id": 42,
                "chat": {"id": -1001},
            }
            _, clear_thread = store.prepare_message(fifth_follow_up, False)
            self.assertTrue(clear_thread)
            self.assertTrue(attachment_path.exists())

            store.finish_message(fifth_follow_up, clear_thread)

            self.assertFalse(attachment_path.exists())
            self.assertTrue(other_thread_path.exists())

    def test_fifth_attachment_free_message_respects_minimum_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AttachmentStore(Path(temp_dir), retention_seconds=60)
            message = {"message_id": 1, "chat": {"id": -1001}}
            attachment_dir, _ = store.prepare_message(message, True)
            attachment_path = attachment_dir / "recent.txt"
            attachment_path.write_text("recent", encoding="utf-8")

            for message_id in range(2, ATTACHMENT_FREE_MESSAGE_RETENTION + 2):
                message = {"message_id": message_id, "chat": {"id": -1001}}
                _, clear_thread = store.prepare_message(message, False)
                store.finish_message(message, clear_thread)

            self.assertTrue(clear_thread)
            self.assertTrue(attachment_path.exists())

    def test_deferred_cleanup_is_cancelled_by_a_new_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AttachmentStore(Path(temp_dir))
            first_message = {"message_id": 1, "chat": {"id": -1001}}
            attachment_dir, _ = store.prepare_message(first_message, True)
            retained_file = attachment_dir / "retained.txt"
            retained_file.write_text("retained", encoding="utf-8")
            clear_message: dict[str, object] = {}
            clear_thread = False
            for message_id in range(2, ATTACHMENT_FREE_MESSAGE_RETENTION + 2):
                clear_message = {"message_id": message_id, "chat": {"id": -1001}}
                _, clear_thread = store.prepare_message(clear_message, False)
            self.assertTrue(clear_thread)

            store.prepare_message(
                {"message_id": 7, "chat": {"id": -1001}},
                True,
            )
            store.finish_message(clear_message, clear_thread)

            self.assertTrue(retained_file.exists())


class SessionStoreTest(unittest.TestCase):
    def test_forum_topics_have_separate_persisted_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sessions.json"
            first_topic = telegram_conversation_key("message", -1001, 42)
            second_topic = telegram_conversation_key("message", -1001, 99)
            guest_topic = telegram_conversation_key("guest_message", -1001, 42)
            store = SessionStore(path)
            store.set(-1001, "non-topic-session")
            store.set(first_topic, "first-topic-session")
            store.set(second_topic, "second-topic-session")
            store.set(guest_topic, "guest-topic-session")

            reloaded = SessionStore(path)

            self.assertEqual(reloaded.get(-1001), "non-topic-session")
            self.assertEqual(reloaded.get(first_topic), "first-topic-session")
            self.assertEqual(reloaded.get(second_topic), "second-topic-session")
            self.assertEqual(reloaded.get(guest_topic), "guest-topic-session")
            self.assertEqual(conversation_chat_id(first_topic), -1001)
            self.assertEqual(conversation_message_thread_id(first_topic), 42)
            self.assertEqual(conversation_chat_id(guest_topic), -1001)
            self.assertEqual(conversation_message_thread_id(guest_topic), 42)

    def test_guest_and_ordinary_chats_with_the_same_id_have_separate_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sessions.json"
            guest_key = guest_conversation_key(1001)
            store = SessionStore(path)
            store.set(1001, "ordinary-session")
            store.set(guest_key, "guest-session")

            reloaded = SessionStore(path)

            self.assertEqual(reloaded.get(1001), "ordinary-session")
            self.assertEqual(reloaded.get(guest_key), "guest-session")
            self.assertEqual(
                dict(reloaded.records()),
                {
                    1001: reloaded.get_record(1001),
                    guest_key: reloaded.get_record(guest_key),
                },
            )

    def test_guest_group_and_ordinary_group_have_separate_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions.json")
            guest_key = guest_conversation_key(-1001)
            store.set(-1001, "ordinary-group-session")
            store.set(guest_key, "guest-group-session")

            self.assertEqual(store.get(-1001), "ordinary-group-session")
            self.assertEqual(store.get(guest_key), "guest-group-session")
            self.assertEqual(
                store.conversation_keys_for_session("guest-group-session"),
                [guest_key],
            )

    def test_sessions_are_persisted_by_chat_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state" / "sessions.json"
            store = SessionStore(path)
            store.set(-1001, "session-one")
            store.set(-1002, "session-two")

            reloaded = SessionStore(path)

            self.assertEqual(reloaded.get(-1001), "session-one")
            self.assertEqual(reloaded.get(-1002), "session-two")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 4)

    def test_version_one_store_is_migrated_without_immediate_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sessions.json"
            path.write_text(
                json.dumps({"version": 1, "sessions": {"-1001": "session-one"}}),
                encoding="utf-8",
            )

            before_load = time.time()
            store = SessionStore(path)

            record = store.get_record(-1001)
            self.assertIsNotNone(record)
            self.assertGreaterEqual(record.last_message_at, before_load)
            self.assertIsNone(record.last_compacted_at)
            self.assertIsNone(record.last_bot_message_id)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 4)

    def test_version_two_store_is_migrated_with_no_bot_message_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sessions.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "sessions": {
                            "-1001": {
                                "session_id": "session-one",
                                "last_message_at": 123.0,
                                "last_compacted_at": None,
                            },
                        },
                    },
                ),
                encoding="utf-8",
            )

            store = SessionStore(path)

            self.assertIsNone(store.get_record(-1001).last_bot_message_id)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 4)

    def test_bot_message_id_is_preserved_only_for_the_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions.json")
            store.set(-1001, "session-one", 100.0)

            self.assertTrue(store.record_bot_message(-1001, "session-one", 3001))
            store.touch(-1001, 200.0)
            store.mark_compacted(-1001, "session-one", 250.0)
            store.set(-1001, "session-one", 300.0)
            self.assertEqual(store.get_record(-1001).last_bot_message_id, 3001)

            store.set(-1001, "session-two", 400.0)
            self.assertIsNone(store.get_record(-1001).last_bot_message_id)
            self.assertFalse(store.record_bot_message(-1001, "session-one", 3002))

    def test_response_error_is_persisted_and_cleared_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sessions.json"
            store = SessionStore(path)
            store.set(-1001, "session-one", 100.0)

            self.assertTrue(
                store.record_response_error(
                    -1001,
                    "Telegram answerGuestQuery failed: query is too old",
                ),
            )
            reloaded = SessionStore(path)
            self.assertEqual(
                reloaded.get_response_error(-1001),
                "Telegram answerGuestQuery failed: query is too old",
            )
            self.assertTrue(reloaded.clear_response_error(-1001))
            self.assertIsNone(reloaded.get_response_error(-1001))
            self.assertFalse(reloaded.clear_response_error(-1001))

    def test_response_error_is_persisted_without_a_codex_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sessions.json"
            store = SessionStore(path)

            self.assertTrue(
                store.record_response_error(
                    -1001,
                    "Telegram answerGuestQuery failed: query is too old",
                ),
            )

            self.assertEqual(
                SessionStore(path).get_response_error(-1001),
                "Telegram answerGuestQuery failed: query is too old",
            )

    def test_remove_session_removes_every_matching_chat_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions.json")
            store.set(-1001, "session-one")
            store.set(1001, "session-one")
            store.set(-1002, "session-two")

            self.assertEqual(store.remove_session("session-one"), [-1001, 1001])
            self.assertIsNone(store.get(-1001))
            self.assertIsNone(store.get(1001))
            self.assertEqual(store.get(-1002), "session-two")
            self.assertEqual(store.remove_session("session-one"), [])


class ChatAllowListTest(unittest.TestCase):
    def test_whitelisted_private_chat_is_allowed(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))

        message = {"message_id": 1, "chat": {"id": 1001, "type": "private"}}

        self.assertTrue(bot.is_allowed_message(message))

    def test_private_chat_outside_allow_list_is_rejected(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))

        message = {"message_id": 1, "chat": {"id": 9999, "type": "private"}}

        self.assertFalse(bot.is_allowed_message(message))

    def test_group_outside_allow_list_is_allowed_for_whitelisted_user(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))

        message = {
            "message_id": 1,
            "chat": {"id": -9999, "type": "supergroup"},
            "from": {"id": 1001, "first_name": "Allowed"},
        }

        self.assertTrue(bot.is_allowed_message(message))

    def test_group_outside_allow_list_is_rejected_for_unlisted_user(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))

        message = {
            "message_id": 1,
            "chat": {"id": -9999, "type": "group"},
            "from": {"id": 9999, "first_name": "Unlisted"},
        }

        self.assertFalse(bot.is_allowed_message(message))

    def test_sender_chat_is_not_treated_as_an_allowed_user(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))

        message = {
            "message_id": 1,
            "chat": {"id": -9999, "type": "supergroup"},
            "sender_chat": {"id": 1001},
        }

        self.assertFalse(bot.is_allowed_message(message))

    def test_whitelisted_channel_is_still_rejected(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))

        message = {
            "message_id": 1,
            "chat": {"id": 1001, "type": "channel"},
            "from": {"id": 1001, "first_name": "Allowed"},
        }

        self.assertFalse(bot.is_allowed_message(message))


class TelegramThreadNameTest(unittest.TestCase):
    def test_group_uses_chat_title(self) -> None:
        self.assertEqual(
            telegram_thread_name(
                "message",
                {
                    "chat": {
                        "id": -1001,
                        "type": "supergroup",
                        "title": "Direct ISC team",
                    },
                },
            ),
            "Direct ISC team",
        )

    def test_forum_topic_name_includes_chat_title_and_topic_id(self) -> None:
        self.assertEqual(
            telegram_thread_name(
                "message",
                {
                    "message_thread_id": 42,
                    "chat": {
                        "id": -1001,
                        "type": "supergroup",
                        "title": "Direct ISC team",
                    },
                },
            ),
            "Direct ISC team / topic 42",
        )

    def test_direct_chat_uses_username(self) -> None:
        self.assertEqual(
            telegram_thread_name(
                "message",
                {"chat": {"id": 1001, "type": "private", "username": "denis"}},
            ),
            "@denis",
        )

    def test_direct_chat_without_username_uses_numeric_telegram_link(self) -> None:
        self.assertEqual(
            telegram_thread_name(
                "message",
                {"chat": {"id": 1001, "type": "private", "first_name": "Denis"}},
            ),
            "tg://user?id=1001",
        )

    def test_guest_private_chat_uses_both_participants_in_stable_order(self) -> None:
        first_direction = {
            "from": {"id": 1002, "first_name": "Alice"},
            "chat": {"id": 1001, "type": "private", "username": "bob"},
        }
        reverse_direction = {
            "from": {"id": 1001, "username": "bob"},
            "chat": {"id": 1002, "type": "private", "first_name": "Alice"},
        }

        self.assertEqual(
            telegram_thread_name("guest_message", first_direction),
            "@bob/Alice",
        )
        self.assertEqual(
            telegram_thread_name("guest_message", reverse_direction),
            "@bob/Alice",
        )

    def test_guest_private_chat_can_name_a_user_to_other_bot_conversation(self) -> None:
        self.assertEqual(
            telegram_thread_name(
                "guest_message",
                {
                    "from": {"id": 1001, "username": "alice"},
                    "chat": {
                        "id": 9001,
                        "type": "private",
                        "username": "helper_bot",
                    },
                },
            ),
            "@alice/@helper_bot",
        )


class CodexRunnerTest(unittest.TestCase):
    def test_latest_bot_message_maps_reaction_back_to_forum_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            first_topic = telegram_conversation_key("message", -1001, 42)
            second_topic = telegram_conversation_key("message", -1001, 99)
            runner.sessions.set(first_topic, "thread-one")
            runner.sessions.set(second_topic, "thread-two")
            runner.sessions.record_bot_message(first_topic, "thread-one", 3001)
            runner.sessions.record_bot_message(second_topic, "thread-two", 3002)

            self.assertEqual(
                runner.conversation_key_for_last_bot_message(-1001, 3001),
                first_topic,
            )
            self.assertIsNone(
                runner.conversation_key_for_last_bot_message(-1001, 2999),
            )

    def test_new_thread_is_named_after_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.client = MagicMock()
            runner.client.start_thread.return_value = "thread-one"

            thread_id = runner._start_thread(-1001, "Direct ISC team")

        self.assertEqual(thread_id, "thread-one")
        runner.client.set_thread_name.assert_called_once_with(
            "thread-one",
            "Direct ISC team",
        )

    def test_thread_naming_failure_does_not_block_the_first_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.client = MagicMock()
            runner.client.start_thread.return_value = "thread-one"
            runner.client.set_thread_name.side_effect = CodexAppServerError("not available")

            with self.assertLogs("bot", level="WARNING"):
                thread_id = runner._start_thread(-1001, "Direct ISC team")

        self.assertEqual(thread_id, "thread-one")

    def test_sticker_tool_is_registered_from_configured_webp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sticker_dir = root / "stickers"
            sticker_dir.mkdir()
            (sticker_dir / "sad.webp").write_bytes(b"sad")
            (sticker_dir / "happy.webp").write_bytes(b"happy")
            (sticker_dir / "ignored.png").write_bytes(b"png")
            runner = CodexRunner(build_config(root, sticker_dir=sticker_dir), MagicMock())

            specs = runner._dynamic_tools_for_chat(-1001)

        self.assertIsNotNone(specs)
        sticker_spec = next(
            spec for spec in specs if spec["name"] == TELEGRAM_STICKER_TOOL_NAME
        )
        self.assertEqual(
            sticker_spec["inputSchema"]["properties"]["sticker"]["enum"],
            ["happy", "sad"],
        )
        guest_specs = runner._dynamic_tools_for_chat(guest_conversation_key(-1001))
        self.assertIsNotNone(guest_specs)
        self.assertEqual(
            [spec["name"] for spec in guest_specs],
            [TELEGRAM_REACTION_TOOL_NAME, TELEGRAM_STICKER_TOOL_NAME],
        )
        guest_sticker_spec = guest_specs[1]
        self.assertIn(
            "guest query accepts only one response",
            guest_sticker_spec["description"],
        )
        self.assertNotEqual(sticker_spec["description"], guest_sticker_spec["description"])

    def test_guest_thread_registers_commentary_reaction_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            telegram = MagicMock()
            runner = CodexRunner(build_config(Path(temp_dir)), telegram)
            runner.client = MagicMock()
            runner.client.start_thread.side_effect = ["ordinary-thread", "guest-thread"]

            self.assertEqual(runner._start_thread(-1001), "ordinary-thread")
            self.assertEqual(
                runner._start_thread(guest_conversation_key(-1001)),
                "guest-thread",
            )

        runner.client.start_thread.assert_has_calls(
            [
                call(telegram_dynamic_tool_specs()),
                call([telegram_reaction_tool_spec(guest_mode=True)]),
            ],
        )

    def test_persisted_guest_group_resumes_with_commentary_reaction_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            telegram = MagicMock()
            runner = CodexRunner(build_config(Path(temp_dir)), telegram)
            guest_key = guest_conversation_key(-1001)
            runner.sessions.set(-1001, "ordinary-group-thread")
            runner.sessions.set(guest_key, "guest-group-thread")
            runner.client = MagicMock()
            runner.client.generation = 1

            runner.subscribe_active_sessions()

        runner.client.resume_thread.assert_has_calls(
            [
                call("ordinary-group-thread", telegram_dynamic_tool_specs()),
                call(
                    "guest-group-thread",
                    [telegram_reaction_tool_spec(guest_mode=True)],
                ),
            ],
        )

    def test_reaction_tool_maps_app_server_thread_to_telegram_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            telegram = MagicMock()
            runner = CodexRunner(build_config(Path(temp_dir)), telegram)
            runner.sessions.set(-1001, "thread-one")

            result = runner._handle_dynamic_tool_call(
                {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "callId": "call-one",
                    "tool": TELEGRAM_REACTION_TOOL_NAME,
                    "namespace": None,
                    "arguments": {"message_id": 2397, "reaction": "👍"},
                },
            )

        telegram.set_message_reaction.assert_called_once_with(-1001, 2397, "👍")
        self.assertTrue(result["success"])

    def test_guest_reaction_tool_emits_separate_commentary_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            telegram = MagicMock()
            runner = CodexRunner(build_config(Path(temp_dir)), telegram)
            guest_key = guest_conversation_key(-1001)
            commentary_handler = MagicMock()
            handle = CodexTurnHandle(
                "guest-thread",
                "turn-one",
                queue.Queue(),
                agent_message_handler=commentary_handler,
            )
            runner.sessions.set(guest_key, "guest-thread")
            runner.active_turns[guest_key] = RunningCodexTurn(
                guest_key,
                "guest-thread",
                handle,
            )

            result = runner._handle_dynamic_tool_call(
                {
                    "threadId": "guest-thread",
                    "turnId": "turn-one",
                    "callId": "call-one",
                    "tool": TELEGRAM_REACTION_TOOL_NAME,
                    "namespace": None,
                    "arguments": {"message_id": 2397, "reaction": "👍"},
                },
            )

        telegram.set_message_reaction.assert_not_called()
        self.assertTrue(result["success"])
        commentary_handler.assert_called_once_with(
            CodexAgentMessage("turn-one", "call-one", "commentary", "👍"),
        )
        self.assertIn("guest commentary", result["contentItems"][0]["text"])

    def test_clear_reaction_tool_maps_app_server_thread_to_telegram_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            telegram = MagicMock()
            runner = CodexRunner(build_config(Path(temp_dir)), telegram)
            runner.sessions.set(-1001, "thread-one")

            result = runner._handle_dynamic_tool_call(
                {
                    "threadId": "thread-one",
                    "tool": TELEGRAM_CLEAR_REACTION_TOOL_NAME,
                    "namespace": None,
                    "arguments": {"message_id": 2397},
                },
            )

        telegram.clear_message_reaction.assert_called_once_with(-1001, 2397)
        telegram.set_message_reaction.assert_not_called()
        self.assertTrue(result["success"])

    def test_reaction_tool_schema_requires_message_id_and_uses_reaction_enum(self) -> None:
        spec = telegram_reaction_tool_spec()
        schema = spec["inputSchema"]

        self.assertEqual(spec["name"], TELEGRAM_REACTION_TOOL_NAME)
        self.assertEqual(schema["required"], ["message_id", "reaction"])
        self.assertEqual(schema["properties"]["message_id"]["type"], "integer")
        self.assertEqual(
            schema["properties"]["reaction"]["enum"],
            list(TELEGRAM_REACTION_EMOJIS),
        )
        self.assertNotIn("chat_id", schema["properties"])
        self.assertIn("may replace a one-line acknowledgement", spec["description"])

    def test_guest_reaction_tool_description_explains_commentary_delivery(self) -> None:
        spec = telegram_reaction_tool_spec(guest_mode=True)

        self.assertIn("separate commentary message", spec["description"])
        self.assertIn("cannot set a native Telegram reaction", spec["description"])

    def test_clear_reaction_tool_schema_only_requires_message_id(self) -> None:
        spec = telegram_clear_reaction_tool_spec()
        schema = spec["inputSchema"]

        self.assertEqual(spec["name"], TELEGRAM_CLEAR_REACTION_TOOL_NAME)
        self.assertEqual(schema["required"], ["message_id"])
        self.assertEqual(set(schema["properties"]), {"message_id"})
        self.assertFalse(schema["additionalProperties"])

    def test_sticker_tool_schema_only_requires_configured_sticker(self) -> None:
        spec = telegram_sticker_tool_spec(("happy", "sad"))
        schema = spec["inputSchema"]

        self.assertEqual(spec["name"], TELEGRAM_STICKER_TOOL_NAME)
        self.assertEqual(schema["required"], ["sticker"])
        self.assertEqual(schema["properties"]["sticker"]["enum"], ["happy", "sad"])
        self.assertEqual(
            set(schema["properties"]),
            {"sticker", "message_thread_id"},
        )
        self.assertEqual(schema["properties"]["message_thread_id"]["minimum"], 1)
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("replace a one-line acknowledgement", spec["description"])

    def test_guest_sticker_tool_description_explains_single_response_delivery(self) -> None:
        spec = telegram_sticker_tool_spec(("happy", "sad"), guest_mode=True)

        self.assertIn("guest query accepts only one response", spec["description"])
        self.assertIn("produce no such text afterward", spec["description"])
        self.assertIn("embedded as an image", spec["description"])

    def test_sticker_tool_maps_configured_sticker_to_telegram_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sticker_dir = root / "stickers"
            sticker_dir.mkdir()
            sticker_path = sticker_dir / "happy.webp"
            sticker_path.write_bytes(b"happy")
            telegram = MagicMock()
            runner = CodexRunner(build_config(root, sticker_dir=sticker_dir), telegram)
            runner.sessions.set(-1001, "thread-one")
            runner.active_turns[-1001] = RunningCodexTurn(
                -1001,
                "thread-one",
                CodexTurnHandle("thread-one", "turn-one", queue.Queue()),
                42,
            )

            result = runner._handle_dynamic_tool_call(
                {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "tool": TELEGRAM_STICKER_TOOL_NAME,
                    "namespace": None,
                    "arguments": {"sticker": "happy"},
                },
            )

        telegram.send_chat_action.assert_called_once_with(-1001, 42, "choose_sticker")
        telegram.send_sticker.assert_called_once_with(-1001, sticker_path.resolve(), 42)
        self.assertTrue(result["success"])

    def test_sticker_tool_ignores_optional_chat_action_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sticker_dir = root / "stickers"
            sticker_dir.mkdir()
            sticker_path = sticker_dir / "happy.webp"
            sticker_path.write_bytes(b"happy")
            telegram = MagicMock()
            telegram.send_chat_action.side_effect = RuntimeError("action unavailable")
            telegram.send_sticker.return_value = 3001
            runner = CodexRunner(build_config(root, sticker_dir=sticker_dir), telegram)
            runner.sessions.set(-1001, "thread-one")

            result = runner._handle_dynamic_tool_call(
                {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "tool": TELEGRAM_STICKER_TOOL_NAME,
                    "namespace": None,
                    "arguments": {"sticker": "happy"},
                },
            )

        telegram.send_chat_action.assert_called_once_with(-1001, None, "choose_sticker")
        telegram.send_sticker.assert_called_once_with(-1001, sticker_path.resolve(), None)
        self.assertEqual(runner.sessions.get_record(-1001).last_bot_message_id, 3001)
        self.assertTrue(result["success"])

    def test_sticker_tool_can_target_an_explicit_forum_topic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sticker_dir = root / "stickers"
            sticker_dir.mkdir()
            sticker_path = sticker_dir / "happy.webp"
            sticker_path.write_bytes(b"happy")
            telegram = MagicMock()
            runner = CodexRunner(build_config(root, sticker_dir=sticker_dir), telegram)
            topic_key = telegram_conversation_key("message", -1001, 42)
            runner.sessions.set(topic_key, "thread-one")

            result = runner._handle_dynamic_tool_call(
                {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "tool": TELEGRAM_STICKER_TOOL_NAME,
                    "namespace": None,
                    "arguments": {
                        "sticker": "happy",
                        "message_thread_id": 99,
                    },
                },
            )

        telegram.send_chat_action.assert_called_once_with(-1001, 99, "choose_sticker")
        telegram.send_sticker.assert_called_once_with(-1001, sticker_path.resolve(), 99)
        self.assertTrue(result["success"])

    def test_sticker_tool_rejects_invalid_forum_topic_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sticker_dir = root / "stickers"
            sticker_dir.mkdir()
            (sticker_dir / "happy.webp").write_bytes(b"happy")
            telegram = MagicMock()
            runner = CodexRunner(build_config(root, sticker_dir=sticker_dir), telegram)
            runner.sessions.set(-1001, "thread-one")

            result = runner._handle_dynamic_tool_call(
                {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "tool": TELEGRAM_STICKER_TOOL_NAME,
                    "namespace": None,
                    "arguments": {
                        "sticker": "happy",
                        "message_thread_id": 0,
                    },
                },
            )

        telegram.send_sticker.assert_not_called()
        self.assertFalse(result["success"])
        self.assertIn("positive integer", result["contentItems"][0]["text"])

    def test_guest_sticker_tool_queues_instead_of_sending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sticker_dir = root / "stickers"
            sticker_dir.mkdir()
            sticker_path = sticker_dir / "happy.webp"
            sticker_path.write_bytes(b"happy")
            telegram = MagicMock()
            runner = CodexRunner(build_config(root, sticker_dir=sticker_dir), telegram)
            guest_key = guest_conversation_key(-1001)
            runner.sessions.set(guest_key, "guest-thread")
            handle = CodexTurnHandle("guest-thread", "turn-one", queue.Queue())
            runner.active_turns[guest_key] = RunningCodexTurn(
                guest_key,
                "guest-thread",
                handle,
            )
            runner.turn_steer_counts["turn-one"] = 0

            result = runner._handle_dynamic_tool_call(
                {
                    "threadId": "guest-thread",
                    "turnId": "turn-one",
                    "tool": TELEGRAM_STICKER_TOOL_NAME,
                    "namespace": None,
                    "arguments": {"sticker": "happy"},
                },
            )
            queued = runner.take_queued_guest_stickers("turn-one")

        telegram.send_sticker.assert_not_called()
        telegram.send_chat_action.assert_not_called()
        self.assertTrue(result["success"])
        self.assertIn("embedded as an image", result["contentItems"][0]["text"])
        self.assertEqual(
            queued,
            (QueuedTelegramSticker("happy", sticker_path.resolve(), 0),),
        )

    def test_guest_sticker_tool_rejects_call_after_non_commentary_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sticker_dir = root / "stickers"
            sticker_dir.mkdir()
            (sticker_dir / "happy.webp").write_bytes(b"happy")
            runner = CodexRunner(build_config(root, sticker_dir=sticker_dir), MagicMock())
            guest_key = guest_conversation_key(-1001)
            runner.sessions.set(guest_key, "guest-thread")
            handle = CodexTurnHandle("guest-thread", "turn-one", queue.Queue())
            runner.active_turns[guest_key] = RunningCodexTurn(
                guest_key,
                "guest-thread",
                handle,
            )
            runner.turn_steer_counts["turn-one"] = 0
            runner.client.non_commentary_agent_message_turns.add("turn-one")

            result = runner._handle_dynamic_tool_call(
                {
                    "threadId": "guest-thread",
                    "turnId": "turn-one",
                    "tool": TELEGRAM_STICKER_TOOL_NAME,
                    "namespace": None,
                    "arguments": {"sticker": "happy"},
                },
            )

        self.assertFalse(result["success"])
        self.assertEqual(runner.take_queued_guest_stickers("turn-one"), ())

    def test_sticker_tool_rejects_unconfigured_sticker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sticker_dir = root / "stickers"
            sticker_dir.mkdir()
            (sticker_dir / "happy.webp").write_bytes(b"happy")
            telegram = MagicMock()
            runner = CodexRunner(build_config(root, sticker_dir=sticker_dir), telegram)
            runner.sessions.set(-1001, "thread-one")

            result = runner._handle_dynamic_tool_call(
                {
                    "threadId": "thread-one",
                    "tool": TELEGRAM_STICKER_TOOL_NAME,
                    "namespace": None,
                    "arguments": {"sticker": "missing"},
                },
            )

        telegram.send_sticker.assert_not_called()
        self.assertFalse(result["success"])

    def test_missing_or_empty_sticker_directory_disables_sticker_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(configured_telegram_stickers(root / "missing"), {})
            self.assertEqual(configured_telegram_stickers(root), {})

            runner = CodexRunner(build_config(root, sticker_dir=root), MagicMock())
            specs = runner._dynamic_tools_for_chat(-1001)

        self.assertIsNotNone(specs)
        self.assertNotIn(TELEGRAM_STICKER_TOOL_NAME, [spec["name"] for spec in specs])

    def test_archive_chat_archives_thread_and_removes_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.sessions.set(-1001, "session-one")
            runner.client = MagicMock()
            runner.subscription_generations["session-one"] = 1

            self.assertEqual(runner.archive_chat(-1001), "session-one")

            runner.client.start.assert_called_once_with()
            runner.client.archive_thread.assert_called_once_with("session-one")
            self.assertIsNone(runner.sessions.get(-1001))
            self.assertNotIn("session-one", runner.subscription_generations)

    def test_archive_chat_without_thread_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.client = MagicMock()

            self.assertIsNone(runner.archive_chat(-1001))

            runner.client.start.assert_not_called()
            runner.client.archive_thread.assert_not_called()

    def test_compact_chat_compacts_existing_thread_and_keeps_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.sessions.set(-1001, "session-one", 100.0)
            runner.client = MagicMock()
            runner.client.generation = 1

            self.assertTrue(runner.compact_chat(-1001))

            runner.client.resume_thread.assert_called_once_with("session-one")
            runner.client.compact_thread.assert_called_once_with("session-one")
            record = runner.sessions.get_record(-1001)
            self.assertEqual(record.session_id, "session-one")
            self.assertIsNotNone(record.last_compacted_at)

    def test_private_chat_compact_command_releases_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.sessions.set(1001, "private-session")
            runner.client = MagicMock()
            runner.client.generation = 1

            self.assertTrue(runner.compact_chat(1001))

            runner.client.unsubscribe_thread.assert_called_once_with("private-session")

    def test_only_group_threads_are_subscribed_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            runner = CodexRunner(config)
            runner.sessions.set(-1001, "session-one")
            runner.sessions.set(-1002, "session-two")
            runner.sessions.set(1001, "private-session")
            runner.client = MagicMock()
            runner.client.generation = 1

            runner.subscribe_active_sessions()
            runner.subscribe_active_sessions()
            runner.client.generation = 2
            runner.subscribe_active_sessions()

        self.assertEqual(
            [call.args[0] for call in runner.client.resume_thread.call_args_list],
            ["session-one", "session-two", "session-one", "session-two"],
        )

    def test_missing_group_thread_is_discarded_during_startup_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.sessions.set(-1001, "missing-session")
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.client.resume_thread.side_effect = CodexAppServerError(
                "missing thread",
                request_method="thread/resume",
                error_code=-32600,
                app_server_message="no rollout found for thread id missing-session",
            )

            with self.assertLogs("bot", level="WARNING"):
                runner.subscribe_active_sessions()

            self.assertIsNone(runner.sessions.get(-1001))
            runner.client.resume_thread.assert_called_once_with("missing-session")

    def test_delete_notification_during_resume_does_not_leave_a_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.sessions.set(-1001, "session-one")
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.client.resume_thread.side_effect = lambda thread_id: (
                runner._handle_app_server_notification(
                    {
                        "method": "thread/deleted",
                        "params": {"threadId": thread_id},
                    },
                )
            )

            runner._ensure_thread_subscribed("session-one")

            self.assertIsNone(runner.sessions.get(-1001))
            self.assertNotIn("session-one", runner.subscription_generations)

    def test_idle_session_is_compacted_only_once_until_another_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            runner = CodexRunner(config)
            now = 2_000_000_000.0
            runner.sessions.set(
                -1001,
                "session-one",
                now - config.session_compaction_idle_seconds - 1,
            )
            runner.client = MagicMock()
            runner.client.generation = 1

            runner.compact_idle_sessions(now)
            runner.compact_idle_sessions(now + 60)

            runner.client.resume_thread.assert_called_once_with("session-one")
            runner.client.compact_thread.assert_called_once_with("session-one")
            record = runner.sessions.get_record(-1001)
            self.assertEqual(record.last_compacted_at, now)

    def test_idle_session_is_not_compacted_when_a_message_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            runner = CodexRunner(config)
            runner.git_puller = MagicMock()
            now = 2_000_000_000.0
            runner.sessions.set(
                -1001,
                "session-one",
                now - config.session_compaction_idle_seconds - 1,
            )
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.client.run_turn.return_value = "response"

            with patch("bot.time.time", return_value=now):
                self.assertEqual(runner.run(-1001, "new message"), "response")

            runner.client.compact_thread.assert_not_called()
            runner.client.run_turn.assert_called_once()

    def test_active_turn_is_never_compacted_by_idle_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            runner = CodexRunner(config)
            now = 2_000_000_000.0
            runner.sessions.set(
                -1001,
                "session-one",
                now - config.session_compaction_idle_seconds - 1,
            )
            runner.client = MagicMock()
            runner.active_turns[-1001] = RunningCodexTurn(
                -1001,
                "session-one",
                CodexTurnHandle("session-one", "turn-one", queue.Queue()),
            )

            runner.compact_idle_sessions(now)

            runner.client.compact_thread.assert_not_called()

    def test_idle_compaction_scan_stops_when_bot_is_no_longer_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            runner = CodexRunner(config)
            now = 2_000_000_000.0
            for chat_id, session_id in ((-1001, "session-one"), (-1002, "session-two")):
                runner.sessions.set(
                    chat_id,
                    session_id,
                    now - config.session_compaction_idle_seconds - 1,
                )
            runner.client = MagicMock()
            runner.client.generation = 1
            waiting = True

            def finish_waiting(thread_id: str) -> None:
                nonlocal waiting
                waiting = False

            runner.client.compact_thread.side_effect = finish_waiting

            runner.compact_idle_sessions(now, can_continue=lambda: waiting)

            runner.client.compact_thread.assert_called_once_with("session-one")
            self.assertIsNone(runner.sessions.get_record(-1002).last_compacted_at)

    def test_seven_day_idle_session_is_archived_and_mapping_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            runner = CodexRunner(config)
            now = 2_000_000_000.0
            runner.sessions.set(
                -1001,
                "session-one",
                now - config.session_archive_idle_seconds - 1,
            )
            runner.client = MagicMock()
            runner.client.generation = 1

            runner.archive_stale_sessions(now)

            runner.client.archive_thread.assert_called_once_with("session-one")
            self.assertIsNone(runner.sessions.get(-1001))

    def test_each_chat_creates_and_resumes_its_own_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            steps: list[str] = []
            session_ids = iter(("session-one", "session-two"))

            def pull_git_repositories(repo_dir: Path, timeout: int) -> list[tuple[Path, str]]:
                steps.append("pull")
                return [(repo_dir, "Already up to date.")]

            def run_turn(*args: object, **kwargs: object) -> str:
                steps.append("codex")
                return "response"

            with patch(
                "bot.pull_configured_git_repositories",
                side_effect=pull_git_repositories,
            ) as pull:
                runner = CodexRunner(config)
                runner.client = MagicMock()
                runner.client.generation = 1
                runner.client.start_thread.side_effect = lambda: next(session_ids)
                runner.client.run_turn.side_effect = run_turn
                runner.run(-1001, "first", fast_mode=True)
                runner.run(-1001, "second")
                runner.run(-1002, "other chat")
                self.assertTrue(runner.git_puller.wait(1))

            self.assertEqual(pull.call_count, 1)
            self.assertEqual(steps.count("pull"), 1)
            self.assertEqual(steps.count("codex"), 3)
            self.assertEqual(runner.client.start_thread.call_count, 2)
            runner.client.resume_thread.assert_not_called()
            runner.client.unsubscribe_thread.assert_not_called()
            turn_calls = runner.client.run_turn.call_args_list
            self.assertEqual([call.args[0] for call in turn_calls], [
                "session-one",
                "session-one",
                "session-two",
            ])
            self.assertEqual([call.args[4] for call in turn_calls], [True, False, False])

            sessions = SessionStore(config.session_store_path)
            self.assertEqual(sessions.get(-1001), "session-one")
            self.assertEqual(sessions.get(-1002), "session-two")

    def test_session_prompt_factory_receives_the_current_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.git_puller = MagicMock()
            runner.sessions.set(-1001, "session-one")
            runner.sessions.record_bot_message(-1001, "session-one", 3001)
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.client.run_turn.return_value = "response"

            runner.run(
                -1001,
                lambda record: f"last={record.last_bot_message_id if record else None}",
            )

            self.assertEqual(runner.client.run_turn.call_args.args[1], "last=3001")

    def test_successful_turn_consumes_previous_response_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.git_puller = MagicMock()
            runner.sessions.set(-1001, "session-one")
            runner.sessions.record_response_error(
                -1001,
                "Telegram answerGuestQuery failed: query is too old",
            )
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.client.run_turn.return_value = "response"

            runner.run(
                -1001,
                lambda record: runner.previous_response_error(-1001) or "missing",
            )

            self.assertEqual(
                runner.client.run_turn.call_args.args[1],
                "Telegram answerGuestQuery failed: query is too old",
            )
            self.assertIsNone(runner.previous_response_error(-1001))

    def test_new_session_rebuilds_session_prompt_without_the_old_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.git_puller = MagicMock()
            runner.sessions.set(-1001, "archived-session")
            runner.sessions.record_bot_message(-1001, "archived-session", 3001)
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.subscription_generations["archived-session"] = 1
            runner.client.start_thread.return_value = "new-session"
            runner.client.run_turn.side_effect = [
                CodexAppServerError(
                    "archived thread",
                    request_method="turn/start",
                    error_code=-32600,
                    app_server_message=(
                        "session archived-session is archived. "
                        "Run `codex unarchive archived-session` to unarchive it first."
                    ),
                ),
                "response",
            ]

            runner.run(
                -1001,
                lambda record: f"last={record.last_bot_message_id if record else None}",
            )

            self.assertEqual(
                [call.args[1] for call in runner.client.run_turn.call_args_list],
                ["last=3001", "last=None"],
            )

    def test_external_archive_notification_removes_matching_session_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.sessions.set(-1001, "session-one")
            runner.sessions.set(1001, "session-one")
            runner.sessions.set(-1002, "session-two")
            runner.subscription_generations = {"session-one": 1, "session-two": 1}

            runner._handle_app_server_notification(
                {
                    "method": "thread/archived",
                    "params": {"threadId": "session-one"},
                },
            )

            self.assertIsNone(runner.sessions.get(-1001))
            self.assertIsNone(runner.sessions.get(1001))
            self.assertEqual(runner.sessions.get(-1002), "session-two")
            self.assertNotIn("session-one", runner.subscription_generations)
            self.assertIn("session-two", runner.subscription_generations)

    def test_missing_thread_during_resume_creates_a_new_session_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.git_puller = MagicMock()
            runner.sessions.set(-1001, "missing-session")
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.client.resume_thread.side_effect = CodexAppServerError(
                "missing thread",
                request_method="thread/resume",
                error_code=-32600,
                app_server_message="no rollout found for thread id missing-session",
            )
            runner.client.start_thread.return_value = "new-session"
            runner.client.run_turn.return_value = "response"

            self.assertEqual(runner.run(-1001, "prompt"), "response")

            self.assertEqual(runner.sessions.get(-1001), "new-session")
            runner.client.resume_thread.assert_called_once_with("missing-session")
            runner.client.start_thread.assert_called_once_with()
            runner.client.run_turn.assert_called_once_with(
                "new-session",
                "prompt",
                None,
                None,
                False,
            )

    def test_archived_thread_during_turn_start_creates_a_new_session_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.git_puller = MagicMock()
            runner.sessions.set(-1001, "archived-session")
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.subscription_generations["archived-session"] = 1
            runner.client.start_thread.return_value = "new-session"
            runner.client.run_turn.side_effect = [
                CodexAppServerError(
                    "archived thread",
                    request_method="turn/start",
                    error_code=-32600,
                    app_server_message=(
                        "session archived-session is archived. "
                        "Run `codex unarchive archived-session` to unarchive it first."
                    ),
                ),
                "response",
            ]

            self.assertEqual(runner.run(-1001, "prompt"), "response")

            self.assertEqual(runner.sessions.get(-1001), "new-session")
            runner.client.resume_thread.assert_not_called()
            self.assertEqual(
                [call.args[0] for call in runner.client.run_turn.call_args_list],
                ["archived-session", "new-session"],
            )

    def test_unrelated_app_server_error_does_not_replace_the_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(build_config(Path(temp_dir)))
            runner.git_puller = MagicMock()
            runner.sessions.set(-1001, "session-one")
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.client.resume_thread.side_effect = CodexAppServerError(
                "server unavailable",
                request_method="thread/resume",
                error_code=-32603,
                app_server_message="internal error",
            )

            with self.assertRaisesRegex(CodexAppServerError, "server unavailable"):
                runner.run(-1001, "prompt")

            self.assertEqual(runner.sessions.get(-1001), "session-one")
            runner.client.start_thread.assert_not_called()

    def test_private_chat_subscription_is_released_after_each_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            runner = CodexRunner(config)
            runner.git_puller = MagicMock()
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.client.start_thread.return_value = "private-session"
            runner.client.run_turn.return_value = "response"

            self.assertEqual(runner.run(1001, "first"), "response")
            self.assertEqual(runner.run(1001, "second"), "response")

            runner.client.start_thread.assert_called_once_with()
            runner.client.resume_thread.assert_called_once_with("private-session")
            self.assertEqual(
                runner.client.unsubscribe_thread.call_args_list,
                [call("private-session"), call("private-session")],
            )

    def test_private_chat_subscription_is_released_after_failed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            runner = CodexRunner(config)
            runner.git_puller = MagicMock()
            runner.client = MagicMock()
            runner.client.generation = 1
            runner.client.start_thread.return_value = "private-session"
            runner.client.run_turn.side_effect = RuntimeError("turn failed")

            with self.assertRaisesRegex(RuntimeError, "turn failed"):
                runner.run(1001, "first")

            runner.client.unsubscribe_thread.assert_called_once_with("private-session")

    def test_private_chat_compaction_releases_on_demand_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            runner = CodexRunner(config)
            now = 2_000_000_000.0
            runner.sessions.set(
                1001,
                "private-session",
                now - config.session_compaction_idle_seconds - 1,
            )
            runner.client = MagicMock()
            runner.client.generation = 1

            runner.compact_idle_sessions(now)

            runner.client.resume_thread.assert_called_once_with("private-session")
            runner.client.compact_thread.assert_called_once_with("private-session")
            runner.client.unsubscribe_thread.assert_called_once_with("private-session")

    def test_git_pull_failure_warns_and_continues_with_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            warnings: list[str] = []

            with (
                patch(
                    "bot.pull_configured_git_repositories",
                    side_effect=GitPullError("network unavailable"),
                ),
                self.assertLogs("bot", level="WARNING") as logs,
            ):
                runner = CodexRunner(config)
                runner.client = MagicMock()
                runner.client.generation = 1
                runner.client.start_thread.return_value = "session-one"
                runner.client.run_turn.return_value = "response"
                response = runner.run(
                    -1001,
                    "prompt",
                    agent_message_handler=warnings.append,
                )
                self.assertTrue(runner.git_puller.wait(1))

        self.assertEqual(response, "response")
        self.assertEqual(warnings, [GIT_PULL_WARNING_MESSAGE])
        runner.client.run_turn.assert_called_once()
        self.assertTrue(any("asynchronous Git update failed" in line for line in logs.output))

    def test_git_pull_failure_without_handler_continues_silently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))

            with (
                patch(
                    "bot.pull_configured_git_repositories",
                    side_effect=GitPullError("network unavailable"),
                ),
                self.assertLogs("bot", level="WARNING"),
            ):
                runner = CodexRunner(config)
                runner.client = MagicMock()
                runner.client.generation = 1
                runner.client.start_thread.return_value = "session-one"
                runner.client.run_turn.return_value = "response"
                response = runner.run(-1001, "prompt")
                self.assertTrue(runner.git_puller.wait(1))

        self.assertEqual(response, "response")
        runner.client.run_turn.assert_called_once()


class ConversationControllerTest(unittest.TestCase):
    class CapturingExecutor:
        def __init__(self) -> None:
            self.calls: list[tuple[Callable[..., object], tuple[object, ...]]] = []

        def submit(self, function: Callable[..., object], *args: object) -> MagicMock:
            self.calls.append((function, args))
            return MagicMock()

    def build_bot(self, temp_dir: Path) -> tuple[Bot, RunningCodexTurn]:
        bot = object.__new__(Bot)
        bot.config = build_config(temp_dir)
        bot.bot_user = {}
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.codex.previous_response_error.return_value = None
        bot.codex.take_queued_guest_stickers.return_value = ()
        bot.download_images = MagicMock(return_value=[])
        bot.download_files = MagicMock(return_value=[])
        bot.send_message_response = MagicMock(return_value=3001)
        bot.answer_guest_response = MagicMock(return_value=GuestResponseDelivery(True))
        bot.answer_guest_sticker_response = MagicMock(
            return_value=GuestResponseDelivery(True),
        )
        bot.keep_typing = MagicMock()
        bot.update_executor = self.CapturingExecutor()
        bot.turn_executor = self.CapturingExecutor()
        handle = CodexTurnHandle("thread-one", "turn-one", queue.Queue())
        running_turn = RunningCodexTurn(-1001, "thread-one", handle)
        bot.codex.begin_turn.return_value = running_turn
        return bot, running_turn

    def test_second_message_steers_the_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, running_turn = self.build_bot(Path(temp_dir))
            controller = ConversationController(bot, -1001)
            first = {
                "message_id": 1,
                "text": "first",
                "chat": {"id": -1001, "type": "supergroup"},
            }
            second = {
                "message_id": 2,
                "text": "second",
                "chat": {"id": -1001, "type": "supergroup"},
            }

            controller._process_incoming("message", first)
            controller._process_incoming("message", second)

            bot.codex.begin_turn.assert_called_once()
            bot.codex.steer_turn.assert_called_once()
            self.assertIs(bot.codex.steer_turn.call_args.args[0], running_turn)
            self.assertEqual(controller.target.message["message_id"], 1)
            self.assertFalse(bot.codex.begin_turn.call_args.kwargs["fast_mode"])
            self.assertEqual(
                bot.codex.begin_turn.call_args.kwargs["thread_name"],
                "Telegram chat -1001",
            )
            controller.shutdown()

    def test_image_generation_start_sends_one_upload_photo_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            controller = ConversationController(bot, -1001)
            message = {
                "message_id": 1,
                "message_thread_id": 42,
                "text": "generate an image",
                "chat": {"id": -1001, "type": "supergroup"},
            }

            controller._process_incoming("message", message)
            handler = bot.codex.begin_turn.call_args.kwargs[
                "image_generation_started_handler"
            ]
            self.assertTrue(callable(handler))
            self.assertEqual(
                bot.codex.begin_turn.call_args.kwargs["message_thread_id"],
                42,
            )

            handler()
            controller._drain()

            bot.codex.send_optional_chat_action.assert_called_once_with(
                -1001,
                "upload_photo",
            )
            controller.shutdown()

    def test_guest_turn_does_not_register_image_generation_chat_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            controller = ConversationController(
                bot,
                -1001,
                guest_conversation_key(-1001),
            )
            message = {
                "message_id": 1,
                "guest_query_id": "guest-one",
                "text": "generate an image",
                "date": time.time(),
                "chat": {"id": -1001, "type": "supergroup"},
            }

            controller._process_incoming("guest_message", message)

            self.assertIsNone(
                bot.codex.begin_turn.call_args.kwargs[
                    "image_generation_started_handler"
                ],
            )
            bot.codex.send_optional_chat_action.assert_not_called()
            controller.shutdown()

    def test_different_chats_start_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            bot.controllers = {}
            bot.controllers_lock = threading.Lock()
            bot.update_executor = ThreadPoolExecutor(max_workers=2)
            first_started = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()

            def begin_turn(chat_id: int, *args: object, **kwargs: object) -> RunningCodexTurn:
                if chat_id == -1001:
                    first_started.set()
                    release_first.wait(2)
                    turn_id = "turn-one"
                else:
                    second_started.set()
                    turn_id = "turn-two"
                return RunningCodexTurn(
                    chat_id,
                    f"thread-{chat_id}",
                    CodexTurnHandle(f"thread-{chat_id}", turn_id, queue.Queue()),
                )

            bot.codex.begin_turn.side_effect = begin_turn
            first_update = {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "text": "first",
                    "chat": {"id": -1001, "type": "supergroup"},
                },
            }
            second_update = {
                "update_id": 2,
                "message": {
                    "message_id": 2,
                    "text": "second",
                    "chat": {"id": -1002, "type": "supergroup"},
                },
            }
            try:
                bot.dispatch_update(first_update)
                self.assertTrue(first_started.wait(1))
                bot.dispatch_update(second_update)
                self.assertTrue(second_started.wait(1))
            finally:
                release_first.set()
                bot.update_executor.shutdown(wait=True)
                for controller in bot.controllers.values():
                    controller.shutdown()

    def test_added_reaction_to_latest_bot_message_starts_a_regular_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            bot.bot_user = {"id": 99, "is_bot": True, "first_name": "Codex"}
            bot.controllers = {}
            bot.controllers_lock = threading.Lock()
            conversation_key = telegram_conversation_key("message", -1001, 42)
            bot.codex.conversation_key_for_last_bot_message.return_value = conversation_key
            update = {
                "update_id": 10,
                "message_reaction": {
                    "chat": {"id": -1001, "type": "supergroup", "title": "Test"},
                    "message_id": 3001,
                    "user": {"id": 1001, "first_name": "Reactor"},
                    "date": 1785000000,
                    "new_reaction": [{"type": "emoji", "emoji": "👍"}],
                },
            }

            bot.dispatch_update(update)
            controller = bot.controllers[conversation_key]
            controller._drain()

            bot.codex.begin_turn.assert_called_once()
            self.assertEqual(bot.codex.begin_turn.call_args.args[0], conversation_key)
            self.assertFalse(bot.codex.begin_turn.call_args.kwargs["fast_mode"])
            self.assertEqual(bot.codex.begin_turn.call_args.kwargs["message_thread_id"], 42)
            self.assertTrue(
                callable(
                    bot.codex.begin_turn.call_args.kwargs[
                        "image_generation_started_handler"
                    ],
                ),
            )
            prompt_builder = bot.codex.begin_turn.call_args.args[1]
            prompt = prompt_builder(
                SessionRecord(
                    "thread-one",
                    1785000000,
                    last_bot_message_id=3001,
                ),
            )
            context = prompt_context(prompt)
            self.assertEqual(context["message_kind"], "message_reaction")
            self.assertNotIn("current_message", context)
            self.assertNotIn("reply_to_message", context)
            self.assertEqual(prompt_user_message(prompt), "👍")
            controller.shutdown()

    def test_reaction_removal_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            bot.controllers = {}
            bot.controllers_lock = threading.Lock()

            bot.dispatch_update(
                {
                    "update_id": 11,
                    "message_reaction": {
                        "chat": {"id": -1001, "type": "supergroup"},
                        "message_id": 3001,
                        "user": {"id": 1001, "first_name": "Reactor"},
                        "date": 1785000001,
                        "old_reaction": [{"type": "emoji", "emoji": "👍"}],
                    },
                },
            )

            self.assertEqual(bot.controllers, {})
            bot.codex.conversation_key_for_last_bot_message.assert_not_called()

    def test_reaction_to_untracked_message_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            bot.controllers = {}
            bot.controllers_lock = threading.Lock()
            bot.codex.conversation_key_for_last_bot_message.return_value = None

            bot.dispatch_update(
                {
                    "update_id": 12,
                    "message_reaction": {
                        "chat": {"id": -1001, "type": "supergroup"},
                        "message_id": 3002,
                        "user": {"id": 1001, "first_name": "Reactor"},
                        "date": 1785000002,
                        "new_reaction": [{"type": "emoji", "emoji": "❤️"}],
                    },
                },
            )

            self.assertEqual(bot.controllers, {})
            bot.codex.conversation_key_for_last_bot_message.assert_called_once_with(
                -1001,
                3002,
            )

    def test_guest_chat_does_not_share_controller_with_ordinary_chat_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            bot.controllers = {}
            bot.controllers_lock = threading.Lock()
            ordinary_update = {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "text": "ordinary",
                    "from": {"id": 1001},
                    "chat": {"id": 1001, "type": "private"},
                },
            }
            guest_update = {
                "update_id": 2,
                "guest_message": {
                    "message_id": 2,
                    "guest_query_id": "guest-query",
                    "text": "guest",
                    "from": {"id": 1001},
                    "chat": {"id": 1001, "type": "private"},
                },
            }

            bot.dispatch_update(ordinary_update)
            bot.dispatch_update(guest_update)

            guest_key = telegram_conversation_key("guest_message", 1001)
            self.assertEqual(set(bot.controllers), {1001, guest_key})
            self.assertIsNot(bot.controllers[1001], bot.controllers[guest_key])
            self.assertEqual(bot.controllers[guest_key].chat_id, 1001)
            self.assertEqual(bot.controllers[guest_key].conversation_key, guest_key)

            second_guest_update = {
                "update_id": 3,
                "guest_message": {
                    "message_id": 3,
                    "guest_query_id": "second-guest-query",
                    "text": "guest from the other participant",
                    "from": {"id": 1002},
                    "chat": {"id": 1001, "type": "private"},
                },
            }
            shared_guest_controller = bot.controllers[guest_key]
            bot.dispatch_update(second_guest_update)

            self.assertIs(bot.controllers[guest_key], shared_guest_controller)

    def test_forum_topics_use_independent_conversation_controllers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            bot.controllers = {}
            bot.controllers_lock = threading.Lock()
            first_update = {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "message_thread_id": 42,
                    "text": "first topic",
                    "from": {"id": 1001},
                    "chat": {"id": -1001, "type": "supergroup"},
                },
            }
            second_update = {
                "update_id": 2,
                "message": {
                    "message_id": 2,
                    "message_thread_id": 99,
                    "text": "second topic",
                    "from": {"id": 1001},
                    "chat": {"id": -1001, "type": "supergroup"},
                },
            }

            bot.dispatch_update(first_update)
            bot.dispatch_update(second_update)

            first_key = telegram_conversation_key("message", -1001, 42)
            second_key = telegram_conversation_key("message", -1001, 99)
            self.assertEqual(set(bot.controllers), {first_key, second_key})
            self.assertIsNot(bot.controllers[first_key], bot.controllers[second_key])
            self.assertEqual(bot.controllers[first_key].chat_id, -1001)
            self.assertEqual(bot.controllers[second_key].chat_id, -1001)
            self.assertEqual(len(bot.controllers), 2)
            for controller in bot.controllers.values():
                controller.shutdown()

    def test_guest_group_does_not_share_controller_with_ordinary_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            bot.controllers = {}
            bot.controllers_lock = threading.Lock()
            bot.dispatch_update(
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "text": "ordinary group message",
                        "from": {"id": 1001},
                        "chat": {"id": -1001, "type": "supergroup"},
                    },
                },
            )
            bot.dispatch_update(
                {
                    "update_id": 2,
                    "guest_message": {
                        "message_id": 2,
                        "guest_query_id": "group-guest-query",
                        "text": "guest group message",
                        "from": {"id": 1001},
                        "chat": {"id": -1001, "type": "supergroup"},
                    },
                },
            )

            guest_key = guest_conversation_key(-1001)
            self.assertEqual(set(bot.controllers), {-1001, guest_key})
            self.assertIsNot(bot.controllers[-1001], bot.controllers[guest_key])
            self.assertEqual(bot.controllers[guest_key].chat_id, -1001)
            for controller in bot.controllers.values():
                controller.shutdown()

    def test_guest_steer_flushes_only_available_intermediate_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, running_turn = self.build_bot(Path(temp_dir))
            controller = ConversationController(bot, -1001)

            def guest(message_id: int) -> dict[str, object]:
                return {
                    "message_id": message_id,
                    "guest_query_id": f"guest-{message_id}",
                    "text": f"message {message_id}",
                    "date": time.time(),
                    "chat": {"id": -1001, "type": "supergroup"},
                }

            controller._process_incoming("guest_message", guest(1))
            controller._process_agent_message(
                CodexAgentMessage("turn-one", "item-one", "commentary", "Working."),
            )
            controller._process_agent_message(
                CodexAgentMessage("turn-one", "call-one", "commentary", "👍"),
            )
            controller._process_incoming("guest_message", guest(2))

            bot.answer_guest_response.assert_called_once()
            self.assertEqual(bot.answer_guest_response.call_args.args[0], "guest-1")
            self.assertEqual(bot.answer_guest_response.call_args.args[1], "Working.\n\n👍")

            # There is no new commentary, so message 2 is deliberately left unanswered.
            controller._process_incoming("guest_message", guest(3))
            bot.answer_guest_response.assert_called_once()

            controller._process_turn_completed(running_turn, "Final answer.", None)
            self.assertEqual(bot.answer_guest_response.call_count, 2)
            self.assertEqual(bot.answer_guest_response.call_args.args[0], "guest-3")
            self.assertEqual(bot.answer_guest_response.call_args.args[1], "Final answer.")
            self.assertEqual(bot.codex.steer_turn.call_count, 2)
            controller.shutdown()

    def test_successful_empty_turn_does_not_send_a_text_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, running_turn = self.build_bot(Path(temp_dir))
            controller = ConversationController(bot, -1001)
            controller._process_incoming(
                "message",
                {
                    "message_id": 1,
                    "text": "react to this",
                    "chat": {"id": -1001, "type": "supergroup"},
                },
            )

            controller._process_turn_completed(running_turn, "", None)

            bot.send_message_response.assert_not_called()
            self.assertIsNone(controller.active_turn)
            controller.shutdown()

    def test_guest_turn_with_only_one_sticker_answers_with_sticker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, running_turn = self.build_bot(Path(temp_dir))
            sticker = QueuedTelegramSticker(
                "happy",
                Path(temp_dir) / "happy.webp",
                0,
            )
            bot.codex.take_queued_guest_stickers.return_value = (sticker,)
            controller = ConversationController(bot, -1001, guest_conversation_key(-1001))
            message = {
                "message_id": 1,
                "guest_query_id": "guest-one",
                "text": "send a sticker",
                "date": time.time(),
                "chat": {"id": -1001, "type": "supergroup"},
            }
            controller._process_incoming("guest_message", message)

            controller._process_turn_completed(running_turn, "", None)

            bot.answer_guest_sticker_response.assert_called_once_with(
                "guest-one",
                sticker.path,
                message,
                "response",
            )
            bot.answer_guest_response.assert_not_called()
            controller.shutdown()

    def test_one_pre_steer_sticker_can_answer_original_and_text_answers_steer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, running_turn = self.build_bot(Path(temp_dir))
            sticker = QueuedTelegramSticker(
                "happy",
                Path(temp_dir) / "happy.webp",
                0,
            )
            bot.codex.take_queued_guest_stickers.return_value = (sticker,)
            controller = ConversationController(bot, -1001, guest_conversation_key(-1001))

            def guest(message_id: int) -> dict[str, object]:
                return {
                    "message_id": message_id,
                    "guest_query_id": f"guest-{message_id}",
                    "text": f"message {message_id}",
                    "date": time.time(),
                    "chat": {"id": -1001, "type": "supergroup"},
                }

            first = guest(1)
            second = guest(2)
            controller._process_incoming("guest_message", first)
            controller._process_incoming("guest_message", second)
            controller._process_turn_completed(running_turn, "Final text.", None)

            bot.answer_guest_sticker_response.assert_called_once_with(
                "guest-1",
                sticker.path,
                first,
                "sticker response before steer",
            )
            bot.answer_guest_response.assert_called_once()
            self.assertEqual(bot.answer_guest_response.call_args.args[0], "guest-2")
            self.assertEqual(bot.answer_guest_response.call_args.args[1], "Final text.")
            controller.shutdown()

    def test_failed_split_sticker_delivery_embeds_it_in_steering_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, running_turn = self.build_bot(Path(temp_dir))
            sticker_path = Path(temp_dir) / "happy.webp"
            sticker = QueuedTelegramSticker("happy", sticker_path, 0)
            bot.codex.take_queued_guest_stickers.return_value = (sticker,)
            bot.answer_guest_sticker_response.return_value = GuestResponseDelivery(
                False,
                RuntimeError("guest query expired"),
            )
            controller = ConversationController(bot, -1001, guest_conversation_key(-1001))

            def guest(message_id: int) -> dict[str, object]:
                return {
                    "message_id": message_id,
                    "guest_query_id": f"guest-{message_id}",
                    "text": f"message {message_id}",
                    "date": time.time(),
                    "chat": {"id": -1001, "type": "supergroup"},
                }

            controller._process_incoming("guest_message", guest(1))
            controller._process_incoming("guest_message", guest(2))
            controller._process_turn_completed(running_turn, "Final text.", None)

            self.assertEqual(bot.answer_guest_response.call_args.args[0], "guest-2")
            response = bot.answer_guest_response.call_args.args[1]
            self.assertTrue(response.startswith(f"![happy]({sticker_path.as_uri()})"))
            self.assertTrue(response.endswith("Final text."))
            self.assertIn(sticker_path.resolve(), bot.answer_guest_response.call_args.args[4])
            controller.shutdown()

    def test_sticker_queued_after_steer_is_embedded_in_current_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, running_turn = self.build_bot(Path(temp_dir))
            sticker_path = Path(temp_dir) / "happy.webp"
            sticker = QueuedTelegramSticker("happy", sticker_path, 1)
            bot.codex.take_queued_guest_stickers.return_value = (sticker,)
            controller = ConversationController(bot, -1001, guest_conversation_key(-1001))

            def guest(message_id: int) -> dict[str, object]:
                return {
                    "message_id": message_id,
                    "guest_query_id": f"guest-{message_id}",
                    "text": f"message {message_id}",
                    "date": time.time(),
                    "chat": {"id": -1001, "type": "supergroup"},
                }

            controller._process_incoming("guest_message", guest(1))
            controller._process_incoming("guest_message", guest(2))
            controller._process_turn_completed(running_turn, "Final text.", None)

            bot.answer_guest_sticker_response.assert_not_called()
            self.assertEqual(bot.answer_guest_response.call_args.args[0], "guest-2")
            self.assertTrue(
                bot.answer_guest_response.call_args.args[1].startswith(
                    f"![happy]({sticker_path.as_uri()})",
                ),
            )
            controller.shutdown()

    def test_ordinary_steer_keeps_first_query_as_delivery_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            controller = ConversationController(bot, -1001)
            first = {
                "message_id": 1,
                "text": "first",
                "chat": {"id": -1001, "type": "supergroup"},
            }
            second = {
                "message_id": 2,
                "text": "second",
                "chat": {"id": -1001, "type": "supergroup"},
            }

            controller._process_incoming("message", first)
            controller._process_agent_message(
                CodexAgentMessage("turn-one", "item-one", "commentary", "First progress."),
            )
            controller._process_incoming("message", second)
            controller._process_agent_message(
                CodexAgentMessage("turn-one", "item-two", "commentary", "Second progress."),
            )
            controller._process_agent_message(
                CodexAgentMessage("turn-one", "item-three", "final_answer", "Final."),
            )

            reply_ids = [
                call.kwargs["reply_to_original"]
                for call in bot.send_message_response.call_args_list
            ]
            message_ids = [call.args[2] for call in bot.send_message_response.call_args_list]
            self.assertEqual(message_ids, [1, 1, 1])
            self.assertEqual(reply_ids, [False, False, False])
            controller.shutdown()

    def test_unsteered_ordinary_turn_does_not_reply_to_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            controller = ConversationController(bot, -1001)
            message = {
                "message_id": 1,
                "text": "question",
                "chat": {"id": -1001, "type": "supergroup"},
            }

            controller._process_incoming("message", message)
            controller._process_agent_message(
                CodexAgentMessage("turn-one", "item-one", "final_answer", "Answer."),
            )

            bot.send_message_response.assert_called_once()
            self.assertFalse(
                bot.send_message_response.call_args.kwargs["reply_to_original"],
            )
            controller.shutdown()

    def test_first_agent_message_after_ordinary_steer_replies_to_first_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, _ = self.build_bot(Path(temp_dir))
            controller = ConversationController(bot, -1001)
            first = {
                "message_id": 1,
                "text": "first",
                "chat": {"id": -1001, "type": "supergroup"},
            }
            second = {
                "message_id": 2,
                "text": "second",
                "chat": {"id": -1001, "type": "supergroup"},
            }

            controller._process_incoming("message", first)
            controller._process_incoming("message", second)
            controller._process_agent_message(
                CodexAgentMessage("turn-one", "item-one", "commentary", "Progress."),
            )

            bot.send_message_response.assert_called_once()
            self.assertEqual(bot.send_message_response.call_args.args[2], 1)
            self.assertTrue(
                bot.send_message_response.call_args.kwargs["reply_to_original"],
            )
            controller.shutdown()

    def test_steer_completion_race_starts_a_new_turn_without_dropping_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot, first_turn = self.build_bot(Path(temp_dir))
            second_turn = RunningCodexTurn(
                -1001,
                "thread-one",
                CodexTurnHandle("thread-one", "turn-two", queue.Queue()),
            )
            bot.codex.begin_turn.side_effect = [first_turn, second_turn]
            bot.codex.steer_turn.side_effect = CodexTurnInactiveError("turn completed")
            controller = ConversationController(bot, -1001)
            first = {
                "message_id": 1,
                "text": "first",
                "chat": {"id": -1001, "type": "supergroup"},
            }
            second = {
                "message_id": 2,
                "text": "second",
                "chat": {"id": -1001, "type": "supergroup"},
            }

            controller._process_incoming("message", first)
            controller._process_incoming("message", second)
            self.assertEqual(len(controller.pending_after_turn), 1)

            controller._process_turn_completed(first_turn, "First answer.", None)

            self.assertEqual(bot.codex.begin_turn.call_count, 2)
            self.assertIs(controller.active_turn, second_turn)
            self.assertEqual(controller.target.message["message_id"], 2)
            controller.shutdown()


class GitPullTest(unittest.TestCase):
    def test_asynchronous_pull_is_nonblocking_and_cached_for_five_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            pull_started = threading.Event()
            release_pull = threading.Event()

            def pull_git_repositories(repo_dir: Path, timeout: int) -> list[tuple[Path, str]]:
                pull_started.set()
                release_pull.wait(1)
                return [(repo_dir, "Already up to date.")]

            with patch(
                "bot.pull_configured_git_repositories",
                side_effect=pull_git_repositories,
            ) as pull:
                puller = AsyncGitPuller(config)
                self.assertTrue(puller.start_if_due())
                self.assertTrue(pull_started.wait(1))
                self.assertFalse(puller.start_if_due())
                release_pull.set()
                self.assertTrue(puller.wait(1))
                self.assertFalse(puller.start_if_due())

            pull.assert_called_once_with(config.repo_dir, config.git_pull_timeout_seconds)

    def test_configured_writable_roots_use_project_config_and_resolve_from_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            repo_dir = base_dir / "primary"
            codex_home = base_dir / "codex-home"
            (repo_dir / ".codex").mkdir(parents=True)
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                '[sandbox_workspace_write]\nwritable_roots = ["/global/repo"]\n',
                encoding="utf-8",
            )
            (repo_dir / ".codex" / "config.toml").write_text(
                '[sandbox_workspace_write]\nwritable_roots = ["../secondary", "../secondary"]\n',
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                roots = configured_writable_roots(repo_dir)

        self.assertEqual(roots, [(base_dir / "secondary").resolve()])

    def test_invalid_writable_roots_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            config_dir = repo_dir / ".codex"
            config_dir.mkdir()
            (config_dir / "config.toml").write_text(
                '[sandbox_workspace_write]\nwritable_roots = "../secondary"\n',
                encoding="utf-8",
            )

            with (
                patch.dict(os.environ, {"CODEX_HOME": str(repo_dir / "no-user-config")}),
                self.assertRaisesRegex(RuntimeError, "must be an array of paths"),
            ):
                configured_writable_roots(repo_dir)

    def test_repository_root_is_detected_with_git(self) -> None:
        completed = subprocess.CompletedProcess(
            ["git", "rev-parse"],
            0,
            stdout="/repo\n",
            stderr="",
        )

        with (
            patch("bot.Path.is_dir", return_value=True),
            patch("bot.subprocess.run", return_value=completed) as run,
        ):
            root = git_repository_root(Path("/repo/subdirectory"), 120)

        self.assertEqual(root, Path("/repo"))
        self.assertEqual(run.call_args.args[0], ["git", "rev-parse", "--show-toplevel"])
        self.assertEqual(run.call_args.kwargs["cwd"], Path("/repo/subdirectory"))

    def test_writable_root_repositories_are_pulled_once_and_non_git_roots_are_skipped(self) -> None:
        primary = Path("/primary")
        nested_primary = Path("/primary/nested")
        secondary = Path("/secondary")
        non_git = Path("/not-a-repository")
        repository_roots = {
            primary: primary,
            nested_primary: primary,
            secondary: secondary,
            non_git: None,
        }

        with (
            patch(
                "bot.configured_writable_roots",
                return_value=[nested_primary, secondary, secondary, non_git],
            ),
            patch(
                "bot.git_repository_root",
                side_effect=lambda path, timeout: repository_roots[path],
            ),
            patch(
                "bot.pull_current_git_branch",
                side_effect=lambda path, timeout: f"updated {path}",
            ) as pull,
        ):
            results = pull_configured_git_repositories(primary, 120)

        self.assertEqual(
            results,
            [
                (primary, "updated /primary"),
                (secondary, "updated /secondary"),
            ],
        )
        self.assertEqual(
            [call.args[0] for call in pull.call_args_list],
            [primary, secondary],
        )

    def test_pull_uses_current_branch_with_fast_forward_only(self) -> None:
        completed = subprocess.CompletedProcess(
            ["git", "pull"],
            0,
            stdout="Already up to date.\n",
            stderr="",
        )

        with patch("bot.subprocess.run", return_value=completed) as run:
            output = pull_current_git_branch(Path("/repo"), 120)

        self.assertEqual(output, "Already up to date.")
        self.assertEqual(
            run.call_args.args[0],
            ["git", "pull", "--ff-only", "--no-tags", "--recurse-submodules=yes"],
        )
        self.assertEqual(run.call_args.kwargs["cwd"], Path("/repo"))
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_pull_failure_raises_git_pull_error_with_diagnostics(self) -> None:
        completed = subprocess.CompletedProcess(
            ["git", "pull"],
            1,
            stdout="",
            stderr="fatal: not possible to fast-forward\n",
        )

        with (
            patch("bot.subprocess.run", return_value=completed),
            self.assertRaisesRegex(GitPullError, "not possible to fast-forward"),
        ):
            pull_current_git_branch(Path("/repo"), 120)


class CodexProgressLoggingTest(unittest.TestCase):
    def test_image_generation_start_invokes_progress_handler_once(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        event_queue: queue.Queue[object] = queue.Queue()
        generation_started = MagicMock()
        handle = CodexTurnHandle(
            "thread-one",
            "turn-one",
            event_queue,
            image_generation_started_handler=generation_started,
        )
        client._unregister_notification_queue = MagicMock()
        event_queue.put(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "item": {
                        "id": "image-one",
                        "type": "imageGeneration",
                    },
                },
            },
        )
        event_queue.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-one", "status": "completed"},
                },
            },
        )

        client.wait_turn(handle)

        generation_started.assert_called_once_with()

    def test_image_generation_completion_forwards_only_saved_path_metadata(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        event_queue: queue.Queue[object] = queue.Queue()
        generated_images: list[CodexGeneratedImage] = []
        handle = CodexTurnHandle(
            "thread-one",
            "turn-one",
            event_queue,
            generated_image_handler=generated_images.append,
        )
        client._unregister_notification_queue = MagicMock()
        event_queue.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "item": {
                        "id": "image-one",
                        "type": "imageGeneration",
                        "savedPath": "/tmp/generated.png",
                        "result": "ignored-image-bytes",
                    },
                },
            },
        )
        event_queue.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-one", "status": "completed"},
                },
            },
        )

        client.wait_turn(handle)

        self.assertEqual(
            generated_images,
            [CodexGeneratedImage("turn-one", "image-one", Path("/tmp/generated.png"))],
        )

    def test_compaction_waits_for_turn_completion_after_item_completion(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        event_queue: queue.Queue[object] = queue.Queue()
        client._register_notification_queue = MagicMock(return_value=event_queue)
        client._unregister_notification_queue = MagicMock()
        client.request = MagicMock(return_value={})
        event_queue.put(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "thread-one",
                    "turn": {"id": "compact-turn", "status": "inProgress"},
                },
            },
        )
        event_queue.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-one",
                    "item": {"id": "compact-item", "type": "contextCompaction"},
                },
            },
        )
        event_queue.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-one",
                    "turn": {
                        "id": "compact-turn",
                        "status": "failed",
                        "error": {"message": "compaction finalization failed"},
                    },
                },
            },
        )

        with self.assertRaisesRegex(CodexAppServerError, "finalization failed"):
            client.compact_thread("thread-one")

        client.request.assert_called_once_with(
            "thread/compact/start",
            {"threadId": "thread-one"},
        )
        client._unregister_notification_queue.assert_called_once_with(
            "thread-one",
            event_queue,
        )

    def test_compaction_returns_after_its_turn_completes(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        event_queue: queue.Queue[object] = queue.Queue()
        client._register_notification_queue = MagicMock(return_value=event_queue)
        client._unregister_notification_queue = MagicMock()
        client.request = MagicMock(return_value={})
        for notification in (
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-one",
                    "turn": {"id": "unrelated-turn", "status": "completed"},
                },
            },
            {
                "method": "turn/started",
                "params": {
                    "threadId": "thread-one",
                    "turn": {"id": "compact-turn", "status": "inProgress"},
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-one",
                    "item": {"id": "compact-item", "type": "contextCompaction"},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-one",
                    "turn": {"id": "compact-turn", "status": "completed"},
                },
            },
        ):
            event_queue.put(notification)

        client.compact_thread("thread-one")

        client._unregister_notification_queue.assert_called_once_with(
            "thread-one",
            event_queue,
        )
        self.assertTrue(event_queue.empty())

    def test_thread_start_and_resume_use_project_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir).resolve()
            instructions_path = repo_dir / "custom-instructions.md"
            instructions_path.write_text("Telegram bot instructions", encoding="utf-8")
            for read_only, sandbox, approval_policy in (
                (True, "read-only", "never"),
                (False, "workspace-write", "on-request"),
            ):
                with self.subTest(read_only=read_only):
                    client = CodexAppServerClient(
                        build_config(
                            repo_dir,
                            read_only=read_only,
                            instructions_file=instructions_path,
                        ),
                    )
                    client.request = MagicMock(
                        side_effect=[
                            {"thread": {"id": "thread-one"}},
                            {"thread": {"id": "thread-one"}},
                        ],
                    )

                    self.assertEqual(client.start_thread(), "thread-one")
                    client.resume_thread("thread-one")

                    start_params = client.request.call_args_list[0].args[1]
                    resume_params = client.request.call_args_list[1].args[1]
                    for params in (start_params, resume_params):
                        self.assertEqual(params["cwd"], str(repo_dir))
                        self.assertEqual(params["sandbox"], sandbox)
                        self.assertEqual(params["approvalPolicy"], approval_policy)
                        if read_only:
                            self.assertNotIn("approvalsReviewer", params)
                        else:
                            self.assertEqual(params["approvalsReviewer"], "auto_review")
                        self.assertEqual(
                            params["baseInstructions"],
                            "Telegram bot instructions",
                        )
                        self.assertEqual(params["config"]["web_search"], "live")
                        self.assertFalse(
                            params["config"]["mcp_servers"]["openaiDeveloperDocs"]["enabled"],
                        )
                    self.assertEqual(start_params["threadSource"], "codex_telegram_bot")
                    self.assertEqual(resume_params["threadId"], "thread-one")

    def test_unconfigured_instructions_omit_override_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir).resolve()
            legacy_path = repo_dir / "tools" / "codex_telegram_bot" / "instructions.md"
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_text("Unconfigured legacy instructions", encoding="utf-8")
            client = CodexAppServerClient(build_config(repo_dir))
            client.request = MagicMock(return_value={"thread": {"id": "thread-one"}})

            with patch("bot.logger.warning") as warning:
                self.assertEqual(client.start_thread(), "thread-one")
                client.resume_thread("thread-one")

        warning.assert_not_called()
        for request_call in client.request.call_args_list:
            self.assertNotIn("baseInstructions", request_call.args[1])

    def test_missing_configured_instructions_fall_back_to_standard_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir).resolve()
            missing_path = repo_dir / "missing-instructions.md"
            client = CodexAppServerClient(
                build_config(repo_dir, instructions_file=missing_path),
            )
            client.request = MagicMock(return_value={"thread": {"id": "thread-one"}})

            with self.assertLogs("bot", level="WARNING") as logs:
                self.assertEqual(client.start_thread(), "thread-one")
                client.resume_thread("thread-one")

        for request_call in client.request.call_args_list:
            self.assertNotIn("baseInstructions", request_call.args[1])
        self.assertTrue(
            any(
                "instructions file" in message
                and str(missing_path) in message
                and "using standard Codex instructions" in message
                for message in logs.output
            ),
        )

    def test_unreadable_configured_instructions_fail_before_server_request(self) -> None:
        for method in ("start_thread", "resume_thread"):
            with self.subTest(method=method):
                client = CodexAppServerClient(
                    build_config(
                        Path("/repo"),
                        instructions_file=Path("/repo/protected-instructions.md"),
                    ),
                )
                client.request = MagicMock()
                with patch(
                    "bot.Path.read_text",
                    side_effect=PermissionError("permission denied"),
                ):
                    with self.assertRaisesRegex(
                        CodexAppServerError,
                        "cannot read Codex bot instructions",
                    ) as raised:
                        if method == "start_thread":
                            client.start_thread()
                        else:
                            client.resume_thread("thread-one")

                self.assertIsInstance(raised.exception.__cause__, PermissionError)
                client.request.assert_not_called()

    def test_thread_name_uses_dedicated_app_server_method(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        client.request = MagicMock(return_value={})

        client.set_thread_name("thread-one", "Direct ISC team")

        client.request.assert_called_once_with(
            "thread/name/set",
            {
                "threadId": "thread-one",
                "name": "Direct ISC team",
            },
        )

    def test_thread_configuration_includes_dynamic_tools_when_requested(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        client.request = MagicMock(return_value={"thread": {"id": "thread-one"}})
        tools = [telegram_reaction_tool_spec()]

        client.start_thread(tools)

        self.assertEqual(client.request.call_args.args[1]["dynamicTools"], tools)

    def test_dynamic_tool_server_request_is_answered(self) -> None:
        response_sent = threading.Event()
        action_completed = threading.Event()
        execution_order: list[str] = []

        def execute_action(params: dict[str, object]) -> dict[str, object]:
            execution_order.append("action")
            action_completed.set()
            return {"success": True, "contentItems": []}

        handler = MagicMock(side_effect=execute_action)
        client = CodexAppServerClient(
            build_config(Path("/repo")),
            server_request_handler=handler,
        )

        def send_response(message: dict[str, object]) -> None:
            execution_order.append("response")
            response_sent.set()

        client._write_started = MagicMock(side_effect=send_response)
        params = {
            "threadId": "thread-one",
            "turnId": "turn-one",
            "callId": "call-one",
            "tool": TELEGRAM_REACTION_TOOL_NAME,
            "namespace": None,
            "arguments": {"message_id": 2397, "reaction": "👍"},
        }

        client._route_message({"id": 17, "method": "item/tool/call", "params": params})

        self.assertTrue(response_sent.wait(1))
        self.assertTrue(action_completed.wait(1))
        self.assertEqual(execution_order, ["response", "action"])
        handler.assert_called_once_with(params)
        client._write_started.assert_called_once_with(
            {
                "id": 17,
                "result": {
                    "success": True,
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": (
                                "Accepted react_to_telegram_message for asynchronous "
                                "execution."
                            ),
                        },
                    ],
                },
            },
        )

    def test_interactive_user_input_is_redirected_to_final_response(self) -> None:
        handler = MagicMock()
        client = CodexAppServerClient(
            build_config(Path("/repo")),
            server_request_handler=handler,
        )
        client._write_started = MagicMock()

        client._route_message(
            {
                "id": 18,
                "method": "item/tool/requestUserInput",
                "params": {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "itemId": "item-one",
                    "isBlocking": True,
                    "questions": [],
                },
            },
        )

        client._write_started.assert_called_once_with(
            {
                "id": 18,
                "error": {
                    "code": -32601,
                    "message": (
                        "Interactive user input is unavailable for Telegram-initiated "
                        "turns. Ask the question in your final response and end the turn."
                    ),
                },
            },
        )
        handler.assert_not_called()

    def test_successful_dynamic_tool_suppresses_missing_final_message(self) -> None:
        client = CodexAppServerClient(
            build_config(Path("/repo")),
            server_request_handler=lambda params: {
                "success": True,
                "contentItems": [],
            },
        )
        client._write_started = MagicMock()
        client._unregister_notification_queue = MagicMock()
        event_queue: queue.Queue[object] = queue.Queue()
        handle = CodexTurnHandle("thread-one", "turn-one", event_queue)

        client._handle_server_request(
            17,
            {
                "threadId": "thread-one",
                "turnId": "turn-one",
                "callId": "call-one",
                "tool": TELEGRAM_REACTION_TOOL_NAME,
                "namespace": None,
                "arguments": {"message_id": 2397, "reaction": "👍"},
            },
        )
        event_queue.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "item": {
                        "id": "commentary-one",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "I will react now.",
                    },
                },
            },
        )
        event_queue.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-one", "status": "completed"},
                },
            },
        )

        self.assertEqual(client.wait_turn(handle), "")
        self.assertNotIn("turn-one", client.successful_dynamic_tool_turns)

    def test_dynamic_tool_exception_after_acknowledgement_is_only_logged(self) -> None:
        def fail_action(params: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("Telegram is unavailable")

        client = CodexAppServerClient(
            build_config(Path("/repo")),
            server_request_handler=fail_action,
        )
        client._write_started = MagicMock()
        params = {
            "threadId": "thread-one",
            "turnId": "turn-one",
            "callId": "call-one",
            "tool": TELEGRAM_REACTION_TOOL_NAME,
            "namespace": None,
            "arguments": {"message_id": 2397, "reaction": "👍"},
        }

        with self.assertLogs("bot", level="ERROR") as logs:
            client._handle_server_request(17, params)

        client._write_started.assert_called_once()
        self.assertTrue(client._write_started.call_args.args[0]["result"]["success"])
        self.assertTrue(
            any("failed after acknowledgement" in message for message in logs.output),
        )

    def test_completed_non_commentary_message_is_tracked_for_dynamic_tools(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))

        client._route_message(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "item": {
                        "id": "answer-one",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Already answered.",
                    },
                },
            },
        )

        self.assertTrue(client.turn_has_non_commentary_agent_text("turn-one"))

    def test_accepted_dynamic_tool_failure_suppresses_missing_final_message(self) -> None:
        client = CodexAppServerClient(
            build_config(Path("/repo")),
            server_request_handler=lambda params: {
                "success": False,
                "contentItems": [],
            },
        )
        client._write_started = MagicMock()
        client._unregister_notification_queue = MagicMock()
        event_queue: queue.Queue[object] = queue.Queue()
        handle = CodexTurnHandle("thread-one", "turn-one", event_queue)

        with self.assertLogs("bot", level="WARNING"):
            client._handle_server_request(
                17,
                {
                    "threadId": "thread-one",
                    "turnId": "turn-one",
                    "callId": "call-one",
                    "tool": TELEGRAM_REACTION_TOOL_NAME,
                    "namespace": None,
                    "arguments": {"message_id": 2397, "reaction": "👍"},
                },
            )
        event_queue.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-one", "status": "completed"},
                },
            },
        )

        self.assertEqual(client.wait_turn(handle), "")

    def test_initialize_enables_experimental_app_server_api(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        connection = MagicMock()
        connection.connected = True

        def connect() -> None:
            client.connection = connection

        client._connect = MagicMock(side_effect=connect)
        client._request_started = MagicMock(return_value={})
        client._notify_started = MagicMock()

        client.start()

        initialize_params = client._request_started.call_args.args[1]
        self.assertEqual(initialize_params["capabilities"], {"experimentalApi": True})

    def test_app_server_client_connects_to_configured_unix_socket(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        unix_socket = MagicMock()
        connection = MagicMock()
        connection.connected = True

        with (
            patch("bot.socket.socket", return_value=unix_socket) as socket_factory,
            patch("bot.websocket.create_connection", return_value=connection) as connect,
            patch("bot.threading.Thread") as thread,
        ):
            client._connect()

        socket_factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        unix_socket.connect.assert_called_once_with("/repo/app-server.sock")
        connect.assert_called_once_with(
            "ws://localhost/",
            socket=unix_socket,
            enable_multithread=True,
            suppress_origin=True,
            timeout=10,
        )
        connection.settimeout.assert_called_once_with(None)
        thread.return_value.start.assert_called_once_with()

    def test_notifications_are_forwarded_to_the_lifecycle_handler(self) -> None:
        notifications: list[dict[str, object]] = []
        client = CodexAppServerClient(
            build_config(Path("/repo")),
            notifications.append,
        )
        notification = {
            "method": "thread/deleted",
            "params": {"threadId": "thread-one"},
        }

        client._route_message(notification)

        self.assertEqual(notifications, [notification])

    def test_agent_message_deltas_are_logged_at_debug_only(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        with self.assertLogs("bot", level="DEBUG") as logs:
            client._route_message(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "thread-one", "delta": "partial"},
                },
            )

        delta_logs = [line for line in logs.output if "agent message delta" in line]
        self.assertEqual(len(delta_logs), 1)
        self.assertTrue(delta_logs[0].startswith("DEBUG:"))

    def test_unavailable_thread_error_requires_an_exact_terminal_error(self) -> None:
        archived = CodexAppServerError(
            "archived",
            request_method="thread/resume",
            error_code=-32600,
            app_server_message="session thread-one is archived. Unarchive it first.",
        )
        unrelated = CodexAppServerError(
            "unrelated",
            request_method="thread/resume",
            error_code=-32600,
            app_server_message="thread thread-one is busy",
        )

        self.assertTrue(is_unavailable_thread_error(archived, "thread-one"))
        self.assertFalse(is_unavailable_thread_error(unrelated, "thread-one"))

    def test_archiving_unsubscribes_the_thread(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        client.request = MagicMock(return_value={})

        client.archive_thread("thread-one")

        self.assertEqual(
            [(call.args[0], call.args[1]) for call in client.request.call_args_list],
            [
                ("thread/archive", {"threadId": "thread-one"}),
                ("thread/unsubscribe", {"threadId": "thread-one"}),
            ],
        )

    def test_event_summaries_include_messages_and_tools(self) -> None:
        message = summarize_app_server_event(
            {
                "method": "item/completed",
                "params": {
                    "item": {"type": "agentMessage", "text": "Checking the implementation."},
                },
            },
        )
        tool = summarize_app_server_event(
            {
                "method": "item/started",
                "params": {
                    "item": {"type": "mcpToolCall", "server": "oracle", "tool": "query"},
                },
            },
        )

        self.assertEqual(message, "agentMessage completed: Checking the implementation.")
        self.assertEqual(tool, "tool started: oracle.query")

    def test_app_server_turn_forwards_messages_and_uses_fast_tier_and_local_images(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        client.start = MagicMock()
        messages: list[str] = []
        requests: list[tuple[str, dict[str, object]]] = []

        def request(method: str, params: dict[str, object], **kwargs: object) -> object:
            requests.append((method, params))
            if method == "turn/start":
                client._route_message(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-one",
                            "turnId": "turn-one",
                            "item": {
                                "type": "agentMessage",
                                "text": "Progress update.",
                                "phase": "commentary",
                            },
                        },
                    },
                )
                client._route_message(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-one",
                            "turnId": "turn-one",
                            "item": {
                                "type": "agentMessage",
                                "text": "Final answer.",
                                "phase": "final_answer",
                            },
                        },
                    },
                )
                client._route_message(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-one",
                            "turn": {"id": "turn-one", "status": "completed"},
                        },
                    },
                )
                return {"turn": {"id": "turn-one"}}
            raise AssertionError(method)

        client.request = request
        response = client.run_turn(
            "thread-one",
            ["<telegram_context>\n{}\n</telegram_context>", "Inspect this"],
            [Path("/repo/image.png")],
            messages.append,
            True,
        )

        self.assertEqual(response, "Final answer.")
        self.assertEqual(messages, ["Progress update.", "Final answer."])
        params = requests[0][1]
        self.assertEqual(params["serviceTier"], "fast")
        self.assertEqual(
            params["input"],
            [
                {
                    "type": "text",
                    "text": "<telegram_context>\n{}\n</telegram_context>",
                },
                {"type": "text", "text": "Inspect this"},
                {"type": "localImage", "path": "/repo/image.png"},
            ],
        )

    def test_turn_start_uses_configured_permissions_for_ordinary_and_guest_turns(self) -> None:
        for read_only in (True, False):
            for fast_mode in (False, True):
                with self.subTest(read_only=read_only, guest=fast_mode):
                    client = CodexAppServerClient(
                        build_config(Path("/repo"), read_only=read_only),
                    )
                    client.start = MagicMock()
                    client.request = MagicMock(return_value={"turn": {"id": "turn-one"}})

                    client.start_turn("thread-one", "hello", [], None, fast_mode)

                    method, params = client.request.call_args.args
                    self.assertEqual(method, "turn/start")
                    self.assertEqual(params["threadId"], "thread-one")
                    self.assertEqual(params["cwd"], "/repo")
                    self.assertEqual(params["serviceTier"], "fast" if fast_mode else None)
                    if read_only:
                        self.assertEqual(params["approvalPolicy"], "never")
                        self.assertEqual(params["sandboxPolicy"], {"type": "readOnly"})
                        self.assertNotIn("approvalsReviewer", params)
                    else:
                        self.assertEqual(params["approvalPolicy"], "on-request")
                        self.assertEqual(params["approvalsReviewer"], "auto_review")
                        self.assertNotIn("sandboxPolicy", params)

    def test_normal_turn_explicitly_clears_fast_tier(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        client.start = MagicMock()
        event_queue: queue.Queue[object] = queue.Queue()
        client._register_notification_queue = MagicMock(return_value=event_queue)
        client._unregister_notification_queue = MagicMock()
        client.request = MagicMock(return_value={"turn": {"id": "turn-one"}})
        event_queue.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-one", "status": "completed"},
                },
            },
        )

        client.run_turn("thread-one", "hello", [], None, False)

        self.assertIsNone(client.request.call_args.args[1]["serviceTier"])

    def test_app_server_steer_targets_the_active_turn_without_turn_overrides(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        client.request = MagicMock(return_value={"turnId": "turn-one"})
        handle = CodexTurnHandle(
            "thread-one",
            "turn-one",
            queue.Queue(),
        )

        client.steer_turn(
            handle,
            ["<telegram_context>context</telegram_context>", "follow up"],
            [Path("/repo/image.png")],
        )

        client.request.assert_called_once_with(
            "turn/steer",
            {
                "threadId": "thread-one",
                "expectedTurnId": "turn-one",
                "input": [
                    {
                        "type": "text",
                        "text": "<telegram_context>context</telegram_context>",
                    },
                    {"type": "text", "text": "follow up"},
                    {"type": "localImage", "path": "/repo/image.png"},
                ],
            },
        )

    def test_app_server_steer_turn_mismatch_is_reported_as_inactive(self) -> None:
        client = CodexAppServerClient(build_config(Path("/repo")))
        client.request = MagicMock(return_value={"turnId": "turn-two"})
        handle = CodexTurnHandle("thread-one", "turn-one", queue.Queue())

        with self.assertRaises(CodexTurnInactiveError):
            client.steer_turn(handle, "follow up", [])

    def test_log_text_redacts_pan_and_credentials(self) -> None:
        text = safe_log_text("card=4111 1111 1111 1111 token=super-secret")

        self.assertEqual(text, "card=[REDACTED PAN] token=[REDACTED]")

    def test_successful_guest_delivery_is_logged(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.codex.run.return_value = "response"
        bot.bot_user = {}
        message = {
            "message_id": 2397,
            "guest_query_id": "guest-query",
            "text": "status",
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="INFO") as logs:
            bot.handle_guest_message(message)

        self.assertTrue(
            any(
                "sent rich response for guest message 2397 to chat -1001" in line
                for line in logs.output
            ),
        )
        self.assertNotIn("agent_message_handler", bot.codex.run.call_args.kwargs)
        self.assertTrue(bot.codex.run.call_args.kwargs["fast_mode"])

    def test_guest_deadline_warning_uses_the_only_response_and_keeps_final_in_thread(self) -> None:
        class ImmediateTimer:
            def __init__(self, interval: float, function: Callable[[], None]) -> None:
                self.function = function

            def start(self) -> None:
                self.function()

            def cancel(self) -> None:
                pass

            def join(self) -> None:
                pass

        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.codex.run.return_value = "completed answer"
        bot.bot_user = {}
        message = {
            "message_id": 2397,
            "guest_query_id": "guest-query",
            "text": "investigate",
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with patch("bot.threading.Timer", ImmediateTimer):
            bot.handle_guest_message(message)

        bot.telegram.answer_guest_query.assert_called_once_with(
            "guest-query",
            GUEST_RESPONSE_WARNING_MESSAGE,
        )
        bot.telegram.answer_guest_query_with_plain_text.assert_not_called()
        bot.codex.record_response_error.assert_called_once_with(
            guest_conversation_key(-1001),
            GUEST_DEADLINE_RESPONSE_ERROR,
        )

    def test_failed_guest_delivery_is_saved_for_the_next_message(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.telegram.answer_guest_query.side_effect = RuntimeError(
            "Telegram answerGuestQuery failed: rich rejected",
        )
        bot.telegram.answer_guest_query_with_plain_text.side_effect = RuntimeError(
            "Telegram answerGuestQuery failed: query is too old",
        )
        bot.codex = MagicMock()
        bot.codex.run.return_value = "response"
        bot.bot_user = {}
        message = {
            "message_id": 2397,
            "guest_query_id": "guest-query",
            "text": "status",
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="ERROR"):
            bot.handle_guest_message(message)

        bot.codex.record_response_error.assert_called_once_with(
            guest_conversation_key(-1001),
            "Telegram answerGuestQuery failed: query is too old",
        )

    def test_guest_rich_failure_sends_error_text_and_persists_original_error(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.telegram.answer_guest_query.side_effect = RuntimeError("invalid rich block")
        bot.codex = MagicMock()
        bot.codex.run.return_value = "Original formatted answer"
        bot.bot_user = {}
        message = {
            "message_id": 2397,
            "guest_query_id": "guest-query",
            "text": "status",
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="INFO"):
            bot.handle_guest_message(message)

        fallback_text = bot.telegram.answer_guest_query_with_plain_text.call_args.args[1]
        self.assertEqual(fallback_text, "invalid rich block")
        bot.codex.record_response_error.assert_called_once_with(
            guest_conversation_key(-1001),
            "invalid rich block",
        )

    def test_successful_standard_delivery_is_logged(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.telegram.send_rich_message.return_value = 3002
        bot.codex = MagicMock()
        bot.codex.run.return_value = "response"
        bot.bot_user = {}
        message = {
            "message_id": 2398,
            "text": "status",
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="INFO") as logs:
            bot.handle_message(message)

        self.assertTrue(
            any("sent rich response for message 2398 to chat -1001" in line for line in logs.output),
        )
        self.assertEqual(bot.telegram.send_rich_message.call_args.args[2], 2398)
        bot.codex.record_bot_message.assert_called_once_with(-1001, 3002)
        self.assertNotIn("fast_mode", bot.codex.run.call_args.kwargs)

    def test_archive_command_resets_conversation_without_starting_a_turn(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.codex.archive_chat.return_value = "session-one"
        bot.bot_user = {"username": "online_codex_bot"}
        message = {
            "message_id": 2403,
            "text": "/archive@online_codex_bot",
            "entities": [{"offset": 0, "length": 25, "type": "bot_command"}],
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="INFO"):
            bot.handle_message(message)

        bot.codex.archive_chat.assert_called_once_with(-1001)
        bot.codex.run.assert_not_called()
        self.assertIn(
            "next message will start a new conversation",
            bot.telegram.send_rich_message.call_args.args[1],
        )
        self.assertIn(
            "Codex thread ID: `session-one`",
            bot.telegram.send_rich_message.call_args.args[1],
        )
        self.assertEqual(bot.telegram.send_rich_message.call_args.args[2], 2403)

    def test_compact_command_keeps_conversation_without_starting_a_turn(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.codex.compact_chat.return_value = True
        bot.bot_user = {"username": "online_codex_bot"}
        message = {
            "message_id": 2404,
            "text": "/compact",
            "entities": [{"offset": 0, "length": 8, "type": "bot_command"}],
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="INFO"):
            bot.handle_message(message)

        bot.codex.compact_chat.assert_called_once_with(-1001)
        bot.codex.run.assert_not_called()
        self.assertEqual(
            bot.telegram.send_rich_message.call_args.args[1],
            "Conversation compacted successfully.",
        )

    def test_archive_and_compact_commands_target_only_the_forum_topic(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.codex.archive_chat.return_value = "topic-session"
        bot.codex.compact_chat.return_value = True
        bot.bot_user = {"username": "online_codex_bot"}
        common = {
            "message_thread_id": 42,
            "chat": {"id": -1001, "type": "supergroup"},
        }
        archive_message = {
            **common,
            "message_id": 2405,
            "text": "/archive",
            "entities": [{"offset": 0, "length": 8, "type": "bot_command"}],
        }
        compact_message = {
            **common,
            "message_id": 2406,
            "text": "/compact",
            "entities": [{"offset": 0, "length": 8, "type": "bot_command"}],
        }

        with self.assertLogs("bot", level="INFO"):
            bot.handle_message(archive_message)
            bot.handle_message(compact_message)

        topic_key = telegram_conversation_key("message", -1001, 42)
        bot.codex.archive_chat.assert_called_once_with(topic_key)
        bot.codex.compact_chat.assert_called_once_with(topic_key)
        self.assertEqual(
            [entry.args[3] for entry in bot.telegram.send_rich_message.call_args_list],
            [42, 42],
        )
        self.assertIn(
            "Codex thread ID: `topic-session`",
            bot.telegram.send_rich_message.call_args_list[0].args[1],
        )

    def test_command_addressed_to_another_bot_is_not_intercepted(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.codex.run.return_value = "response"
        bot.bot_user = {"username": "online_codex_bot"}
        message = {
            "message_id": 2405,
            "text": "/archive@another_bot",
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="INFO"):
            bot.handle_message(message)

        bot.codex.archive_chat.assert_not_called()
        bot.codex.run.assert_called_once()

    def test_reply_context_is_omitted_only_for_the_same_mapped_session(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.bot_user = {"id": 99}
        contexts: list[dict[str, object]] = []

        def run_codex(
            chat_id: int,
            prompt: list[str] | Callable[[SessionRecord | None], list[str]],
            image_paths: list[Path],
            agent_message_handler: Callable[[str], None],
            thread_name: str | None = None,
        ) -> str:
            self.assertTrue(callable(prompt))
            for record in (
                SessionRecord("session-one", 100.0, last_bot_message_id=3001),
                None,
            ):
                resolved_prompt = prompt(record)
                contexts.append(prompt_context(resolved_prompt))
            return "response"

        bot.codex.run.side_effect = run_codex
        message = {
            "message_id": 2399,
            "text": "follow up",
            "chat": {"id": -1001, "type": "supergroup"},
            "reply_to_message": {
                "message_id": 3001,
                "text": "previous bot response",
                "from": {"id": 99, "is_bot": True},
            },
        }

        with self.assertLogs("bot", level="INFO"):
            bot.handle_message(message)

        self.assertNotIn("reply_to_message", contexts[0])
        self.assertEqual(contexts[1]["reply_to_message"]["message_id"], 3001)

    def test_standard_delivery_streams_all_agent_messages_without_duplicate_final(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.bot_user = {}

        def run_codex(
            chat_id: int,
            prompt: str,
            image_paths: list[Path],
            agent_message_handler: Callable[[str], None],
            thread_name: str | None = None,
        ) -> str:
            agent_message_handler("Working on it.")
            agent_message_handler("Final answer.")
            return "Final answer."

        bot.codex.run.side_effect = run_codex
        message = {
            "message_id": 2399,
            "text": "status",
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="INFO") as logs:
            bot.handle_message(message)

        delivered_texts = [call.args[1] for call in bot.telegram.send_rich_message.call_args_list]
        reply_message_ids = [call.args[2] for call in bot.telegram.send_rich_message.call_args_list]
        self.assertEqual(delivered_texts, ["Working on it.", "Final answer."])
        self.assertEqual(reply_message_ids, [2399, None])
        self.assertTrue(any("already sent as an agent message" in line for line in logs.output))

    def test_standard_git_pull_warning_is_sent_before_response(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.bot_user = {}

        def run_codex(
            chat_id: int,
            prompt: str,
            image_paths: list[Path],
            agent_message_handler: Callable[[str], None],
            thread_name: str | None = None,
        ) -> str:
            agent_message_handler(GIT_PULL_WARNING_MESSAGE)
            return "Final answer."

        bot.codex.run.side_effect = run_codex
        message = {
            "message_id": 2402,
            "text": "status",
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="INFO"):
            bot.handle_message(message)

        delivered_texts = [call.args[1] for call in bot.telegram.send_rich_message.call_args_list]
        reply_message_ids = [call.args[2] for call in bot.telegram.send_rich_message.call_args_list]
        self.assertEqual(delivered_texts, [GIT_PULL_WARNING_MESSAGE, "Final answer."])
        self.assertEqual(reply_message_ids, [2402, None])

    def test_failed_streamed_final_is_retried_from_output_file(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.telegram.send_rich_message.side_effect = (RuntimeError("temporary failure"), None)
        bot.telegram.send_plain_message.side_effect = RuntimeError("temporary failure")
        bot.codex = MagicMock()
        bot.bot_user = {}

        def run_codex(
            chat_id: int,
            prompt: str,
            image_paths: list[Path],
            agent_message_handler: Callable[[str], None],
            thread_name: str | None = None,
        ) -> str:
            agent_message_handler("Final answer.")
            return "Final answer."

        bot.codex.run.side_effect = run_codex
        message = {
            "message_id": 2400,
            "text": "status",
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="INFO"):
            bot.handle_message(message)

        self.assertEqual(bot.telegram.send_rich_message.call_count, 2)
        self.assertEqual(bot.telegram.send_plain_message.call_count, 1)
        self.assertEqual(
            [call.args[2] for call in bot.telegram.send_rich_message.call_args_list],
            [2400, 2400],
        )
        self.assertEqual(bot.telegram.send_plain_message.call_args.args[2], 2400)

    def test_output_file_final_after_progress_is_not_a_reply(self) -> None:
        bot = object.__new__(Bot)
        bot.config = build_config(Path("."))
        bot.attachments = MagicMock()
        bot.attachments.prepare_message.return_value = (None, False)
        bot.telegram = MagicMock()
        bot.codex = MagicMock()
        bot.bot_user = {}

        def run_codex(
            chat_id: int,
            prompt: str,
            image_paths: list[Path],
            agent_message_handler: Callable[[str], None],
            thread_name: str | None = None,
        ) -> str:
            agent_message_handler("Working on it.")
            return "Final answer from output file."

        bot.codex.run.side_effect = run_codex
        message = {
            "message_id": 2401,
            "text": "status",
            "chat": {"id": -1001, "type": "supergroup"},
        }

        with self.assertLogs("bot", level="INFO"):
            bot.handle_message(message)

        self.assertEqual(
            [call.args[2] for call in bot.telegram.send_rich_message.call_args_list],
            [2401, None],
        )


def prompt_context(prompt: list[str]) -> dict[str, object]:
    context_text = prompt[0].split("<telegram_context>\n", 1)[1].split(
        "\n</telegram_context>",
        1,
    )[0]
    return json.loads(context_text)


def prompt_user_message(prompt: list[str]) -> str:
    return prompt[1]


def prompt_text(prompt: list[str]) -> str:
    return "\n".join(prompt)


def build_config(
    temp_dir: Path,
    *,
    sticker_dir: Path | None = None,
    read_only: bool = True,
    instructions_file: Path | None = None,
) -> Config:
    return Config(
        telegram_bot_token="test-token",
        allowed_chat_ids=frozenset({1001, -1001, -1002}),
        repo_dir=temp_dir,
        app_server_socket_path=temp_dir / "app-server.sock",
        session_store_path=temp_dir / "sessions.json",
        poll_timeout_seconds=30,
        chat_action_interval_seconds=5,
        git_pull_timeout_seconds=120,
        git_pull_interval_seconds=5 * 60,
        codex_timeout_seconds=None,
        attachment_retention_seconds=24 * 60 * 60,
        session_compaction_idle_seconds=24 * 60 * 60,
        session_archive_idle_seconds=7 * 24 * 60 * 60,
        session_maintenance_interval_seconds=60 * 60,
        session_maintenance_timeout_seconds=10 * 60,
        guest_response_warning_seconds=4 * 60 + 50,
        delete_webhook_on_start=False,
        drop_pending_updates_on_webhook_delete=False,
        sticker_dir=sticker_dir,
        read_only=read_only,
        instructions_file=instructions_file,
    )


if __name__ == "__main__":
    unittest.main()
