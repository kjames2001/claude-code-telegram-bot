# Claude Code Telegram Bot

A Telegram bot that bridges to the [Claude Code](https://github.com/anthropics/claude-code) CLI, letting you use Claude Code from your phone via Telegram — including running shell commands, editing files, managing servers, and generating images.

## Features

- **Full Claude Code access** over Telegram — same power as the CLI
- **Session continuity** — conversation persists across messages via `--resume`
- **Live status updates** — emoji indicator shows what Claude is currently doing (reading files, running commands, searching, etc.)
- **Image delivery** — generated images are automatically sent as photos in the chat
- **Command menu** — tap the `/` button to browse all available commands
- **Claude Code slash commands** — `/compact`, `/clear`, `/cost`, `/memory`, `/model`, `/doctor`, `/review` all work natively
- **Queue support** — multiple messages are processed in order
- **Cancel anytime** — `/cancel` kills the running request instantly
- **Single-user** — locked to one Telegram user ID for security

## Requirements

- A machine running [Claude Code](https://github.com/anthropics/claude-code) with a valid Anthropic auth session
- Python 3.10+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/kjames2001/claude-code-telegram-bot.git
cd claude-code-telegram-bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
nano .env
```

Fill in:
- `TELEGRAM_TOKEN` — your bot token from BotFather
- `ALLOWED_USER_ID` — your Telegram user ID (send `/id` to the bot after starting, or use [@userinfobot](https://t.me/userinfobot))

### 4. Run

```bash
python3 bot.py
```

### 5. (Optional) Run as a systemd service

```bash
cp claude-code-telegram-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now claude-code-telegram-bot
```

## Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/reset` | Start a fresh conversation |
| `/cancel` | Cancel the running request |
| `/status` | Show whether Claude is busy |
| `/id` | Show your Telegram user & chat IDs |
| `/compact` | Compact conversation to save context |
| `/clear` | Clear conversation history |
| `/cost` | Show token usage and cost |
| `/memory` | Show and manage memory |
| `/model` | Show or switch the active model |
| `/doctor` | Check Claude Code installation health |
| `/review` | Review recent code changes |
| `/init` | Initialize Claude Code in a directory |
| `/bug` | Report a bug to Anthropic |

## Status Emojis

While Claude is working, a status message shows what it's currently doing:

| Emoji | Meaning |
|-------|---------|
| 🤔 | Thinking / generating text |
| 📖 | Reading a file |
| ✏️ | Editing a file |
| 💾 | Writing a file |
| ⚡ | Running a shell command |
| 🔍 | Searching file contents |
| 📂 | Finding files |
| 🌐 | Fetching a URL |
| 🔎 | Web search |
| 🤖 | Spawning a sub-agent |
| 📝 | Managing tasks |

## Memory System

The bot automatically loads Claude's persistent memory files from `~/.claude/projects/-root/memory/` and injects them as system context on every request. This gives Claude continuity across sessions — it remembers your servers, credentials locations, project context, and preferences.

## License

MIT
