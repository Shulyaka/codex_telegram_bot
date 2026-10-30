# Codex Telegram Bot

Small long-polling Telegram bot that forwards messages and attachments from allow-listed Telegram chats and users to Codex.
Each ordinary bot chat or forum topic has its own persistent Codex session history. Guest invocations use a separate
guest-chat namespace because Telegram guest chat IDs can collide with ordinary bot chat IDs.
For ordinary messages, it streams every completed Codex agent message back as a Telegram Rich Message.
Guest bot invocations are handled through Telegram's `guest_message` updates and `answerGuestQuery`,
which permits one response for each guest query.

## Setup

Requires Python 3.10+.

```bash
python3 -m venv .venv/codex-telegram-bot
. .venv/codex-telegram-bot/bin/activate
python -m pip install -r tools/codex_telegram_bot/requirements.txt
```

## Configuration

Required environment variables:

- `TELEGRAM_BOT_TOKEN` - token from BotFather.
- `TELEGRAM_ALLOWED_CHAT_IDS` - comma-separated numeric IDs of users, private chats, groups, or supergroups the bot may answer for.

Optional environment variables:

- `CODEX_TELEGRAM_REPO_DIR` - repository directory for Codex app-server threads; defaults to the process working directory.
- `CODEX_TELEGRAM_INSTRUCTIONS_FILE` - optional base instructions file for new and resumed app-server threads.
  Unset or empty uses Codex's standard instructions. Paths expand `~`; relative paths are resolved against
  `CODEX_TELEGRAM_REPO_DIR`, and absolute paths are supported. A missing configured file logs a warning and
  uses standard instructions; other read errors remain fatal.
- `CODEX_TELEGRAM_APP_SERVER_SOCKET` - Unix socket of the shared Codex app-server; defaults to
  `$CODEX_HOME/app-server-control/app-server-control.sock`, or `~/.codex/app-server-control/app-server-control.sock`
  when `CODEX_HOME` is unset.
- `CODEX_TELEGRAM_READ_ONLY` - defaults to `true`, preserving the `read-only` sandbox and `never` approval
  policy. Set to `false` for `workspace-write`, `on-request` approvals, and the `auto_review` approvals reviewer.
  Auto-review can approve sandbox escalation for additional file or network access. The bot does not provide
  manual approval prompts in Telegram; rejected requests remain blocked.
- `CODEX_TELEGRAM_SESSION_STORE` - JSON file used to persist the Codex session ID for each chat; defaults to
  `sessions.json` in `CODEX_TELEGRAM_REPO_DIR`, keeping histories isolated between repositories. Telegram topic
  recovery uses a separate file beside it, replacing the filename suffix with `.topics.json`
  (`sessions.topics.json` by default).
- `CODEX_TELEGRAM_CODEX_TIMEOUT_SECONDS` - Codex turn timeout; unset or `0` means no timeout.
- `CODEX_TELEGRAM_ATTACHMENT_RETENTION_SECONDS` - minimum time downloaded Telegram attachments remain
  available after their most recent use; defaults to `86400` (24 hours).
- `CODEX_TELEGRAM_GIT_PULL_TIMEOUT_SECONDS` - timeout for each background `git pull`; defaults to `120`.
- `CODEX_TELEGRAM_GIT_PULL_INTERVAL_SECONDS` - minimum age of the last successful pull before another background pull is started; defaults to `300` (5 minutes).
- `CODEX_TELEGRAM_COMPACTION_IDLE_SECONDS` - idle time before a Codex thread is compacted; defaults to `86400` (24 hours).
- `CODEX_TELEGRAM_ARCHIVE_IDLE_SECONDS` - idle time before a Codex thread is archived and replaced on the next message; defaults to `604800` (7 days).
- `CODEX_TELEGRAM_MAINTENANCE_INTERVAL_SECONDS` - interval between idle-thread compaction scans; defaults to `3600` (1 hour).
- `CODEX_TELEGRAM_MAINTENANCE_TIMEOUT_SECONDS` - timeout for each Codex app-server maintenance operation; defaults to `600`.
- `CODEX_TELEGRAM_GUEST_WARNING_SECONDS` - when to use a still-working warning as the single guest response;
  defaults to `290` (4 minutes 50 seconds).
- `CODEX_TELEGRAM_MEDIA_UPLOAD_CHAT_ID` - private channel or chat used to pre-upload images and documents referenced by
  guest responses, which can only use existing Telegram file IDs. The bot must be able to post there.
- `CODEX_TELEGRAM_STICKER_DIR` - directory containing static `.webp` stickers. Relative paths are resolved
  against `CODEX_TELEGRAM_REPO_DIR`. When the directory exists and contains stickers, all threads get a
  `send_sticker` dynamic tool whose choices are the filenames without `.webp`; restart the bot after changing
  the directory contents.
- `CODEX_TELEGRAM_UPLOAD_DIR` - optional existing directory of files the bot may send through the `send_file`
  dynamic tool. Unset or empty disables the tool. Paths expand `~`; relative paths are resolved against
  `CODEX_TELEGRAM_REPO_DIR`, and absolute paths may point outside the repository. A configured path that is
  missing or is not a directory is a startup error. Only existing non-empty regular files whose resolved paths remain
  inside this directory can be sent, up to 50 MiB per file. Symlinks and `..` paths cannot escape the directory.
- `CODEX_TELEGRAM_POLL_TIMEOUT_SECONDS` - Telegram long-poll timeout; defaults to `30`.
- `CODEX_TELEGRAM_CHAT_ACTION_INTERVAL_SECONDS` - `sendChatAction` interval; defaults to `5`.
- `CODEX_TELEGRAM_MAX_CONCURRENT_CHATS` - size of each worker pool used for chat-event processing and
  waiting on concurrent Codex turns; defaults to `16`.
- `CODEX_TELEGRAM_DELETE_WEBHOOK_ON_START` - remove an existing Telegram webhook before polling; defaults to `false`.
- `CODEX_TELEGRAM_DROP_PENDING_UPDATES` - drop queued updates when deleting the webhook; defaults to `false`.
- `CODEX_TELEGRAM_LOG_LEVEL` - Python logging level; defaults to `INFO`.

At `INFO`, the bot logs Codex lifecycle events, reasoning summaries, intermediate agent messages,
tool-call status, response readiness, and successful Telegram delivery. Logged text is bounded and
potential PAN or credential values are redacted. Raw tool output and hidden model reasoning are not logged.
Incremental agent-message deltas are logged only at `DEBUG`; completed agent messages remain visible at
`INFO`.
The `httpx` and `httpcore` transport loggers are held at `WARNING` so routine request lines do not expose the
Telegram bot token embedded in Bot API URLs, even when the bot's own log level is `DEBUG`.

When a Codex invocation starts, the bot starts `git pull --ff-only --no-tags --recurse-submodules=yes`
asynchronously in the configured repository and in each distinct Git repository found through Codex's effective
`sandbox_workspace_write.writable_roots` setting. Relative writable roots are resolved from the configured
repository. Missing and non-Git roots are skipped; duplicate roots and paths inside an already-pulled repository
are pulled only once. At most one pull batch runs at a time, and a new batch is skipped when the last successful
one completed less than five minutes ago. Each pull updates the checked-out branch and its populated submodules.
Codex does not wait for the pull, so on a cache miss it may briefly read a checkout while Git is updating it.
Pull failures are logged only; Codex continues processing the request.

## Run

Start the shared Codex app-server before the bot. For a daemon-managed server, this can be done with
`codex app-server daemon start` or `codex remote-control start`. The bot connects to its Unix control
socket and does not start its own app-server or MCP processes.

```bash
export TELEGRAM_BOT_TOKEN='123456:replace-me'
export TELEGRAM_ALLOWED_CHAT_IDS='-1001234567890,-1009876543210'
python tools/codex_telegram_bot/bot.py
```

For backward compatibility, `TELEGRAM_ALLOWED_CHAT_ID` is still accepted when
`TELEGRAM_ALLOWED_CHAT_IDS` is unset.

The bot accepts messages in private chats, groups, and supergroups when either the chat ID or
the sending user's `from.id` is in the allow-list. It ignores channels and messages for which
neither identity is allowed. Forwarded, replied-to, and anonymous `sender_chat` identities do not
grant access.
For each accepted text message, caption, image, or other file attachment, it keeps sending Telegram's `typing` chat action while
the Codex turn is running.
Ordinary turns additionally send a single `choose_sticker` action when the `send_sticker` dynamic tool is
called, a single `upload_document` action when an accepted `send_file` upload runs, and a single
`upload_photo` action when an app-server image-generation item starts. These one-shot
actions are best-effort and use the originating Telegram topic. Guest queries do not send chat actions.
Telegram long polling and Bot API requests use separate asynchronous HTTP clients. Updates are routed to an
ordered controller for their `(chat_id, message_thread_id)`: different chats and forum topics can run
concurrently, while messages in the same conversation retain Telegram update order. If a conversation already
has an active Codex turn, a subsequent message is appended with
app-server `turn/steer` instead of waiting for the turn to finish. If the turn completes at the same moment and
the app-server rejects the steer, the message starts a new turn after completion and is not dropped.
Telegram requests and attachment downloads are retried up to three times for rate limits, timeouts, and other
transient network failures. A Telegram `RetryAfter` response supplies the exact delay; other transient failures
wait half a second before each retry. Invalid requests and other semantic API failures are not retried. The
optional `sendChatAction` notifications remain best-effort and are never retried.
The bot handles `/compact` (and `/compact@botname` in groups) locally to compact the current Codex
thread without starting a model turn. In a forum, the command applies only to its topic. `/archive` archives
the current Codex thread and removes its
Telegram mapping, reports the archived Codex thread ID for possible manual recovery, and makes the next
ordinary or guest message start a new thread. Both commands are
idempotent when the chat does not yet have an active thread.
Ordinary messages receive Codex progress updates and the final answer as separate messages. Only the first
delivered message replies to the original query; subsequent messages remain in the same chat or topic without
reply metadata. If additional ordinary messages steer the same active turn, that reply anchor remains the first
message that started the turn; it is not moved to the latest steer. The completed final answer is retained as a
fallback and is not sent twice when the matching agent message was already delivered.
For ordinary messages, the session store also keeps the Telegram message ID of the latest successfully
sent Codex response. If the next user message replies to that response while the same Codex thread is still
mapped, the bot omits the redundant `reply_to_message` block from the model prompt. A newly created thread
still receives the reply block, including when the old thread was archived or deleted between interactions.
The long poll also requests Telegram's `message_reaction` updates. When a user adds a supported reaction to
the recorded latest bot response, the bot feeds the added emoji into the same ordinary-message turn/steering
path with `message_kind` set to `message_reaction`. Reaction removals, aggregate count updates, and reactions
to untracked messages are ignored. Telegram delivers these updates only where the bot has the required chat
administrator access. Telegram does not include the reacted-to message body or forum topic in
the update; the bot does not cache message bodies, so it uses the existing latest-response mapping to recover
the Codex conversation and topic and does not add a quoted body. Reaction prompts omit `current_message`
because the reaction event is not itself a Telegram message.
The first accepted message from a chat or forum topic creates a Codex session. Later messages resume that
conversation's stored session, including after the bot restarts. Non-threaded chats retain their original
chat-ID-based session keys. Existing chat-wide mappings are preserved during upgrade but are not assigned to
an arbitrary forum topic; each topic creates its own history when it is first used.
Before selecting a conversation, the bot resolves the Telegram topic from the current message, a same-chat
reply's topic or creation header, or previously observed message IDs. When those fields are missing, a reply
to the current bot can also be matched against a previously delivered response using a fingerprint of its
complete body with presentation formatting normalized. Guest and ordinary histories are separate, and matches
are scoped to the Telegram chat. Responses seen in multiple topics, or with an unknown topic, are ambiguous
and do not select a topic. Explicit non-topic messages do not acquire a topic through inference.
The topic-history file stores message IDs, topic IDs, and response fingerprints rather than message bodies.
It survives restarts and Codex thread archival. Recovering a topic happens before session routing, attachment
handling, and prompt construction; the resolved ID is included as top-level `message_thread_id` in
`<telegram_context>`. Without enough evidence, the message retains the existing chat-wide routing behavior.

Topic recovery learns from live Telegram messages and successful bot deliveries. An older reply without
enough evidence can remain chat-wide until a later message supplies the topic ID or its creation header.
Copy the topic-history file together with the session store when moving the bot's runtime data.
New Codex threads receive a user-facing name immediately after creation. Group and supergroup threads use
the Telegram chat title, followed by the numeric topic ID for forum topics. Direct user-to-bot threads use
`@username` when available or the Telegram
`tg://user?id=...` identity link otherwise. Guest private-chat threads use the identities of both
participants separated by `/`, including guest calls made from a user-to-user or user-to-another-bot chat.
The bot owns one persistent WebSocket connection over the configured Unix socket. The shared server owns
the model runtime, threads, and MCP processes; the bot does not start a Codex child process. The bot sends
its repository directory, custom base instructions, and thread-specific configuration on every
`thread/start` and `thread/resume`, so several bots using different repositories can share one server
without inheriting another bot's working directory.
Ordinary Telegram threads also register the experimental `react_to_telegram_message` and
`clear_telegram_reaction` dynamic tools. Both require a Telegram `message_id`; setting a reaction additionally
requires a reaction selected from Telegram's supported emoji enum. The bot resolves the target `chat_id` from
the app-server `threadId`, so the model cannot select another chat. If `CODEX_TELEGRAM_STICKER_DIR` contains
`.webp` files, ordinary threads additionally register `send_sticker` with the available sticker basenames as
an enum and an optional `message_thread_id` for explicit forum-topic targeting. Without that argument, stickers
use the current topic. Guest threads register `react_to_telegram_message`, representing its emoji as a separate
commentary message because native reactions are unavailable there. The emoji then follows the existing guest
commentary delivery rules. Guest `send_sticker` calls are queued until the final response. A sole queued sticker
with no final text is returned as a sticker; otherwise queued stickers are embedded as photos in the rich
response. With one steer, a sticker queued before the steer may consume the still-unanswered
original guest query while the textual result answers the steering message. Telegram photo and sticker file
IDs are cached separately in memory by source/content, so repeated guest images and repeated stickers avoid
another temporary upload for the lifetime of the bot process.
When `CODEX_TELEGRAM_UPLOAD_DIR` is configured, ordinary and guest threads additionally register `send_file`
with a required `path` and optional `caption`. Relative file paths use the upload directory. The bot chooses
the destination chat from the Codex thread and keeps ordinary documents in the originating forum topic.
Captions are limited to 1024 characters. Guest files are queued for the query's single rich response together
with its text, images, and stickers, and require `CODEX_TELEGRAM_MEDIA_UPLOAD_CHAT_ID` for pre-uploading
reusable Telegram document IDs. Steering keeps files queued until the newest guest query's final response.
If a deadline warning has already consumed that response, queued files are drained without uploading.
Dynamic tool calls are acknowledged successfully before their Telegram actions run. This lets the local bot
win the app-server response race against other subscribed clients; execution then continues in the request's
background thread. A later action failure is logged but cannot change the already-returned tool result.
File-upload calls validate their arguments, paths, and conversation mapping before acknowledgement, so an
invalid request returns a failed tool result without uploading. Guest files are queued before acknowledgement
so an immediately completed turn can include them in its response.
Successful file-tool results confirm acceptance; actual Telegram delivery follows asynchronously.
On startup the bot resumes stored non-archived group and supergroup threads (negative Telegram chat IDs)
and keeps those subscriptions for the lifetime of the connection. Private-chat threads are resumed on
demand and unsubscribed after each turn or scheduled maintenance operation. Their stored session mapping
remains available for the next message;
app-server may unload them after its 30-minute no-subscriber, no-activity grace period. Archived
threads are removed from the mapping and unsubscribed.
If another Codex client archives or deletes a mapped thread, the app-server notification removes the stale
mapping and the next Telegram message creates a fresh thread. If that notification was missed while the bot
was disconnected, a specific archived-thread or missing-rollout error during resume or turn startup triggers
the same cleanup and one retry with a new thread. Other app-server errors are not retried.
The session store also records the last message, compaction time, and latest ordinary bot-response ID. On startup, threads idle for at
least seven days are archived and removed from the mapping, so their next message creates a fresh thread.
Threads idle for at least 24 hours are compacted once per idle period through Codex's app-server API.
Compaction is checked on startup and every hour only while the bot is waiting for Telegram updates and no
conversation controller has queued work or an active turn. A scan stops scheduling additional compactions when
an update arrives, and message handling never starts compaction.
Compaction waits for the app-server's final `turn/completed` event, not only completion of its
`contextCompaction` item. The pre-message check only archives a thread that crossed the seven-day limit while
the bot remained running. Maintenance failures are logged and do not prevent the bot from processing the Telegram request. Version-1 session stores are
migrated with the migration time as their initial activity timestamp, avoiding accidental immediate archival.
Version-2 stores are upgraded with no recorded bot-response ID.
Images sent as Telegram photos or image documents are downloaded and passed as app-server `localImage` inputs.
All non-image documents and media types (including PDF, audio, video, animation, voice, video notes, and
stickers) are streamed to mode-`0600` files in a runtime cache under the configured repository, then passed
through the structured prompt's `attachments` array as metadata and local paths. Their contents are not
embedded in the prompt. Image metadata is omitted from Telegram context because the images are already
provided as native image inputs.
Media embedded in RichMessage blocks is handled the same way, including media nested in collages,
slideshows, lists, quotations, and details blocks. Duplicate top-level and RichMessage file references are
downloaded only once. Within each Telegram thread, retained files are keyed by a hash of their Telegram
`file_id`, so sending or replying to the same file again reuses the existing download.
If a response references a local image that has already been removed, the missing image is replaced with an
`[Image unavailable]` text placeholder instead of failing delivery of the entire response. Available image alt
text is included in the placeholder.
On startup, cache entries older than the minimum retention period are removed and younger entries survive the
restart. Within a running process, files become eligible for cleanup after both the minimum retention period
and five consecutive accepted messages in the same chat topic have no attachments. Cleanup occurs after Codex
finishes the qualifying follow-up, so the paths remain usable during that interaction. Reusing a cached
attachment restarts its minimum retention period.
When the accepted message replies to another message with an image, that replied-to image is attached as context too.
When the accepted message replies to another message with a file, that replied-to file path is included as context too.
Guest messages use the same chat allow-list and reply with one rich `InlineQueryResultArticle`.
Telegram's guest-query result exposes an inline message ID rather than the numeric message ID used by
`reply_to_message`, so this reply-context deduplication applies only to ordinary bot messages.
Codex receives a structured Telegram context block with the asking user's name and any replied-to message.
The `<telegram_context>` block and the Markdown user message are sent as separate text input items in the
same app-server turn; the user message does not use an additional wrapper tag. The replied-to body is
converted to Markdown and inserted as a block quote at the beginning of the user message. Rich-message
bodies use the local rich converter; ordinary text and captions use the local Telegram entity converter,
which interprets Bot API UTF-16 ranges and emits model-readable Markdown. It prefers CommonMark/GFM and
uses concise Telegram MarkdownV2 constructs for features such as underline, spoilers, and date-time
entities. Converted rich bodies and the redundant
`quoted_original_message` field are removed from the structured reply metadata. Converted `text`/`entities`
and `caption`/`caption_entities` fields are removed as well; an empty `raw_payload` is omitted. The current
user message is converted by the same format-specific path but is not quoted.
Replies to service messages and other messages without a text, caption, or rich body retain their structured
metadata once; the bot does not additionally turn that metadata into a synthetic JSON quote.

### Telegram Rich Message conversion

`telegram_rich_to_markdown.py` provides a dependency-free converter for Telegram `RichMessage` objects:

```python
from telegram_rich_to_markdown import rich_message_to_markdown

markdown = rich_message_to_markdown(message["rich_message"])
```

It covers all currently documented received rich-text and rich-block variants. CommonMark/GFM is used where
possible; constructs without a native Markdown representation use small HTML fragments or readable textual
fallbacks.

### Telegram MessageEntity conversion

`telegram_entities_to_markdown.py` converts Bot API `MessageEntity` arrays into readable Markdown while
preserving Telegram's UTF-16 offset semantics, nested formatting, links, quotes, and code blocks:

```python
from telegram_entities_to_markdown import entities_to_markdown

markdown = entities_to_markdown(message["text"], message.get("entities"))
```

It covers every currently documented Bot API `MessageEntity` type. Reply-context and current-message prompt
formatting both use this helper. If local conversion fails, the bot retries with
`telegramify_markdown.entities_to_markdownv2`; plain text is used only if that fallback also fails.

## Troubleshooting Updates

Run with debug logging when checking delivery:

```bash
export CODEX_TELEGRAM_LOG_LEVEL=DEBUG
python tools/codex_telegram_bot/bot.py
```

On startup, the bot calls `getMe` and `getWebhookInfo`.
If the log says a webhook is set, long polling will not receive updates.
Either remove it manually or run this polling bot with:

```bash
export CODEX_TELEGRAM_DELETE_WEBHOOK_ON_START=true
```

If polling works but no ordinary group messages arrive, check BotFather privacy mode.
With privacy enabled, Telegram does not deliver all group messages to the bot.
Disable privacy for the bot when you want every group message in an allowed chat to reach this process.

If updates arrive but are ignored, the logs include the actual chat ID and chat type.
Add that value to `TELEGRAM_ALLOWED_CHAT_IDS`.

## Guest Mode

Enable Guest Mode in BotFather before expecting `guest_message` updates.
When an allowed user mentions the bot as a guest, or the mention occurs in an allowed group, the bot:

- reads the prompt from the `guest_message` text or caption;
- includes the asking user and replied-to message in the prompt sent to Codex;
- creates or resumes the Codex session assigned to that guest chat, independently of any ordinary bot chat
  with the same numeric ID; both participants in the same guest chat share that guest session, and guest
  group chats use the same separate guest namespace even though their Telegram IDs are negative;
- sends the response through `answerGuestQuery`.

Guest replies are limited to one Telegram guest response.
Guest Codex turns temporarily request the Fast service tier to help meet Telegram's response deadline.
Completed commentary is retained while a guest turn runs. When another guest message steers the same active
turn, any commentary accumulated so far is concatenated and used as the previous unanswered guest query's one
response, provided that query is still accepted by Telegram. The new guest query becomes the target for the
eventual final answer and receives a fresh deadline timer. If no commentary is available, the previous query is
left unanswered and no synthetic follow-up acknowledgement is sent.
If rich-message delivery fails, the bot makes one plain-text attempt containing only the delivery exception; it
does not resend the full answer as plain text.
If a turn is still running 4 minutes 50 seconds after the Telegram message timestamp, the bot uses the one
available response to ask the user to ping again shortly, while Codex continues working in the same thread.
A delivery failure, or the fact that the deadline warning consumed the response, is persisted and included once as
`previous_response_error` in the next message's `<telegram_context>`.
