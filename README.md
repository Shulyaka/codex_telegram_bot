# Codex Telegram Bot

Forwards Telegram messages and attachments to Codex and sends back its replies.
Each chat or forum topic keeps its own persistent conversation history.

100% vibe codedd.

Requires Python 3.10+ and an installed, authenticated Codex CLI. Run from this directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.sample .env
```

Edit `.env`: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_IDS`
(comma-separated chat or user IDs). Then start the shared Codex server and the bot:

```bash
set -a
. ./.env
set +a
codex remote-control start
python bot.py
```

Codex works in the current directory by default; set `CODEX_TELEGRAM_REPO_DIR`
to use another repository. Set `CODEX_TELEGRAM_READ_ONLY=false` to allow edits.
See [.env.sample](.env.sample) for other settings.

## systemd

Adjust the paths in the two `.service` files for your installation, then install
them as user units:

```bash
mkdir -p ~/.config/systemd/user
cp codex-remote-control.service codex_telegram_bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-remote-control.service codex_telegram_bot.service
loginctl enable-linger "$USER"
```

Lingering starts your user services at boot and keeps them running after logout.
