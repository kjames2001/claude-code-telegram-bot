#!/usr/bin/env python3
"""
Telegram bot bridging to Claude Code CLI.
- Single allowed user (locked by Telegram user ID)
- Per-chat session continuity via claude --resume
- No Claude API key needed — uses the claude CLI auth
"""

import asyncio
import json
import logging
import os
import re
import signal
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
SESSIONS_DIR = Path("/root/telegram-claude-bot/sessions")
CLAUDE_BIN = "/root/.local/bin/claude"
CLAUDE_TIMEOUT = 900  # seconds (15 minutes)
MEMORY_DIR = Path("/root/.claude/projects/-root/memory")

SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Emoji shown while Claude is working — keyed by the tool Claude is currently calling.
# None = Claude is generating text (no active tool call).
TOOL_EMOJIS: dict[str | None, str] = {
    None:          "🤔",  # thinking / writing
    "Read":        "📖",  # reading a file
    "Write":       "💾",  # writing a file
    "Edit":        "✏️",  # editing a file
    "Bash":        "⚡",  # running a shell command
    "Grep":        "🔍",  # searching file contents
    "Glob":        "📂",  # finding files
    "Agent":       "🤖",  # spawning a sub-agent
    "WebFetch":    "🌐",  # fetching a URL
    "WebSearch":   "🔎",  # web search
    "Task":        "📋",  # task management
    "TaskCreate":  "📋",
    "TaskUpdate":  "📋",
    "TaskGet":     "📋",
    "TaskList":    "📋",
    "TodoWrite":   "📝",
    "TodoRead":    "📝",
    "NotebookEdit":"📓",
}
DEFAULT_TOOL_EMOJI = "⚙️"   # fallback for unknown tools
STATUS_INTERVAL = 8          # seconds between status nudges

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Active process tracking (for /cancel) ─────────────────────────────────────
# chat_id -> asyncio.subprocess.Process
active_procs: dict[int, asyncio.subprocess.Process] = {}
# chat_id -> bool (is Claude currently running for this chat)
busy_chats: set[int] = set()
# chat_id -> asyncio.Queue of (text, update) tuples
chat_queues: dict[int, asyncio.Queue] = {}
# chat_id -> asyncio.Task (per-chat worker draining the queue)
chat_workers: dict[int, asyncio.Task] = {}

# ── Memory loader ──────────────────────────────────────────────────────────────

def load_memory() -> str:
    """Read all memory files and return combined context string."""
    if not MEMORY_DIR.exists():
        return ""
    parts = []
    for md_file in sorted(MEMORY_DIR.glob("*.md")):
        content = md_file.read_text().strip()
        if content:
            parts.append(f"## [{md_file.name}]\n{content}")
    if not parts:
        return ""
    return "# Persistent Memory (auto-loaded)\n\n" + "\n\n".join(parts)

# ── Session helpers ────────────────────────────────────────────────────────────

def session_file(chat_id: int) -> Path:
    return SESSIONS_DIR / f"{chat_id}.json"


def load_session_id(chat_id: int) -> str | None:
    f = session_file(chat_id)
    if f.exists():
        return json.loads(f.read_text()).get("session_id")
    return None


def save_session_id(chat_id: int, session_id: str):
    session_file(chat_id).write_text(json.dumps({"session_id": session_id}))


def clear_session(chat_id: int):
    f = session_file(chat_id)
    if f.exists():
        f.unlink()

# ── Claude runner ──────────────────────────────────────────────────────────────

class CancelledError(Exception):
    pass


async def run_claude(
    message: str,
    session_id: str | None,
    chat_id: int,
    current_tool: list,   # 1-element list used as a mutable shared reference
) -> tuple[str, str]:
    """
    Run claude CLI asynchronously and return (response_text, new_session_id).
    Streams JSON events so current_tool[0] is updated in real-time with the
    name of the tool Claude is currently calling (None = generating text).
    Raises on failure. Registers process in active_procs for cancellation.
    """
    cmd = [CLAUDE_BIN, "-p", message, "--output-format", "stream-json", "--verbose"]
    if session_id:
        cmd += ["--resume", session_id]

    memory = load_memory()
    if memory:
        cmd += ["--append-system-prompt", memory]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/root",
        start_new_session=True,
    )

    active_procs[chat_id] = proc

    result_text: str | None = None
    new_session_id: str | None = None
    is_error = False
    stderr_buf: list[bytes] = []

    async def read_stdout():
        nonlocal result_text, new_session_id, is_error
        # Read in raw chunks and split on newlines manually to avoid asyncio's
        # default 64 KB per-line StreamReader limit, which raises:
        # "Separator is not found, and chunk exceed the limit"
        # when Claude emits a large JSON event in a single line.
        buf = b""
        while True:
            chunk = await proc.stdout.read(131072)  # 128 KB at a time
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    etype = event.get("type")
                    if etype == "assistant":
                        content = event.get("message", {}).get("content", [])
                        tool_name = next(
                            (b.get("name") for b in content if b.get("type") == "tool_use"),
                            None,
                        )
                        current_tool[0] = tool_name  # None → generating text
                    elif etype == "result":
                        result_text = event.get("result")
                        new_session_id = event.get("session_id")
                        is_error = event.get("is_error", False)
                        current_tool[0] = "done"
                except json.JSONDecodeError:
                    pass

    async def drain_stderr():
        data = await proc.stderr.read()
        stderr_buf.append(data)

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), drain_stderr(), proc.wait()),
            timeout=CLAUDE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        raise TimeoutError("Claude timed out")
    finally:
        active_procs.pop(chat_id, None)

    if proc.returncode in (-signal.SIGKILL, -9):
        raise CancelledError("Cancelled.")

    # Exit code 143 = 128+15 = SIGTERM (Node.js/Claude CLI convention).
    # If we already received the result event before the process was killed,
    # the response is complete — don't treat it as an error.
    if proc.returncode != 0 and result_text is None:
        err = (stderr_buf[0] if stderr_buf else b"").decode().strip()
        raise RuntimeError(f"claude exited {proc.returncode}: {err or '(no stderr)'}")
    elif proc.returncode not in (0, None) and result_text is not None:
        log.warning("claude exited %s but result was already received — treating as success", proc.returncode)

    if is_error:
        raise RuntimeError(result_text or "Unknown error from claude")

    if result_text is None:
        raise RuntimeError("No result received from claude")

    return result_text, new_session_id

# ── Status updater ─────────────────────────────────────────────────────────────

TOOL_LABELS: dict[str | None, str] = {
    None:          "thinking",
    "Read":        "reading file",
    "Write":       "writing file",
    "Edit":        "editing file",
    "Bash":        "running command",
    "Grep":        "searching",
    "Glob":        "finding files",
    "Agent":       "spawning agent",
    "WebFetch":    "fetching URL",
    "WebSearch":   "searching web",
    "Task":        "managing tasks",
    "TaskCreate":  "managing tasks",
    "TaskUpdate":  "managing tasks",
    "TaskGet":     "managing tasks",
    "TaskList":    "managing tasks",
    "TodoWrite":   "updating todos",
    "TodoRead":    "reading todos",
    "NotebookEdit":"editing notebook",
}

async def status_updater(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    done: asyncio.Event,
    current_tool: list,
    status_msg_id: list,   # 1-element list; populated here once message is sent
):
    """
    Send one status message, then edit it in-place as Claude's active tool changes.
    Also refreshes the 'typing' chat action every 4s so the indicator stays visible.
    """
    EDIT_INTERVAL = 3     # seconds between edits
    TYPING_INTERVAL = 4   # seconds between typing action refreshes

    await asyncio.sleep(1)   # brief pause so the status msg appears after the user's msg

    # Send the initial status message and record its id
    tool = current_tool[0]
    emoji = TOOL_EMOJIS.get(tool, DEFAULT_TOOL_EMOJI)
    label = TOOL_LABELS.get(tool, "working")
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=f"{emoji} {label}…")
        status_msg_id[0] = msg.message_id
    except Exception:
        return

    last_text = f"{emoji} {label}…"
    last_edit = asyncio.get_event_loop().time()
    last_typing = last_edit

    while not done.is_set():
        await asyncio.sleep(0.5)
        now = asyncio.get_event_loop().time()

        tool = current_tool[0]
        if tool == "done":
            break

        # Refresh typing indicator every 4s
        if now - last_typing >= TYPING_INTERVAL:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            last_typing = now

        # Edit status message every 3s if content changed or interval elapsed
        emoji = TOOL_EMOJIS.get(tool, DEFAULT_TOOL_EMOJI)
        label = TOOL_LABELS.get(tool, "working")
        new_text = f"{emoji} {label}…"
        if new_text != last_text and now - last_edit >= EDIT_INTERVAL:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id[0],
                    text=new_text,
                )
                last_text = new_text
            except Exception:
                pass
            last_edit = now

# ── Handlers ───────────────────────────────────────────────────────────────────

def auth(update: Update) -> bool:
    uid = update.effective_user.id
    if uid != ALLOWED_USER_ID:
        log.warning("Rejected message from user %s", uid)
        return False
    return True


HELP_TEXT = (
    "Claude Code bridge — available commands:\n\n"
    "/cancel — stop the current running command\n"
    "/reset — start a fresh conversation (clears session)\n"
    "/status — show whether Claude is currently busy\n"
    "/id — show your Telegram user & chat IDs\n"
    "/help — show this message"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    await update.message.reply_text(
        "Claude Code bridge active.\n"
        "Chat away — your conversation thread persists across messages.\n\n"
        + HELP_TEXT
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    await update.message.reply_text(HELP_TEXT)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    chat_id = update.effective_chat.id
    if chat_id in busy_chats:
        await update.message.reply_text("Claude is busy. Use /cancel first.")
        return
    queue = chat_queues.get(chat_id)
    if queue:
        while not queue.empty():
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break
    clear_session(chat_id)
    await update.message.reply_text("Conversation reset. Starting fresh.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    chat_id = update.effective_chat.id

    queue = chat_queues.get(chat_id)
    cleared = 0
    if queue:
        while not queue.empty():
            try:
                queue.get_nowait()
                queue.task_done()
                cleared += 1
            except asyncio.QueueEmpty:
                break

    proc = active_procs.get(chat_id)
    if proc is None and cleared == 0:
        await update.message.reply_text("Nothing is running.")
        return

    if proc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    parts = []
    if proc:
        parts.append("current request cancelled")
    if cleared:
        parts.append(f"{cleared} queued message{'s' if cleared > 1 else ''} cleared")
    await update.message.reply_text(", ".join(parts).capitalize() + ".")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    chat_id = update.effective_chat.id
    queue = chat_queues.get(chat_id)
    queued = queue.qsize() if queue else 0
    if chat_id in busy_chats:
        msg = "Busy — Claude is currently processing a request."
        if queued:
            msg += f" {queued} more message{'s' if queued > 1 else ''} queued."
    else:
        session_id = load_session_id(chat_id)
        session_info = f"Active session: `{session_id[:8]}…`" if session_id else "No active session."
        msg = f"Idle. {session_info}"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    await update.message.reply_text(
        f"User ID: `{update.effective_user.id}`\n"
        f"Chat ID: `{update.effective_chat.id}`",
        parse_mode="Markdown",
    )


async def _run_with_updates(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    session_id: str | None,
) -> tuple[str, str]:
    """Run claude with a single editable status message reflecting the active tool."""
    chat_id = update.effective_chat.id
    done = asyncio.Event()
    current_tool: list = [None]   # shared mutable state: None = thinking
    status_msg_id: list = [None]  # filled by status_updater once message is sent
    updater = asyncio.create_task(
        status_updater(context, chat_id, done, current_tool, status_msg_id)
    )
    try:
        result = await run_claude(text, session_id, chat_id, current_tool)
    finally:
        done.set()
        updater.cancel()
        try:
            await updater
        except asyncio.CancelledError:
            pass
        # Delete the status message so the chat stays clean
        if status_msg_id[0] is not None:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=status_msg_id[0])
            except Exception:
                pass
    return result


async def _process_one(chat_id: int, text: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process a single message. Called sequentially by chat_worker."""
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    session_id = load_session_id(chat_id)
    log.info("chat=%s session=%s msg=%r", chat_id, session_id, text[:60])
    busy_chats.add(chat_id)
    try:
        response, new_session_id = await _run_with_updates(update, context, text, session_id)
        save_session_id(chat_id, new_session_id)
        for chunk in _split(response, 4096):
            await update.message.reply_text(chunk)
        images = extract_images(response)
        if images:
            await send_images(chat_id, images, update, context)
    except CancelledError:
        await update.message.reply_text("Cancelled.")
    except TimeoutError:
        await update.message.reply_text("⏱️ Timed out waiting for Claude. Try again.")
    except RuntimeError as e:
        if "No conversation found with session ID" in str(e) and session_id:
            log.warning("Stale session %s, retrying fresh", session_id)
            clear_session(chat_id)
            try:
                response, new_session_id = await _run_with_updates(update, context, text, None)
                save_session_id(chat_id, new_session_id)
                for chunk in _split(response, 4096):
                    await update.message.reply_text(chunk)
                images = extract_images(response)
                if images:
                    await send_images(chat_id, images, update, context)
            except CancelledError:
                await update.message.reply_text("Cancelled.")
            except Exception as e2:
                log.error("Error on retry: %s", e2)
                await update.message.reply_text(f"Error: {e2}")
        else:
            log.error("Error: %s", e)
            await update.message.reply_text(f"Error: {e}")
    except Exception as e:
        log.error("Error: %s", e)
        await update.message.reply_text(f"Error: {e}")
    finally:
        busy_chats.discard(chat_id)


async def _chat_worker(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Drain the per-chat queue, processing one message at a time."""
    queue = chat_queues[chat_id]
    try:
        while True:
            try:
                text, update = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                break  # Queue idle — exit worker
            try:
                await _process_one(chat_id, text, update, context)
            finally:
                queue.task_done()
    finally:
        chat_workers.pop(chat_id, None)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if not text:
        return

    if chat_id not in chat_queues:
        chat_queues[chat_id] = asyncio.Queue()
    queue = chat_queues[chat_id]

    pending = queue.qsize() + (1 if chat_id in busy_chats else 0)
    await queue.put((text, update))

    if pending > 0:
        ahead = pending
        msg = f"Queued — {ahead} request{'s' if ahead > 1 else ''} ahead."
        await update.message.reply_text(msg)

    worker = chat_workers.get(chat_id)
    if worker is None or worker.done():
        chat_workers[chat_id] = asyncio.create_task(_chat_worker(chat_id, context))


def _split(text: str, size: int) -> list[str]:
    return [text[i:i+size] for i in range(0, len(text), size)] if text else ["(empty response)"]


IMAGE_EXTS = re.compile(r'(/(?:tmp|root|home|var|srv)[^\s\'"`,;)>]*\.(?:png|jpg|jpeg|gif|webp))', re.IGNORECASE)

def extract_images(text: str) -> list[Path]:
    """Find image file paths in text that actually exist on disk."""
    seen = set()
    images = []
    for match in IMAGE_EXTS.finditer(text):
        p = Path(match.group(1))
        if p not in seen and p.exists():
            seen.add(p)
            images.append(p)
    return images


async def send_images(chat_id: int, images: list[Path], update: Update, context: ContextTypes.DEFAULT_TYPE):
    for img in images:
        try:
            with open(img, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f, caption=img.name)
        except Exception as e:
            log.warning("Failed to send image %s: %s", img, e)

# ── Claude Code slash command passthrough ──────────────────────────────────────

# These commands are forwarded to Claude as-is (e.g. /compact → sends "/compact" to Claude).
CLAUDE_CODE_COMMANDS: dict[str, str] = {
    "compact":  "Compact conversation to save context",
    "clear":    "Clear conversation history",
    "cost":     "Show token usage and cost",
    "memory":   "Show and manage memory",
    "model":    "Show or switch the active model",
    "doctor":   "Check Claude Code installation health",
    "review":   "Review recent code changes",
    "init":     "Initialize Claude Code in current directory",
    "bug":      "Report a bug to Anthropic",
}


async def cmd_claude_passthrough(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward a Claude Code slash command as a plain message to Claude."""
    if not auth(update):
        return
    cmd = update.message.text.split()[0].lstrip("/").split("@")[0]
    args = " ".join(context.args) if context.args else ""
    text = f"/{cmd}" + (f" {args}" if args else "")
    # Reuse the normal message pipeline
    update.message.text = text
    await handle_message(update, context)


async def post_init(app) -> None:
    """Register all commands in the Telegram menu button."""
    bot_commands = [
        # Bot-native commands
        BotCommand("help",    "Show available commands"),
        BotCommand("reset",   "Start a fresh conversation"),
        BotCommand("cancel",  "Cancel the running request"),
        BotCommand("status",  "Show whether Claude is busy"),
        BotCommand("id",      "Show your Telegram user & chat IDs"),
        # Claude Code commands (passed through to Claude)
        BotCommand("compact", "Compact conversation to save context"),
        BotCommand("clear",   "Clear conversation history"),
        BotCommand("cost",    "Show token usage and cost"),
        BotCommand("memory",  "Show and manage memory"),
        BotCommand("model",   "Show or switch the active model"),
        BotCommand("doctor",  "Check Claude Code installation health"),
        BotCommand("review",  "Review recent code changes"),
        BotCommand("init",    "Initialize Claude Code in current directory"),
        BotCommand("bug",     "Report a bug to Anthropic"),
    ]
    await app.bot.set_my_commands(bot_commands)
    log.info("Registered %d commands in Telegram menu", len(bot_commands))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("id",      cmd_id))
    # Claude Code passthrough commands
    for cmd in CLAUDE_CODE_COMMANDS:
        app.add_handler(CommandHandler(cmd, cmd_claude_passthrough))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot started. Allowed user ID: %s", ALLOWED_USER_ID)
    app.run_polling()


if __name__ == "__main__":
    main()
