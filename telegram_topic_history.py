"""Persist topic identities learned from live Telegram messages and deliveries."""

import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from telegramify_markdown import telegramify_rich

try:
    from .telegram_math import normalize_math_delimiters
except ImportError:
    from telegram_math import normalize_math_delimiters


logger = logging.getLogger(__name__)
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _valid_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_chat_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def private_guest_chat_key(message: dict[str, Any]) -> str | None:
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or chat.get("type") != "private" or not isinstance(sender, dict):
        return None
    chat_id = chat.get("id")
    sender_id = sender.get("id")
    if not _valid_id(chat_id) or not _valid_id(sender_id) or chat_id == sender_id:
        return None
    first, second = sorted((chat_id, sender_id))
    return f"guest:private:{first}:{second}"


def _scope(kind: str, chat_id: int, message: dict[str, Any]) -> str:
    if kind == "guest_message":
        private_key = private_guest_chat_key(message)
        if private_key is not None:
            return f"{private_key}:view:{chat_id}"
    namespace = "guest" if kind == "guest_message" else "ordinary"
    return f"{namespace}:{chat_id}"


def _valid_scope(scope: Any) -> bool:
    if not isinstance(scope, str):
        return False
    if re.fullmatch(r"(?:ordinary|guest):-?\d+", scope):
        return True
    match = re.fullmatch(r"guest:private:([1-9]\d*):([1-9]\d*):view:([1-9]\d*)", scope)
    if match is None:
        return False
    first, second, view = map(int, match.groups())
    return first < second and view in {first, second}


class _RichHTMLText(HTMLParser):
    """Canonical visible text, preserving link destinations and literal code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str | None] = []
        self.code_depth = 0
        self.code_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"code", "pre"}:
            self.code_depth += 1
        elif tag == "a":
            self.links.append(dict(attrs).get("href"))
        elif tag in {"p", "div", "br", "li", "tr", "td", "th", "blockquote", "figure", "figcaption"} or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"code", "pre"}:
            self.code_depth = max(0, self.code_depth - 1)
            if not self.code_depth:
                content = " ".join("".join(self.code_parts).split())
                self.parts.append(f"\ue002{content}\ue003")
                self.code_parts.clear()
        elif tag == "a" and self.links:
            target = self.links.pop()
            if target:
                self.parts.append(f" [{target}]")
        elif tag in {"p", "div", "li", "tr", "td", "th", "blockquote", "figure", "figcaption"} or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        (self.code_parts if self.code_depth else self.parts).append(data)


def _default_message_text(message: dict[str, Any]) -> str:
    rich_message = message.get("rich_message")
    if isinstance(rich_message, dict):
        if isinstance(rich_message.get("html"), str):
            parser = _RichHTMLText()
            parser.feed(rich_message["html"])
            return "".join(parser.parts)
        if isinstance(rich_message.get("markdown"), str):
            return rich_message["markdown"]
        try:
            from telegram_rich_to_markdown import rich_message_to_markdown
        except ImportError:
            from .telegram_rich_to_markdown import rich_message_to_markdown
        return rich_message_to_markdown(rich_message)

    text = message.get("text")
    entities = message.get("entities")
    if not isinstance(text, str):
        text = message.get("caption")
        entities = message.get("caption_entities")
    if not isinstance(text, str):
        return ""
    if entities:
        try:
            from telegram_entities_to_markdown import entities_to_markdown
        except ImportError:
            from .telegram_entities_to_markdown import entities_to_markdown
        return entities_to_markdown(text, entities)
    return text


def normalize_topic_response(text: str | dict[str, Any]) -> str:
    """Compare complete rendered bodies using the same Markdown renderer as delivery."""
    rich = text.get("rich_message", text) if isinstance(text, dict) else None
    if isinstance(rich, dict) and isinstance(rich.get("html"), str):
        fragments = [rich["html"]]
    else:
        if isinstance(text, dict):
            if isinstance(rich, dict) and any(key in rich for key in ("markdown", "blocks")):
                text = _default_message_text({"rich_message": rich})
            else:
                text = _default_message_text(text)
        if not isinstance(text, str) or not text.strip():
            return ""
        fragments = []
        for item in telegramify_rich(normalize_math_delimiters(text)):
            if isinstance(item, dict):
                payload = item
            elif hasattr(item, "to_dict"):
                payload = item.to_dict()
            else:
                payload = item.model_dump(exclude_none=True)
            fragments.append(payload.get("html", ""))
    parser = _RichHTMLText()
    for fragment in fragments:
        parser.feed(fragment)
    parser.close()
    return " ".join(unicodedata.normalize("NFKC", "".join(parser.parts)).split())


def _fingerprint(text: str | dict[str, Any]) -> str | None:
    normalized = normalize_topic_response(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None


class TelegramTopicHistory:
    def __init__(
        self,
        path: Path,
        message_text: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.message_text = message_text or _default_message_text
        self.lock = threading.RLock()
        self._dirty = False
        self._scopes: dict[str, dict[str, dict[str, set[int]]]] = self._load()

    def resolve(
        self,
        kind: str,
        message: dict[str, Any],
        bot_user: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = copy.deepcopy(message)
        chat = normalized.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if not _valid_chat_id(chat_id):
            return normalized
        scope = _scope(kind, chat_id, normalized)
        current_id = normalized.get("message_id")
        explicit = normalized.get("message_thread_id")
        if normalized.get("is_topic_message") is False:
            normalized.pop("message_thread_id", None)
            return normalized
        if not _valid_id(explicit):
            normalized.pop("message_thread_id", None)
            explicit = None

        reply = normalized.get("reply_to_message")
        if not isinstance(reply, dict) or reply.get("is_topic_message") is False or not self._same_chat(reply, chat_id):
            reply = None
        with self.lock:
            current_root = self._creation_topic(normalized)
            reply_root = self._creation_topic(reply) if reply is not None else None
            reply_explicit = None
            if (
                reply is not None
                and normalized.get("is_topic_message") is not False
                and reply.get("is_topic_message") is not False
                and _valid_id(reply.get("message_thread_id"))
            ):
                reply_explicit = reply["message_thread_id"]
            known_current = self._known_message_topic(scope, current_id)
            known_reply = self._known_message_topic(scope, reply.get("message_id")) if reply is not None else None
            strong = {topic for topic in (current_root, reply_root, reply_explicit, known_current, known_reply) if topic is not None}
            topic = explicit if explicit is not None else (next(iter(strong)) if len(strong) == 1 else None)

            is_bot_reply = reply is not None and self._is_current_bot(reply, bot_user)
            reply_text = self._extract_text(reply) if is_bot_reply else ""
            if topic is None and not strong and is_bot_reply:
                matches = self._response_topics(scope, reply_text)
                if len(matches) == 1 and _valid_id(next(iter(matches))):
                    topic = next(iter(matches))

            if topic is not None:
                normalized["message_thread_id"] = topic
                self._record_message(scope, current_id, topic)
                # Native same-chat replies belong to the same thread; an explicit
                # reply topic, when present, remains its own authoritative identity.
                reply_topic = reply_explicit or reply_root or known_reply or topic
                if reply is not None:
                    self._record_message(scope, reply.get("message_id"), reply_topic)
                    if is_bot_reply and reply_text:
                        self._record_response(scope, reply_text, reply_topic)
            self._save_if_dirty()
        return normalized

    def record_response(
        self,
        kind: str,
        message: dict[str, Any],
        text: str | dict[str, Any],
        message_id: int | None = None,
    ) -> None:
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        topic = message.get("message_thread_id")
        if not _valid_chat_id(chat_id):
            return
        if message.get("is_topic_message") is False or not _valid_id(topic):
            topic = 0
        with self.lock:
            scope = _scope(kind, chat_id, message)
            if topic:
                self._record_message(scope, message_id, topic)
            self._record_response(scope, text, topic)
            self._save_if_dirty()


    @staticmethod
    def _same_chat(message: dict[str, Any], chat_id: int) -> bool:
        chat = message.get("chat")
        return chat is None or (isinstance(chat, dict) and chat.get("id") == chat_id)

    @staticmethod
    def _creation_topic(message: dict[str, Any] | None) -> int | None:
        if not isinstance(message, dict):
            return None
        message_id = message.get("message_id")
        return message_id if _valid_id(message_id) and isinstance(message.get("forum_topic_created"), dict) else None

    @staticmethod
    def _is_current_bot(message: dict[str, Any], bot_user: dict[str, Any]) -> bool:
        sender = message.get("from")
        bot_id = bot_user.get("id")
        return isinstance(sender, dict) and isinstance(bot_id, int) and not isinstance(bot_id, bool) and sender.get("id") == bot_id

    def _extract_text(self, message: dict[str, Any]) -> str:
        try:
            text = self.message_text(message)
            return text if isinstance(text, str) else ""
        except Exception:
            logger.warning("could not extract Telegram reply text for topic recovery", exc_info=True)
            return ""

    def _known_message_topic(self, scope: str, message_id: Any) -> int | None:
        if not _valid_id(message_id):
            return None
        topics = self._scopes.get(scope, {}).get("messages", {}).get(str(message_id), set())
        return next(iter(topics)) if len(topics) == 1 else None

    def _response_topics(self, scope: str, text: str) -> set[int]:
        fingerprint = _fingerprint(text)
        return self._scopes.get(scope, {}).get("responses", {}).get(fingerprint, set()) if fingerprint else set()

    def _add(self, scope: str, category: str, key: str, topic: int) -> None:
        data = self._scopes.setdefault(scope, {"messages": {}, "responses": {}})
        topics = data[category].setdefault(key, set())
        if topic not in topics:
            topics.add(topic)
            self._dirty = True

    def _record_message(self, scope: str, message_id: Any, topic: int) -> None:
        if _valid_id(message_id):
            self._add(scope, "messages", str(message_id), topic)

    def _record_response(self, scope: str, text: str | dict[str, Any], topic: int) -> None:
        fingerprint = _fingerprint(text)
        if fingerprint:
            self._add(scope, "responses", fingerprint, topic)

    def _load(self) -> dict[str, dict[str, dict[str, set[int]]]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise ValueError(f"cannot read Telegram topic history {self.path}") from err
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("scopes"), dict):
            raise ValueError(f"invalid Telegram topic history {self.path}")
        scopes = {}
        for scope, data in payload["scopes"].items():
            if not _valid_scope(scope) or not isinstance(data, dict):
                raise ValueError(f"invalid Telegram topic history scope in {self.path}")
            loaded = {}
            for category in ("messages", "responses"):
                values = data.get(category)
                if not isinstance(values, dict):
                    raise ValueError(f"invalid Telegram topic history index in {self.path}")
                loaded[category] = {}
                for key, topics in values.items():
                    valid_key = isinstance(key, str) and (key.isdigit() and int(key) > 0 if category == "messages" else bool(_HASH_PATTERN.fullmatch(key)))
                    valid_topics = isinstance(topics, list) and bool(topics) and all(
                        _valid_id(topic) or (category == "responses" and isinstance(topic, int) and not isinstance(topic, bool) and topic == 0)
                        for topic in topics
                    )
                    if not valid_key or not valid_topics:
                        raise ValueError(f"invalid Telegram topic history entry in {self.path}")
                    loaded[category][key] = set(topics)
            scopes[scope] = loaded
        return scopes

    def _save_if_dirty(self) -> None:
        if not self._dirty:
            return
        temp_path = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "scopes": {
                    scope: {category: {key: sorted(topics) for key, topics in values.items()} for category, values in data.items()}
                    for scope, data in self._scopes.items()
                },
            }
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent, prefix=f".{self.path.name}.", encoding="utf-8", delete=False) as output:
                temp_path = Path(output.name)
                os.fchmod(output.fileno(), 0o600)
                json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, self.path)
            self._dirty = False
        except OSError:
            logger.warning("could not persist Telegram topic history %s; retaining the in-memory index", self.path, exc_info=True)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("could not remove temporary Telegram topic history %s", temp_path, exc_info=True)

