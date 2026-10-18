#!/usr/bin/env python3

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable, Coroutine, Iterator
from urllib.parse import unquote, urlsplit

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import httpx
import websocket
from telegram import Bot as AsyncTelegramBot
from telegram.constants import ReactionEmoji
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.request import HTTPXRequest
from telegramify_markdown import MessageEntity, entities_to_markdownv2, telegramify_rich

try:
    from .telegram_entities_to_markdown import entities_to_markdown
    from .telegram_rich_to_markdown import rich_message_to_markdown
except ImportError:
    from telegram_entities_to_markdown import entities_to_markdown
    from telegram_rich_to_markdown import rich_message_to_markdown


logger = logging.getLogger(__name__)

MAX_CODEX_LOG_TEXT_LENGTH = 2000
ATTACHMENT_CACHE_DIRECTORY = ".codex-telegram-attachments"
ATTACHMENT_FREE_MESSAGE_RETENTION = 5
DEFAULT_ATTACHMENT_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_SESSION_COMPACTION_IDLE_SECONDS = 24 * 60 * 60
DEFAULT_SESSION_ARCHIVE_IDLE_SECONDS = 7 * 24 * 60 * 60
DEFAULT_SESSION_MAINTENANCE_INTERVAL_SECONDS = 60 * 60
DEFAULT_SESSION_MAINTENANCE_TIMEOUT_SECONDS = 10 * 60
DEFAULT_GIT_PULL_INTERVAL_SECONDS = 5 * 60
DEFAULT_GUEST_RESPONSE_WARNING_SECONDS = 4 * 60 + 50
DEFAULT_MAX_CONCURRENT_CHATS = 16
TELEGRAM_REQUEST_ATTEMPTS = 4
TELEGRAM_NETWORK_RETRY_DELAY_SECONDS = 0.5
MAX_TELEGRAM_RICH_MEDIA = 50
MAX_TELEGRAM_PHOTO_BYTES = 10 * 1024 * 1024
MAX_TELEGRAM_DOCUMENT_BYTES = 50 * 1024 * 1024
TELEGRAM_MEDIA_PLACEHOLDER_PREFIX = "https://codex-telegram.invalid/media/"
TELEGRAM_PHOTO_URI_PREFIX = "tg://photo?id="
MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]\((?P<url>(?:file|https?)://[^\s)]+)\)",
    re.IGNORECASE,
)
MARKDOWN_FENCE_PATTERN = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})")
GUEST_RESPONSE_WARNING_MESSAGE = (
    "I am still working on it and need some more time. Telegram does not allow "
    "guest responses after 5 minutes. Please give me a couple more minutes and then ping me."
)
GUEST_DEADLINE_RESPONSE_ERROR = (
    "The previous turn's final result was not delivered because Telegram's one guest response "
    "was used for a deadline warning while Codex continued working."
)
POTENTIAL_PAN_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(\b(?:api[_-]?key|authorization|password|passwd|secret|token)\b\s*[=:]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|\S+)",
)
BOT_COMMAND_PATTERN = re.compile(r"/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?")
GUEST_CONVERSATION_PREFIX = "guest:"
TOPIC_CONVERSATION_PREFIX = "topic:"
TELEGRAM_REACTION_TOOL_NAME = "react_to_telegram_message"
TELEGRAM_CLEAR_REACTION_TOOL_NAME = "clear_telegram_reaction"
TELEGRAM_STICKER_TOOL_NAME = "send_sticker"
TELEGRAM_FILE_TOOL_NAME = "send_file"
TELEGRAM_REACTION_EMOJIS = tuple(reaction.value for reaction in ReactionEmoji)

ConversationKey = int | str


class GitPullError(RuntimeError):
    pass


class CodexAppServerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        request_method: str | None = None,
        error_code: int | None = None,
        app_server_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.request_method = request_method
        self.error_code = error_code
        self.app_server_message = app_server_message


class CodexTurnInactiveError(CodexAppServerError):
    pass


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_chat_ids: frozenset[int]
    repo_dir: Path
    app_server_socket_path: Path
    session_store_path: Path
    poll_timeout_seconds: int
    chat_action_interval_seconds: int
    git_pull_timeout_seconds: int
    git_pull_interval_seconds: int
    codex_timeout_seconds: int | None
    attachment_retention_seconds: int
    session_compaction_idle_seconds: int
    session_archive_idle_seconds: int
    session_maintenance_interval_seconds: int
    session_maintenance_timeout_seconds: int
    guest_response_warning_seconds: int
    delete_webhook_on_start: bool
    drop_pending_updates_on_webhook_delete: bool
    media_upload_chat_id: int | None = None
    max_concurrent_chats: int = DEFAULT_MAX_CONCURRENT_CHATS
    sticker_dir: Path | None = None
    read_only: bool = True
    instructions_file: Path | None = None
    upload_dir: Path | None = None

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        allowed_chat_ids_raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS")
        allowed_chat_ids_name = "TELEGRAM_ALLOWED_CHAT_IDS"
        if not allowed_chat_ids_raw:
            allowed_chat_ids_raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")
            allowed_chat_ids_name = "TELEGRAM_ALLOWED_CHAT_ID"
        if not allowed_chat_ids_raw:
            raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS or TELEGRAM_ALLOWED_CHAT_ID is required")

        allowed_chat_ids = parse_chat_ids(allowed_chat_ids_name, allowed_chat_ids_raw)

        repo_dir = Path(
            os.environ.get("CODEX_TELEGRAM_REPO_DIR", Path.cwd()),
        ).expanduser().resolve()

        codex_timeout_seconds = read_optional_int("CODEX_TELEGRAM_CODEX_TIMEOUT_SECONDS")
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        sticker_dir = resolve_repo_relative_path(
            os.environ.get("CODEX_TELEGRAM_STICKER_DIR"),
            repo_dir,
        )
        return cls(
            telegram_bot_token=token,
            allowed_chat_ids=allowed_chat_ids,
            repo_dir=repo_dir,
            app_server_socket_path=Path(
                os.environ.get(
                    "CODEX_TELEGRAM_APP_SERVER_SOCKET",
                    codex_home / "app-server-control" / "app-server-control.sock",
                ),
            ).expanduser().resolve(),
            session_store_path=Path(
                os.environ.get("CODEX_TELEGRAM_SESSION_STORE", default_session_store_path(repo_dir)),
            ).expanduser().resolve(),
            poll_timeout_seconds=read_positive_int("CODEX_TELEGRAM_POLL_TIMEOUT_SECONDS", 30),
            chat_action_interval_seconds=read_positive_int("CODEX_TELEGRAM_CHAT_ACTION_INTERVAL_SECONDS", 5),
            git_pull_timeout_seconds=read_positive_int("CODEX_TELEGRAM_GIT_PULL_TIMEOUT_SECONDS", 120),
            git_pull_interval_seconds=read_positive_int(
                "CODEX_TELEGRAM_GIT_PULL_INTERVAL_SECONDS",
                DEFAULT_GIT_PULL_INTERVAL_SECONDS,
            ),
            codex_timeout_seconds=codex_timeout_seconds if codex_timeout_seconds and codex_timeout_seconds > 0 else None,
            attachment_retention_seconds=read_positive_int(
                "CODEX_TELEGRAM_ATTACHMENT_RETENTION_SECONDS",
                DEFAULT_ATTACHMENT_RETENTION_SECONDS,
            ),
            session_compaction_idle_seconds=read_positive_int(
                "CODEX_TELEGRAM_COMPACTION_IDLE_SECONDS",
                DEFAULT_SESSION_COMPACTION_IDLE_SECONDS,
            ),
            session_archive_idle_seconds=read_positive_int(
                "CODEX_TELEGRAM_ARCHIVE_IDLE_SECONDS",
                DEFAULT_SESSION_ARCHIVE_IDLE_SECONDS,
            ),
            session_maintenance_interval_seconds=read_positive_int(
                "CODEX_TELEGRAM_MAINTENANCE_INTERVAL_SECONDS",
                DEFAULT_SESSION_MAINTENANCE_INTERVAL_SECONDS,
            ),
            session_maintenance_timeout_seconds=read_positive_int(
                "CODEX_TELEGRAM_MAINTENANCE_TIMEOUT_SECONDS",
                DEFAULT_SESSION_MAINTENANCE_TIMEOUT_SECONDS,
            ),
            guest_response_warning_seconds=read_guest_response_warning_seconds(),
            delete_webhook_on_start=read_bool("CODEX_TELEGRAM_DELETE_WEBHOOK_ON_START", False),
            drop_pending_updates_on_webhook_delete=read_bool("CODEX_TELEGRAM_DROP_PENDING_UPDATES", False),
            media_upload_chat_id=read_optional_int("CODEX_TELEGRAM_MEDIA_UPLOAD_CHAT_ID"),
            max_concurrent_chats=read_positive_int(
                "CODEX_TELEGRAM_MAX_CONCURRENT_CHATS",
                DEFAULT_MAX_CONCURRENT_CHATS,
            ),
            sticker_dir=sticker_dir,
            read_only=read_bool("CODEX_TELEGRAM_READ_ONLY", True),
            instructions_file=resolve_repo_relative_path(
                os.environ.get("CODEX_TELEGRAM_INSTRUCTIONS_FILE"),
                repo_dir,
            ),
            upload_dir=configured_upload_dir(resolve_repo_relative_path(
                os.environ.get("CODEX_TELEGRAM_UPLOAD_DIR"),
                repo_dir,
            )),
        )


@dataclass(frozen=True)
class TelegramImage:
    file_id: str
    source: str
    file_name: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True)
class TelegramFile:
    file_id: str
    source: str
    file_name: str | None = None
    mime_type: str | None = None
    attachment_type: str = "document"


@dataclass(frozen=True)
class GuestResponseDelivery:
    delivered: bool
    error: Exception | None = None


@dataclass(frozen=True)
class CodexAgentMessage:
    turn_id: str
    item_id: str | None
    phase: str | None
    text: str


@dataclass(frozen=True)
class CodexGeneratedImage:
    turn_id: str
    item_id: str | None
    saved_path: Path


@dataclass(frozen=True)
class TelegramRichMediaReference:
    media_id: str
    source_url: str
    placeholder_url: str
    local_path: Path | None


@dataclass(frozen=True)
class TelegramRichPart:
    rich_message: dict[str, Any]
    uploads: dict[str, Path]


@dataclass(frozen=True)
class GuestUploadedPhoto:
    file_id: str
    message_id: int | None


@dataclass(frozen=True)
class QueuedTelegramSticker:
    name: str
    path: Path
    steer_count: int


@dataclass(frozen=True)
class QueuedTelegramFile:
    path: Path
    caption: str | None = None
    upload_dir: Path | None = None


@dataclass(frozen=True)
class DeferredTelegramAction:
    run: Callable[[], None]
    cancel: Callable[[], None]

    def __call__(self) -> None:
        self.run()


@dataclass
class CodexTurnHandle:
    thread_id: str
    turn_id: str
    event_queue: queue.Queue[Any]
    agent_message_handler: Callable[[CodexAgentMessage], None] | None = None
    generated_image_handler: Callable[[CodexGeneratedImage], None] | None = None
    image_generation_started_handler: Callable[[], None] | None = None
    closed: bool = False


@dataclass
class RunningCodexTurn:
    chat_id: ConversationKey
    session_id: str
    handle: CodexTurnHandle
    message_thread_id: int | None = None


def guest_conversation_key(chat_id: int, message_thread_id: int | None = None) -> str:
    key = f"{GUEST_CONVERSATION_PREFIX}{chat_id}"
    if valid_message_thread_id(message_thread_id):
        return f"{key}:{message_thread_id}"
    return key


def telegram_conversation_key(
    kind: str,
    chat_id: int,
    message_thread_id: int | None = None,
) -> ConversationKey:
    if kind == "guest_message":
        return guest_conversation_key(chat_id, message_thread_id)
    if valid_message_thread_id(message_thread_id):
        return f"{TOPIC_CONVERSATION_PREFIX}{chat_id}:{message_thread_id}"
    return chat_id


def added_telegram_reaction_text(
    old_reactions: Any,
    new_reactions: Any,
) -> str:
    if not isinstance(old_reactions, list) or not isinstance(new_reactions, list):
        return ""

    remaining_old = [reaction for reaction in old_reactions if isinstance(reaction, dict)]
    added_reactions: list[dict[str, Any]] = []
    for reaction in new_reactions:
        if not isinstance(reaction, dict):
            continue
        try:
            old_index = remaining_old.index(reaction)
        except ValueError:
            added_reactions.append(reaction)
        else:
            remaining_old.pop(old_index)

    rendered = []
    for reaction in added_reactions:
        reaction_type = reaction.get("type")
        if reaction_type == "emoji":
            emoji = reaction.get("emoji")
            if isinstance(emoji, str) and emoji:
                rendered.append(emoji)
        elif reaction_type == "paid":
            rendered.append("⭐")
        elif reaction_type == "custom_emoji":
            custom_emoji_id = reaction.get("custom_emoji_id")
            if isinstance(custom_emoji_id, str) and custom_emoji_id:
                rendered.append(f"[custom emoji reaction: {custom_emoji_id}]")

    return " ".join(rendered)


def telegram_reaction_actor(reaction: dict[str, Any]) -> dict[str, Any]:
    user = reaction.get("user")
    if isinstance(user, dict):
        return user

    actor_chat = reaction.get("actor_chat")
    if not isinstance(actor_chat, dict):
        return {}
    display_name = actor_chat.get("title") or actor_chat.get("username")
    return {
        "id": actor_chat.get("id"),
        "is_bot": False,
        "first_name": display_name or str(actor_chat.get("id", "unknown")),
        "username": actor_chat.get("username"),
    }


def telegram_reaction_message(
    reaction: dict[str, Any],
    text: str,
    bot_user: dict[str, Any],
    message_thread_id: int | None,
) -> dict[str, Any]:
    chat = reaction.get("chat")
    if not isinstance(chat, dict):
        chat = {}
    message_id = reaction.get("message_id")
    reply_to_message: dict[str, Any] = {
        "message_id": message_id,
        "chat": chat,
        "from": bot_user,
    }
    message: dict[str, Any] = {
        # A reaction is not itself a Telegram message. Reusing the reacted-to
        # ID here lets the existing response and reply-deduplication paths work.
        "message_id": message_id,
        "date": reaction.get("date"),
        "text": text,
        "chat": chat,
        "from": telegram_reaction_actor(reaction),
        "reply_to_message": reply_to_message,
    }
    if valid_message_thread_id(message_thread_id):
        message["message_thread_id"] = message_thread_id
        reply_to_message["message_thread_id"] = message_thread_id
    return message


def telegram_thread_name(kind: str, message: dict[str, Any]) -> str:
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return "Telegram conversation"

    message_thread_id = message.get("message_thread_id")

    def with_topic(name: str) -> str:
        if valid_message_thread_id(message_thread_id):
            return f"{name} / topic {message_thread_id}"
        return name

    chat_type = chat.get("type")
    if chat_type in {"group", "supergroup", "channel"}:
        return with_topic(telegram_chat_identity(chat))
    if chat_type != "private":
        return with_topic(telegram_chat_identity(chat))
    if kind != "guest_message":
        return with_topic(telegram_private_chat_identity(chat))

    participants: dict[tuple[str, str], str] = {}
    for participant in (
        message.get("from"),
        message.get("guest_bot_caller_user"),
        chat,
    ):
        if not isinstance(participant, dict):
            continue
        label = telegram_user_identity(participant)
        participant_id = participant.get("id")
        key = (
            ("id", str(participant_id))
            if isinstance(participant_id, int)
            else ("label", label.casefold())
        )
        participants.setdefault(key, label)

    caller_chat = message.get("guest_bot_caller_chat")
    if isinstance(caller_chat, dict):
        label = telegram_chat_identity(caller_chat)
        caller_chat_id = caller_chat.get("id")
        key = (
            ("chat", str(caller_chat_id))
            if isinstance(caller_chat_id, int)
            else ("label", label.casefold())
        )
        participants.setdefault(key, label)

    labels = [participants[key] for key in sorted(participants)]
    if len(labels) >= 2:
        return with_topic("/".join(labels[:2]))
    if labels:
        return with_topic(labels[0])
    return with_topic(telegram_private_chat_identity(chat))


def telegram_private_chat_identity(chat: dict[str, Any]) -> str:
    username = chat.get("username")
    if isinstance(username, str) and username.strip():
        return f"@{username.strip().lstrip('@')}"
    chat_id = chat.get("id")
    if isinstance(chat_id, int):
        return f"tg://user?id={chat_id}"
    return "Telegram private chat"


def telegram_user_identity(user: dict[str, Any]) -> str:
    username = user.get("username")
    if isinstance(username, str) and username.strip():
        return f"@{username.strip().lstrip('@')}"
    display_name = " ".join(
        name.strip()
        for name in (user.get("first_name"), user.get("last_name"))
        if isinstance(name, str) and name.strip()
    )
    if display_name:
        return display_name
    user_id = user.get("id")
    if isinstance(user_id, int):
        return f"tg://user?id={user_id}"
    return "Telegram user"


def telegram_chat_identity(chat: dict[str, Any]) -> str:
    title = chat.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    username = chat.get("username")
    if isinstance(username, str) and username.strip():
        return f"@{username.strip().lstrip('@')}"
    chat_id = chat.get("id")
    if isinstance(chat_id, int):
        return f"Telegram chat {chat_id}"
    return "Telegram chat"


def is_guest_conversation_key(conversation_key: ConversationKey) -> bool:
    return (
        isinstance(conversation_key, str)
        and conversation_key.startswith(GUEST_CONVERSATION_PREFIX)
    )


def valid_message_thread_id(message_thread_id: object) -> bool:
    return (
        isinstance(message_thread_id, int)
        and not isinstance(message_thread_id, bool)
        and message_thread_id > 0
    )


def parsed_conversation_key(
    conversation_key: ConversationKey,
) -> tuple[int, int | None, bool]:
    if isinstance(conversation_key, int) and not isinstance(conversation_key, bool):
        return conversation_key, None, False
    if not isinstance(conversation_key, str):
        raise ValueError(f"invalid Telegram conversation key {conversation_key!r}")

    is_guest = conversation_key.startswith(GUEST_CONVERSATION_PREFIX)
    is_topic = conversation_key.startswith(TOPIC_CONVERSATION_PREFIX)
    if not is_guest and not is_topic:
        raise ValueError(f"invalid Telegram conversation key {conversation_key!r}")
    encoded = conversation_key.removeprefix(
        GUEST_CONVERSATION_PREFIX if is_guest else TOPIC_CONVERSATION_PREFIX,
    )
    chat_id_text, separator, message_thread_id_text = encoded.partition(":")
    try:
        chat_id = int(chat_id_text)
    except ValueError as err:
        raise ValueError(f"invalid Telegram conversation key {conversation_key!r}") from err

    if is_topic and not separator:
        raise ValueError(f"invalid Telegram conversation key {conversation_key!r}")
    if not separator:
        return chat_id, None, is_guest
    try:
        message_thread_id = int(message_thread_id_text)
    except ValueError as err:
        raise ValueError(f"invalid Telegram conversation key {conversation_key!r}") from err
    if not valid_message_thread_id(message_thread_id):
        raise ValueError(f"invalid Telegram conversation key {conversation_key!r}")
    return chat_id, message_thread_id, is_guest


def conversation_chat_id(conversation_key: ConversationKey) -> int:
    return parsed_conversation_key(conversation_key)[0]


def conversation_message_thread_id(conversation_key: ConversationKey) -> int | None:
    return parsed_conversation_key(conversation_key)[1]


def parse_conversation_storage_key(storage_key: str) -> ConversationKey:
    try:
        return int(storage_key)
    except ValueError:
        conversation_chat_id(storage_key)
        return storage_key


def valid_conversation_storage_key(storage_key: object) -> bool:
    if not isinstance(storage_key, str):
        return False
    try:
        parse_conversation_storage_key(storage_key)
    except ValueError:
        return False
    return True


def read_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError as err:
        raise ValueError(f"{name} must be an integer") from err


def read_optional_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None:
        return None

    try:
        return int(value)
    except ValueError as err:
        raise ValueError(f"{name} must be an integer") from err


def read_positive_int(name: str, default: int) -> int:
    value = read_int(name, default)
    if value <= 0:
        raise ValueError(f"{name} must be positive")

    return value


def read_guest_response_warning_seconds() -> int:
    value = read_positive_int(
        "CODEX_TELEGRAM_GUEST_WARNING_SECONDS",
        DEFAULT_GUEST_RESPONSE_WARNING_SECONDS,
    )
    if value >= 5 * 60:
        raise ValueError("CODEX_TELEGRAM_GUEST_WARNING_SECONDS must be less than 300")

    return value


def telegram_retry_after_delay(error: RetryAfter) -> float:
    retry_after = error.retry_after
    delay = (
        retry_after.total_seconds()
        if hasattr(retry_after, "total_seconds")
        else float(retry_after)
    )
    return max(0.0, delay)


def telegram_http_status_is_retryable(status_code: int) -> bool:
    return status_code in {408, 425, 429} or status_code >= 500


def telegram_response_retry_delay(response: httpx.Response) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return TELEGRAM_NETWORK_RETRY_DELAY_SECONDS


def telegram_http_error_is_retryable(error: httpx.HTTPError) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return telegram_http_status_is_retryable(error.response.status_code)
    return False


def telegram_http_retry_delay(error: httpx.HTTPError) -> float:
    if isinstance(error, httpx.HTTPStatusError):
        return telegram_response_retry_delay(error.response)
    return TELEGRAM_NETWORK_RETRY_DELAY_SECONDS


def parse_chat_ids(name: str, value: str) -> frozenset[int]:
    raw_chat_ids = [item.strip() for item in value.split(",")]
    if not raw_chat_ids or any(not item for item in raw_chat_ids):
        raise ValueError(f"{name} must be a comma-separated list of integers")

    try:
        chat_ids = frozenset(int(item) for item in raw_chat_ids)
    except ValueError as err:
        raise ValueError(f"{name} must be a comma-separated list of integers") from err

    return chat_ids


def default_session_store_path(repo_dir: Path) -> Path:
    return repo_dir / "sessions.json"


def resolve_repo_relative_path(value: str | None, repo_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_dir / path
    return path.resolve()


def configured_telegram_stickers(sticker_dir: Path | None) -> dict[str, Path]:
    if sticker_dir is None:
        return {}
    if not sticker_dir.is_dir():
        logger.warning(
            "configured Telegram sticker directory does not exist or is not a directory: %s",
            sticker_dir,
        )
        return {}

    stickers = {
        path.name.removesuffix(".webp"): path.resolve()
        for path in sorted(sticker_dir.glob("*.webp"), key=lambda item: item.name)
        if path.is_file() and path.name != ".webp"
    }
    if stickers:
        logger.info(
            "loaded %s Telegram sticker(s) from %s: %s",
            len(stickers),
            sticker_dir,
            ", ".join(stickers),
        )
    else:
        logger.warning(
            "configured Telegram sticker directory contains no .webp files: %s",
            sticker_dir,
        )
    return stickers


def read_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ValueError(f"{name} must be a boolean")


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def configured_upload_dir(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("the configured path is not a directory")
    except (OSError, RuntimeError, ValueError) as err:
        raise ValueError(
            f"CODEX_TELEGRAM_UPLOAD_DIR must name an existing directory: {path}",
        ) from err
    return resolved


def _validate_upload_file_stat(file_stat: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"upload path is not a regular file: {path}")
    if file_stat.st_size <= 0:
        raise ValueError(f"upload file is empty: {path}")
    if file_stat.st_size > MAX_TELEGRAM_DOCUMENT_BYTES:
        raise ValueError(
            f"upload file exceeds Telegram's {MAX_TELEGRAM_DOCUMENT_BYTES}-byte "
            f"document limit: {path}",
        )


def validate_upload_file(path: Path, upload_dir: Path) -> Path:
    # The configured directory is canonicalized once at startup. Keep that fixed
    # boundary if a directory is subsequently replaced with a symlink.
    root = upload_dir.expanduser().absolute()
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        if not path_is_within(resolved, root):
            raise ValueError(
                f"upload file is outside CODEX_TELEGRAM_UPLOAD_DIR: {path}",
            )
        _validate_upload_file_stat(resolved.stat(), resolved)
    except (OSError, RuntimeError) as err:
        raise ValueError(f"upload file is unavailable: {path}") from err
    return resolved


@contextlib.contextmanager
def open_upload_file(path: Path, upload_dir: Path) -> Iterator[BinaryIO]:
    resolved = validate_upload_file(path, upload_dir)
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            # Open every component relative to a pinned directory descriptor. A
            # symlink swap between validation and open can never redirect the read.
            directory_fd = os.open(resolved.anchor, directory_flags)
            for component in resolved.parts[1:-1]:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(
                resolved.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            _validate_upload_file_stat(os.fstat(file_fd), resolved)
        except OSError as err:
            raise ValueError(f"upload file could not be opened safely: {path}") from err
        with os.fdopen(file_fd, "rb") as file_stream:
            file_fd = None
            yield file_stream
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def validate_telegram_photo(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"embedded image is not a regular file: {resolved}")
    size = resolved.stat().st_size
    if size <= 0:
        raise ValueError(f"embedded image is empty: {resolved}")
    if size > MAX_TELEGRAM_PHOTO_BYTES:
        raise ValueError(
            f"embedded image exceeds Telegram's {MAX_TELEGRAM_PHOTO_BYTES}-byte photo limit: "
            f"{resolved}",
        )

    with resolved.open("rb") as image_file:
        header = image_file.read(16)
    supported = (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    )
    if not supported:
        raise ValueError(f"embedded file is not a supported PNG, JPEG, or WebP image: {resolved}")
    return resolved


def local_image_path_from_uri(
    uri: str,
    allowed_roots: tuple[Path, ...],
    generated_image_paths: frozenset[Path],
) -> Path:
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError(f"unsupported local image URI: {uri}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"local image URI must not contain a query or fragment: {uri}")

    path = validate_telegram_photo(Path(unquote(parsed.path)))
    resolved_generated_paths = {
        generated_path.expanduser().resolve()
        for generated_path in generated_image_paths
    }
    resolved_roots = tuple(root.expanduser().resolve() for root in allowed_roots)
    if path not in resolved_generated_paths and not any(
        path_is_within(path, root)
        for root in resolved_roots
    ):
        raise ValueError(f"embedded image is outside the configured safe roots: {path}")
    return path


def unavailable_local_image_placeholder(alt_text: str) -> str:
    description = re.sub(r"\\(.)", r"\1", alt_text).strip()
    if description:
        return f"[Image unavailable: {description}]"
    return "[Image unavailable]"


def extract_telegram_rich_media(
    markdown: str,
    allowed_roots: tuple[Path, ...],
    generated_image_paths: frozenset[Path],
    *,
    replace_remote_images: bool,
) -> tuple[str, list[TelegramRichMediaReference]]:
    references_by_source: dict[str, TelegramRichMediaReference] = {}

    def replace(match: re.Match[str]) -> str:
        source_url = match.group("url")
        is_local = source_url.lower().startswith("file:")
        if not is_local and not replace_remote_images:
            return match.group(0)

        reference = references_by_source.get(source_url)
        if reference is None:
            if len(references_by_source) >= MAX_TELEGRAM_RICH_MEDIA:
                raise ValueError(
                    f"a rich message can contain at most {MAX_TELEGRAM_RICH_MEDIA} media items",
                )
            media_id = f"image_{hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:24]}"
            try:
                local_path = (
                    local_image_path_from_uri(
                        source_url,
                        allowed_roots,
                        generated_image_paths,
                    )
                    if is_local
                    else None
                )
            except (FileNotFoundError, NotADirectoryError):
                logger.warning(
                    "embedded local image is unavailable; using a placeholder: %s",
                    source_url,
                )
                return unavailable_local_image_placeholder(match.group("alt"))
            reference = TelegramRichMediaReference(
                media_id=media_id,
                source_url=source_url,
                placeholder_url=f"{TELEGRAM_MEDIA_PLACEHOLDER_PREFIX}{media_id}",
                local_path=local_path,
            )
            references_by_source[source_url] = reference
        return f"![{match.group('alt')}]({reference.placeholder_url})"

    return replace_markdown_images_outside_code(markdown, replace), list(
        references_by_source.values(),
    )


def replace_markdown_images_outside_code(
    markdown: str,
    replace: Callable[[re.Match[str]], str],
) -> str:
    output: list[str] = []
    active_fence: tuple[str, int] | None = None
    for line in markdown.splitlines(keepends=True):
        fence_match = MARKDOWN_FENCE_PATTERN.match(line)
        if active_fence is not None:
            output.append(line)
            if fence_match:
                fence = fence_match.group("fence")
                if fence[0] == active_fence[0] and len(fence) >= active_fence[1]:
                    active_fence = None
            continue
        if fence_match:
            fence = fence_match.group("fence")
            active_fence = (fence[0], len(fence))
            output.append(line)
            continue
        output.append(replace_markdown_images_outside_inline_code(line, replace))
    return "".join(output)


def replace_markdown_images_outside_inline_code(
    text: str,
    replace: Callable[[re.Match[str]], str],
) -> str:
    output: list[str] = []
    position = 0
    while position < len(text):
        opening = text.find("`", position)
        if opening < 0:
            output.append(MARKDOWN_IMAGE_PATTERN.sub(replace, text[position:]))
            break
        output.append(MARKDOWN_IMAGE_PATTERN.sub(replace, text[position:opening]))
        delimiter_end = opening
        while delimiter_end < len(text) and text[delimiter_end] == "`":
            delimiter_end += 1
        delimiter = text[opening:delimiter_end]
        closing = text.find(delimiter, delimiter_end)
        if closing < 0:
            output.append(text[opening:])
            break
        closing_end = closing + len(delimiter)
        output.append(text[opening:closing_end])
        position = closing_end
    return "".join(output)


def build_telegram_rich_parts(
    markdown: str,
    references: list[TelegramRichMediaReference],
    uploaded_file_ids: dict[str, str] | None = None,
) -> list[TelegramRichPart]:
    items = telegramify_rich(markdown)
    if not items:
        raise ValueError("Telegram rich-message conversion returned no items")

    parts: list[TelegramRichPart] = []
    for item in items:
        rich_message = dict(rich_message_to_dict(item))
        html = rich_message.get("html")
        uploads: dict[str, Path] = {}
        media: list[dict[str, Any]] = list(rich_message.get("media") or [])
        if isinstance(html, str):
            for reference in references:
                if reference.placeholder_url not in html:
                    continue
                html = html.replace(
                    reference.placeholder_url,
                    f"{TELEGRAM_PHOTO_URI_PREFIX}{reference.media_id}",
                )
                if uploaded_file_ids is not None:
                    media_value = uploaded_file_ids[reference.source_url]
                else:
                    if reference.local_path is None:
                        raise ValueError(
                            f"remote image was prepared without a Telegram file ID: "
                            f"{reference.source_url}",
                        )
                    media_value = f"attach://{reference.media_id}"
                    uploads[reference.media_id] = reference.local_path
                media.append(
                    {
                        "id": reference.media_id,
                        "media": {"type": "photo", "media": media_value},
                    },
                )
            rich_message["html"] = html
        if media:
            if len(media) > MAX_TELEGRAM_RICH_MEDIA:
                raise ValueError(
                    f"a rich-message part can contain at most {MAX_TELEGRAM_RICH_MEDIA} media items",
                )
            rich_message["media"] = media
        parts.append(TelegramRichPart(rich_message, uploads))
    return parts


def prepare_telegram_rich_parts(
    markdown: str,
    allowed_roots: tuple[Path, ...] = (),
    generated_image_paths: frozenset[Path] = frozenset(),
) -> list[TelegramRichPart]:
    prepared_markdown, references = extract_telegram_rich_media(
        markdown,
        allowed_roots,
        generated_image_paths,
        replace_remote_images=False,
    )
    return build_telegram_rich_parts(prepared_markdown, references)


class TelegramClient:
    def __init__(
        self,
        token: str,
        media_upload_chat_id: int | None = None,
        allowed_media_roots: tuple[Path, ...] = (),
    ) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.media_upload_chat_id = media_upload_chat_id
        self.allowed_media_roots = allowed_media_roots
        self.uploaded_media_file_ids: dict[str, str] = {}
        self.uploaded_sticker_file_ids: dict[str, str] = {}
        self.uploaded_media_lock = threading.Lock()
        self.bot = AsyncTelegramBot(
            token,
            request=HTTPXRequest(connection_pool_size=32, pool_timeout=30),
            get_updates_request=HTTPXRequest(connection_pool_size=1, pool_timeout=30),
        )
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: threading.Thread | None = None
        self.lifecycle_lock = threading.Lock()
        self.started = False
        self.command_queue: queue.Queue[
            tuple[Coroutine[Any, Any, Any] | None, Future[Any]]
        ] = queue.Queue()
        self.command_tasks: set[asyncio.Task[Any]] = set()

    def get_updates(self, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        updates = self._submit(
            self._call_ptb_api(
                "getUpdates",
                lambda: self.bot.get_updates(
                    offset=offset,
                    timeout=timeout_seconds,
                    allowed_updates=["message", "guest_message", "message_reaction"],
                    read_timeout=timeout_seconds + 10,
                ),
            ),
        )
        return [update.to_dict() for update in updates]

    def get_me(self) -> dict[str, Any]:
        result = self._submit(
            self._call_ptb_api("getMe", self.bot.get_me),
        )
        return telegram_api_object_to_dict(result, "getMe")

    def get_webhook_info(self) -> dict[str, Any]:
        result = self._submit(
            self._call_ptb_api("getWebhookInfo", self.bot.get_webhook_info),
        )
        return telegram_api_object_to_dict(result, "getWebhookInfo")

    def delete_webhook(self, drop_pending_updates: bool) -> None:
        self._api("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self._api("getFile", {"file_id": file_id}).get("result", {})

    def download_file(self, file_path: str, destination: Path) -> None:
        self._submit(self._download_file(file_path, destination))
        destination.chmod(0o600)

    async def _download_file(self, file_path: str, destination: Path) -> None:
        file_url = f"{self.base_url.replace('/bot', '/file/bot')}/{file_path}"
        for attempt in range(TELEGRAM_REQUEST_ATTEMPTS):
            try:
                await self._download_file_once(file_url, destination)
                return
            except httpx.HTTPError as err:
                if (
                    attempt + 1 < TELEGRAM_REQUEST_ATTEMPTS
                    and telegram_http_error_is_retryable(err)
                ):
                    await self._wait_before_retry(
                        "file download",
                        telegram_http_retry_delay(err),
                        err,
                    )
                    continue
                raise RuntimeError(
                    f"Telegram file download failed: {type(err).__name__}",
                ) from err

    async def _download_file_once(self, file_url: str, destination: Path) -> None:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("GET", file_url) as response:
                response.raise_for_status()
                with destination.open("wb") as destination_file:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if chunk:
                            destination_file.write(chunk)

    def answer_guest_query(
        self,
        guest_query_id: str,
        text: str,
        generated_image_paths: frozenset[Path] = frozenset(),
        queued_files: tuple[QueuedTelegramFile, ...] = (),
    ) -> None:
        prepared_markdown, references = extract_telegram_rich_media(
            text,
            self.allowed_media_roots,
            generated_image_paths,
            replace_remote_images=True,
        )
        uploaded_file_ids: dict[str, str] = {}
        upload_message_ids: list[int] = []
        try:
            for reference in references:
                uploaded = self.upload_photo_for_guest(reference)
                uploaded_file_ids[reference.source_url] = uploaded.file_id
                if uploaded.message_id is not None:
                    upload_message_ids.append(uploaded.message_id)
            if queued_files and not prepared_markdown.strip():
                rich_message: dict[str, Any] = {"html": ""}
            else:
                parts = build_telegram_rich_parts(
                    prepared_markdown,
                    references,
                    uploaded_file_ids,
                )
                rich_message = parts[0].rich_message

            if queued_files:
                rich_message = dict(rich_message)
                media = list(rich_message.get("media") or [])
                if len(media) + len(queued_files) > MAX_TELEGRAM_RICH_MEDIA:
                    raise ValueError(
                        f"a guest response can contain at most {MAX_TELEGRAM_RICH_MEDIA} media items",
                    )
                document_html = []
                for index, queued_file in enumerate(queued_files):
                    uploaded = self.upload_document_for_guest(
                        queued_file.path,
                        upload_dir=queued_file.upload_dir,
                    )
                    if uploaded.message_id is not None:
                        upload_message_ids.append(uploaded.message_id)
                    media_id = f"document_{index}"
                    document = f'<tg-document src="tg://document?id={media_id}"></tg-document>'
                    if queued_file.caption:
                        document = (
                            f"<figure>{document}<figcaption>"
                            f"{escape(queued_file.caption)}</figcaption></figure>"
                        )
                    document_html.append(document)
                    media.append(
                        {
                            "id": media_id,
                            "media": {"type": "document", "media": uploaded.file_id},
                        },
                    )
                rich_message["html"] = (rich_message.get("html") or "") + "".join(document_html)
                rich_message["media"] = media

            self._api(
                "answerGuestQuery",
                {
                    "guest_query_id": guest_query_id,
                    "result": self.build_guest_rich_result(rich_message),
                },
            )
        finally:
            self.delete_guest_upload_messages(upload_message_ids)

    def upload_photo_for_guest(
        self,
        reference: TelegramRichMediaReference,
    ) -> GuestUploadedPhoto:
        if self.media_upload_chat_id is None:
            raise ValueError(
                "CODEX_TELEGRAM_MEDIA_UPLOAD_CHAT_ID is required to embed images in guest responses",
            )
        cache_key = guest_media_cache_key(reference)
        with self.uploaded_media_lock:
            cached_file_id = self.uploaded_media_file_ids.get(cache_key)
            if cached_file_id:
                return GuestUploadedPhoto(cached_file_id, None)

            photo: Path | str = reference.local_path or reference.source_url
            result = self._submit(
                self._call_ptb_api(
                    "sendPhoto",
                    lambda: self.bot.send_photo(
                        chat_id=self.media_upload_chat_id,
                        photo=photo,
                    ),
                ),
            )
            message_id = telegram_uploaded_message_id(result)
            try:
                file_id = telegram_uploaded_photo_file_id(result)
            except Exception:
                if message_id is not None:
                    self.delete_guest_upload_messages([message_id])
                raise
            self.uploaded_media_file_ids[cache_key] = file_id
            return GuestUploadedPhoto(file_id, message_id)

    def delete_guest_upload_messages(self, message_ids: list[int]) -> None:
        if not message_ids or self.media_upload_chat_id is None:
            return
        try:
            self._api(
                "deleteMessages",
                {
                    "chat_id": self.media_upload_chat_id,
                    "message_ids": message_ids,
                },
            )
        except Exception:
            logger.warning(
                "failed to delete %s guest media upload message(s) from chat %s",
                len(message_ids),
                self.media_upload_chat_id,
                exc_info=True,
            )

    def answer_guest_query_with_plain_text(self, guest_query_id: str, text: str) -> None:
        self._api(
            "answerGuestQuery",
            {
                "guest_query_id": guest_query_id,
                "result": self.build_guest_text_result(split_text(text, 4096)[0]),
            },
        )

    def answer_guest_query_with_sticker(
        self,
        guest_query_id: str,
        sticker_path: Path,
    ) -> None:
        uploaded = self.upload_sticker_for_guest(sticker_path)
        try:
            self._api(
                "answerGuestQuery",
                {
                    "guest_query_id": guest_query_id,
                    "result": {
                        "type": "sticker",
                        "id": str(uuid.uuid4()),
                        "sticker_file_id": uploaded.file_id,
                    },
                },
            )
        finally:
            if uploaded.message_id is not None:
                self.delete_guest_upload_messages([uploaded.message_id])

    def send_chat_action(
        self,
        chat_id: int,
        message_thread_id: int | None,
        action: str,
    ) -> None:
        self._submit(
            self.bot.send_chat_action(
                chat_id=chat_id,
                action=action,
                message_thread_id=message_thread_id,
            ),
        )

    def send_typing_action(self, chat_id: int, message_thread_id: int | None) -> None:
        self.send_chat_action(chat_id, message_thread_id, "typing")

    def set_message_reaction(self, chat_id: int, message_id: int, reaction: str) -> None:
        response = self._api(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": reaction}],
            },
        )
        if response.get("result") is not True:
            raise RuntimeError("Telegram setMessageReaction returned an unsuccessful result")

    def clear_message_reaction(self, chat_id: int, message_id: int) -> None:
        response = self._api(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [],
            },
        )
        if response.get("result") is not True:
            raise RuntimeError("Telegram setMessageReaction returned an unsuccessful result")

    def send_sticker(
        self,
        chat_id: int,
        sticker_path: Path,
        message_thread_id: int | None = None,
    ) -> int | None:
        cache_key = local_media_cache_key(sticker_path)
        payload: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        with self.uploaded_media_lock:
            cached_file_id = self.uploaded_sticker_file_ids.get(cache_key)
            if cached_file_id:
                payload["sticker"] = cached_file_id
                response = self._api("sendSticker", payload)
            else:
                payload["sticker"] = "attach://sticker"
                response = self._api_multipart(
                    "sendSticker",
                    payload,
                    {"sticker": sticker_path},
                )
                self.uploaded_sticker_file_ids[cache_key] = telegram_sticker_file_id(response)
        return telegram_response_message_id(response)

    def upload_sticker_for_guest(self, sticker_path: Path) -> GuestUploadedPhoto:
        if self.media_upload_chat_id is None:
            raise ValueError(
                "CODEX_TELEGRAM_MEDIA_UPLOAD_CHAT_ID is required to send stickers "
                "in guest responses",
            )
        cache_key = local_media_cache_key(sticker_path)
        with self.uploaded_media_lock:
            cached_file_id = self.uploaded_sticker_file_ids.get(cache_key)
            if cached_file_id:
                return GuestUploadedPhoto(cached_file_id, None)

            response = self._api_multipart(
                "sendSticker",
                {
                    "chat_id": self.media_upload_chat_id,
                    "sticker": "attach://sticker",
                },
                {"sticker": sticker_path},
            )
            file_id = telegram_sticker_file_id(response)
            self.uploaded_sticker_file_ids[cache_key] = file_id
            return GuestUploadedPhoto(
                file_id,
                telegram_response_message_id(response),
            )

    def send_document(
        self,
        chat_id: int,
        path: Path,
        caption: str | None = None,
        message_thread_id: int | None = None,
        *,
        upload_dir: Path | None = None,
    ) -> int | None:
        if upload_dir is not None:
            path = validate_upload_file(path, upload_dir)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "document": "attach://document",
        }
        if caption is not None:
            payload["caption"] = caption
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if upload_dir is not None:
            response = self._api_multipart(
                "sendDocument",
                payload,
                {"document": path},
                upload_roots={"document": upload_dir},
            )
        else:
            response = self._api_multipart("sendDocument", payload, {"document": path})
        return telegram_response_message_id(response)

    def upload_document_for_guest(
        self,
        path: Path,
        *,
        upload_dir: Path | None = None,
    ) -> GuestUploadedPhoto:
        if self.media_upload_chat_id is None:
            raise ValueError(
                "CODEX_TELEGRAM_MEDIA_UPLOAD_CHAT_ID is required to send files in guest responses",
            )
        if upload_dir is not None:
            path = validate_upload_file(path, upload_dir)
        payload = {
            "chat_id": self.media_upload_chat_id,
            "document": "attach://document",
        }
        if upload_dir is not None:
            response = self._api_multipart(
                "sendDocument",
                payload,
                {"document": path},
                upload_roots={"document": upload_dir},
            )
        else:
            response = self._api_multipart("sendDocument", payload, {"document": path})
        message_id = telegram_response_message_id(response)
        try:
            file_id = telegram_document_file_id(response)
        except Exception:
            if message_id is not None:
                self.delete_guest_upload_messages([message_id])
            raise
        return GuestUploadedPhoto(file_id, message_id)

    def send_rich_message(
        self,
        chat_id: int,
        text: str,
        message_id: int | None,
        message_thread_id: int | None,
        generated_image_paths: frozenset[Path] = frozenset(),
    ) -> int | None:
        parts = prepare_telegram_rich_parts(
            text,
            self.allowed_media_roots,
            generated_image_paths,
        )

        sent_message_id = None
        for index, part in enumerate(parts):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "rich_message": part.rich_message,
            }
            if index == 0 and message_id is not None:
                payload["reply_parameters"] = {"message_id": message_id}
            if message_thread_id is not None:
                payload["message_thread_id"] = message_thread_id

            response = (
                self._api_multipart("sendRichMessage", payload, part.uploads)
                if part.uploads
                else self._api("sendRichMessage", payload)
            )
            response_message_id = telegram_response_message_id(response)
            if response_message_id is not None:
                sent_message_id = response_message_id

        return sent_message_id

    def send_plain_message(
        self,
        chat_id: int,
        text: str,
        message_id: int | None,
        message_thread_id: int | None,
    ) -> int | None:
        sent_message_id = None
        for index, chunk in enumerate(split_text(text, 4096)):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
            }
            if index == 0 and message_id is not None:
                payload["reply_parameters"] = {"message_id": message_id}
            if message_thread_id is not None:
                payload["message_thread_id"] = message_thread_id

            response = self._api("sendMessage", payload)
            response_message_id = telegram_response_message_id(response)
            if response_message_id is not None:
                sent_message_id = response_message_id

        return sent_message_id

    def build_guest_rich_result(self, rich_message: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "article",
            "id": str(uuid.uuid4()),
            "title": "Codex response",
            "input_message_content": {
                "rich_message": rich_message,
            },
        }

    def build_guest_text_result(self, text: str) -> dict[str, Any]:
        return {
            "type": "article",
            "id": str(uuid.uuid4()),
            "title": "Codex response",
            "input_message_content": {
                "message_text": text,
            },
        }

    def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._submit(self._call_api(method, payload))

    def _api_multipart(
        self,
        method: str,
        payload: dict[str, Any],
        uploads: dict[str, Path],
        upload_roots: dict[str, Path] | None = None,
    ) -> dict[str, Any]:
        if upload_roots:
            return self._submit(
                self._call_multipart_api(method, payload, uploads, upload_roots=upload_roots),
            )
        return self._submit(self._call_multipart_api(method, payload, uploads))

    async def _call_api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self._call_ptb_api(
            method,
            lambda: self.bot.do_api_request(
                method,
                api_kwargs=payload,
                read_timeout=60,
            ),
        )
        return {"ok": True, "result": result}

    async def _call_ptb_api(
        self,
        method: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        for attempt in range(TELEGRAM_REQUEST_ATTEMPTS):
            try:
                return await operation()
            except RetryAfter as err:
                retry_error = err
                delay = telegram_retry_after_delay(err)
            except BadRequest as err:
                raise RuntimeError(f"Telegram {method} failed: {err}") from err
            except NetworkError as err:
                retry_error = err
                delay = TELEGRAM_NETWORK_RETRY_DELAY_SECONDS
            except Exception as err:
                raise RuntimeError(f"Telegram {method} failed: {err}") from err

            if attempt + 1 >= TELEGRAM_REQUEST_ATTEMPTS:
                raise RuntimeError(
                    f"Telegram {method} failed: {retry_error}",
                ) from retry_error
            await self._wait_before_retry(method, delay, retry_error)

        raise RuntimeError(f"Telegram {method} failed after retry")

    async def _wait_before_retry(
        self,
        method: str,
        delay: float,
        error: Exception,
    ) -> None:
        logger.warning(
            "Telegram %s failed with %s; retrying in %.1f seconds",
            method,
            type(error).__name__,
            delay,
        )
        await asyncio.sleep(delay)

    async def _call_multipart_api(
        self,
        method: str,
        payload: dict[str, Any],
        uploads: dict[str, Path],
        upload_roots: dict[str, Path] | None = None,
    ) -> dict[str, Any]:
        form_data = {
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list))
            else "true" if value is True
            else "false" if value is False
            else str(value)
            for key, value in payload.items()
            if value is not None
        }
        for attempt in range(TELEGRAM_REQUEST_ATTEMPTS):
            try:
                if upload_roots:
                    response = await self._post_multipart_api(
                        method,
                        form_data,
                        uploads,
                        upload_roots=upload_roots,
                    )
                else:
                    response = await self._post_multipart_api(method, form_data, uploads)
                try:
                    result = response.json()
                except ValueError as err:
                    response.raise_for_status()
                    raise RuntimeError(
                        f"Telegram {method} returned a non-JSON response",
                    ) from err
                if not isinstance(result, dict):
                    raise RuntimeError(f"Telegram {method} returned an invalid response")
                if response.is_success and result.get("ok") is True:
                    return result
                retry_after = result.get("parameters", {}).get("retry_after")
                if (
                    attempt + 1 < TELEGRAM_REQUEST_ATTEMPTS
                    and isinstance(retry_after, (int, float))
                ):
                    await self._wait_before_retry(
                        method,
                        float(retry_after),
                        RuntimeError("RetryAfter"),
                    )
                    continue
                if (
                    attempt + 1 < TELEGRAM_REQUEST_ATTEMPTS
                    and telegram_http_status_is_retryable(response.status_code)
                ):
                    await self._wait_before_retry(
                        method,
                        telegram_response_retry_delay(response),
                        RuntimeError(f"HTTP {response.status_code}"),
                    )
                    continue
                description = result.get("description") or response.reason_phrase
                raise RuntimeError(f"Telegram {method} failed: {description}")
            except httpx.HTTPError as err:
                if (
                    attempt + 1 < TELEGRAM_REQUEST_ATTEMPTS
                    and telegram_http_error_is_retryable(err)
                ):
                    await self._wait_before_retry(
                        method,
                        telegram_http_retry_delay(err),
                        err,
                    )
                    continue
                raise RuntimeError(f"Telegram {method} failed: {err}") from err
        raise RuntimeError(f"Telegram {method} failed after retry")

    async def _post_multipart_api(
        self,
        method: str,
        form_data: dict[str, str],
        uploads: dict[str, Path],
        upload_roots: dict[str, Path] | None = None,
    ) -> httpx.Response:
        with contextlib.ExitStack() as stack:
            files = {}
            for field_name, path in uploads.items():
                upload_root = upload_roots.get(field_name) if upload_roots else None
                source = (
                    open_upload_file(path, upload_root)
                    if upload_root is not None
                    else path.open("rb")
                )
                files[field_name] = (
                    path.name,
                    stack.enter_context(source),
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                )
            async with httpx.AsyncClient(timeout=60) as client:
                return await client.post(
                    f"{self.base_url}/{method}",
                    data=form_data,
                    files=files,
                )

    def _submit(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        self._ensure_started()
        return self._submit_started(coroutine)

    def _submit_started(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        future: Future[Any] = Future()
        self.command_queue.put((coroutine, future))
        return future.result()

    async def _dispatch_commands(self, loop_ready: threading.Event) -> None:
        loop_ready.set()
        while True:
            while True:
                try:
                    coroutine, future = self.command_queue.get_nowait()
                except queue.Empty:
                    break
                if coroutine is None:
                    future.set_result(None)
                    asyncio.get_running_loop().stop()
                    return
                task = asyncio.create_task(self._execute_command(coroutine, future))
                self.command_tasks.add(task)
                task.add_done_callback(self.command_tasks.discard)
            await asyncio.sleep(0.01)

    async def _execute_command(
        self,
        coroutine: Coroutine[Any, Any, Any],
        future: Future[Any],
    ) -> None:
        try:
            result = await coroutine
        except BaseException as err:
            future.set_exception(err)
        else:
            future.set_result(result)

    def _ensure_started(self) -> None:
        with self.lifecycle_lock:
            if self.started:
                return

            loop = asyncio.new_event_loop()
            loop_ready = threading.Event()

            def run_loop() -> None:
                asyncio.set_event_loop(loop)
                loop.create_task(self._dispatch_commands(loop_ready))
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            loop_thread = threading.Thread(
                target=run_loop,
                name="telegram-asyncio",
                daemon=True,
            )
            loop_thread.start()
            loop_ready.wait()
            self.loop = loop
            self.loop_thread = loop_thread
            self.started = True
            try:
                self._submit_started(
                    self._call_ptb_api("initialize", self.bot.initialize),
                )
            except BaseException:
                stop_future: Future[Any] = Future()
                self.command_queue.put((None, stop_future))
                stop_future.result(timeout=1)
                loop_thread.join(timeout=1)
                self.loop = None
                self.loop_thread = None
                self.started = False
                raise

    def close(self) -> None:
        with self.lifecycle_lock:
            if not self.started:
                return
            loop = self.loop
            loop_thread = self.loop_thread
            self.started = False
            self.loop = None
            self.loop_thread = None

        if loop is None:
            return
        try:
            self._submit_started(self.bot.shutdown())
        except Exception:
            logger.warning("failed to shut down Telegram client cleanly", exc_info=True)
        stop_future = Future()
        self.command_queue.put((None, stop_future))
        try:
            stop_future.result(timeout=1)
        except Exception:
            logger.warning("failed to stop Telegram asyncio loop cleanly", exc_info=True)
        if loop_thread and loop_thread is not threading.current_thread():
            loop_thread.join(timeout=1)


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    last_message_at: float
    last_compacted_at: float | None = None
    last_bot_message_id: int | None = None


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.sessions, self.response_errors, migrated = self._load()
        if migrated:
            self._save()
            logger.info(
                "migrated Codex session store %s to the latest schema",
                self.path,
            )

    def get(self, chat_id: ConversationKey) -> str | None:
        record = self.get_record(chat_id)
        return record.session_id if record else None

    def get_record(self, chat_id: ConversationKey) -> SessionRecord | None:
        with self.lock:
            return self.sessions.get(str(chat_id))

    def records(self) -> list[tuple[ConversationKey, SessionRecord]]:
        with self.lock:
            return [
                (parse_conversation_storage_key(chat_id), record)
                for chat_id, record in self.sessions.items()
            ]

    def has_session(self, session_id: str) -> bool:
        with self.lock:
            return any(record.session_id == session_id for record in self.sessions.values())

    def conversation_keys_for_session(self, session_id: str) -> list[ConversationKey]:
        with self.lock:
            return [
                parse_conversation_storage_key(chat_id)
                for chat_id, record in self.sessions.items()
                if record.session_id == session_id
            ]

    def set(
        self,
        chat_id: ConversationKey,
        session_id: str,
        last_message_at: float | None = None,
    ) -> None:
        with self.lock:
            old_record = self.sessions.get(str(chat_id))
            last_compacted_at = (
                old_record.last_compacted_at
                if old_record and old_record.session_id == session_id
                else None
            )
            last_bot_message_id = (
                old_record.last_bot_message_id
                if old_record and old_record.session_id == session_id
                else None
            )
            self.sessions[str(chat_id)] = SessionRecord(
                session_id=session_id,
                last_message_at=last_message_at if last_message_at is not None else time.time(),
                last_compacted_at=last_compacted_at,
                last_bot_message_id=last_bot_message_id,
            )
            self._save()

    def touch(self, chat_id: ConversationKey, last_message_at: float | None = None) -> None:
        with self.lock:
            record = self.sessions.get(str(chat_id))
            if not record:
                return
            self.sessions[str(chat_id)] = SessionRecord(
                session_id=record.session_id,
                last_message_at=last_message_at if last_message_at is not None else time.time(),
                last_compacted_at=record.last_compacted_at,
                last_bot_message_id=record.last_bot_message_id,
            )
            self._save()

    def mark_compacted(
        self,
        chat_id: ConversationKey,
        session_id: str,
        compacted_at: float | None = None,
    ) -> bool:
        with self.lock:
            record = self.sessions.get(str(chat_id))
            if not record or record.session_id != session_id:
                return False
            self.sessions[str(chat_id)] = SessionRecord(
                session_id=record.session_id,
                last_message_at=record.last_message_at,
                last_compacted_at=compacted_at if compacted_at is not None else time.time(),
                last_bot_message_id=record.last_bot_message_id,
            )
            self._save()
            return True

    def record_bot_message(
        self,
        chat_id: ConversationKey,
        session_id: str,
        message_id: int,
    ) -> bool:
        if not valid_telegram_message_id(message_id):
            return False

        with self.lock:
            record = self.sessions.get(str(chat_id))
            if not record or record.session_id != session_id:
                return False
            self.sessions[str(chat_id)] = SessionRecord(
                session_id=record.session_id,
                last_message_at=record.last_message_at,
                last_compacted_at=record.last_compacted_at,
                last_bot_message_id=message_id,
            )
            self._save()
            return True

    def record_response_error(self, chat_id: ConversationKey, error: str) -> bool:
        normalized_error = error.strip()
        if not normalized_error:
            return False

        with self.lock:
            self.response_errors[str(chat_id)] = normalized_error
            self._save()
            return True

    def get_response_error(self, chat_id: ConversationKey) -> str | None:
        with self.lock:
            return self.response_errors.get(str(chat_id))

    def clear_response_error(self, chat_id: ConversationKey) -> bool:
        with self.lock:
            if str(chat_id) not in self.response_errors:
                return False
            del self.response_errors[str(chat_id)]
            self._save()
            return True

    def remove(self, chat_id: ConversationKey, session_id: str) -> bool:
        with self.lock:
            record = self.sessions.get(str(chat_id))
            if not record or record.session_id != session_id:
                return False
            del self.sessions[str(chat_id)]
            self.response_errors.pop(str(chat_id), None)
            self._save()
            return True

    def remove_session(self, session_id: str) -> list[ConversationKey]:
        with self.lock:
            chat_ids = [
                chat_id
                for chat_id, record in self.sessions.items()
                if record.session_id == session_id
            ]
            if not chat_ids:
                return []
            for chat_id in chat_ids:
                del self.sessions[chat_id]
                self.response_errors.pop(chat_id, None)
            self._save()
            return [parse_conversation_storage_key(chat_id) for chat_id in chat_ids]

    def _load(self) -> tuple[dict[str, SessionRecord], dict[str, str], bool]:
        if not self.path.exists():
            return {}, {}, False

        try:
            contents = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise ValueError(f"cannot read Codex session store {self.path}: {err}") from err

        if not isinstance(contents, dict) or contents.get("version") not in {1, 2, 3, 4}:
            raise ValueError(f"invalid Codex session store format in {self.path}")

        sessions = contents.get("sessions")
        if not isinstance(sessions, dict):
            raise ValueError(f"invalid Codex session mapping in {self.path}")

        if contents["version"] == 1:
            if not all(
                valid_conversation_storage_key(chat_id) and isinstance(session_id, str)
                for chat_id, session_id in sessions.items()
            ):
                raise ValueError(f"invalid Codex session mapping in {self.path}")
            migrated_at = time.time()
            return {
                chat_id: SessionRecord(session_id, migrated_at)
                for chat_id, session_id in sessions.items()
            }, {}, True

        response_errors = contents.get("response_errors", {})
        if not isinstance(response_errors, dict) or not all(
            valid_conversation_storage_key(chat_id)
            and isinstance(error, str)
            and bool(error.strip())
            for chat_id, error in response_errors.items()
        ):
            raise ValueError(f"invalid Telegram response errors in {self.path}")
        loaded_response_errors = dict(response_errors)
        migrated_embedded_errors = False

        loaded_sessions: dict[str, SessionRecord] = {}
        for chat_id, value in sessions.items():
            if not valid_conversation_storage_key(chat_id) or not isinstance(value, dict):
                raise ValueError(f"invalid Codex session mapping in {self.path}")
            session_id = value.get("session_id")
            last_message_at = value.get("last_message_at")
            last_compacted_at = value.get("last_compacted_at")
            last_bot_message_id = value.get("last_bot_message_id")
            previous_response_error = value.get("previous_response_error")
            if (
                not isinstance(session_id, str)
                or not isinstance(last_message_at, (int, float))
                or isinstance(last_message_at, bool)
                or (
                    last_compacted_at is not None
                    and (
                        not isinstance(last_compacted_at, (int, float))
                        or isinstance(last_compacted_at, bool)
                    )
                )
                or (
                    last_bot_message_id is not None
                    and not valid_telegram_message_id(last_bot_message_id)
                )
                or (
                    previous_response_error is not None
                    and not isinstance(previous_response_error, str)
                )
            ):
                raise ValueError(f"invalid Codex session mapping in {self.path}")
            loaded_sessions[chat_id] = SessionRecord(
                session_id,
                float(last_message_at),
                float(last_compacted_at) if last_compacted_at is not None else None,
                last_bot_message_id,
            )
            if previous_response_error:
                loaded_response_errors.setdefault(chat_id, previous_response_error)
                migrated_embedded_errors = True

        return (
            loaded_sessions,
            loaded_response_errors,
            contents["version"] in {2, 3} or migrated_embedded_errors,
        )

    def _save(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "version": 4,
                "sessions": {
                    chat_id: {
                        "session_id": record.session_id,
                        "last_message_at": record.last_message_at,
                        "last_compacted_at": record.last_compacted_at,
                        "last_bot_message_id": record.last_bot_message_id,
                    }
                    for chat_id, record in self.sessions.items()
                },
                "response_errors": self.response_errors,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.path.parent,
                encoding="utf-8",
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temp_file:
                temp_file.write(payload)
                temp_file.write("\n")
                temp_path = Path(temp_file.name)
            temp_path.replace(self.path)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()


def resolve_session_prompt(
    prompt: str | list[str] | Callable[[SessionRecord | None], str | list[str]],
    record: SessionRecord | None,
) -> str | list[str]:
    return prompt(record) if callable(prompt) else prompt


class CodexAppServerClient:
    def __init__(
        self,
        config: Config,
        notification_handler: Callable[[dict[str, Any]], None] | None = None,
        server_request_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        server_request_preparer: Callable[
            [dict[str, Any]], tuple[dict[str, Any], Callable[[], None] | None]
        ] | None = None,
    ) -> None:
        self.config = config
        self.notification_handler = notification_handler
        self.server_request_handler = server_request_handler
        self.server_request_preparer = server_request_preparer
        self.connection: websocket.WebSocket | None = None
        self.reader_thread: threading.Thread | None = None
        self.lifecycle_lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.routing_lock = threading.Lock()
        self.pending_requests: dict[int, queue.Queue[Any]] = {}
        self.notification_queues: dict[str, set[queue.Queue[Any]]] = {}
        self.successful_dynamic_tool_turns: set[str] = set()
        self.non_commentary_agent_message_turns: set[str] = set()
        self.next_request_id = 1
        self.generation = 0

    def __enter__(self) -> "CodexAppServerClient":
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def start(self) -> None:
        with self.lifecycle_lock:
            if self.connection is not None and self.connection.connected:
                return

            self._close_connection()
            self._connect()
            try:
                self._request_started(
                    "initialize",
                    {
                    "clientInfo": {
                        "name": "codex_telegram_bot",
                        "title": "Codex Telegram Bot",
                        "version": "1.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                )
                self._notify_started("initialized", {})
            except BaseException:
                self._close_connection()
                raise

            self.generation += 1
            logger.info(
                "connected to shared Codex app-server generation %s via %s",
                self.generation,
                self.config.app_server_socket_path,
            )

    def _connect(self) -> None:
        unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix_socket.settimeout(min(self.config.session_maintenance_timeout_seconds, 10))
        try:
            unix_socket.connect(str(self.config.app_server_socket_path))
            connection = websocket.create_connection(
                "ws://localhost/",
                socket=unix_socket,
                enable_multithread=True,
                suppress_origin=True,
                timeout=min(self.config.session_maintenance_timeout_seconds, 10),
            )
        except (OSError, websocket.WebSocketException) as err:
            unix_socket.close()
            raise CodexAppServerError(
                f"cannot connect to Codex app-server socket "
                f"{self.config.app_server_socket_path}: {err}",
            ) from err

        connection.settimeout(None)
        self.connection = connection
        self.reader_thread = threading.Thread(
            target=self._read_messages,
            args=(connection,),
            name="codex-app-server-reader",
            daemon=True,
        )
        self.reader_thread.start()

    def start_thread(self, dynamic_tools: list[dict[str, Any]] | None = None) -> str:
        params = self._thread_configuration(dynamic_tools)
        params["threadSource"] = "codex_telegram_bot"
        result = self.request(
            "thread/start",
            params,
        )
        return response_thread_id(result, "thread/start")

    def set_thread_name(self, thread_id: str, name: str) -> None:
        self.request(
            "thread/name/set",
            {
                "threadId": thread_id,
                "name": name,
            },
        )

    def resume_thread(
        self,
        thread_id: str,
        dynamic_tools: list[dict[str, Any]] | None = None,
    ) -> None:
        params = self._thread_configuration(dynamic_tools)
        params["threadId"] = thread_id
        result = self.request(
            "thread/resume",
            params,
        )
        resumed_thread_id = response_thread_id(result, "thread/resume")
        if resumed_thread_id != thread_id:
            raise CodexAppServerError(
                f"Codex app-server resumed thread {resumed_thread_id} instead of {thread_id}",
            )

    def _thread_configuration(
        self,
        dynamic_tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        instructions_path = self.config.instructions_file
        base_instructions = None
        if instructions_path is not None:
            try:
                base_instructions = instructions_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                logger.warning(
                    "Codex bot instructions file %s is missing; using standard Codex instructions",
                    instructions_path,
                )
            except OSError as err:
                raise CodexAppServerError(
                    f"cannot read Codex bot instructions from {instructions_path}: {err}",
                ) from err

        configuration = {
            "cwd": str(self.config.repo_dir),
            "approvalPolicy": "never" if self.config.read_only else "on-request",
            "sandbox": "read-only" if self.config.read_only else "workspace-write",
            "config": {
                "mcp_servers": {"openaiDeveloperDocs": {"enabled": False}},
                "web_search": "live",
            },
        }
        if not self.config.read_only:
            configuration["approvalsReviewer"] = "auto_review"
        if base_instructions is not None:
            configuration["baseInstructions"] = base_instructions
        if dynamic_tools is not None:
            configuration["dynamicTools"] = dynamic_tools
        return configuration

    def run_turn(
        self,
        thread_id: str,
        prompt: str | list[str],
        image_paths: list[Path] | None,
        agent_message_handler: Callable[[str], None] | None,
        fast_mode: bool,
    ) -> str:
        structured_handler = (
            (lambda message: agent_message_handler(message.text))
            if agent_message_handler
            else None
        )
        handle = self.start_turn(
            thread_id,
            prompt,
            image_paths,
            structured_handler,
            fast_mode,
        )
        return self.wait_turn(handle)

    def start_turn(
        self,
        thread_id: str,
        prompt: str | list[str],
        image_paths: list[Path] | None,
        agent_message_handler: Callable[[CodexAgentMessage], None] | None,
        fast_mode: bool,
        generated_image_handler: Callable[[CodexGeneratedImage], None] | None = None,
        image_generation_started_handler: Callable[[], None] | None = None,
    ) -> CodexTurnHandle:
        event_queue = self._register_notification_queue(thread_id)
        try:
            params = {
                "threadId": thread_id,
                "input": self._turn_inputs(prompt, image_paths),
                "cwd": str(self.config.repo_dir),
                "approvalPolicy": "never" if self.config.read_only else "on-request",
                "serviceTier": "fast" if fast_mode else None,
            }
            if self.config.read_only:
                params["sandboxPolicy"] = {"type": "readOnly"}
            else:
                # Inherit workspace-write and its effective roots/network settings
                # from thread/start or thread/resume instead of resetting them.
                params["approvalsReviewer"] = "auto_review"
            result = self.request(
                "turn/start",
                params,
            )
            turn = result.get("turn") if isinstance(result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise CodexAppServerError("Codex app-server turn/start returned no turn ID")
            return CodexTurnHandle(
                thread_id,
                turn_id,
                event_queue,
                agent_message_handler,
                generated_image_handler,
                image_generation_started_handler,
            )
        except BaseException:
            self._unregister_notification_queue(thread_id, event_queue)
            raise

    def steer_turn(
        self,
        handle: CodexTurnHandle,
        prompt: str | list[str],
        image_paths: list[Path] | None,
    ) -> None:
        try:
            result = self.request(
                "turn/steer",
                {
                    "threadId": handle.thread_id,
                    "input": self._turn_inputs(prompt, image_paths),
                    "expectedTurnId": handle.turn_id,
                },
            )
        except CodexAppServerError as err:
            if is_inactive_turn_error(err):
                raise CodexTurnInactiveError(
                    str(err),
                    request_method=err.request_method,
                    error_code=err.error_code,
                    app_server_message=err.app_server_message,
                ) from err
            raise
        turn_id = response_turn_id(result, "turn/steer")
        if turn_id != handle.turn_id:
            raise CodexTurnInactiveError(
                f"Codex app-server steered turn {turn_id} instead of {handle.turn_id}",
                request_method="turn/steer",
            )
        logger.info("steered active Codex turn %s", handle.turn_id)

    def wait_turn(self, handle: CodexTurnHandle) -> str:
        try:
            return self._wait_for_turn(handle)
        finally:
            with self.routing_lock:
                self.successful_dynamic_tool_turns.discard(handle.turn_id)
                self.non_commentary_agent_message_turns.discard(handle.turn_id)
            if not handle.closed:
                handle.closed = True
                self._unregister_notification_queue(handle.thread_id, handle.event_queue)

    def _turn_inputs(
        self,
        prompt: str | list[str],
        image_paths: list[Path] | None,
    ) -> list[dict[str, Any]]:
        text_inputs = [prompt] if isinstance(prompt, str) else prompt
        inputs: list[dict[str, Any]] = [
            {"type": "text", "text": text_input}
            for text_input in text_inputs
        ]
        inputs.extend(
            {"type": "localImage", "path": str(image_path.resolve())}
            for image_path in image_paths or []
        )
        return inputs

    def _wait_for_turn(
        self,
        handle: CodexTurnHandle,
    ) -> str:
        event_queue = handle.event_queue
        thread_id = handle.thread_id
        turn_id = handle.turn_id
        deadline = (
            time.monotonic() + self.config.codex_timeout_seconds
            if self.config.codex_timeout_seconds is not None
            else None
        )
        agent_messages: list[str] = []
        final_message: str | None = None

        while True:
            try:
                message = self._next_notification(
                    event_queue,
                    deadline,
                    f"Codex turn {turn_id}",
                )
            except TimeoutError:
                try:
                    self.request(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": turn_id},
                        timeout=10,
                    )
                except Exception:
                    logger.exception("failed to interrupt timed-out Codex turn %s", turn_id)
                raise

            if message.get("method") == "item/started":
                params = message.get("params")
                if not isinstance(params, dict) or params.get("turnId") != turn_id:
                    continue
                item = params.get("item")
                if not isinstance(item, dict) or item.get("type") != "imageGeneration":
                    continue
                if handle.image_generation_started_handler:
                    try:
                        handle.image_generation_started_handler()
                    except Exception:
                        logger.exception(
                            "failed to report image generation start for thread %s",
                            thread_id,
                        )
                continue

            if message.get("method") == "item/completed":
                params = message.get("params")
                if not isinstance(params, dict) or params.get("turnId") != turn_id:
                    continue
                item = params.get("item")
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "imageGeneration":
                    saved_path = item.get("savedPath")
                    if isinstance(saved_path, str) and saved_path and handle.generated_image_handler:
                        try:
                            item_id = item.get("id")
                            handle.generated_image_handler(
                                CodexGeneratedImage(
                                    turn_id,
                                    item_id if isinstance(item_id, str) else None,
                                    Path(saved_path),
                                ),
                            )
                        except Exception:
                            logger.exception(
                                "failed to record generated image for thread %s",
                                thread_id,
                            )
                    continue
                if item.get("type") != "agentMessage":
                    continue
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                text = text.strip()
                agent_messages.append(text)
                if item.get("phase") == "final_answer":
                    final_message = text
                if handle.agent_message_handler:
                    try:
                        item_id = item.get("id")
                        phase = item.get("phase")
                        handle.agent_message_handler(
                            CodexAgentMessage(
                                turn_id,
                                item_id if isinstance(item_id, str) else None,
                                phase if isinstance(phase, str) else None,
                                text,
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "failed to deliver Codex agent message for thread %s",
                            thread_id,
                        )
                continue

            if message.get("method") != "turn/completed":
                continue
            params = message.get("params")
            turn = params.get("turn") if isinstance(params, dict) else None
            if not isinstance(turn, dict) or turn.get("id") != turn_id:
                continue
            status = turn.get("status")
            if status == "completed":
                with self.routing_lock:
                    dynamic_tool_succeeded = turn_id in self.successful_dynamic_tool_turns
                if final_message:
                    return final_message
                if dynamic_tool_succeeded:
                    logger.info(
                        "Codex turn %s finished without a final message after a successful "
                        "dynamic tool call; no fallback response will be sent",
                        turn_id,
                    )
                    return ""
                return (agent_messages[-1] if agent_messages else "") or (
                    "Codex finished without a final message."
                )
            error = turn.get("error") or f"turn ended with status {status or 'unknown'}"
            raise CodexAppServerError(
                f"Codex app-server turn {turn_id} failed: {safe_log_text(json.dumps(error))}",
            )

    def compact_thread(self, thread_id: str) -> None:
        event_queue = self._register_notification_queue(thread_id)
        try:
            self.request("thread/compact/start", {"threadId": thread_id})
            deadline = time.monotonic() + self.config.session_maintenance_timeout_seconds
            compaction_turn_id: str | None = None
            compaction_started = False
            while True:
                message = self._next_notification(
                    event_queue,
                    deadline,
                    f"compaction of thread {thread_id}",
                )
                method = message.get("method")
                params = message.get("params")
                if not isinstance(params, dict) or params.get("threadId") != thread_id:
                    continue
                if method == "thread/compacted":
                    return
                turn = params.get("turn")
                if method == "turn/started" and isinstance(turn, dict):
                    turn_id = turn.get("id")
                    if isinstance(turn_id, str) and turn_id:
                        compaction_turn_id = turn_id
                        compaction_started = True
                    continue
                item = params.get("item")
                if (
                    method in {"item/started", "item/completed"}
                    and isinstance(item, dict)
                    and item.get("type") == "contextCompaction"
                ):
                    compaction_started = True
                    continue
                if method != "turn/completed" or not isinstance(turn, dict):
                    continue
                if not compaction_started:
                    continue
                turn_id = turn.get("id")
                if compaction_turn_id is not None and turn_id != compaction_turn_id:
                    continue
                status = turn.get("status")
                if status == "completed":
                    return
                error = turn.get("error") or f"turn ended with status {status or 'unknown'}"
                raise CodexAppServerError(
                    "Codex compaction failed: "
                    f"{safe_log_text(json.dumps(error, ensure_ascii=False))}",
                )
        finally:
            self._unregister_notification_queue(thread_id, event_queue)

    def unsubscribe_thread(self, thread_id: str) -> None:
        self.request("thread/unsubscribe", {"threadId": thread_id})

    def archive_thread(self, thread_id: str) -> None:
        self.request("thread/archive", {"threadId": thread_id})
        try:
            self.unsubscribe_thread(thread_id)
        except Exception:
            logger.warning(
                "archived Codex thread %s but could not remove its app-server subscription",
                thread_id,
                exc_info=True,
            )

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> Any:
        self.start()
        return self._request_started(method, params, timeout)

    def _request_started(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> Any:
        with self.routing_lock:
            request_id = self.next_request_id
            self.next_request_id += 1
            response_queue: queue.Queue[Any] = queue.Queue()
            self.pending_requests[request_id] = response_queue
        try:
            self._write_started({"id": request_id, "method": method, "params": params})
            try:
                response = response_queue.get(
                    timeout=(
                        timeout
                        if timeout is not None
                        else self.config.session_maintenance_timeout_seconds
                    ),
                )
            except queue.Empty:
                raise CodexAppServerError(
                    f"timed out waiting for Codex app-server request {method}",
                ) from None
        finally:
            with self.routing_lock:
                self.pending_requests.pop(request_id, None)

        if isinstance(response, BaseException):
            raise response
        if not isinstance(response, dict):
            raise CodexAppServerError(f"Codex app-server returned an invalid response to {method}")
        error = response.get("error")
        if error is not None:
            error_code = error.get("code") if isinstance(error, dict) else None
            app_server_message = error.get("message") if isinstance(error, dict) else None
            raise CodexAppServerError(
                f"Codex app-server request {method} failed: {safe_log_text(json.dumps(error))}",
                request_method=method,
                error_code=error_code if isinstance(error_code, int) else None,
                app_server_message=(
                    app_server_message if isinstance(app_server_message, str) else None
                ),
            )
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.start()
        self._notify_started(method, params)

    def _notify_started(self, method: str, params: dict[str, Any]) -> None:
        self._write_started({"method": method, "params": params})

    def close(self) -> None:
        with self.lifecycle_lock:
            self._close_connection()

    def _close_connection(self) -> None:
        connection = self.connection
        self.connection = None
        if connection is None:
            return

        error = CodexAppServerError("Codex app-server connection closed")
        self._broadcast_failure(error)
        try:
            connection.close()
        except (OSError, websocket.WebSocketException):
            logger.debug("failed to close Codex app-server connection cleanly", exc_info=True)
        if self.reader_thread and self.reader_thread is not threading.current_thread():
            self.reader_thread.join(timeout=1)
        self.reader_thread = None

    def _write_started(self, message: dict[str, Any]) -> None:
        connection = self.connection
        if not connection or not connection.connected:
            raise CodexAppServerError("Codex app server is not running")
        try:
            with self.write_lock:
                connection.send(json.dumps(message, ensure_ascii=False))
        except (OSError, websocket.WebSocketException) as err:
            raise CodexAppServerError(
                f"Codex app server stopped accepting requests: {err}",
            ) from err

    def _register_notification_queue(self, thread_id: str) -> queue.Queue[Any]:
        self.start()
        event_queue: queue.Queue[Any] = queue.Queue()
        with self.routing_lock:
            self.notification_queues.setdefault(thread_id, set()).add(event_queue)
        return event_queue

    def _unregister_notification_queue(
        self,
        thread_id: str,
        event_queue: queue.Queue[Any],
    ) -> None:
        with self.routing_lock:
            queues = self.notification_queues.get(thread_id)
            if not queues:
                return
            queues.discard(event_queue)
            if not queues:
                self.notification_queues.pop(thread_id, None)

    def _next_notification(
        self,
        event_queue: queue.Queue[Any],
        deadline: float | None,
        operation: str,
    ) -> dict[str, Any]:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError(f"timed out waiting for {operation}")
        try:
            message = event_queue.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError(f"timed out waiting for {operation}") from None
        if isinstance(message, BaseException):
            raise message
        if not isinstance(message, dict):
            raise CodexAppServerError(f"Codex app-server returned an invalid event for {operation}")
        return message

    def _read_messages(self, connection: websocket.WebSocket) -> None:
        while self.connection is connection and connection.connected:
            try:
                payload = connection.recv()
            except websocket.WebSocketConnectionClosedException:
                break
            except (OSError, websocket.WebSocketException) as err:
                try:
                    connection.close()
                except (OSError, websocket.WebSocketException):
                    pass
                if self.connection is connection:
                    self._broadcast_failure(
                        CodexAppServerError(f"Codex app-server connection failed: {err}"),
                    )
                return

            if payload in (None, "", b""):
                break
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            if not isinstance(payload, str):
                logger.warning("Codex app-server returned an unsupported WebSocket message")
                continue
            try:
                message = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("Codex app-server returned invalid JSON: %s", safe_log_text(payload))
                continue
            if not isinstance(message, dict):
                logger.warning("Codex app-server returned a non-object message")
                continue
            self._route_message(message)

        if self.connection is connection:
            try:
                connection.close()
            except (OSError, websocket.WebSocketException):
                pass
            self._broadcast_failure(
                CodexAppServerError("Codex app-server connection closed unexpectedly"),
            )

    def _route_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is not None and not method:
            with self.routing_lock:
                response_queue = self.pending_requests.get(request_id)
            if response_queue:
                response_queue.put(message)
            else:
                logger.debug("ignored late Codex app-server response id=%s", request_id)
            return

        if request_id is not None and method:
            if method == "item/tool/call" and (
                self.server_request_handler or self.server_request_preparer
            ):
                request_thread = threading.Thread(
                    target=self._handle_server_request,
                    args=(request_id, message.get("params")),
                    name="codex-dynamic-tool-call",
                    daemon=True,
                )
                request_thread.start()
                return

            if method == "item/tool/requestUserInput":
                logger.info(
                    "declining interactive user-input request for a Telegram turn",
                )
                try:
                    self._write_started(
                        {
                            "id": request_id,
                            "error": {
                                "code": -32601,
                                "message": (
                                    "Interactive user input is unavailable for "
                                    "Telegram-initiated turns. Ask the question in your "
                                    "final response and end the turn."
                                ),
                            },
                        },
                    )
                except Exception:
                    logger.exception(
                        "failed to decline interactive Codex user-input request",
                    )
                return

            logger.warning("declining unsupported Codex app-server request %s", method)
            try:
                self._write_started(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": f"Telegram bot does not support server request {method}",
                        },
                    },
                )
            except Exception:
                logger.exception("failed to decline Codex app-server request %s", method)
            return

        params = message.get("params")
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        if method == "item/completed" and isinstance(params, dict):
            turn_id = params.get("turnId")
            item = params.get("item")
            if (
                isinstance(turn_id, str)
                and isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and item.get("phase") != "commentary"
                and isinstance(item.get("text"), str)
                and item["text"].strip()
            ):
                with self.routing_lock:
                    self.non_commentary_agent_message_turns.add(turn_id)
        summary = summarize_app_server_event(message)
        if summary:
            log_progress = (
                logger.debug if method == "item/agentMessage/delta" else logger.info
            )
            if isinstance(thread_id, str):
                log_progress(
                    "codex progress for thread %s: %s",
                    thread_id,
                    safe_log_text(summary),
                )
            else:
                log_progress("codex progress: %s", safe_log_text(summary))
        else:
            logger.debug("Codex app-server event: method=%s", method or "unknown")

        if isinstance(thread_id, str):
            with self.routing_lock:
                event_queues = list(self.notification_queues.get(thread_id, ()))
            for event_queue in event_queues:
                event_queue.put(message)

        if self.notification_handler:
            try:
                self.notification_handler(message)
            except Exception:
                logger.exception(
                    "failed to process Codex app-server notification %s",
                    method or "unknown",
                )

    def _handle_server_request(self, request_id: Any, params: Any) -> None:
        if not isinstance(params, dict):
            try:
                self._write_started(
                    {
                        "id": request_id,
                        "result": dynamic_tool_result(
                            False,
                            "The dynamic tool call has invalid parameters.",
                        ),
                    },
                )
            except Exception:
                logger.exception("failed to decline invalid Codex dynamic tool call")
            return

        if params.get("tool") == TELEGRAM_FILE_TOOL_NAME and self.server_request_preparer:
            try:
                result, action = self.server_request_preparer(params)
            except Exception as err:
                logger.exception("failed to validate Telegram file upload")
                result, action = dynamic_tool_result(False, safe_log_text(str(err))), None
            turn_id = params.get("turnId")
            if result.get("success") is True and isinstance(turn_id, str) and turn_id:
                # A tool-only turn may complete as soon as the acknowledgement arrives.
                with self.routing_lock:
                    self.successful_dynamic_tool_turns.add(turn_id)
            try:
                self._write_started({"id": request_id, "result": result})
            except Exception:
                logger.exception("failed to acknowledge Telegram file upload")
                if isinstance(action, DeferredTelegramAction):
                    action.cancel()
                return
            if result.get("success") is not True:
                return
            if action is not None:
                try:
                    action()
                except Exception:
                    logger.exception("Telegram file upload failed after acknowledgement")
            return

        handler = self.server_request_handler
        if handler is None:
            try:
                self._write_started(
                    {
                        "id": request_id,
                        "result": dynamic_tool_result(
                            False,
                            "Dynamic tools are not available.",
                        ),
                    },
                )
            except Exception:
                logger.exception("failed to decline unavailable Codex dynamic tool call")
            return

        turn_id = params.get("turnId")
        if isinstance(turn_id, str) and turn_id:
            with self.routing_lock:
                self.successful_dynamic_tool_turns.add(turn_id)

        tool_name = params.get("tool")
        tool_label = tool_name if isinstance(tool_name, str) and tool_name else "dynamic tool"

        # Dynamic tool requests are broadcast to every app-server subscriber and the first
        # response wins. Acknowledge locally before doing Telegram I/O so a remote subscriber
        # cannot reject a tool which this bot is able to execute. The action is consequently
        # best-effort: a later execution failure can only be logged.
        try:
            self._write_started(
                {
                    "id": request_id,
                    "result": dynamic_tool_result(
                        True,
                        f"Accepted {tool_label} for asynchronous execution.",
                    ),
                },
            )
        except Exception:
            logger.exception(
                "failed to acknowledge Codex dynamic tool %s",
                tool_label,
            )

        try:
            result = handler(params)
        except Exception:
            logger.exception(
                "asynchronous Codex dynamic tool %s failed after acknowledgement",
                tool_label,
            )
            return

        if not isinstance(result, dict) or result.get("success") is not True:
            logger.warning(
                "asynchronous Codex dynamic tool %s failed after acknowledgement: %s",
                tool_label,
                safe_log_text(json.dumps(result, ensure_ascii=False, default=str)),
            )
        else:
            logger.info(
                "asynchronous Codex dynamic tool %s completed after acknowledgement",
                tool_label,
            )

    def turn_has_non_commentary_agent_text(self, turn_id: str) -> bool:
        with self.routing_lock:
            return turn_id in self.non_commentary_agent_message_turns

    def _broadcast_failure(self, error: CodexAppServerError) -> None:
        with self.routing_lock:
            response_queues = list(self.pending_requests.values())
            event_queues = {
                event_queue
                for queues in self.notification_queues.values()
                for event_queue in queues
            }
        for destination in [*response_queues, *event_queues]:
            destination.put(error)

def response_thread_id(result: Any, operation: str) -> str:
    thread = result.get("thread") if isinstance(result, dict) else None
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        raise CodexAppServerError(f"Codex app-server {operation} returned no thread ID")
    return thread_id


def dynamic_tool_result(success: bool, text: str) -> dict[str, Any]:
    return {
        "success": success,
        "contentItems": [{"type": "inputText", "text": text}],
    }


def telegram_reaction_tool_spec(guest_mode: bool = False) -> dict[str, Any]:
    if guest_mode:
        description = (
            "Express a reaction to a Telegram guest message as a separate commentary "
            "message containing the selected emoji. Guest mode cannot set a native Telegram "
            "reaction, so the emoji follows the same delivery rules as other commentary. "
            "Use a reaction on your own initiative whenever it adds useful social or "
            "emotional meaning to the exchange. It may replace a one-line acknowledgement "
            "when the reaction communicates it naturally. Use judgment and avoid making "
            "reactions routine or attaching one to every response."
        )
    else:
        description = (
            "Set the bot's reaction on a Telegram message in this conversation. "
            "Use a reaction on your own initiative whenever it adds useful social or "
            "emotional meaning to the exchange. It may replace a one-line acknowledgement "
            "when the reaction communicates it naturally. Use judgment and avoid making "
            "reactions routine or attaching one to every response."
        )

    return {
        "type": "function",
        "name": TELEGRAM_REACTION_TOOL_NAME,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Telegram guest message ID associated with this commentary reaction."
                        if guest_mode
                        else "Telegram message ID to react to."
                    ),
                },
                "reaction": {
                    "type": "string",
                    "enum": list(TELEGRAM_REACTION_EMOJIS),
                    "description": (
                        "Reaction emoji to emit as commentary."
                        if guest_mode
                        else "Reaction emoji to set."
                    ),
                },
            },
            "required": ["message_id", "reaction"],
            "additionalProperties": False,
        },
    }


def telegram_clear_reaction_tool_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": TELEGRAM_CLEAR_REACTION_TOOL_NAME,
        "description": "Clear the bot's reaction from a Telegram message in this conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Telegram message ID whose reaction should be cleared.",
                },
            },
            "required": ["message_id"],
            "additionalProperties": False,
        },
    }


def telegram_sticker_tool_spec(
    sticker_names: tuple[str, ...],
    guest_mode: bool = False,
) -> dict[str, Any]:
    if guest_mode:
        description = (
            "Queue one of the configured stickers for this Telegram guest response. "
            "Use a sticker on your own initiative whenever it communicates part or all of "
            "the response naturally or improves how the message is received. A sticker may "
            "replace a one-line acknowledgement, including when it conveys the intended "
            "meaning better than a reaction or short text reply. A guest query accepts only "
            "one response: if one sticker fully conveys the response, call this tool before "
            "producing non-commentary text and produce no such text afterward so the sticker "
            "can be delivered on its own; if text is needed, the queued sticker will be "
            "embedded as an image in that response. Use judgment and avoid making stickers "
            "routine."
        )
    else:
        description = (
            "Send one of the configured stickers to this Telegram conversation. Use a "
            "sticker on your own initiative whenever it communicates part or all of the "
            "response naturally or improves how the message is received. It may accompany a "
            "substantive answer or replace a one-line acknowledgement, including when it "
            "conveys the intended meaning better than a reaction or short text reply. Use "
            "judgment and avoid making stickers routine."
        )

    return {
        "type": "function",
        "name": TELEGRAM_STICKER_TOOL_NAME,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "sticker": {
                    "type": "string",
                    "enum": list(sticker_names),
                    "description": "Configured sticker to send.",
                },
                "message_thread_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Telegram forum topic ID to receive the sticker; omit it to use "
                        "the current topic."
                    ),
                },
            },
            "required": ["sticker"],
            "additionalProperties": False,
        },
    }


def telegram_file_tool_spec(
    upload_dir: Path,
    guest_mode: bool = False,
) -> dict[str, Any]:
    delivery = (
        "Queue a file to attach to the final Telegram guest response. "
        if guest_mode else "Send a file to the current Telegram chat and topic. "
    )
    return {
        "type": "function",
        "name": TELEGRAM_FILE_TOOL_NAME,
        "description": (
            delivery
            + f"Only files inside {upload_dir} may be uploaded. "
            "Relative paths are resolved against that directory. "
            "The tool validates the request before returning; successful acceptance "
            "does not confirm delivery, which happens asynchronously."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the file to upload."},
                "caption": {"type": "string", "maxLength": 1024},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    }


def telegram_dynamic_tool_specs(
    sticker_names: tuple[str, ...] = (),
    upload_dir: Path | None = None,
) -> list[dict[str, Any]]:
    specs = [
        telegram_reaction_tool_spec(),
        telegram_clear_reaction_tool_spec(),
    ]
    if sticker_names:
        specs.append(telegram_sticker_tool_spec(sticker_names))
    if upload_dir is not None:
        specs.append(telegram_file_tool_spec(upload_dir))
    return specs


def response_turn_id(result: Any, operation: str) -> str:
    turn_id = result.get("turnId") if isinstance(result, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise CodexAppServerError(f"Codex app-server {operation} returned no turn ID")
    return turn_id


def is_inactive_turn_error(error: CodexAppServerError) -> bool:
    if error.request_method != "turn/steer" or not error.app_server_message:
        return False
    message = error.app_server_message.lower()
    return any(
        fragment in message
        for fragment in (
            "no active turn",
            "active turn not found",
            "expectedturnid",
            "expected turn",
            "turn id does not match",
            "turn is not active",
            "is not active",
            "not the active turn",
        )
    )


class AttachmentStore:
    def __init__(
        self,
        repo_dir: Path,
        retention_seconds: int = DEFAULT_ATTACHMENT_RETENTION_SECONDS,
    ) -> None:
        self.root = repo_dir / ATTACHMENT_CACHE_DIRECTORY
        self.retention_seconds = retention_seconds
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._remove_expired_startup_entries()
        self.attachment_free_messages: dict[str, int] = {}
        self.lock = threading.Lock()

    def _remove_expired_startup_entries(self) -> None:
        removed = 0
        for entry in self.root.iterdir():
            if not self._cache_entry_expired(entry):
                continue
            try:
                self._remove_cache_entry(entry)
            except OSError:
                logger.exception("failed to remove expired attachment cache entry %s", entry)
                continue
            removed += 1
        if removed:
            logger.info("removed %s expired attachment cache entries left by the previous bot process", removed)

    def _cache_entry_expired(self, entry: Path) -> bool:
        try:
            if entry.is_dir():
                file_mtimes = [
                    path.stat().st_mtime
                    for path in entry.rglob("*")
                    if path.is_file()
                ]
                newest_mtime = max(file_mtimes, default=entry.stat().st_mtime)
            else:
                newest_mtime = entry.stat().st_mtime
        except OSError:
            logger.exception("failed to inspect attachment cache entry %s", entry)
            return False
        return newest_mtime <= time.time() - self.retention_seconds

    @staticmethod
    def _remove_cache_entry(entry: Path) -> None:
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()

    def prepare_message(self, message: dict[str, Any], has_attachments: bool) -> tuple[Path | None, bool]:
        thread_key = telegram_thread_key(message)
        with self.lock:
            if not has_attachments:
                attachment_free_messages = self.attachment_free_messages.get(thread_key, 0) + 1
                self.attachment_free_messages[thread_key] = attachment_free_messages
                return None, attachment_free_messages >= ATTACHMENT_FREE_MESSAGE_RETENTION

            self.attachment_free_messages[thread_key] = 0
        attachment_dir = self.root / thread_key / "files"
        attachment_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return attachment_dir, False

    def finish_message(self, message: dict[str, Any], clear_thread: bool) -> None:
        if not clear_thread:
            return

        thread_key = telegram_thread_key(message)
        with self.lock:
            if (
                self.attachment_free_messages.get(thread_key, 0)
                < ATTACHMENT_FREE_MESSAGE_RETENTION
            ):
                return
            thread_dir = self.root / thread_key
            if thread_dir.exists():
                if not self._cache_entry_expired(thread_dir):
                    logger.debug(
                        "retaining attachments for chat %s thread %s until the %s-second minimum expires",
                        message.get("chat", {}).get("id"),
                        message.get("message_thread_id"),
                        self.retention_seconds,
                    )
                    return
                try:
                    shutil.rmtree(thread_dir)
                except OSError:
                    logger.exception(
                        "failed to remove retained attachments for chat %s thread %s",
                        message.get("chat", {}).get("id"),
                        message.get("message_thread_id"),
                    )
                    return
                logger.info(
                    "removed retained attachments for chat %s thread %s after %s attachment-free messages",
                    message.get("chat", {}).get("id"),
                    message.get("message_thread_id"),
                    ATTACHMENT_FREE_MESSAGE_RETENTION,
                )
            self.attachment_free_messages.pop(thread_key, None)


class AsyncGitPuller:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.active_thread: threading.Thread | None = None
        self.latest_thread: threading.Thread | None = None
        self.last_success_at: float | None = None

    def start_if_due(self) -> bool:
        with self.lock:
            now = time.monotonic()
            if self.active_thread is not None:
                logger.debug("skipping Git update: an asynchronous pull is already running")
                return False
            if (
                self.last_success_at is not None
                and now - self.last_success_at < self.config.git_pull_interval_seconds
            ):
                logger.info(
                    "skipping Git update: last successful pull completed %.1f seconds ago",
                    now - self.last_success_at,
                )
                return False

            pull_thread = threading.Thread(
                target=self._pull,
                name="codex-git-pull",
                daemon=True,
            )
            self.active_thread = pull_thread
            self.latest_thread = pull_thread
            pull_thread.start()
            logger.info("started asynchronous update of configured Git repositories")
            return True

    def wait(self, timeout: float | None = None) -> bool:
        with self.lock:
            pull_thread = self.latest_thread
        if not pull_thread:
            return True
        pull_thread.join(timeout)
        return not pull_thread.is_alive()

    def _pull(self) -> None:
        try:
            pull_results = pull_configured_git_repositories(
                self.config.repo_dir,
                self.config.git_pull_timeout_seconds,
            )
        except Exception as err:
            logger.warning(
                "asynchronous Git update failed; continuing with existing checkouts: %s",
                safe_log_text(str(err)),
            )
            with self.lock:
                self.active_thread = None
            return

        completed_at = time.monotonic()
        with self.lock:
            self.last_success_at = completed_at
            self.active_thread = None
        logger.info(
            "configured Git repositories are up to date: %s",
            safe_log_text(
                "; ".join(
                    f"{repo_dir}: {output or 'git pull completed'}"
                    for repo_dir, output in pull_results
                ),
            ),
        )


class CodexRunner:
    def __init__(self, config: Config, telegram: TelegramClient | None = None) -> None:
        self.config = config
        self.telegram = telegram
        self.telegram_stickers = configured_telegram_stickers(config.sticker_dir)
        self.upload_dir = configured_upload_dir(config.upload_dir)
        self.sessions = SessionStore(config.session_store_path)
        self.git_puller = AsyncGitPuller(config)
        self.client = CodexAppServerClient(
            config,
            self._handle_app_server_notification,
            self._handle_dynamic_tool_call if telegram is not None else None,
            server_request_preparer=self._prepare_file_upload if telegram is not None else None,
        )
        self.chat_locks: dict[ConversationKey, threading.RLock] = {}
        self.chat_locks_lock = threading.Lock()
        self.subscription_generations: dict[str, int] = {}
        self.subscription_lock = threading.Lock()
        self.active_turns: dict[ConversationKey, RunningCodexTurn] = {}
        self.guest_sticker_lock = threading.Lock()
        self.queued_guest_stickers: dict[str, list[QueuedTelegramSticker]] = {}
        self.queued_guest_files: dict[str, list[QueuedTelegramFile]] = {}
        self.turn_steer_counts: dict[str, int] = {}

    def start(self) -> None:
        self.client.start()
        self.archive_stale_sessions()
        self.subscribe_active_sessions()

    def close(self) -> None:
        self.client.close()

    def subscribe_active_sessions(self) -> None:
        for chat_id, record in self.sessions.records():
            if conversation_chat_id(chat_id) >= 0:
                continue
            with self._chat_lock(chat_id):
                current_record = self.sessions.get_record(chat_id)
                if current_record != record:
                    continue
                try:
                    self._ensure_thread_subscribed(record.session_id, chat_id)
                except CodexAppServerError as err:
                    if self._discard_unavailable_session(
                        chat_id,
                        record.session_id,
                        err,
                    ):
                        continue
                    logger.exception(
                        "failed to subscribe persisted Codex thread %s for chat %s",
                        record.session_id,
                        chat_id,
                    )
                except Exception:
                    logger.exception(
                        "failed to subscribe persisted Codex thread %s for chat %s",
                        record.session_id,
                        chat_id,
                    )

    def run(
        self,
        chat_id: ConversationKey,
        prompt: str | list[str] | Callable[[SessionRecord | None], str | list[str]],
        image_paths: list[Path] | None = None,
        agent_message_handler: Callable[[str], None] | None = None,
        fast_mode: bool = False,
        thread_name: str | None = None,
    ) -> str:
        self.git_puller.start_if_due()
        with self._chat_lock(chat_id):
            try:
                if chat_id in self.active_turns:
                    raise CodexAppServerError(
                        f"chat {chat_id} already has an active Codex turn",
                    )
                message_at = time.time()
                self._archive_stale_chat_before_turn(chat_id, message_at)
                return self._run_codex(
                    chat_id,
                    prompt,
                    image_paths,
                    agent_message_handler,
                    fast_mode,
                    message_at,
                    thread_name,
                )
            finally:
                if conversation_chat_id(chat_id) >= 0:
                    record = self.sessions.get_record(chat_id)
                    if record:
                        self._release_on_demand_subscription(record.session_id)

    def begin_turn(
        self,
        chat_id: ConversationKey,
        prompt: str | list[str] | Callable[[SessionRecord | None], str | list[str]],
        image_paths: list[Path] | None = None,
        agent_message_handler: Callable[[CodexAgentMessage], None] | None = None,
        fast_mode: bool = False,
        generated_image_handler: Callable[[CodexGeneratedImage], None] | None = None,
        thread_name: str | None = None,
        message_thread_id: int | None = None,
        image_generation_started_handler: Callable[[], None] | None = None,
    ) -> RunningCodexTurn:
        self.git_puller.start_if_due()
        with self._chat_lock(chat_id):
            if chat_id in self.active_turns:
                raise CodexAppServerError(f"chat {chat_id} already has an active Codex turn")

            message_at = time.time()
            self._archive_stale_chat_before_turn(chat_id, message_at)
            self.client.start()
            record = self.sessions.get_record(chat_id)
            if record:
                session_id = record.session_id
                try:
                    self._ensure_thread_subscribed(session_id, chat_id)
                    self.sessions.touch(chat_id, message_at)
                    handle = self.client.start_turn(
                        session_id,
                        resolve_session_prompt(prompt, record),
                        image_paths,
                        agent_message_handler,
                        fast_mode,
                        generated_image_handler,
                        image_generation_started_handler,
                    )
                except CodexAppServerError as err:
                    if not self._discard_unavailable_session(chat_id, session_id, err):
                        self._release_after_failed_start(chat_id, session_id)
                        raise
                else:
                    running_turn = RunningCodexTurn(
                        chat_id,
                        session_id,
                        handle,
                        message_thread_id,
                    )
                    with self.guest_sticker_lock:
                        self.turn_steer_counts[handle.turn_id] = 0
                    self.active_turns[chat_id] = running_turn
                    return running_turn

            session_id = self._start_thread(chat_id, thread_name)
            self.sessions.set(chat_id, session_id, message_at)
            self._remember_subscription(session_id)
            try:
                handle = self.client.start_turn(
                    session_id,
                    resolve_session_prompt(prompt, None),
                    image_paths,
                    agent_message_handler,
                    fast_mode,
                    generated_image_handler,
                    image_generation_started_handler,
                )
            except BaseException:
                self._release_after_failed_start(chat_id, session_id)
                raise
            running_turn = RunningCodexTurn(
                chat_id,
                session_id,
                handle,
                message_thread_id,
            )
            with self.guest_sticker_lock:
                self.turn_steer_counts[handle.turn_id] = 0
            self.active_turns[chat_id] = running_turn
            return running_turn

    def steer_turn(
        self,
        running_turn: RunningCodexTurn,
        prompt: str | list[str] | Callable[[SessionRecord | None], str | list[str]],
        image_paths: list[Path] | None = None,
    ) -> None:
        with self._chat_lock(running_turn.chat_id):
            if self.active_turns.get(running_turn.chat_id) is not running_turn:
                raise CodexTurnInactiveError(
                    f"chat {running_turn.chat_id} no longer has turn "
                    f"{running_turn.handle.turn_id} active",
                    request_method="turn/steer",
                )
            record = self.sessions.get_record(running_turn.chat_id)
            if not record or record.session_id != running_turn.session_id:
                raise CodexTurnInactiveError(
                    f"Codex thread {running_turn.session_id} is no longer mapped to chat "
                    f"{running_turn.chat_id}",
                    request_method="turn/steer",
                )
            self.client.steer_turn(
                running_turn.handle,
                resolve_session_prompt(prompt, record),
                image_paths,
            )
            with self.guest_sticker_lock:
                if running_turn.handle.turn_id in self.turn_steer_counts:
                    self.turn_steer_counts[running_turn.handle.turn_id] += 1
            self.sessions.touch(running_turn.chat_id, time.time())

    def wait_turn(self, running_turn: RunningCodexTurn) -> str:
        try:
            response = self.client.wait_turn(running_turn.handle)
            self.sessions.clear_response_error(running_turn.chat_id)
            return response
        finally:
            with self._chat_lock(running_turn.chat_id):
                if self.active_turns.get(running_turn.chat_id) is running_turn:
                    self.active_turns.pop(running_turn.chat_id, None)
            if conversation_chat_id(running_turn.chat_id) >= 0:
                self._release_on_demand_subscription(running_turn.session_id)

    def is_chat_active(self, chat_id: ConversationKey) -> bool:
        with self._chat_lock(chat_id):
            return chat_id in self.active_turns

    def _release_after_failed_start(
        self,
        chat_id: ConversationKey,
        session_id: str,
    ) -> None:
        if conversation_chat_id(chat_id) >= 0:
            self._release_on_demand_subscription(session_id)

    def archive_chat(self, chat_id: ConversationKey) -> str | None:
        with self._chat_lock(chat_id):
            if chat_id in self.active_turns:
                raise CodexAppServerError("cannot archive a conversation while its turn is active")
            record = self.sessions.get_record(chat_id)
            if not record:
                self.sessions.clear_response_error(chat_id)
                return None

            self.client.start()
            self.client.archive_thread(record.session_id)
            self.sessions.remove(chat_id, record.session_id)
            self._forget_subscription(record.session_id)
            logger.info(
                "archived Codex thread %s for chat %s on user request; "
                "the next message will create a new thread",
                record.session_id,
                chat_id,
            )
            return record.session_id

    def compact_chat(self, chat_id: ConversationKey) -> bool:
        with self._chat_lock(chat_id):
            if chat_id in self.active_turns:
                raise CodexAppServerError("cannot compact a conversation while its turn is active")
            record = self.sessions.get_record(chat_id)
            if not record:
                return False

            self.client.start()
            try:
                self._ensure_thread_subscribed(record.session_id, chat_id)
                self.client.compact_thread(record.session_id)
            finally:
                if conversation_chat_id(chat_id) >= 0:
                    self._release_on_demand_subscription(record.session_id)

            self.sessions.mark_compacted(chat_id, record.session_id)
            logger.info(
                "compacted Codex thread %s for chat %s on user request",
                record.session_id,
                chat_id,
            )
            return True

    def archive_stale_sessions(self, now: float | None = None) -> None:
        checked_at = now if now is not None else time.time()
        stale_records = [
            (chat_id, record)
            for chat_id, record in self.sessions.records()
            if session_is_idle(
                record,
                checked_at,
                self.config.session_archive_idle_seconds,
            )
        ]
        if not stale_records:
            logger.info("no stale Codex sessions require archival")
            return

        self.client.start()
        for chat_id, record in stale_records:
            with self._chat_lock(chat_id):
                if chat_id in self.active_turns:
                    continue
                current_record = self.sessions.get_record(chat_id)
                if (
                    current_record != record
                    or not session_is_idle(
                        record,
                        checked_at,
                        self.config.session_archive_idle_seconds,
                    )
                ):
                    continue
                try:
                    self.client.archive_thread(record.session_id)
                except Exception:
                    logger.exception(
                        "failed to archive stale Codex thread %s for chat %s",
                        record.session_id,
                        chat_id,
                    )
                    continue
                if self.sessions.remove(chat_id, record.session_id):
                    self._forget_subscription(record.session_id)
                    logger.info(
                        "archived stale Codex thread %s for chat %s after %.1f idle hours; "
                        "the next message will create a new thread",
                        record.session_id,
                        chat_id,
                        (checked_at - record.last_message_at) / 3600,
                    )

    def compact_idle_sessions(
        self,
        now: float | None = None,
        can_continue: Callable[[], bool] | None = None,
    ) -> None:
        checked_at = now if now is not None else time.time()
        idle_records = [
            (chat_id, record)
            for chat_id, record in self.sessions.records()
            if session_needs_compaction(
                record,
                checked_at,
                self.config.session_compaction_idle_seconds,
            )
        ]
        if not idle_records:
            logger.debug("no idle Codex sessions require compaction")
            return

        self.client.start()
        for chat_id, record in idle_records:
            if can_continue is not None and not can_continue():
                logger.debug(
                    "stopped idle Codex session compaction because Telegram is processing updates",
                )
                return
            with self._chat_lock(chat_id):
                if chat_id in self.active_turns:
                    continue
                if can_continue is not None and not can_continue():
                    logger.debug(
                        "stopped idle Codex session compaction because Telegram is processing updates",
                    )
                    return
                current_record = self.sessions.get_record(chat_id)
                if (
                    current_record != record
                    or not session_needs_compaction(
                        record,
                        checked_at,
                        self.config.session_compaction_idle_seconds,
                    )
                ):
                    continue
                try:
                    self._ensure_thread_subscribed(record.session_id, chat_id)
                    self.client.compact_thread(record.session_id)
                except Exception:
                    logger.exception(
                        "failed to compact idle Codex thread %s for chat %s",
                        record.session_id,
                        chat_id,
                    )
                    continue
                finally:
                    if conversation_chat_id(chat_id) >= 0:
                        self._release_on_demand_subscription(record.session_id)
                if self.sessions.mark_compacted(
                    chat_id,
                    record.session_id,
                    checked_at,
                ):
                    logger.info(
                        "compacted idle Codex thread %s for chat %s after %.1f idle hours",
                        record.session_id,
                        chat_id,
                        (checked_at - record.last_message_at) / 3600,
                    )

    def _chat_lock(self, chat_id: ConversationKey) -> threading.RLock:
        with self.chat_locks_lock:
            return self.chat_locks.setdefault(chat_id, threading.RLock())

    def _archive_stale_chat_before_turn(
        self,
        chat_id: ConversationKey,
        now: float,
    ) -> None:
        record = self.sessions.get_record(chat_id)
        if not record:
            return

        if session_is_idle(record, now, self.config.session_archive_idle_seconds):
            try:
                self.client.archive_thread(record.session_id)
            except Exception:
                logger.exception(
                    "failed to archive stale Codex thread %s for chat %s before new message; "
                    "continuing the existing thread",
                    record.session_id,
                    chat_id,
                )
            else:
                if self.sessions.remove(chat_id, record.session_id):
                    self._forget_subscription(record.session_id)
                    logger.info(
                        "archived stale Codex thread %s for chat %s before new message; "
                        "creating a new thread",
                        record.session_id,
                        chat_id,
                    )
                return

    def _run_codex(
        self,
        chat_id: ConversationKey,
        prompt: str | list[str] | Callable[[SessionRecord | None], str | list[str]],
        image_paths: list[Path] | None,
        agent_message_handler: Callable[[str], None] | None,
        fast_mode: bool,
        message_at: float,
        thread_name: str | None,
    ) -> str:
        self.client.start()
        record = self.sessions.get_record(chat_id)
        if record:
            session_id = record.session_id
            try:
                self._ensure_thread_subscribed(session_id, chat_id)
                self.sessions.touch(chat_id, message_at)
                response = self.client.run_turn(
                    session_id,
                    resolve_session_prompt(prompt, record),
                    image_paths,
                    agent_message_handler,
                    fast_mode,
                )
                self.sessions.clear_response_error(chat_id)
                return response
            except CodexAppServerError as err:
                if not self._discard_unavailable_session(chat_id, session_id, err):
                    raise

        session_id = self._start_thread(chat_id, thread_name)
        self.sessions.set(chat_id, session_id, message_at)
        self._remember_subscription(session_id)
        response = self.client.run_turn(
            session_id,
            resolve_session_prompt(prompt, None),
            image_paths,
            agent_message_handler,
            fast_mode,
        )
        self.sessions.clear_response_error(chat_id)
        return response

    def record_bot_message(self, chat_id: ConversationKey, message_id: int) -> bool:
        record = self.sessions.get_record(chat_id)
        if not record:
            return False
        return self.sessions.record_bot_message(chat_id, record.session_id, message_id)

    def conversation_key_for_last_bot_message(
        self,
        chat_id: int,
        message_id: int,
    ) -> ConversationKey | None:
        matches = [
            conversation_key
            for conversation_key, record in self.sessions.records()
            if conversation_chat_id(conversation_key) == chat_id
            and record.last_bot_message_id == message_id
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "ignored reaction to Telegram message %s in chat %s: "
                "the message maps to multiple Codex conversations",
                message_id,
                chat_id,
            )
        return None

    def record_response_error(self, chat_id: ConversationKey, error: str) -> bool:
        return self.sessions.record_response_error(chat_id, error)

    def previous_response_error(self, chat_id: ConversationKey) -> str | None:
        return self.sessions.get_response_error(chat_id)

    def send_optional_chat_action(
        self,
        conversation_key: ConversationKey,
        action: str,
        message_thread_id: int | None = None,
    ) -> None:
        if self.telegram is None or is_guest_conversation_key(conversation_key):
            return
        running_turn = self.active_turns.get(conversation_key)
        if message_thread_id is None:
            message_thread_id = (
                running_turn.message_thread_id
                if running_turn
                else conversation_message_thread_id(conversation_key)
            )
        try:
            self.telegram.send_chat_action(
                conversation_chat_id(conversation_key),
                message_thread_id,
                action,
            )
        except Exception:
            logger.debug(
                "failed to send optional Telegram %s chat action for chat %s",
                action,
                conversation_chat_id(conversation_key),
                exc_info=True,
            )

    def take_queued_guest_stickers(
        self,
        turn_id: str,
    ) -> tuple[QueuedTelegramSticker, ...]:
        with self.guest_sticker_lock:
            stickers = tuple(self.queued_guest_stickers.pop(turn_id, ()))
            self.turn_steer_counts.pop(turn_id, None)
        return stickers

    def take_queued_guest_files(self, turn_id: str) -> tuple[QueuedTelegramFile, ...]:
        with self.guest_sticker_lock:
            return tuple(self.queued_guest_files.pop(turn_id, ()))

    def _handle_app_server_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method not in {"thread/archived", "thread/deleted"}:
            return
        params = message.get("params")
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        if not isinstance(thread_id, str):
            return

        chat_ids = self.sessions.remove_session(thread_id)
        if not chat_ids:
            return
        self._forget_subscription(thread_id)
        logger.info(
            "removed Telegram session mapping for chat(s) %s after Codex %s thread %s",
            chat_ids,
            "archived" if method == "thread/archived" else "deleted",
            thread_id,
        )

    def _discard_unavailable_session(
        self,
        chat_id: ConversationKey,
        session_id: str,
        error: CodexAppServerError,
    ) -> bool:
        if not is_unavailable_thread_error(error, session_id):
            return False
        self.sessions.remove(chat_id, session_id)
        self._forget_subscription(session_id)
        logger.warning(
            "discarded unavailable Codex thread %s for chat %s; the next turn will use a new thread",
            session_id,
            chat_id,
        )
        return True

    def _ensure_thread_subscribed(
        self,
        thread_id: str,
        chat_id: ConversationKey | None = None,
    ) -> None:
        self.client.start()
        generation = self.client.generation
        with self.subscription_lock:
            if self.subscription_generations.get(thread_id) == generation:
                return

        self._resume_thread(thread_id, chat_id)
        with self.subscription_lock:
            self.subscription_generations[thread_id] = generation
        if not self.sessions.has_session(thread_id):
            self._forget_subscription(thread_id)
            return
        logger.info("subscribed Codex thread %s", thread_id)

    def _start_thread(
        self,
        chat_id: ConversationKey,
        thread_name: str | None = None,
    ) -> str:
        dynamic_tools = self._dynamic_tools_for_chat(chat_id)
        if dynamic_tools is None:
            thread_id = self.client.start_thread()
        else:
            thread_id = self.client.start_thread(dynamic_tools)
        if thread_name:
            try:
                self.client.set_thread_name(thread_id, thread_name)
            except Exception as err:
                logger.warning(
                    "failed to name new Codex thread %s %r: %s",
                    thread_id,
                    thread_name,
                    safe_log_text(str(err)),
                )
        return thread_id

    def _resume_thread(
        self,
        thread_id: str,
        chat_id: ConversationKey | None,
    ) -> None:
        if chat_id is None and self.telegram is not None:
            conversation_keys = self.sessions.conversation_keys_for_session(thread_id)
            if len(conversation_keys) == 1:
                chat_id = conversation_keys[0]
        dynamic_tools = self._dynamic_tools_for_chat(chat_id)
        if dynamic_tools is None:
            self.client.resume_thread(thread_id)
        else:
            self.client.resume_thread(thread_id, dynamic_tools)

    def _dynamic_tools_for_chat(
        self,
        chat_id: ConversationKey | None,
    ) -> list[dict[str, Any]] | None:
        if self.telegram is None or chat_id is None:
            return None
        if is_guest_conversation_key(chat_id):
            specs = [telegram_reaction_tool_spec(guest_mode=True)]
            if self.upload_dir is not None:
                specs.append(telegram_file_tool_spec(self.upload_dir, guest_mode=True))
            if self.telegram_stickers:
                specs.append(
                    telegram_sticker_tool_spec(
                        tuple(self.telegram_stickers),
                        guest_mode=True,
                    ),
                )
            return specs
        return telegram_dynamic_tool_specs(tuple(self.telegram_stickers), self.upload_dir)

    def _prepare_file_upload(
        self,
        params: dict[str, Any],
    ) -> tuple[dict[str, Any], Callable[[], None] | None]:
        def reject(message: str) -> tuple[dict[str, Any], None]:
            return dynamic_tool_result(False, message), None

        if self.upload_dir is None:
            return reject("File uploads are disabled; CODEX_TELEGRAM_UPLOAD_DIR is not set.")
        if params.get("tool") != TELEGRAM_FILE_TOOL_NAME or params.get("namespace") is not None:
            return reject("Unsupported file upload tool.")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict) or set(arguments) - {"path", "caption"}:
            return reject("File upload arguments must contain only path and optional caption.")
        path = arguments.get("path")
        caption = arguments.get("caption")
        if not isinstance(path, str) or not path.strip():
            return reject("path must be a non-empty file path.")
        if caption is not None and (not isinstance(caption, str) or len(caption) > 1024):
            return reject("caption must be a string of at most 1024 characters.")
        try:
            file_path = validate_upload_file(Path(path), self.upload_dir)
        except (OSError, ValueError, RuntimeError) as err:
            return reject(str(err))
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            return reject("The file upload has no valid Codex thread ID.")
        conversations = self.sessions.conversation_keys_for_session(thread_id)
        if len(conversations) != 1:
            return reject("The Codex thread is not mapped to exactly one Telegram conversation.")
        if self.telegram is None:
            return reject("Telegram file uploads are unavailable.")
        conversation_key = conversations[0]
        if is_guest_conversation_key(conversation_key):
            if self.config.media_upload_chat_id is None:
                return reject(
                    "CODEX_TELEGRAM_MEDIA_UPLOAD_CHAT_ID is required for guest file uploads.",
                )
            turn_id = params.get("turnId")
            with self._chat_lock(conversation_key):
                running_turn = self.active_turns.get(conversation_key)
                if (
                    not isinstance(turn_id, str)
                    or running_turn is None
                    or running_turn.handle.turn_id != turn_id
                ):
                    return reject("The guest file upload does not belong to the active Codex turn.")
                with self.guest_sticker_lock:
                    files = self.queued_guest_files.setdefault(turn_id, [])
                    if len(files) >= MAX_TELEGRAM_RICH_MEDIA:
                        return reject("Too many files queued for this guest response.")
                    queued_file = QueuedTelegramFile(file_path, caption, self.upload_dir)
                    files.append(queued_file)

            def cancel_queued_file() -> None:
                with self.guest_sticker_lock:
                    pending = self.queued_guest_files.get(turn_id, [])
                    pending[:] = [entry for entry in pending if entry is not queued_file]
                    if not pending:
                        self.queued_guest_files.pop(turn_id, None)

            return dynamic_tool_result(
                True,
                f"Queued {file_path.name!r} for the final guest response; delivery is pending.",
            ), DeferredTelegramAction(lambda: None, cancel_queued_file)

        active_turn = self.active_turns.get(conversation_key)
        message_thread_id = (
            active_turn.message_thread_id
            if active_turn is not None
            else conversation_message_thread_id(conversation_key)
        )
        chat_id = conversation_chat_id(conversation_key)

        def upload() -> None:
            self.send_optional_chat_action(conversation_key, "upload_document", message_thread_id)
            sent_message_id = self.telegram.send_document(
                chat_id,
                file_path,
                caption,
                message_thread_id,
                upload_dir=self.upload_dir,
            )
            if valid_telegram_message_id(sent_message_id):
                self.record_bot_message(conversation_key, sent_message_id)
            logger.info("sent Telegram file %s to chat %s", file_path.name, chat_id)

        return dynamic_tool_result(
            True,
            f"Accepted {file_path.name!r} for asynchronous upload; delivery is pending.",
        ), upload

    def _handle_dynamic_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = params.get("threadId")
        tool_name = params.get("tool")
        namespace = params.get("namespace")
        arguments = params.get("arguments")
        if not isinstance(thread_id, str) or not thread_id:
            return dynamic_tool_result(False, "The tool call has no valid Codex thread ID.")
        supported_tools = {
            TELEGRAM_REACTION_TOOL_NAME,
            TELEGRAM_CLEAR_REACTION_TOOL_NAME,
        }
        if self.telegram_stickers:
            supported_tools.add(TELEGRAM_STICKER_TOOL_NAME)
        if tool_name not in supported_tools or namespace is not None:
            return dynamic_tool_result(False, f"Unsupported dynamic tool: {tool_name!r}.")
        if not isinstance(arguments, dict):
            return dynamic_tool_result(False, "The Telegram tool arguments must be an object.")

        conversation_keys = self.sessions.conversation_keys_for_session(thread_id)
        if len(conversation_keys) != 1:
            return dynamic_tool_result(
                False,
                "The Codex thread is not mapped to exactly one Telegram conversation.",
            )
        conversation_key = conversation_keys[0]
        if self.telegram is None:
            return dynamic_tool_result(False, "Telegram tools are unavailable.")

        chat_id = conversation_chat_id(conversation_key)
        if tool_name == TELEGRAM_STICKER_TOOL_NAME:
            sticker_name = arguments.get("sticker")
            if not isinstance(sticker_name, str):
                return dynamic_tool_result(False, "sticker must be one of the configured stickers.")
            sticker_path = self.telegram_stickers.get(sticker_name)
            if sticker_path is None:
                return dynamic_tool_result(False, "sticker must be one of the configured stickers.")
            requested_thread_id = arguments.get("message_thread_id")
            if requested_thread_id is not None and not valid_message_thread_id(
                requested_thread_id,
            ):
                return dynamic_tool_result(
                    False,
                    "message_thread_id must be a positive integer.",
                )
            if is_guest_conversation_key(conversation_key):
                turn_id = params.get("turnId")
                if not isinstance(turn_id, str) or not turn_id:
                    return dynamic_tool_result(
                        False,
                        "The guest sticker call has no valid Codex turn ID.",
                    )
                if self.client.turn_has_non_commentary_agent_text(turn_id):
                    return dynamic_tool_result(
                        False,
                        "A guest sticker can only be queued before non-commentary response "
                        "text has been produced.",
                    )
                with self.guest_sticker_lock:
                    steer_count = self.turn_steer_counts.get(turn_id)
                    running_turn = self.active_turns.get(conversation_key)
                    if (
                        steer_count is None
                        or running_turn is None
                        or running_turn.handle.turn_id != turn_id
                    ):
                        return dynamic_tool_result(
                            False,
                            "The guest sticker call does not belong to the active Codex turn.",
                        )
                    self.queued_guest_stickers.setdefault(turn_id, []).append(
                        QueuedTelegramSticker(sticker_name, sticker_path, steer_count),
                    )
                logger.info(
                    "queued Telegram sticker %s for guest Codex turn %s",
                    sticker_name,
                    turn_id,
                )
                return dynamic_tool_result(
                    True,
                    f"Queued Telegram sticker {sticker_name!r}. If you send non-commentary "
                    "text in the final response, the sticker will be embedded as an image. "
                    "If this remains the only queued sticker and you send no such text, it "
                    "will be sent as a sticker.",
                )
            active_turn = self.active_turns.get(conversation_key)
            message_thread_id = (
                requested_thread_id
                if valid_message_thread_id(requested_thread_id)
                else (
                    active_turn.message_thread_id
                    if active_turn is not None
                    else conversation_message_thread_id(conversation_key)
                )
            )
            self.send_optional_chat_action(
                conversation_key,
                "choose_sticker",
                message_thread_id,
            )
            try:
                sent_message_id = self.telegram.send_sticker(
                    chat_id,
                    sticker_path,
                    message_thread_id,
                )
            except Exception as err:
                logger.warning(
                    "failed to execute Telegram sticker tool for %s in chat %s: %s",
                    sticker_name,
                    chat_id,
                    safe_log_text(str(err)),
                )
                return dynamic_tool_result(False, safe_log_text(str(err)))
            if (
                message_thread_id == conversation_message_thread_id(conversation_key)
                and valid_telegram_message_id(sent_message_id)
            ):
                self.record_bot_message(conversation_key, sent_message_id)
            logger.info(
                "sent Telegram sticker %s in chat %s for Codex thread %s",
                sticker_name,
                chat_id,
                thread_id,
            )
            return dynamic_tool_result(True, f"Sent Telegram sticker {sticker_name!r}.")

        message_id = arguments.get("message_id")
        if not valid_telegram_message_id(message_id):
            return dynamic_tool_result(False, "message_id must be a positive integer.")
        reaction = arguments.get("reaction") if tool_name == TELEGRAM_REACTION_TOOL_NAME else None
        if tool_name == TELEGRAM_REACTION_TOOL_NAME and reaction not in TELEGRAM_REACTION_EMOJIS:
            return dynamic_tool_result(
                False,
                "reaction must be one of the supported Telegram emojis.",
            )

        if is_guest_conversation_key(conversation_key):
            if tool_name != TELEGRAM_REACTION_TOOL_NAME:
                return dynamic_tool_result(
                    False,
                    "Native Telegram reactions cannot be cleared in guest mode.",
                )
            turn_id = params.get("turnId")
            if not isinstance(turn_id, str) or not turn_id:
                return dynamic_tool_result(
                    False,
                    "The guest reaction call has no valid Codex turn ID.",
                )
            running_turn = self.active_turns.get(conversation_key)
            if running_turn is None or running_turn.handle.turn_id != turn_id:
                return dynamic_tool_result(
                    False,
                    "The guest reaction call does not belong to the active Codex turn.",
                )
            commentary_handler = running_turn.handle.agent_message_handler
            if commentary_handler is None:
                return dynamic_tool_result(
                    False,
                    "The guest reaction call has no commentary delivery handler.",
                )
            call_id = params.get("callId")
            commentary_handler(
                CodexAgentMessage(
                    turn_id,
                    call_id if isinstance(call_id, str) else None,
                    "commentary",
                    reaction,
                ),
            )
            logger.info(
                "queued Telegram reaction %s as commentary for guest Codex turn %s",
                reaction,
                turn_id,
            )
            return dynamic_tool_result(
                True,
                f"Queued reaction {reaction} as guest commentary.",
            )

        try:
            if tool_name == TELEGRAM_REACTION_TOOL_NAME:
                self.telegram.set_message_reaction(chat_id, message_id, reaction)
                result_text = f"Set reaction {reaction} on Telegram message {message_id}."
            else:
                self.telegram.clear_message_reaction(chat_id, message_id)
                result_text = f"Cleared the reaction from Telegram message {message_id}."
        except Exception as err:
            logger.warning(
                "failed to execute Telegram reaction tool %s on message %s in chat %s: %s",
                tool_name,
                message_id,
                chat_id,
                safe_log_text(str(err)),
            )
            return dynamic_tool_result(False, safe_log_text(str(err)))

        logger.info(
            "executed Telegram reaction tool %s on message %s in chat %s for Codex thread %s",
            tool_name,
            message_id,
            chat_id,
            thread_id,
        )
        return dynamic_tool_result(True, result_text)

    def _remember_subscription(self, thread_id: str) -> None:
        with self.subscription_lock:
            self.subscription_generations[thread_id] = self.client.generation
        logger.info("subscribed new Codex thread %s", thread_id)

    def _forget_subscription(self, thread_id: str) -> None:
        with self.subscription_lock:
            self.subscription_generations.pop(thread_id, None)

    def _release_on_demand_subscription(self, thread_id: str) -> None:
        with self.subscription_lock:
            if thread_id not in self.subscription_generations:
                return
        try:
            self.client.unsubscribe_thread(thread_id)
        except Exception:
            logger.warning(
                "failed to release on-demand Codex thread subscription %s",
                thread_id,
                exc_info=True,
            )
            return
        self._forget_subscription(thread_id)
        logger.info("released on-demand Codex thread subscription %s", thread_id)


def session_is_idle(record: SessionRecord, now: float, idle_seconds: int) -> bool:
    return now - record.last_message_at >= idle_seconds


def is_unavailable_thread_error(error: CodexAppServerError, thread_id: str) -> bool:
    if (
        error.request_method not in {"thread/resume", "turn/start"}
        or error.error_code != -32600
        or not error.app_server_message
    ):
        return False

    return (
        error.app_server_message.startswith(f"session {thread_id} is archived.")
        or error.app_server_message == f"no rollout found for thread id {thread_id}"
    )


def session_needs_compaction(record: SessionRecord, now: float, idle_seconds: int) -> bool:
    if not session_is_idle(record, now, idle_seconds):
        return False
    return record.last_compacted_at is None or record.last_compacted_at < record.last_message_at


def pull_current_git_branch(repo_dir: Path, timeout: int) -> str:
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only", "--no-tags", "--recurse-submodules=yes"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise GitPullError(f"git pull in {repo_dir} timed out after {timeout} seconds") from err
    except OSError as err:
        raise GitPullError(f"cannot run git pull in {repo_dir}: {err}") from err

    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode != 0:
        details = safe_log_text(output or "no diagnostic output")
        raise GitPullError(
            f"git pull --ff-only in {repo_dir} failed with exit code {result.returncode}: {details}",
        )

    return output


def pull_configured_git_repositories(repo_dir: Path, timeout: int) -> list[tuple[Path, str]]:
    primary_repo = repo_dir.resolve()
    results = [(primary_repo, pull_current_git_branch(primary_repo, timeout))]
    primary_git_root = git_repository_root(primary_repo, timeout) or primary_repo
    pulled_git_roots = {primary_git_root}

    for writable_root in configured_writable_roots(primary_repo):
        git_root = git_repository_root(writable_root, timeout)
        if git_root is None:
            logger.info("skipping writable root %s: not a Git repository", writable_root)
            continue
        if git_root in pulled_git_roots:
            logger.debug("skipping writable root %s: Git repository already pulled", writable_root)
            continue

        logger.info("pulling writable-root Git repository %s", git_root)
        results.append((git_root, pull_current_git_branch(git_root, timeout)))
        pulled_git_roots.add(git_root)

    return results


def configured_writable_roots(repo_dir: Path) -> list[Path]:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    config_paths = [codex_home / "config.toml", repo_dir / ".codex" / "config.toml"]
    configured_roots: list[str] | None = None

    for config_path in config_paths:
        if not config_path.is_file():
            continue

        try:
            with config_path.open("rb") as config_file:
                config = tomllib.load(config_file)
        except (OSError, tomllib.TOMLDecodeError) as err:
            raise RuntimeError(f"cannot read Codex config {config_path}: {err}") from err

        sandbox_config = config.get("sandbox_workspace_write")
        if not isinstance(sandbox_config, dict) or "writable_roots" not in sandbox_config:
            continue

        writable_roots = sandbox_config["writable_roots"]
        if not isinstance(writable_roots, list) or not all(
            isinstance(root, str) and root.strip()
            for root in writable_roots
        ):
            raise RuntimeError(
                f"sandbox_workspace_write.writable_roots in {config_path} must be an array of paths",
            )
        configured_roots = writable_roots

    resolved_roots = []
    seen_roots = set()
    for root in configured_roots or []:
        writable_root = Path(root).expanduser()
        if not writable_root.is_absolute():
            writable_root = repo_dir / writable_root
        writable_root = writable_root.resolve()
        if writable_root not in seen_roots:
            resolved_roots.append(writable_root)
            seen_roots.add(writable_root)

    return resolved_roots


def git_repository_root(path: Path, timeout: int) -> Path | None:
    if not path.is_dir():
        return None

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise GitPullError(
            f"Git repository detection in {path} timed out after {timeout} seconds",
        ) from err
    except OSError as err:
        raise GitPullError(f"cannot inspect Git repository {path}: {err}") from err

    if result.returncode != 0:
        return None

    root = result.stdout.strip()
    return Path(root).resolve() if root else None


def summarize_app_server_event(event: dict[str, Any]) -> str | None:
    method = event.get("method")
    params = event.get("params")
    if not isinstance(params, dict):
        params = {}

    if method == "thread/started":
        thread = params.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else "unknown"
        return f"thread started ({thread_id})"
    if method == "thread/archived":
        return f"thread archived ({params.get('threadId', 'unknown')})"
    if method == "thread/deleted":
        return f"thread deleted ({params.get('threadId', 'unknown')})"
    if method == "thread/closed":
        return f"thread closed ({params.get('threadId', 'unknown')})"
    if method == "turn/started":
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else "unknown"
        return f"turn started ({turn_id})"
    if method == "turn/completed":
        turn = params.get("turn")
        if isinstance(turn, dict):
            return f"turn completed ({turn.get('id', 'unknown')}, status={turn.get('status')})"
        return "turn completed"
    if method in {"warning", "configWarning", "error"}:
        return f"{method}: {first_text_value(params, 'message', 'summary', 'error') or 'unknown'}"
    if method == "item/agentMessage/delta":
        return f"agent message delta: {first_text_value(params, 'delta') or ''}"
    if method == "item/reasoning/summaryTextDelta":
        return f"reasoning summary delta: {first_text_value(params, 'delta') or ''}"
    if method == "item/commandExecution/outputDelta":
        delta = params.get("delta")
        delta_length = len(delta) if isinstance(delta, str) else 0
        return f"command output received ({delta_length} characters)"
    if method == "turn/plan/updated":
        return f"plan updated: {first_text_value(params, 'plan') or ''}"
    if method == "mcpServer/startupStatus/updated":
        return (
            f"MCP server {params.get('name', 'unknown')} startup: "
            f"{params.get('status', 'unknown')}"
        )
    if method not in {"item/started", "item/completed"}:
        return None

    item = params.get("item")
    if not isinstance(item, dict):
        return method

    stage = method.removeprefix("item/")
    item_type = item.get("type", "unknown item")
    status = item.get("status")
    status_text = f", status={status}" if status else ""

    if item_type == "agentMessage":
        text = first_text_value(item, "text")
        return f"{item_type} {stage}: {text}" if text else f"{item_type} {stage}"
    if item_type == "reasoning":
        text = first_text_value(item, "summary")
        return f"{item_type} {stage}: {text}" if text else f"{item_type} {stage}"
    if item_type == "plan":
        text = first_text_value(item, "text")
        return f"{item_type} {stage}: {text}" if text else f"{item_type} {stage}"
    if item_type == "commandExecution":
        command = first_text_value(item, "command") or "unknown command"
        exit_code = item.get("exitCode")
        exit_text = f", exit_code={exit_code}" if exit_code is not None else ""
        return f"command {stage}: {command}{status_text}{exit_text}"
    if item_type in {"mcpToolCall", "dynamicToolCall"}:
        tool_name = first_text_value(item, "tool", "name", "actionName") or "unknown tool"
        server = first_text_value(item, "server")
        qualified_name = f"{server}.{tool_name}" if server else tool_name
        return f"tool {stage}: {qualified_name}{status_text}"
    if item_type == "webSearch":
        query = first_text_value(item, "query", "action") or "search"
        return f"web search {stage}: {query}{status_text}"
    if item_type == "fileChange":
        changes = item.get("changes")
        change_count = len(changes) if isinstance(changes, list) else "unknown"
        return f"file change {stage}: {change_count} change(s){status_text}"
    if item_type == "contextCompaction":
        return f"context compaction {stage}"

    return f"{item_type} {stage}{status_text}"


def first_text_value(item: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = item.get(name)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list) and value:
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, dict) and value:
            return json.dumps(value, ensure_ascii=False)

    return None


def safe_log_text(text: str) -> str:
    single_line = " ".join(text.split())
    redacted = SECRET_VALUE_PATTERN.sub(r"\1[REDACTED]", single_line)
    redacted = POTENTIAL_PAN_PATTERN.sub(redact_pan_candidate, redacted)
    if len(redacted) <= MAX_CODEX_LOG_TEXT_LENGTH:
        return redacted

    return f"{redacted[:MAX_CODEX_LOG_TEXT_LENGTH]}... [truncated]"


def redact_pan_candidate(match: re.Match[str]) -> str:
    candidate = match.group(0)
    digits = "".join(character for character in candidate if character.isdigit())
    if 13 <= len(digits) <= 19 and passes_luhn_check(digits):
        return "[REDACTED PAN]"

    return candidate


def passes_luhn_check(digits: str) -> bool:
    checksum = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        checksum += value

    return checksum % 10 == 0


@dataclass
class PreparedTelegramInput:
    kind: str
    message: dict[str, Any]
    prompt: str | list[str] | Callable[[SessionRecord | None], str | list[str]]
    image_paths: list[Path]
    clear_attachments: bool


@dataclass
class TelegramResponseTarget:
    kind: str
    message: dict[str, Any]
    generation: int
    delivery_count: int = 0
    was_steered: bool = False
    response_sent: bool = False
    consumed_reason: str | None = None
    delivered_agent_messages: list[str] | None = None
    deadline_timer: threading.Timer | None = None

    def __post_init__(self) -> None:
        if self.delivered_agent_messages is None:
            self.delivered_agent_messages = []

    @property
    def is_guest(self) -> bool:
        return self.kind == "guest_message"


class ConversationController:
    def __init__(
        self,
        bot: "Bot",
        chat_id: int,
        conversation_key: ConversationKey | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.conversation_key = chat_id if conversation_key is None else conversation_key
        self.queue_lock = threading.Lock()
        self.events: deque[tuple[str, Any]] = deque()
        self.draining = False
        self.active_turn: RunningCodexTurn | None = None
        self.target: TelegramResponseTarget | None = None
        self.target_generation = 0
        self.original_guest_target: TelegramResponseTarget | None = None
        self.guest_steer_count = 0
        self.intermediate_messages: list[str] = []
        self.generated_image_paths: set[Path] = set()
        self.pending_after_turn: deque[PreparedTelegramInput] = deque()
        self.deferred_attachment_cleanup: list[tuple[dict[str, Any], bool]] = []
        self.stop_typing: threading.Event | None = None
        self.typing_thread: threading.Thread | None = None

    def enqueue(self, event_type: str, payload: Any = None) -> None:
        should_submit = False
        with self.queue_lock:
            self.events.append((event_type, payload))
            if not self.draining:
                self.draining = True
                should_submit = True
        if not should_submit:
            return
        try:
            self.bot.update_executor.submit(self._drain)
        except RuntimeError:
            with self.queue_lock:
                self.draining = False
            logger.warning("discarded event for chat %s while bot is stopping", self.chat_id)

    def is_busy(self) -> bool:
        with self.queue_lock:
            return self.draining or bool(self.events) or self.active_turn is not None

    def shutdown(self) -> None:
        self._cancel_target_timer()
        self._stop_typing()

    def _drain(self) -> None:
        while True:
            with self.queue_lock:
                if not self.events:
                    self.draining = False
                    return
                event_type, payload = self.events.popleft()
            try:
                self._process_event(event_type, payload)
            except Exception:
                logger.exception(
                    "conversation controller failed to process %s for chat %s",
                    event_type,
                    self.chat_id,
                )

    def _process_event(self, event_type: str, payload: Any) -> None:
        if event_type == "incoming":
            kind, message = payload
            self._process_incoming(kind, message)
        elif event_type == "agent_message":
            self._process_agent_message(payload)
        elif event_type == "generated_image":
            self._process_generated_image(payload)
        elif event_type == "image_generation_started":
            self._process_image_generation_started()
        elif event_type == "turn_completed":
            self._process_turn_completed(*payload)
        elif event_type == "guest_deadline":
            self._process_guest_deadline(payload)

    def _process_incoming(self, kind: str, message: dict[str, Any]) -> None:
        command = telegram_session_command(message, self.bot.bot_user) if kind == "message" else None
        if command:
            if self.active_turn is not None:
                self.bot.send_message_response(
                    self.chat_id,
                    f"Cannot {command} the conversation while Codex is responding.",
                    message["message_id"],
                    message.get("message_thread_id"),
                    f"/{command} busy response",
                    reply_to_original=True,
                )
            else:
                self.bot.handle_session_command(message, command)
            return

        try:
            prepared = self._prepare_input(kind, message)
        except Exception as err:
            logger.exception(
                "failed to prepare %s %s from chat %s",
                kind,
                message.get("message_id"),
                self.chat_id,
            )
            self._send_input_error(kind, message, err)
            return
        if prepared is None:
            return

        if self.active_turn is None:
            self._start_turn(prepared)
        else:
            self._steer_turn(prepared)

    def _prepare_input(
        self,
        kind: str,
        message: dict[str, Any],
    ) -> PreparedTelegramInput | None:
        guest_query_id = message.get("guest_query_id")
        if kind == "guest_message" and not guest_query_id:
            logger.info("ignored guest message %s: no guest_query_id", message.get("message_id"))
            return None

        text = message.get("text") or message.get("caption")
        rich_message = message.get("rich_message")
        images = collect_message_images(message)
        files = collect_message_files(message)
        if not text and not rich_message and not images and not files:
            logger.info(
                "ignored %s %s: no supported text or attachment",
                kind,
                message.get("message_id"),
            )
            return None

        logger.info(
            "processing%s message %s from chat %s",
            " guest" if kind == "guest_message" else "",
            message.get("message_id"),
            self.chat_id,
        )
        clear_attachments = False
        try:
            attachment_dir, clear_attachments = self.bot.attachments.prepare_message(
                message,
                bool(images or files),
            )
            if attachment_dir:
                image_paths = self.bot.download_images(images, attachment_dir)
                file_paths = self.bot.download_files(files, attachment_dir)
            else:
                image_paths = []
                file_paths = []

            if kind != "guest_message":
                def build_prompt(record: SessionRecord | None) -> list[str]:
                    return build_codex_prompt(
                        kind,
                        message,
                        self.bot.bot_user,
                        file_paths,
                        include_reply_to_message=not reply_targets_recorded_bot_message(
                            message,
                            record,
                        ),
                        previous_response_error=self._previous_response_error(),
                    )
            else:
                def build_prompt(record: SessionRecord | None) -> list[str]:
                    return build_codex_prompt(
                        kind,
                        message,
                        self.bot.bot_user,
                        file_paths,
                        previous_response_error=self._previous_response_error(),
                    )
            return PreparedTelegramInput(
                kind,
                message,
                build_prompt,
                image_paths,
                clear_attachments,
            )
        except BaseException:
            self.bot.attachments.finish_message(message, clear_attachments)
            raise

    def _previous_response_error(self) -> str | None:
        error = self.bot.codex.previous_response_error(self.conversation_key)
        return error if isinstance(error, str) else None

    def _start_turn(self, prepared: PreparedTelegramInput) -> None:
        self.generated_image_paths.clear()
        self._activate_target(prepared.kind, prepared.message)
        self.original_guest_target = self.target if prepared.kind == "guest_message" else None
        self.guest_steer_count = 0
        try:
            running_turn = self.bot.codex.begin_turn(
                self.conversation_key,
                prepared.prompt,
                prepared.image_paths,
                agent_message_handler=lambda message: self.enqueue("agent_message", message),
                fast_mode=prepared.kind == "guest_message",
                generated_image_handler=lambda image: self.enqueue("generated_image", image),
                thread_name=telegram_thread_name(prepared.kind, prepared.message),
                message_thread_id=prepared.message.get("message_thread_id"),
                image_generation_started_handler=(
                    (lambda: self.enqueue("image_generation_started"))
                    if prepared.kind != "guest_message"
                    else None
                ),
            )
        except Exception as err:
            logger.exception("failed to start Codex turn for chat %s", self.chat_id)
            self._deliver_final(self._execution_error(err), codex_succeeded=False)
            self._finish_target()
            self.bot.attachments.finish_message(
                prepared.message,
                prepared.clear_attachments,
            )
            return

        self.active_turn = running_turn
        self.deferred_attachment_cleanup.append(
            (prepared.message, prepared.clear_attachments),
        )
        self.bot.turn_executor.submit(self._wait_for_turn, running_turn)
        logger.info(
            "started Codex turn %s for message %s in chat %s",
            running_turn.handle.turn_id,
            prepared.message.get("message_id"),
            self.chat_id,
        )

    def _steer_turn(self, prepared: PreparedTelegramInput) -> None:
        running_turn = self.active_turn
        if running_turn is None:
            self._start_turn(prepared)
            return

        self._flush_guest_intermediates_before_steer()
        try:
            self.bot.codex.steer_turn(
                running_turn,
                prepared.prompt,
                prepared.image_paths,
            )
        except CodexTurnInactiveError:
            logger.info(
                "turn %s completed while message %s was being steered; "
                "the message will start a new turn",
                running_turn.handle.turn_id,
                prepared.message.get("message_id"),
            )
            self.pending_after_turn.append(prepared)
            return
        except Exception as err:
            logger.exception(
                "failed to steer message %s into turn %s",
                prepared.message.get("message_id"),
                running_turn.handle.turn_id,
            )
            self._send_input_error(prepared.kind, prepared.message, err)
            self.bot.attachments.finish_message(
                prepared.message,
                prepared.clear_attachments,
            )
            return

        target = self.target
        preserve_ordinary_target = (
            prepared.kind != "guest_message"
            and target is not None
            and not target.is_guest
        )
        if preserve_ordinary_target:
            target.was_steered = True
        else:
            if prepared.kind == "guest_message":
                self.guest_steer_count += 1
            self._activate_target(prepared.kind, prepared.message)
        self.deferred_attachment_cleanup.append(
            (prepared.message, prepared.clear_attachments),
        )
        logger.info(
            "steered message %s from chat %s into Codex turn %s",
            prepared.message.get("message_id"),
            self.chat_id,
            running_turn.handle.turn_id,
        )

    def _wait_for_turn(self, running_turn: RunningCodexTurn) -> None:
        try:
            response = self.bot.codex.wait_turn(running_turn)
            error = None
        except Exception as err:
            logger.exception(
                "Codex turn %s failed for chat %s",
                running_turn.handle.turn_id,
                self.chat_id,
            )
            response = self._execution_error(err)
            error = err
        self.enqueue("turn_completed", (running_turn, response, error))

    def _process_agent_message(self, message: CodexAgentMessage) -> None:
        running_turn = self.active_turn
        if running_turn is None or message.turn_id != running_turn.handle.turn_id:
            logger.debug("ignored agent message for inactive turn %s", message.turn_id)
            return
        target = self.target
        if target is None:
            return
        if target.is_guest:
            if message.phase != "final_answer":
                self.intermediate_messages.append(message.text)
            return

        self._deliver_standard_message(
            target,
            message.text,
            "agent message",
            record_as_bot_response=True,
        )
        target.delivered_agent_messages.append(message.text)

    def _process_generated_image(self, image: CodexGeneratedImage) -> None:
        running_turn = self.active_turn
        if running_turn is None or image.turn_id != running_turn.handle.turn_id:
            logger.debug("ignored generated image for inactive turn %s", image.turn_id)
            return
        self.generated_image_paths.add(image.saved_path.expanduser().resolve())
        logger.info(
            "recorded generated image %s for Codex turn %s",
            image.saved_path,
            image.turn_id,
        )

    def _process_image_generation_started(self) -> None:
        if self.active_turn is None or self.target is None or self.target.is_guest:
            return
        self.bot.codex.send_optional_chat_action(
            self.conversation_key,
            "upload_photo",
        )

    def _process_turn_completed(
        self,
        running_turn: RunningCodexTurn,
        response: str,
        error: Exception | None,
    ) -> None:
        if self.active_turn is not running_turn:
            return
        self.active_turn = None
        queued_stickers = self.bot.codex.take_queued_guest_stickers(
            running_turn.handle.turn_id,
        )
        queued_files = (
            tuple(self.bot.codex.take_queued_guest_files(running_turn.handle.turn_id))
            if self.bot.config.upload_dir is not None else ()
        )
        self._deliver_final(
            response,
            codex_succeeded=error is None,
            queued_stickers=queued_stickers,
            queued_files=queued_files,
        )
        self._finish_target()
        for message, clear_attachments in self.deferred_attachment_cleanup:
            self.bot.attachments.finish_message(message, clear_attachments)
        self.deferred_attachment_cleanup.clear()

        pending = list(self.pending_after_turn)
        self.pending_after_turn.clear()
        for prepared in pending:
            if self.active_turn is None:
                self._start_turn(prepared)
            else:
                self._steer_turn(prepared)

    def _activate_target(self, kind: str, message: dict[str, Any]) -> None:
        self._cancel_target_timer()
        self._stop_typing()
        self.target_generation += 1
        target = TelegramResponseTarget(kind, message, self.target_generation)
        self.target = target
        self.intermediate_messages.clear()
        if target.is_guest:
            delay = guest_response_warning_delay(
                message,
                self.bot.config.guest_response_warning_seconds,
            )
            timer = threading.Timer(
                delay,
                lambda: self.enqueue("guest_deadline", target.generation),
            )
            timer.daemon = True
            target.deadline_timer = timer
            timer.start()
        else:
            stop_typing = threading.Event()
            typing_thread = threading.Thread(
                target=self.bot.keep_typing,
                args=(
                    self.chat_id,
                    message.get("message_thread_id"),
                    stop_typing,
                ),
                daemon=True,
            )
            self.stop_typing = stop_typing
            self.typing_thread = typing_thread
            typing_thread.start()

    def _flush_guest_intermediates_before_steer(self) -> None:
        target = self.target
        if (
            target is None
            or not target.is_guest
            or target.response_sent
            or not self.intermediate_messages
        ):
            return
        response = "\n\n".join(self.intermediate_messages)
        delivery = self.bot.answer_guest_response(
            target.message["guest_query_id"],
            response,
            target.message,
            "intermediate response before steer",
            frozenset(self.generated_image_paths),
        )
        if delivery.error is not None:
            self.bot.codex.record_response_error(self.conversation_key, str(delivery.error))
        if delivery.delivered:
            target.response_sent = True
            target.consumed_reason = "handoff"
            self._cancel_target_timer()
        self.intermediate_messages.clear()

    def _process_guest_deadline(self, generation: int) -> None:
        target = self.target
        if (
            target is None
            or not target.is_guest
            or target.generation != generation
            or target.response_sent
            or self.active_turn is None
        ):
            return
        delivery = self.bot.answer_guest_response(
            target.message["guest_query_id"],
            GUEST_RESPONSE_WARNING_MESSAGE,
            target.message,
            "deadline warning",
            frozenset(self.generated_image_paths),
        )
        if delivery.error is not None:
            self.bot.codex.record_response_error(self.conversation_key, str(delivery.error))
        if delivery.delivered:
            target.response_sent = True
            target.consumed_reason = "deadline"

    def _deliver_final(
        self,
        response: str,
        codex_succeeded: bool,
        queued_stickers: tuple[QueuedTelegramSticker, ...] = (),
        queued_files: tuple[QueuedTelegramFile, ...] = (),
    ) -> None:
        target = self.target
        if target is None:
            return
        if target.is_guest:
            self._deliver_guest_final(
                target,
                response,
                queued_stickers,
                queued_files,
            )
            return
        if codex_succeeded and not response.strip():
            logger.info(
                "Codex turn for message %s in chat %s completed through a dynamic tool "
                "without a textual response",
                target.message.get("message_id"),
                self.chat_id,
            )
            return
        delivered_messages = target.delivered_agent_messages or []
        if delivered_messages and delivered_messages[-1] == response.strip():
            logger.info(
                "final response for message %s to chat %s was already sent as an agent message",
                target.message.get("message_id"),
                self.chat_id,
            )
            return
        self._deliver_standard_message(
            target,
            response,
            "response",
            record_as_bot_response=codex_succeeded,
        )

    def _deliver_guest_final(
        self,
        target: TelegramResponseTarget,
        response: str,
        queued_stickers: tuple[QueuedTelegramSticker, ...],
        queued_files: tuple[QueuedTelegramFile, ...] = (),
    ) -> None:
        if target.response_sent:
            if target.consumed_reason == "deadline":
                self.bot.codex.record_response_error(
                    self.conversation_key,
                    GUEST_DEADLINE_RESPONSE_ERROR,
                )
            logger.info(
                "guest response target for message %s in chat %s was already consumed",
                target.message.get("message_id"),
                self.chat_id,
            )
            return

        if len(queued_stickers) == 1 and not response.strip() and not queued_files:
            delivery = self.bot.answer_guest_sticker_response(
                target.message["guest_query_id"],
                queued_stickers[0].path,
                target.message,
                "response",
            )
            if delivery.delivered:
                target.response_sent = True
                return

        original_target = self.original_guest_target
        split_sticker_delivery = (
            len(queued_stickers) == 1
            and not queued_files
            and bool(response.strip())
            and self.guest_steer_count == 1
            and queued_stickers[0].steer_count == 0
            and original_target is not None
            and original_target is not target
            and not original_target.response_sent
        )
        if split_sticker_delivery:
            delivery = self.bot.answer_guest_sticker_response(
                original_target.message["guest_query_id"],
                queued_stickers[0].path,
                original_target.message,
                "sticker response before steer",
            )
            if delivery.delivered:
                original_target.response_sent = True
                queued_stickers = ()

        if queued_stickers:
            response = prepend_queued_sticker_images(response, queued_stickers)

        generated_image_paths = {
            *self.generated_image_paths,
            *(sticker.path.resolve() for sticker in queued_stickers),
        }
        file_arguments = {"queued_files": queued_files} if queued_files else {}
        delivery = self.bot.answer_guest_response(
            target.message["guest_query_id"],
            response,
            target.message,
            "response",
            frozenset(generated_image_paths),
            **file_arguments,
        )
        if delivery.error is not None:
            self.bot.codex.record_response_error(
                self.conversation_key,
                str(delivery.error),
            )
        target.response_sent = delivery.delivered

    def _deliver_standard_message(
        self,
        target: TelegramResponseTarget,
        text: str,
        response_kind: str,
        record_as_bot_response: bool,
    ) -> None:
        message = target.message
        sent_message_id = self.bot.send_message_response(
            self.chat_id,
            text,
            message["message_id"],
            message.get("message_thread_id"),
            response_kind,
            reply_to_original=target.delivery_count == 0 and target.was_steered,
            generated_image_paths=frozenset(self.generated_image_paths),
        )
        target.delivery_count += 1
        if record_as_bot_response and valid_telegram_message_id(sent_message_id):
            self.bot.codex.record_bot_message(self.conversation_key, sent_message_id)

    def _send_input_error(
        self,
        kind: str,
        message: dict[str, Any],
        error: Exception,
    ) -> None:
        response = self._execution_error(error)
        if kind == "guest_message" and message.get("guest_query_id"):
            delivery = self.bot.answer_guest_response(
                message["guest_query_id"],
                response,
                message,
                "execution error",
            )
            if delivery.error is not None:
                self.bot.codex.record_response_error(
                    self.conversation_key,
                    str(delivery.error),
                )
            return
        self.bot.send_message_response(
            self.chat_id,
            response,
            message["message_id"],
            message.get("message_thread_id"),
            "execution error",
            reply_to_original=True,
        )

    def _execution_error(self, error: Exception) -> str:
        return f"Codex execution failed:\n\n```text\n{error}\n```"

    def _finish_target(self) -> None:
        self._cancel_target_timer()
        self._stop_typing()
        self.target = None
        self.original_guest_target = None
        self.guest_steer_count = 0
        self.intermediate_messages.clear()
        self.generated_image_paths.clear()

    def _cancel_target_timer(self) -> None:
        target = self.target
        if target and target.deadline_timer:
            target.deadline_timer.cancel()
            target.deadline_timer = None

    def _stop_typing(self) -> None:
        if self.stop_typing:
            self.stop_typing.set()
        if self.typing_thread and self.typing_thread is not threading.current_thread():
            self.typing_thread.join(timeout=1)
        self.stop_typing = None
        self.typing_thread = None


class Bot:
    def __init__(self, config: Config) -> None:
        self.config = config
        media_roots = [config.repo_dir.resolve(), *configured_writable_roots(config.repo_dir)]
        if config.sticker_dir is not None:
            media_roots.append(config.sticker_dir.resolve())
        allowed_media_roots = tuple(
            dict.fromkeys(media_roots),
        )
        self.telegram = TelegramClient(
            config.telegram_bot_token,
            config.media_upload_chat_id,
            allowed_media_roots,
        )
        self.codex = CodexRunner(config, self.telegram)
        self.attachments = AttachmentStore(
            config.repo_dir,
            config.attachment_retention_seconds,
        )
        self.offset: int | None = None
        self.bot_user: dict[str, Any] = {}
        self.stop_session_maintenance = threading.Event()
        self.waiting_for_telegram_updates = threading.Event()
        self.controllers: dict[ConversationKey, ConversationController] = {}
        self.controllers_lock = threading.Lock()
        self.update_executor = ThreadPoolExecutor(
            max_workers=config.max_concurrent_chats,
            thread_name_prefix="telegram-chat",
        )
        self.turn_executor = ThreadPoolExecutor(
            max_workers=config.max_concurrent_chats,
            thread_name_prefix="codex-turn",
        )

    def run_forever(self) -> None:
        self.log_startup_state()
        self.codex.start()
        self.waiting_for_telegram_updates.set()

        maintenance_thread = threading.Thread(
            target=self.keep_maintaining_sessions,
            name="codex-session-maintenance",
            daemon=True,
        )
        maintenance_thread.start()
        try:
            while True:
                try:
                    self.waiting_for_telegram_updates.set()
                    updates = self.telegram.get_updates(self.offset, self.config.poll_timeout_seconds)
                    self.waiting_for_telegram_updates.clear()
                    logger.debug("poll returned %s update(s)", len(updates))
                    for update in updates:
                        self.offset = update["update_id"] + 1
                        self.dispatch_update(update)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    self.waiting_for_telegram_updates.clear()
                    logger.exception("failed to process updates")
                    time.sleep(5)
        finally:
            self.waiting_for_telegram_updates.clear()
            self.stop_session_maintenance.set()
            maintenance_thread.join(timeout=1)
            self.codex.close()
            with self.controllers_lock:
                controllers = list(self.controllers.values())
            for controller in controllers:
                controller.shutdown()
            self.turn_executor.shutdown(wait=True, cancel_futures=False)
            self.update_executor.shutdown(wait=True, cancel_futures=False)
            self.telegram.close()

    def keep_maintaining_sessions(self) -> None:
        while not self.stop_session_maintenance.is_set():
            try:
                if self.is_idle():
                    self.codex.compact_idle_sessions(
                        can_continue=self.is_idle,
                    )
                else:
                    logger.debug(
                        "skipping scheduled Codex session compaction while processing Telegram updates",
                    )
            except Exception:
                logger.exception("scheduled Codex session compaction failed; will retry later")
            self.stop_session_maintenance.wait(
                self.config.session_maintenance_interval_seconds,
            )

    def is_idle(self) -> bool:
        if not self.waiting_for_telegram_updates.is_set():
            return False
        with self.controllers_lock:
            controllers = list(self.controllers.values())
        return not any(controller.is_busy() for controller in controllers)

    def dispatch_update(self, update: dict[str, Any]) -> None:
        logger.info("received update %s", update)
        message = update.get("message")
        kind = "message"
        if not message:
            message = update.get("guest_message")
            kind = "guest_message"
        if not message:
            reaction = update.get("message_reaction")
            if isinstance(reaction, dict):
                self.dispatch_message_reaction(update.get("update_id"), reaction)
                return
        if not isinstance(message, dict):
            logger.info(
                "ignored update %s: no message, guest_message, or message_reaction field",
                update.get("update_id"),
            )
            return
        if not self.is_allowed_message(message):
            return
        chat_id = message.get("chat", {}).get("id")
        if not isinstance(chat_id, int):
            logger.info("ignored update %s: no numeric chat id", update.get("update_id"))
            return
        conversation_key = telegram_conversation_key(
            kind,
            chat_id,
            message.get("message_thread_id"),
        )
        with self.controllers_lock:
            controller = self.controllers.get(conversation_key)
            if controller is None:
                controller = ConversationController(self, chat_id, conversation_key)
                self.controllers[conversation_key] = controller
        controller.enqueue("incoming", (kind, message))

    def dispatch_message_reaction(
        self,
        update_id: Any,
        reaction: dict[str, Any],
    ) -> None:
        chat = reaction.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        message_id = reaction.get("message_id")
        if not isinstance(chat_id, int) or not valid_telegram_message_id(message_id):
            logger.info(
                "ignored message_reaction update %s: invalid chat or message ID",
                update_id,
            )
            return

        reaction_text = added_telegram_reaction_text(
            reaction.get("old_reaction", []),
            reaction.get("new_reaction", []),
        )
        if not reaction_text:
            logger.info(
                "ignored message_reaction update %s for message %s in chat %s: "
                "no supported reaction was added",
                update_id,
                message_id,
                chat_id,
            )
            return

        conversation_key = self.codex.conversation_key_for_last_bot_message(
            chat_id,
            message_id,
        )
        if conversation_key is None:
            logger.info(
                "ignored message_reaction update %s for message %s in chat %s: "
                "it is not the recorded latest bot message of a conversation",
                update_id,
                message_id,
                chat_id,
            )
            return

        message = telegram_reaction_message(
            reaction,
            reaction_text,
            self.bot_user,
            conversation_message_thread_id(conversation_key),
        )
        if not self.is_allowed_message(message):
            return

        with self.controllers_lock:
            controller = self.controllers.get(conversation_key)
            if controller is None:
                controller = ConversationController(self, chat_id, conversation_key)
                self.controllers[conversation_key] = controller
        controller.enqueue("incoming", ("message_reaction", message))

    def log_startup_state(self) -> None:
        bot_user = self.telegram.get_me()
        self.bot_user = bot_user
        logger.info(
            "codex telegram bot started with allow-list %s as @%s",
            sorted(self.config.allowed_chat_ids),
            bot_user.get("username"),
        )

        webhook_info = self.telegram.get_webhook_info()
        webhook_url = webhook_info.get("url")
        pending_update_count = webhook_info.get("pending_update_count")
        if not webhook_url:
            logger.info("telegram webhook is not set; getUpdates long polling is available")
            return

        if self.config.delete_webhook_on_start:
            self.telegram.delete_webhook(self.config.drop_pending_updates_on_webhook_delete)
            logger.warning(
                "deleted existing telegram webhook; drop_pending_updates=%s",
                self.config.drop_pending_updates_on_webhook_delete,
            )
            return

        logger.warning(
            "telegram webhook is set; getUpdates will not receive updates until it is removed. "
            "url=%s pending_update_count=%s",
            webhook_url,
            pending_update_count,
        )

    def handle_update(self, update: dict[str, Any]) -> None:
        logger.info("received update %s", update)
        message = update.get("message")
        if message:
            self.handle_message(message)
            return

        guest_message = update.get("guest_message")
        if guest_message:
            self.handle_guest_message(guest_message)
            return

        message_reaction = update.get("message_reaction")
        if isinstance(message_reaction, dict):
            self.dispatch_message_reaction(update.get("update_id"), message_reaction)
            return

        logger.info(
            "ignored update %s: no message, guest_message, or message_reaction field",
            update.get("update_id"),
        )

    def handle_message(self, message: dict[str, Any]) -> None:
        if not self.is_allowed_message(message):
            return

        command = telegram_session_command(message, self.bot_user)
        if command:
            self.handle_session_command(message, command)
            return

        text = message.get("text") or message.get("caption")
        rich_message = message.get("rich_message")
        images = collect_message_images(message)
        files = collect_message_files(message)
        if not text and not rich_message and not images and not files:
            logger.info("ignored message %s: no supported text or attachment", message.get("message_id"))
            return

        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        message_thread_id = message.get("message_thread_id")
        conversation_key = telegram_conversation_key(
            "message",
            chat_id,
            message_thread_id,
        )
        previous_response_error = self.codex.previous_response_error(conversation_key)
        if not isinstance(previous_response_error, str):
            previous_response_error = None
        logger.info("processing message %s from chat %s", message_id, chat_id)

        stop_typing = threading.Event()
        typing_thread = threading.Thread(
            target=self.keep_typing,
            args=(chat_id, message_thread_id, stop_typing),
            daemon=True,
        )
        typing_thread.start()

        agent_message_queue: queue.Queue[str | None] = queue.Queue()
        delivered_agent_messages: list[str] = []
        agent_stream_lock = threading.Lock()
        agent_stream_open = True
        delivery_lock = threading.Lock()
        delivery_count = 0
        codex_succeeded = False

        def deliver_message(
            text: str,
            response_kind: str,
            record_as_bot_response: bool = False,
        ) -> None:
            nonlocal delivery_count
            with delivery_lock:
                sent_message_id = self.send_message_response(
                    chat_id,
                    text,
                    message_id,
                    message_thread_id,
                    response_kind,
                    reply_to_original=delivery_count == 0,
                )
                delivery_count += 1
                if record_as_bot_response and valid_telegram_message_id(sent_message_id):
                    self.codex.record_bot_message(conversation_key, sent_message_id)

        def handle_agent_message(agent_message: str) -> None:
            with agent_stream_lock:
                if agent_stream_open:
                    agent_message_queue.put(agent_message)
                    return

            try:
                deliver_message(agent_message, "agent message", record_as_bot_response=True)
            except Exception:
                logger.exception(
                    "failed to send late Codex agent message for message %s to chat %s",
                    message_id,
                    chat_id,
                )

        def send_agent_messages() -> None:
            message_number = 0
            while True:
                agent_message = agent_message_queue.get()
                if agent_message is None:
                    return

                message_number += 1
                try:
                    deliver_message(
                        agent_message,
                        f"agent message {message_number}",
                        record_as_bot_response=True,
                    )
                    delivered_agent_messages.append(agent_message)
                except Exception:
                    logger.exception(
                        "failed to send agent message %s for message %s to chat %s",
                        message_number,
                        message_id,
                        chat_id,
                    )

        agent_message_thread = threading.Thread(target=send_agent_messages, daemon=True)
        agent_message_thread.start()

        clear_attachments = False
        try:
            attachment_dir, clear_attachments = self.attachments.prepare_message(
                message,
                bool(images or files),
            )
            if attachment_dir:
                image_paths = self.download_images(images, attachment_dir)
                file_paths = self.download_files(files, attachment_dir)
            else:
                image_paths = []
                file_paths = []
            def build_prompt(record: SessionRecord | None) -> list[str]:
                return build_codex_prompt(
                    "message",
                    message,
                    self.bot_user,
                    file_paths,
                    include_reply_to_message=not reply_targets_recorded_bot_message(
                        message,
                        record,
                    ),
                    previous_response_error=previous_response_error,
                )

            response = self.codex.run(
                conversation_key,
                build_prompt,
                image_paths,
                agent_message_handler=handle_agent_message,
                thread_name=telegram_thread_name("message", message),
            )
            codex_succeeded = True
            logger.info(
                "codex response ready for message %s from chat %s (%s characters)",
                message_id,
                chat_id,
                len(response),
            )
        except Exception as err:
            logger.exception("codex execution failed")
            response = f"Codex execution failed:\n\n```text\n{err}\n```"
        finally:
            self.attachments.finish_message(message, clear_attachments)
            with agent_stream_lock:
                agent_stream_open = False
                agent_message_queue.put(None)
            agent_message_thread.join()
            stop_typing.set()
            typing_thread.join(timeout=1)

        if delivered_agent_messages and delivered_agent_messages[-1] == response.strip():
            logger.info(
                "final response for message %s to chat %s was already sent as an agent message",
                message_id,
                chat_id,
            )
            return

        deliver_message(
            response,
            "response",
            record_as_bot_response=codex_succeeded,
        )

    def handle_session_command(self, message: dict[str, Any], command: str) -> None:
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        message_thread_id = message.get("message_thread_id")
        conversation_key = telegram_conversation_key(
            "message",
            chat_id,
            message_thread_id,
        )
        logger.info(
            "processing /%s command in message %s from chat %s",
            command,
            message_id,
            chat_id,
        )

        stop_typing = threading.Event()
        typing_thread: threading.Thread | None = None
        if command == "compact":
            typing_thread = threading.Thread(
                target=self.keep_typing,
                args=(chat_id, message_thread_id, stop_typing),
                daemon=True,
            )
            typing_thread.start()

        try:
            if command == "archive":
                archived_thread_id = self.codex.archive_chat(conversation_key)
                response = (
                    "Conversation archived.\n\n"
                    f"Codex thread ID: `{archived_thread_id}`\n\n"
                    "Your next message will start a new conversation."
                    if archived_thread_id
                    else "There is no active conversation to archive. "
                    "Your next message will start a new conversation."
                )
            else:
                changed = self.codex.compact_chat(conversation_key)
                response = (
                    "Conversation compacted successfully."
                    if changed
                    else "There is no active conversation to compact."
                )
        except Exception as err:
            logger.exception(
                "failed to process /%s command in chat %s",
                command,
                chat_id,
            )
            response = f"Could not {command} the conversation:\n\n```text\n{err}\n```"
        finally:
            stop_typing.set()
            if typing_thread:
                typing_thread.join(timeout=1)

        self.send_message_response(
            chat_id,
            response,
            message_id,
            message_thread_id,
            f"/{command} command response",
            reply_to_original=True,
        )

    def send_message_response(
        self,
        chat_id: int,
        text: str,
        message_id: int,
        message_thread_id: int | None,
        response_kind: str,
        reply_to_original: bool,
        generated_image_paths: frozenset[Path] = frozenset(),
    ) -> int | None:
        if not text.strip():
            logger.info(
                "skipped empty %s for message %s to chat %s",
                response_kind,
                message_id,
                chat_id,
            )
            return None
        reply_to_message_id = message_id if reply_to_original else None
        try:
            sent_message_id = self.telegram.send_rich_message(
                chat_id,
                text,
                reply_to_message_id,
                message_thread_id,
                generated_image_paths,
            )
            logger.info("sent rich %s for message %s to chat %s", response_kind, message_id, chat_id)
            return sent_message_id
        except Exception as err:
            logger.exception(
                "failed to send rich %s; sending the delivery error as plain text",
                response_kind,
            )
            sent_message_id = self.telegram.send_plain_message(
                chat_id,
                telegram_delivery_error_message(err),
                reply_to_message_id,
                message_thread_id,
            )
            logger.info(
                "sent plain-text delivery error for rich %s for message %s to chat %s",
                response_kind,
                message_id,
                chat_id,
            )
            return sent_message_id

    def handle_guest_message(self, message: dict[str, Any]) -> None:
        if not self.is_allowed_message(message):
            return

        guest_query_id = message.get("guest_query_id")
        if not guest_query_id:
            logger.info("ignored guest message %s: no guest_query_id", message.get("message_id"))
            return

        text = message.get("text") or message.get("caption")
        rich_message = message.get("rich_message")
        images = collect_message_images(message)
        files = collect_message_files(message)
        if not text and not rich_message and not images and not files:
            logger.info("ignored guest message %s: no supported text or attachment", message.get("message_id"))
            return

        logger.info(
            "processing guest message %s from chat %s",
            message.get("message_id"),
            message.get("chat", {}).get("id"),
        )
        chat_id = message["chat"]["id"]
        conversation_key = guest_conversation_key(
            chat_id,
            message.get("message_thread_id"),
        )
        previous_response_error = self.codex.previous_response_error(conversation_key)
        if not isinstance(previous_response_error, str):
            previous_response_error = None
        turn_finished = threading.Event()
        guest_response_lock = threading.Lock()
        guest_response_sent = False

        def send_deadline_warning() -> None:
            nonlocal guest_response_sent
            with guest_response_lock:
                if turn_finished.is_set() or guest_response_sent:
                    return
                delivery = self.answer_guest_response(
                    guest_query_id,
                    GUEST_RESPONSE_WARNING_MESSAGE,
                    message,
                    "deadline warning",
                )
                if delivery.error is not None:
                    self.codex.record_response_error(conversation_key, str(delivery.error))
                guest_response_sent = delivery.delivered

        deadline_timer = threading.Timer(
            guest_response_warning_delay(
                message,
                self.config.guest_response_warning_seconds,
            ),
            send_deadline_warning,
        )
        deadline_timer.daemon = True
        deadline_timer.start()

        clear_attachments = False
        try:
            attachment_dir, clear_attachments = self.attachments.prepare_message(
                message,
                bool(images or files),
            )
            if attachment_dir:
                image_paths = self.download_images(images, attachment_dir)
                file_paths = self.download_files(files, attachment_dir)
            else:
                image_paths = []
                file_paths = []
            def build_prompt(record: SessionRecord | None) -> list[str]:
                return build_codex_prompt(
                    "guest_message",
                    message,
                    self.bot_user,
                    file_paths,
                    previous_response_error=previous_response_error,
                )

            response = self.codex.run(
                conversation_key,
                build_prompt,
                image_paths,
                fast_mode=True,
                thread_name=telegram_thread_name("guest_message", message),
            )
            logger.info(
                "codex response ready for guest message %s from chat %s (%s characters)",
                message.get("message_id"),
                chat_id,
                len(response),
            )
        except Exception as err:
            logger.exception("codex execution failed for guest message")
            response = f"Codex execution failed:\n\n```text\n{err}\n```"
        finally:
            turn_finished.set()
            deadline_timer.cancel()
            deadline_timer.join()
            self.attachments.finish_message(message, clear_attachments)

        with guest_response_lock:
            if guest_response_sent:
                self.codex.record_response_error(
                    conversation_key,
                    GUEST_DEADLINE_RESPONSE_ERROR,
                )
                logger.info(
                    "guest deadline warning was sent for message %s to chat %s; "
                    "Telegram will not accept a second guest response",
                    message.get("message_id"),
                    chat_id,
                )
                return

            delivery = self.answer_guest_response(
                guest_query_id,
                response,
                message,
                "response",
            )
            if delivery.error is not None:
                self.codex.record_response_error(conversation_key, str(delivery.error))

    def answer_guest_response(
        self,
        guest_query_id: str,
        response: str,
        message: dict[str, Any],
        response_kind: str,
        generated_image_paths: frozenset[Path] = frozenset(),
        queued_files: tuple[QueuedTelegramFile, ...] = (),
    ) -> GuestResponseDelivery:
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        rich_error: Exception | None = None
        try:
            if queued_files:
                self.telegram.answer_guest_query(
                    guest_query_id,
                    response,
                    generated_image_paths,
                    queued_files=queued_files,
                )
            elif generated_image_paths:
                self.telegram.answer_guest_query(
                    guest_query_id,
                    response,
                    generated_image_paths,
                )
            else:
                self.telegram.answer_guest_query(guest_query_id, response)
            logger.info(
                "sent rich %s for guest message %s to chat %s",
                response_kind,
                message_id,
                chat_id,
            )
            return GuestResponseDelivery(True)
        except Exception as err:
            rich_error = err
            logger.exception(
                "failed to answer guest query with rich %s; "
                "sending the delivery error as plain text",
                response_kind,
            )

        try:
            if rich_error is None:
                raise RuntimeError("rich guest delivery failed without an exception")
            self.telegram.answer_guest_query_with_plain_text(
                guest_query_id,
                telegram_delivery_error_message(rich_error),
            )
            logger.info(
                "sent plain-text delivery error for rich %s for guest message %s to chat %s",
                response_kind,
                message_id,
                chat_id,
            )
            return GuestResponseDelivery(True, rich_error)
        except Exception as err:
            logger.exception(
                "failed to send plain-text delivery error for rich guest %s",
                response_kind,
            )
            return GuestResponseDelivery(False, err)

    def answer_guest_sticker_response(
        self,
        guest_query_id: str,
        sticker_path: Path,
        message: dict[str, Any],
        response_kind: str,
    ) -> GuestResponseDelivery:
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        try:
            self.telegram.answer_guest_query_with_sticker(
                guest_query_id,
                sticker_path,
            )
            logger.info(
                "sent %s as a sticker for guest message %s to chat %s",
                response_kind,
                message_id,
                chat_id,
            )
            return GuestResponseDelivery(True)
        except Exception as err:
            logger.warning(
                "failed to send %s as a sticker for guest message %s to chat %s: %s",
                response_kind,
                message_id,
                chat_id,
                safe_log_text(str(err)),
            )
            return GuestResponseDelivery(False, err)

    def is_allowed_message(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat", {})
        if chat.get("type") not in {"private", "group", "supergroup"}:
            logger.info(
                "ignored message %s: chat type %s is not supported",
                message.get("message_id"),
                chat.get("type"),
            )
            return False

        sender = message.get("from", {})
        chat_id = chat.get("id")
        user_id = sender.get("id") if isinstance(sender, dict) else None
        if chat_id not in self.config.allowed_chat_ids and user_id not in self.config.allowed_chat_ids:
            logger.info(
                "ignored message %s: neither chat %s nor user %s is in allow-list %s",
                message.get("message_id"),
                chat_id,
                user_id,
                sorted(self.config.allowed_chat_ids),
            )
            return False

        return True

    def keep_typing(self, chat_id: int, message_thread_id: int | None, stop_typing: threading.Event) -> None:
        while not stop_typing.is_set():
            try:
                self.telegram.send_typing_action(chat_id, message_thread_id)
            except Exception:
                logger.exception("failed to send typing action")

            stop_typing.wait(self.config.chat_action_interval_seconds)

    def download_images(self, images: list[TelegramImage], cache_dir: Path) -> list[Path]:
        image_paths = []
        for image in images:
            cached_path = find_cached_attachment(cache_dir, image.file_id)
            if cached_path:
                refresh_cached_attachment(cached_path)
                logger.debug("reusing cached Telegram image %s", cached_path)
                image_paths.append(cached_path)
                continue

            file_info = self.telegram.get_file(image.file_id)
            file_path = file_info.get("file_path")
            if not file_path:
                raise RuntimeError(f"Telegram getFile returned no file_path for {image.source} image")

            destination = cache_dir / cached_attachment_name(
                image.file_id,
                image_extension(image, file_path),
            )
            self.download_to_cache(file_path, destination)
            image_paths.append(destination)

        return image_paths

    def download_files(self, files: list[TelegramFile], cache_dir: Path) -> list[Path]:
        file_paths = []
        for attached_file in files:
            cached_path = find_cached_attachment(cache_dir, attached_file.file_id)
            if cached_path:
                refresh_cached_attachment(cached_path)
                logger.debug("reusing cached Telegram file %s", cached_path)
                file_paths.append(cached_path)
                continue

            file_info = self.telegram.get_file(attached_file.file_id)
            file_path = file_info.get("file_path")
            if not file_path:
                raise RuntimeError(
                    f"Telegram getFile returned no file_path for {attached_file.source} file",
                )

            destination = cache_dir / cached_attachment_name(
                attached_file.file_id,
                attached_file_extension(attached_file, file_path),
            )
            self.download_to_cache(file_path, destination)
            file_paths.append(destination)

        return file_paths

    def download_to_cache(self, telegram_file_path: str, destination: Path) -> None:
        temporary = destination.with_name(f".{destination.name}-{uuid.uuid4().hex}.part")
        try:
            self.telegram.download_file(telegram_file_path, temporary)
            if not temporary.is_file() or not os.access(temporary, os.R_OK):
                raise RuntimeError(f"downloaded Telegram file is not readable: {temporary}")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        chunks.append(remaining[:limit])
        remaining = remaining[limit:]

    return chunks


def telegram_delivery_error_message(error: Exception) -> str:
    error_text = str(error).strip() or type(error).__name__
    error_text = error_text[:3000]
    return error_text


def valid_telegram_message_id(message_id: Any) -> bool:
    return isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0


def telegram_session_command(
    message: dict[str, Any],
    bot_user: dict[str, Any],
) -> str | None:
    text = message.get("text")
    if not isinstance(text, str):
        return None

    token = text.split(maxsplit=1)[0] if text.split() else ""
    match = BOT_COMMAND_PATTERN.fullmatch(token)
    if not match:
        return None

    command, addressed_username = match.groups()
    command = command.lower()
    if command not in {"archive", "compact"}:
        return None

    if addressed_username:
        bot_username = bot_user.get("username")
        if not isinstance(bot_username, str) or addressed_username.lower() != bot_username.lower():
            return None

    return command


def telegram_response_message_id(response: dict[str, Any]) -> int | None:
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    message_id = result.get("message_id")
    return message_id if valid_telegram_message_id(message_id) else None


def prepend_queued_sticker_images(
    response: str,
    stickers: tuple[QueuedTelegramSticker, ...],
) -> str:
    images = []
    for sticker in stickers:
        alt = sticker.name.replace("\\", "\\\\").replace("]", "\\]")
        images.append(f"![{alt}]({sticker.path.resolve().as_uri()})")
    if response:
        images.append(response)
    return "\n\n".join(images)


def guest_media_cache_key(reference: TelegramRichMediaReference) -> str:
    if reference.local_path is None:
        return f"url:{reference.source_url}"
    return f"file:{local_media_cache_key(reference.local_path)}"


def local_media_cache_key(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as image_file:
        for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def telegram_uploaded_photo_file_id(message: Any) -> str:
    photo = message.get("photo") if isinstance(message, dict) else getattr(message, "photo", None)
    if not isinstance(photo, (list, tuple)) or not photo:
        raise RuntimeError("Telegram sendPhoto returned no photo sizes")
    largest = photo[-1]
    file_id = largest.get("file_id") if isinstance(largest, dict) else getattr(largest, "file_id", None)
    if not isinstance(file_id, str) or not file_id:
        raise RuntimeError("Telegram sendPhoto returned no reusable file ID")
    return file_id


def telegram_sticker_file_id(response: dict[str, Any]) -> str:
    message = response.get("result")
    sticker = message.get("sticker") if isinstance(message, dict) else None
    file_id = sticker.get("file_id") if isinstance(sticker, dict) else None
    if not isinstance(file_id, str) or not file_id:
        raise RuntimeError("Telegram sendSticker returned no reusable file ID")
    return file_id


def telegram_document_file_id(response: dict[str, Any]) -> str:
    message = response.get("result")
    document = message.get("document") if isinstance(message, dict) else None
    file_id = document.get("file_id") if isinstance(document, dict) else None
    if not isinstance(file_id, str) or not file_id:
        raise RuntimeError("Telegram sendDocument returned no reusable file ID")
    return file_id


def telegram_uploaded_message_id(message: Any) -> int | None:
    message_id = (
        message.get("message_id")
        if isinstance(message, dict)
        else getattr(message, "message_id", None)
    )
    return message_id if valid_telegram_message_id(message_id) else None


def collect_message_images(message: dict[str, Any]) -> list[TelegramImage]:
    images = extract_images(message, "current_message")
    images.extend(extract_rich_images(message.get("rich_message"), "current_message.rich_message"))
    reply_to_message = message.get("reply_to_message")
    if reply_to_message:
        images.extend(extract_images(reply_to_message, "reply_to_message"))
        images.extend(
            extract_rich_images(
                reply_to_message.get("rich_message"),
                "reply_to_message.rich_message",
            ),
        )

    return deduplicate_telegram_attachments(images)


def collect_message_files(message: dict[str, Any]) -> list[TelegramFile]:
    files = extract_files(message, "current_message")
    files.extend(extract_rich_files(message.get("rich_message"), "current_message.rich_message"))
    reply_to_message = message.get("reply_to_message")
    if reply_to_message:
        files.extend(extract_files(reply_to_message, "reply_to_message"))
        files.extend(
            extract_rich_files(
                reply_to_message.get("rich_message"),
                "reply_to_message.rich_message",
            ),
        )

    return deduplicate_telegram_attachments(files)


def extract_images(message: dict[str, Any], source: str) -> list[TelegramImage]:
    images = []
    photo = largest_photo(message.get("photo"))
    if photo:
        images.append(TelegramImage(file_id=photo["file_id"], source=source))

    document = message.get("document")
    if document and is_image_document(document):
        images.append(
            TelegramImage(
                file_id=document["file_id"],
                source=source,
                file_name=document.get("file_name"),
                mime_type=document.get("mime_type"),
            ),
        )

    return images


def extract_files(message: dict[str, Any], source: str) -> list[TelegramFile]:
    files = []
    document = message.get("document")
    if document and not is_image_document(document):
        files.append(
            TelegramFile(
                file_id=document["file_id"],
                source=source,
                file_name=document.get("file_name"),
                mime_type=document.get("mime_type"),
            ),
        )

    for attachment_type in ("audio", "video", "animation", "voice", "video_note", "sticker"):
        attachment = message.get(attachment_type)
        if not isinstance(attachment, dict) or not attachment.get("file_id"):
            continue

        files.append(
            TelegramFile(
                file_id=attachment["file_id"],
                source=source,
                file_name=attachment.get("file_name"),
                mime_type=attachment.get("mime_type"),
                attachment_type=attachment_type,
            ),
        )

    return files


def extract_rich_images(rich_message: Any, source: str) -> list[TelegramImage]:
    images = []
    for block in iter_rich_blocks(rich_message):
        if block.get("type") != "photo":
            continue

        photo = largest_photo(block.get("photo"))
        if photo and photo.get("file_id"):
            images.append(TelegramImage(file_id=photo["file_id"], source=source))

    return images


def extract_rich_files(rich_message: Any, source: str) -> list[TelegramFile]:
    files = []
    for block in iter_rich_blocks(rich_message):
        attachment_type = block.get("type")
        if attachment_type not in {"animation", "audio", "video", "voice_note"}:
            continue

        attachment = block.get(attachment_type)
        if attachment_type == "voice_note" and attachment is None:
            attachment = block.get("voice")
        if not isinstance(attachment, dict) or not attachment.get("file_id"):
            continue

        files.append(
            TelegramFile(
                file_id=attachment["file_id"],
                source=source,
                file_name=attachment.get("file_name"),
                mime_type=attachment.get("mime_type"),
                attachment_type=attachment_type,
            ),
        )

    return files


def iter_rich_blocks(rich_message: Any) -> Iterator[dict[str, Any]]:
    if not isinstance(rich_message, dict):
        return

    yield from iter_nested_rich_blocks(rich_message.get("blocks"))


def iter_nested_rich_blocks(blocks: Any) -> Iterator[dict[str, Any]]:
    if not isinstance(blocks, list):
        return

    for block in blocks:
        if not isinstance(block, dict):
            continue

        yield block
        yield from iter_nested_rich_blocks(block.get("blocks"))

        items = block.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                yield from iter_nested_rich_blocks(item.get("blocks"))


def deduplicate_telegram_attachments(attachments: list[Any]) -> list[Any]:
    unique = []
    seen_file_ids = set()
    for attachment in attachments:
        if attachment.file_id in seen_file_ids:
            continue
        unique.append(attachment)
        seen_file_ids.add(attachment.file_id)

    return unique


def largest_photo(photos: Any) -> dict[str, Any] | None:
    if not isinstance(photos, list) or not photos:
        return None

    return max(
        photos,
        key=lambda item: (
            item.get("file_size") or 0,
            item.get("width") or 0,
            item.get("height") or 0,
        ),
    )


def is_image_document(document: dict[str, Any]) -> bool:
    mime_type = document.get("mime_type")
    if isinstance(mime_type, str) and mime_type.startswith("image/"):
        return True

    file_name = document.get("file_name")
    if not isinstance(file_name, str):
        return False

    guessed_type, _ = mimetypes.guess_type(file_name)
    return bool(guessed_type and guessed_type.startswith("image/"))


def attachment_cache_key(file_id: str) -> str:
    return hashlib.sha256(file_id.encode("utf-8")).hexdigest()


def guest_response_warning_delay(
    message: dict[str, Any],
    warning_seconds: int,
    now: float | None = None,
) -> float:
    message_date = message.get("date")
    if not isinstance(message_date, (int, float)) or isinstance(message_date, bool):
        return float(warning_seconds)

    checked_at = now if now is not None else time.time()
    message_age = max(0.0, checked_at - float(message_date))
    return max(0.0, warning_seconds - message_age)


def find_cached_attachment(cache_dir: Path, file_id: str) -> Path | None:
    cache_key = attachment_cache_key(file_id)
    for candidate in cache_dir.glob(f"{cache_key}.*"):
        if candidate.is_file() and os.access(candidate, os.R_OK):
            return candidate

    return None


def refresh_cached_attachment(path: Path) -> None:
    try:
        path.touch()
    except OSError:
        logger.warning("failed to refresh retained Telegram attachment %s", path, exc_info=True)


def cached_attachment_name(file_id: str, extension: str) -> str:
    return f"{attachment_cache_key(file_id)}{extension}"


def image_extension(image: TelegramImage, telegram_file_path: str) -> str:
    for candidate in (image.file_name, telegram_file_path):
        if candidate:
            suffix = Path(candidate).suffix
            if suffix:
                return suffix

    if image.mime_type:
        extension = mimetypes.guess_extension(image.mime_type)
        if extension:
            return extension

    return ".jpg"


def attached_file_extension(attached_file: TelegramFile, telegram_file_path: str) -> str:
    for candidate in (attached_file.file_name, telegram_file_path):
        if candidate:
            suffix = Path(candidate).suffix
            if suffix:
                return suffix

    if attached_file.mime_type:
        extension = mimetypes.guess_extension(attached_file.mime_type)
        if extension:
            return extension

    return ".bin"


def build_codex_prompt(
    message_kind: str,
    message: dict[str, Any],
    bot_user: dict[str, Any],
    file_paths: list[Path] | None = None,
    *,
    include_reply_to_message: bool = True,
    previous_response_error: str | None = None,
) -> list[str]:
    user_text = format_current_message_markdown(message)
    if not user_text:
        if collect_message_images(message):
            user_text = "Please respond to the attached image."
        elif collect_message_files(message):
            user_text = "Please inspect the attached file."

    reply_to_message = message.get("reply_to_message")
    formatted_reply = None
    reply_quotes: list[str] = []
    if reply_to_message and include_reply_to_message:
        formatted_reply = format_reply_to_message(reply_to_message, bot_user)
        reply_quotes = format_reply_quotes(reply_to_message, formatted_reply)

    context: dict[str, Any] = {
        "source": "telegram",
        "message_kind": message_kind,
        "attachments": format_attachments(collect_message_files(message), file_paths or []),
        "chat": format_chat(message.get("chat", {})),
        "asking_user": format_user(message.get("from", {})),
    }
    if message_kind != "message_reaction":
        context["current_message"] = {
            "message_id": message.get("message_id"),
            "date": message.get("date"),
        }
    if include_reply_to_message:
        context["reply_to_message"] = formatted_reply
    if previous_response_error:
        context["previous_response_error"] = previous_response_error

    user_message_parts = [quote_markdown(quote) for quote in reply_quotes if quote]
    if user_text:
        user_message_parts.append(user_text)
    user_message = "\n\n".join(user_message_parts)

    telegram_context = "\n".join(
        [
            "<telegram_context>",
            json.dumps(context, ensure_ascii=False, indent=2),
            "</telegram_context>\n",
        ],
    )
    return [telegram_context, user_message]


def format_current_message_markdown(message: dict[str, Any]) -> str:
    rich_message = message.get("rich_message")
    if rich_message is not None:
        try:
            return rich_message_to_markdown(rich_message)
        except Exception:
            logger.warning(
                "failed to convert current Telegram rich message %s; using raw payload",
                message.get("message_id"),
                exc_info=True,
            )
            return json.dumps(message_payload(message), ensure_ascii=False, indent=2)

    text, entities = message_text_and_entities(message)
    if not text:
        return ""
    try:
        return telegram_entities_to_markdown(text, entities)
    except Exception:
        logger.warning(
            "failed to convert current Telegram message %s entities; using plain text",
            message.get("message_id"),
            exc_info=True,
        )
        return text


def format_reply_quotes(
    message: dict[str, Any],
    formatted_reply: dict[str, Any],
) -> list[str]:
    raw_payload = formatted_reply.get("raw_payload")
    if not isinstance(raw_payload, dict):
        raw_payload = {}

    quoted_original = formatted_reply.pop("quoted_original_message", None)
    quoted_content = (
        quoted_original.get("content")
        if isinstance(quoted_original, dict)
        else None
    )
    raw_text = raw_payload.get("text") or raw_payload.get("caption")

    rich_message = message.get("rich_message")
    rich_conversion_failed = False
    if rich_message is not None:
        try:
            reply_markdown = rich_message_to_markdown(rich_message)
        except Exception:
            logger.warning(
                "failed to convert replied-to Telegram rich message %s; omitting its content",
                message.get("message_id"),
                exc_info=True,
            )
            reply_markdown = ""
            rich_conversion_failed = True
    else:
        text, entities = message_text_and_entities(message)
        reply_markdown = ""
        if text:
            try:
                reply_markdown = telegram_entities_to_markdown(text, entities)
            except Exception:
                logger.warning(
                    "failed to convert replied-to Telegram message %s entities; using plain text",
                    message.get("message_id"),
                    exc_info=True,
                )
                reply_markdown = text

    for body_field in (
        "rich_message",
        "text",
        "entities",
        "caption",
        "caption_entities",
    ):
        raw_payload.pop(body_field, None)

    if not raw_payload:
        formatted_reply.pop("raw_payload", None)
    if rich_conversion_failed:
        return []

    quotes = [reply_markdown] if reply_markdown else []
    if isinstance(quoted_content, str) and quoted_content and quoted_content != raw_text:
        try:
            quotes.append(telegram_entities_to_markdown(quoted_content, None))
        except Exception:
            logger.warning(
                "failed to format quoted original Telegram message %s; using plain text",
                message.get("message_id"),
                exc_info=True,
            )
            quotes.append(quoted_content)
    return quotes


def message_text_and_entities(message: dict[str, Any]) -> tuple[str, Any]:
    text = message.get("text")
    if isinstance(text, str):
        return text, message.get("entities")
    caption = message.get("caption")
    if isinstance(caption, str):
        return caption, message.get("caption_entities")
    return "", None


def telegram_entities_to_markdown(text: str, entities: Any) -> str:
    try:
        return entities_to_markdown(text, entities)
    except Exception:
        logger.warning(
            "local Telegram entity conversion failed; falling back to Telegram MarkdownV2",
            exc_info=True,
        )
        return telegram_entities_to_markdownv2(text, entities)


def telegram_entities_to_markdownv2(text: str, entities: Any) -> str:
    if entities is None:
        return entities_to_markdownv2(text)
    if not isinstance(entities, list):
        raise TypeError("Telegram message entities must be a list")

    converted_entities = []
    supported_fields = {
        "type",
        "offset",
        "length",
        "url",
        "language",
        "custom_emoji_id",
        "user",
        "unix_time",
        "date_time_format",
    }
    for entity in entities:
        if not isinstance(entity, dict):
            raise TypeError("Telegram message entity must be an object")
        converted_entities.append(
            MessageEntity(
                **{key: value for key, value in entity.items() if key in supported_fields},
            ),
        )
    return entities_to_markdownv2(text, converted_entities)


def quote_markdown(markdown: str) -> str:
    return "\n".join(">" if not line else f"> {line}" for line in markdown.splitlines())


def reply_targets_recorded_bot_message(
    message: dict[str, Any],
    record: SessionRecord | None,
) -> bool:
    if not record or record.last_bot_message_id is None:
        return False
    reply_to_message = message.get("reply_to_message")
    if not isinstance(reply_to_message, dict):
        return False
    reply_message_id = reply_to_message.get("message_id")
    return (
        valid_telegram_message_id(reply_message_id)
        and reply_message_id == record.last_bot_message_id
    )


def format_attachments(files: list[TelegramFile], file_paths: list[Path]) -> list[dict[str, Any]]:
    formatted = []
    for index, attached_file in enumerate(files):
        item = attached_file.__dict__.copy()
        if index < len(file_paths):
            item["local_path"] = str(file_paths[index])
        formatted.append(item)

    return formatted


def telegram_thread_key(message: dict[str, Any]) -> str:
    chat_id = message.get("chat", {}).get("id", "unknown")
    message_thread_id = message.get("message_thread_id")
    thread_id = message_thread_id if message_thread_id is not None else "main"
    return f"chat-{chat_id}-thread-{thread_id}"


def format_reply_to_message(message: dict[str, Any], bot_user: dict[str, Any]) -> dict[str, Any]:
    sender = format_user(message.get("from", {}))
    is_from_current_bot = is_current_bot_user(message.get("from", {}), bot_user)
    formatted: dict[str, Any] = {
        "message_id": message.get("message_id"),
        "date": message.get("date"),
        "sender": sender,
        "is_from_current_bot": is_from_current_bot,
        "raw_payload": message_payload(message),
    }

    quoted_content = best_effort_message_content(message)
    if not is_from_current_bot and quoted_content:
        formatted["quoted_original_message"] = {
            "author": sender["display_name"],
            "content": quoted_content,
        }
    return formatted


def message_payload(message: dict[str, Any]) -> dict[str, Any]:
    metadata_keys = {
        "message_id",
        "from",
        "sender_chat",
        "chat",
        "date",
        "reply_to_message",
        "guest_query_id",
        "guest_bot_caller_user",
    }
    return {key: value for key, value in message.items() if key not in metadata_keys}


def best_effort_message_content(message: dict[str, Any]) -> str:
    text = message.get("text") or message.get("caption")
    if text:
        return text

    # RichMessage text and captions are converted separately. Serializing the
    # same rich_message here would add its raw JSON as a second reply quote.
    if message.get("rich_message") is not None:
        return ""

    payload = message_payload(message)
    if not payload:
        return ""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def format_chat(chat: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": chat.get("id"),
        "type": chat.get("type"),
        "title": chat.get("title"),
        "username": chat.get("username"),
    }


def format_user(user: dict[str, Any]) -> dict[str, Any]:
    first_name = user.get("first_name")
    last_name = user.get("last_name")
    username = user.get("username")
    display_name = " ".join(name for name in (first_name, last_name) if name).strip()
    if not display_name:
        display_name = f"@{username}" if username else str(user.get("id", "unknown"))

    return {
        "id": user.get("id"),
        "is_bot": user.get("is_bot"),
        "display_name": display_name,
        "username": username,
    }


def is_current_bot_user(user: dict[str, Any], bot_user: dict[str, Any]) -> bool:
    if not user or not bot_user:
        return False

    return user.get("id") == bot_user.get("id")


def rich_message_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "to_dict"):
        return item.to_dict()
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)

    raise TypeError(f"unsupported rich message type: {type(item).__name__}")


def telegram_api_object_to_dict(result: Any, method: str) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if hasattr(result, "to_dict"):
        converted = result.to_dict()
        if isinstance(converted, dict):
            return converted
    raise RuntimeError(f"Telegram {method} returned an invalid result")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("CODEX_TELEGRAM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    Bot(Config.from_env()).run_forever()


if __name__ == "__main__":
    main()
