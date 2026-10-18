import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx

from bot import GuestUploadedPhoto, QueuedTelegramFile, TelegramClient


class TelegramFileDeliveryTests(unittest.TestCase):
    def test_document_uses_multipart_with_caption_and_current_topic(self) -> None:
        client = TelegramClient("test-token")
        path = Path("/repo/uploads/report.csv")
        client._api_multipart = MagicMock(return_value={"result": {"message_id": 3001}})

        message_id = client.send_document(-1001, path, "Report", 42)

        self.assertEqual(message_id, 3001)
        client._api_multipart.assert_called_once_with(
            "sendDocument",
            {
                "chat_id": -1001,
                "document": "attach://document",
                "caption": "Report",
                "message_thread_id": 42,
            },
            {"document": path},
        )

    def test_document_revalidates_upload_directory_before_delivery(self) -> None:
        client = TelegramClient("test-token")
        path = Path("/repo/uploads/report.csv")
        upload_dir = path.parent
        client._api_multipart = MagicMock(return_value={"result": {"message_id": 3001}})

        with patch("bot.validate_upload_file", side_effect=ValueError("outside upload directory")):
            with self.assertRaisesRegex(ValueError, "outside upload directory"):
                client.send_document(-1001, path, upload_dir=upload_dir)

        client._api_multipart.assert_not_called()

    def test_document_passes_upload_boundary_to_actual_multipart_transport(self) -> None:
        client = TelegramClient("test-token")
        path = Path("/repo/uploads/report.csv")
        client._api_multipart = MagicMock(return_value={"result": {"message_id": 3001}})

        with patch("bot.validate_upload_file", return_value=path):
            client.send_document(-1001, path, upload_dir=path.parent)

        client._api_multipart.assert_called_once_with(
            "sendDocument",
            {"chat_id": -1001, "document": "attach://document"},
            {"document": path},
            upload_roots={"document": path.parent},
        )

    def test_constrained_multipart_retries_preserve_upload_boundary(self) -> None:
        client = TelegramClient("test-token")
        path = Path("/repo/uploads/report.csv")
        roots = {"document": path.parent}
        client._post_multipart_api = AsyncMock(
            side_effect=[
                httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}}),
                httpx.Response(200, json={"ok": True, "result": {"message_id": 3001}}),
            ],
        )
        client._wait_before_retry = AsyncMock()

        result = asyncio.run(
            client._call_multipart_api(
                "sendDocument",
                {"chat_id": -1001, "document": "attach://document"},
                {"document": path},
                upload_roots=roots,
            ),
        )

        self.assertEqual(result["result"]["message_id"], 3001)
        self.assertEqual(client._post_multipart_api.await_count, 2)
        for api_call in client._post_multipart_api.await_args_list:
            self.assertEqual(api_call.kwargs, {"upload_roots": roots})

    def test_multipart_file_reads_use_confined_open(self) -> None:
        client = TelegramClient("test-token")
        path = Path("/repo/uploads/report.csv")
        opened = io.BytesIO(b"report")
        async_client = AsyncMock()
        async_client.__aenter__.return_value = async_client

        async def post(*args, **kwargs):
            self.assertIs(kwargs["files"]["document"][1], opened)
            self.assertEqual(opened.read(), b"report")
            return httpx.Response(200, json={"ok": True})

        async_client.post.side_effect = post
        with (
            patch("bot.open_upload_file", return_value=contextlib.closing(opened)) as safe_open,
            patch("bot.httpx.AsyncClient", return_value=async_client),
        ):
            asyncio.run(
                client._post_multipart_api(
                    "sendDocument",
                    {"chat_id": "-1001"},
                    {"document": path},
                    upload_roots={"document": path.parent},
                ),
            )

        safe_open.assert_called_once_with(path, path.parent)
        self.assertTrue(opened.closed)

    def test_guest_documents_are_preuploaded_each_time_and_preserve_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.csv"
            path.write_bytes(b"a,b\n1,2\n")
            other_path = Path(temp_dir) / "other.csv"
            other_path.write_bytes(path.read_bytes())
            client = TelegramClient("test-token", media_upload_chat_id=-10099)
            client._api_multipart = MagicMock(
                side_effect=[
                    {"result": {"message_id": 9001, "document": {"file_id": "first"}}},
                    {"result": {"message_id": 9002, "document": {"file_id": "second"}}},
                    {"result": {"message_id": 9003, "document": {"file_id": "third"}}},
                ],
            )
            client.uploaded_media_file_ids["irrelevant-photo"] = "photo-id"

            first = client.upload_document_for_guest(path)
            repeated = client.upload_document_for_guest(path)
            renamed = client.upload_document_for_guest(other_path)

        self.assertEqual(first, GuestUploadedPhoto("first", 9001))
        self.assertEqual(repeated, GuestUploadedPhoto("second", 9002))
        self.assertEqual(renamed, GuestUploadedPhoto("third", 9003))
        self.assertEqual(
            [api_call.args[2] for api_call in client._api_multipart.call_args_list],
            [{"document": path}, {"document": path}, {"document": other_path}],
        )
        self.assertEqual(client.uploaded_media_file_ids, {"irrelevant-photo": "photo-id"})

    def test_guest_document_upload_uses_configured_upload_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            path = root / "report.csv"
            path.write_bytes(b"a,b\n")
            client = TelegramClient("test-token", media_upload_chat_id=-10099)
            client._api_multipart = MagicMock(
                return_value={
                    "result": {"message_id": 9001, "document": {"file_id": "csv-id"}},
                },
            )

            uploaded = client.upload_document_for_guest(path, upload_dir=root)

        self.assertEqual(uploaded, GuestUploadedPhoto("csv-id", 9001))
        client._api_multipart.assert_called_once_with(
            "sendDocument",
            {"chat_id": -10099, "document": "attach://document"},
            {"document": path},
            upload_roots={"document": root},
        )

    def test_guest_response_preserves_text_and_images_with_multiple_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = Path(temp_dir) / "report.csv"
            first_path.write_bytes(b"a,b\n")
            second_path = Path(temp_dir) / "notes.txt"
            second_path.write_text("Notes", encoding="utf-8")
            client = TelegramClient("test-token", media_upload_chat_id=-10099)
            client.upload_photo_for_guest = MagicMock(
                return_value=GuestUploadedPhoto("photo-id", 9000),
            )
            client._api_multipart = MagicMock(
                side_effect=[
                    {"result": {"message_id": 9001, "document": {"file_id": "csv-id"}}},
                    {"result": {"message_id": 9002, "document": {"file_id": "text-id"}}},
                ],
            )
            client._api = MagicMock(return_value={"result": True})

            client.answer_guest_query(
                "guest-query",
                "Answer.\n\n![image](https://example.com/image.png)",
                queued_files=(
                    QueuedTelegramFile(first_path, "<Report> & data"),
                    QueuedTelegramFile(second_path),
                ),
            )

        answer = client._api.call_args_list[0]
        self.assertEqual(answer.args[0], "answerGuestQuery")
        rich = answer.args[1]["result"]["input_message_content"]["rich_message"]
        self.assertIn("Answer.", rich["html"])
        self.assertIn('src="tg://photo?id=image_', rich["html"])
        self.assertIn('<tg-document src="tg://document?id=document_0">', rich["html"])
        self.assertIn('<tg-document src="tg://document?id=document_1">', rich["html"])
        self.assertIn("<figcaption>&lt;Report&gt; &amp; data</figcaption>", rich["html"])
        self.assertEqual(
            [(item["media"]["type"], item["media"]["media"]) for item in rich["media"]],
            [("photo", "photo-id"), ("document", "csv-id"), ("document", "text-id")],
        )
        self.assertEqual(
            client._api.call_args_list[-1],
            call("deleteMessages", {"chat_id": -10099, "message_ids": [9000, 9001, 9002]}),
        )

    def test_documents_only_guest_response_does_not_require_markdown_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.csv"
            path.write_bytes(b"a,b\n")
            client = TelegramClient("test-token", media_upload_chat_id=-10099)
            client._api_multipart = MagicMock(
                return_value={
                    "result": {"message_id": 9001, "document": {"file_id": "csv-id"}},
                },
            )
            client._api = MagicMock(return_value={"result": True})

            with patch("bot.telegramify_rich", side_effect=AssertionError("unexpected conversion")):
                client.answer_guest_query(
                    "guest-query",
                    "",
                    queued_files=(QueuedTelegramFile(path),),
                )

        rich = client._api.call_args_list[0].args[1]["result"]["input_message_content"]["rich_message"]
        self.assertEqual(rich["html"], '<tg-document src="tg://document?id=document_0"></tg-document>')
        self.assertEqual(
            rich["media"],
            [{"id": "document_0", "media": {"type": "document", "media": "csv-id"}}],
        )

    def test_guest_document_upload_is_deleted_when_final_delivery_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.csv"
            path.write_bytes(b"a,b\n")
            client = TelegramClient("test-token", media_upload_chat_id=-10099)
            client._api_multipart = MagicMock(
                return_value={
                    "result": {"message_id": 9001, "document": {"file_id": "csv-id"}},
                },
            )
            client._api = MagicMock(side_effect=[RuntimeError("guest rejected"), {"result": True}])

            with self.assertRaisesRegex(RuntimeError, "guest rejected"):
                client.answer_guest_query(
                    "guest-query",
                    "",
                    queued_files=(QueuedTelegramFile(path),),
                )

        self.assertEqual(
            client._api.call_args_list[-1],
            call("deleteMessages", {"chat_id": -10099, "message_ids": [9001]}),
        )

    def test_invalid_document_upload_result_is_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.csv"
            path.write_bytes(b"a,b\n")
            client = TelegramClient("test-token", media_upload_chat_id=-10099)
            client._api_multipart = MagicMock(return_value={"result": {"message_id": 9001}})
            client._api = MagicMock(return_value={"result": True})

            with self.assertRaisesRegex(RuntimeError, "no reusable file ID"):
                client.upload_document_for_guest(path)

        client._api.assert_called_once_with(
            "deleteMessages",
            {"chat_id": -10099, "message_ids": [9001]},
        )

    def test_guest_document_requires_media_upload_chat(self) -> None:
        client = TelegramClient("test-token")
        client._api_multipart = MagicMock()

        with self.assertRaisesRegex(ValueError, "CODEX_TELEGRAM_MEDIA_UPLOAD_CHAT_ID.*files"):
            client.upload_document_for_guest(Path("report.csv"))

        client._api_multipart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
