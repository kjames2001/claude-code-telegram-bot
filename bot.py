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
from telegram.error import NetworkError as TelegramNetworkError, TimedOut as TelegramTimedOut
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
SESSIONS_DIR = Path("/root/telegram-claude-bot/sessions")
CLAUDE_BIN = "/root/.local/bin/claude"
CLAUDE_TIMEOUT      = 3600  # hard wall-clock cap (1 hour emergency backstop)
CLAUDE_IDLE_TIMEOUT = 60    # kill if no output for this many seconds (1 minute)
MEMORY_DIR = Path("/root/.claude/projects/-root/memory")

SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR = Path("/tmp/telegram-uploads")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


# Single animated emoji per tool — Telegram renders these as GIF animations
# when the message contains only the emoji character.
TOOL_EMOJI: dict[str | None, str] = {
    None:          "🤔",   # thinking / writing
    "Read":        "👀",   # reading a file
    "Write":       "✍️",   # writing a file
    "Edit":        "✏️",   # editing a file
    "Bash":        "⚡",   # running a shell command
    "Grep":        "🔍",   # searching file contents
    "Glob":        "📂",   # finding files
    "Agent":       "🤖",   # spawning a sub-agent
    "WebFetch":    "🌐",   # fetching a URL
    "WebSearch":   "🔎",   # web search
    "Task":        "📋",   # task management
    "TaskCreate":  "📋",
    "TaskUpdate":  "📋",
    "TaskGet":     "📋",
    "TaskList":    "📋",
    "TodoWrite":   "📝",
    "TodoRead":    "📝",
    "NotebookEdit":"📓",
}
DEFAULT_TOOL_EMOJI = "⚙️"  # fallback for unknown tools

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
# chats where the current run was auto-interrupted by a new message (suppress "Cancelled." reply)
interrupted_chats: set[int] = set()

# ── Memory loader ──────────────────────────────────────────────────────────────

def load_memory() -> str:
    """Read all memory files and return combined context string."""
    if not MEMORY_DIR.exists():
        return ""
    # Skip large/sensitive files that bloat the system prompt unnecessarily.
    SKIP_FILES = {
        "reference_ssh_key.md", "reference_credentials.md",
        "MEMORY.md", "last_session.md",
        "feedback_session_continuity.md", "feedback_session_summary.md",
        "feedback_cleanup.md", "feedback_webfetch.md",
    }
    parts = []
    for md_file in sorted(MEMORY_DIR.glob("*.md")):
        if md_file.name in SKIP_FILES:
            continue
        content = md_file.read_text().strip()
        if content:
            parts.append(f"## [{md_file.name}]\n{content}")
    if not parts:
        return ""
    return "# Persistent Memory (auto-loaded)\n\n" + "\n\n".join(parts)

# ── Network retry helper ───────────────────────────────────────────────────────

async def send_with_retry(coro_fn, max_retries: int = 3, base_delay: float = 2.0):
    """
    Call coro_fn() (a coroutine factory) and retry on transient NetworkErrors.
    Raises on final failure.
    """
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except TelegramNetworkError as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            log.warning("NetworkError on send attempt %d/%d (%s) — retrying in %.0fs", attempt + 1, max_retries, e, delay)
            await asyncio.sleep(delay)


# ── Session helpers ────────────────────────────────────────────────────────────

def session_file(chat_id: int) -> Path:
    return SESSIONS_DIR / f"{chat_id}.json"


def load_session_id(chat_id: int) -> str | None:
    f = session_file(chat_id)
    if f.exists():
        return json.loads(f.read_text()).get("session_id")
    return None


def load_status_msg_id(chat_id: int) -> int | None:
    f = session_file(chat_id)
    if f.exists():
        return json.loads(f.read_text()).get("status_msg_id")
    return None


def save_session_id(chat_id: int, session_id: str):
    f = session_file(chat_id)
    data = json.loads(f.read_text()) if f.exists() else {}
    data["session_id"] = session_id
    data.pop("status_msg_id", None)
    f.write_text(json.dumps(data))


def save_status_msg_id(chat_id: int, msg_id: int):
    f = session_file(chat_id)
    data = json.loads(f.read_text()) if f.exists() else {}
    data["status_msg_id"] = msg_id
    f.write_text(json.dumps(data))


def clear_status_msg_id(chat_id: int):
    f = session_file(chat_id)
    if f.exists():
        data = json.loads(f.read_text())
        data.pop("status_msg_id", None)
        f.write_text(json.dumps(data))


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
    current_tool: list,    # 1-element list: current tool name or None/done
    current_thinking: list, # 1-element list: latest reasoning text from Claude
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
    last_activity: list[float] = [asyncio.get_event_loop().time()]
    idle_timed_out: list[bool] = [False]

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
            last_activity[0] = asyncio.get_event_loop().time()
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
                        # Capture reasoning text (thinking block, or text before a tool call)
                        for block in content:
                            btype = block.get("type")
                            if btype == "thinking":
                                t = block.get("thinking", "").strip()
                                if t:
                                    current_thinking[0] = t
                                break
                            elif btype == "text":
                                t = block.get("text", "").strip()
                                if t:
                                    current_thinking[0] = t
                                break
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

    def _proc_cpu_ticks(pid: int) -> int | None:
        """Return cumulative CPU ticks for pid+children, or None if unreadable."""
        try:
            total = 0
            for p in [pid] + _child_pids(pid):
                with open(f"/proc/{p}/stat") as f:
                    fields = f.read().split()
                total += int(fields[13]) + int(fields[14])  # utime + stime
            return total
        except Exception:
            return None

    def _child_pids(pid: int) -> list[int]:
        """Return immediate child PIDs of pid."""
        children = []
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/stat") as f:
                        fields = f.read().split()
                    if int(fields[3]) == pid:
                        children.append(int(entry))
                except Exception:
                    pass
        except Exception:
            pass
        return children

    async def idle_watchdog():
        """Kill the process only if truly stuck: no stdout AND no CPU activity."""
        prev_cpu: int | None = None
        while True:
            await asyncio.sleep(30)
            if current_tool[0] == "done":
                return
            idle = asyncio.get_event_loop().time() - last_activity[0]
            if idle < CLAUDE_IDLE_TIMEOUT:
                prev_cpu = None
                continue
            # No stdout for CLAUDE_IDLE_TIMEOUT — check CPU to confirm it's not just a slow tool
            curr_cpu = _proc_cpu_ticks(proc.pid)
            if curr_cpu is None or (prev_cpu is not None and curr_cpu == prev_cpu):
                # CPU hasn't moved since last check — genuinely stuck
                idle_timed_out[0] = True
                proc.kill()
                return
            prev_cpu = curr_cpu

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), drain_stderr(), proc.wait(), idle_watchdog()),
            timeout=CLAUDE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        if idle_timed_out[0]:
            raise TimeoutError(f"Claude stopped responding (no output for {CLAUDE_IDLE_TIMEOUT // 60} min)")
        raise TimeoutError("Claude exceeded the maximum time limit")
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
    current_thinking: list,
    status_msg_id: list,   # 1-element list; populated here once message is sent
):
    """
    Send one status message — emoji + label + elapsed time.
    Updates every 10 seconds or when the tool changes, keeping API calls low.
    """
    TYPING_INTERVAL = 5    # seconds between typing action refreshes (indicator lasts 5s)
    EDIT_INTERVAL = 20     # seconds between elapsed-time updates
    # Combined rate: 60/5 + 60/20 = 12+3 = ~15 API calls/min — safely under Telegram's 20/min per-chat limit.

    await asyncio.sleep(1)   # brief pause so the status msg appears after the user's msg

    start_time = asyncio.get_event_loop().time()
    tool = current_tool[0]
    initial_text = f"{TOOL_EMOJI.get(tool, DEFAULT_TOOL_EMOJI)} {TOOL_LABELS.get(tool, 'working')}"
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=initial_text)
        status_msg_id[0] = msg.message_id
        save_status_msg_id(chat_id, msg.message_id)
    except Exception:
        return

    last_tool = tool
    last_thinking = None
    last_text = initial_text
    last_typing = start_time
    last_edit = start_time

    while not done.is_set():
        await asyncio.sleep(1)
        now = asyncio.get_event_loop().time()

        tool = current_tool[0]
        if tool == "done":
            break

        # Refresh typing indicator periodically
        if now - last_typing >= TYPING_INTERVAL:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            last_typing = now

        # Send new reasoning blocks as separate messages (never edit/replace old ones)
        thinking = current_thinking[0]
        if thinking and thinking != last_thinking:
            last_thinking = thinking
            for chunk in _split(thinking[:10000], 4096):
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"💭 {chunk}")
                except Exception:
                    pass

        # Update status badge on tool change or every EDIT_INTERVAL for elapsed time
        tool_changed = tool != last_tool
        time_to_update = now - last_edit >= EDIT_INTERVAL
        if tool_changed or time_to_update:
            elapsed = int(now - start_time)
            elapsed_str = f"{elapsed // 60}m {elapsed % 60}s" if elapsed >= 60 else f"{elapsed}s"
            emoji = TOOL_EMOJI.get(tool, DEFAULT_TOOL_EMOJI)
            label = TOOL_LABELS.get(tool, "working")
            new_text = f"{emoji} {label} — {elapsed_str}"
            if new_text != last_text:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg_id[0],
                        text=new_text,
                    )
                    last_text = new_text
                except Exception:
                    pass
            last_tool = tool
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
        interrupted_chats.add(chat_id)  # suppress "Cancelled." from _process_one

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
    current_tool: list = [None]     # shared mutable state: None = thinking
    current_thinking: list = [None] # latest reasoning text from Claude
    status_msg_id: list = [None]    # filled by status_updater once message is sent
    updater = asyncio.create_task(
        status_updater(context, chat_id, done, current_tool, current_thinking, status_msg_id)
    )
    try:
        result = await run_claude(text, session_id, chat_id, current_tool, current_thinking)
    finally:
        done.set()
        updater.cancel()
        try:
            await updater
        except asyncio.CancelledError:
            pass
        # Delete the status message so the chat stays clean.
        # Also clears the persisted ID so restarts don't try to re-delete it.
        mid = status_msg_id[0]
        if mid is not None:
            clear_status_msg_id(chat_id)
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass
    return result


async def _process_one(chat_id: int, text: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process a single message. Called sequentially by chat_worker."""
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass
    session_id = load_session_id(chat_id)
    log.info("chat=%s session=%s msg=%r", chat_id, session_id, text[:60])
    busy_chats.add(chat_id)
    async def reply(text: str):
        await send_with_retry(lambda: update.message.reply_text(text))

    async def deliver(response: str, new_session_id: str):
        save_session_id(chat_id, new_session_id)
        for chunk in _split(response, 4096):
            await send_with_retry(lambda c=chunk: update.message.reply_text(c))
        images = extract_images(response)
        if images:
            await send_images(chat_id, images, update, context)

    try:
        response, new_session_id = await _run_with_updates(update, context, text, session_id)
        await deliver(response, new_session_id)
    except CancelledError:
        if chat_id not in interrupted_chats:
            try:
                await reply("Cancelled.")
            except Exception:
                pass
        interrupted_chats.discard(chat_id)
    except TimeoutError as e:
        if session_id:
            log.warning("Timeout on session %s, retrying fresh", session_id)
            clear_session(chat_id)
            try:
                response, new_session_id = await _run_with_updates(update, context, text, None)
                await deliver(response, new_session_id)
            except CancelledError:
                if chat_id not in interrupted_chats:
                    try:
                        await reply("Cancelled.")
                    except Exception:
                        pass
                interrupted_chats.discard(chat_id)
            except Exception as e2:
                log.error("Error on fresh retry after timeout: %s", e2)
                try:
                    await reply(f"Error: {e2}")
                except Exception:
                    pass
        else:
            try:
                await reply(f"⏱️ {e}. You can /cancel and try again.")
            except Exception:
                pass
    except RuntimeError as e:
        if "No conversation found with session ID" in str(e) and session_id:
            log.warning("Stale session %s, retrying fresh", session_id)
            clear_session(chat_id)
            try:
                response, new_session_id = await _run_with_updates(update, context, text, None)
                await deliver(response, new_session_id)
            except CancelledError:
                if chat_id not in interrupted_chats:
                    try:
                        await reply("Cancelled.")
                    except Exception:
                        pass
                interrupted_chats.discard(chat_id)
            except Exception as e2:
                log.error("Error on retry: %s", e2)
                try:
                    await reply(f"Error: {e2}")
                except Exception:
                    pass
        else:
            log.error("Error: %s", e)
            try:
                await reply(f"Error: {e}")
            except Exception:
                pass
    except Exception as e:
        log.error("Error: %s", e)
        try:
            await reply(f"Error: {e}")
        except Exception:
            pass
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


async def _enqueue(text: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Push text into the per-chat queue and start a worker if needed."""
    chat_id = update.effective_chat.id
    if chat_id not in chat_queues:
        chat_queues[chat_id] = asyncio.Queue()
    queue = chat_queues[chat_id]

    pending = queue.qsize() + (1 if chat_id in busy_chats else 0)
    await queue.put((text, update))

    if pending > 0:
        ahead = pending
        await update.message.reply_text(f"Queued — {ahead} request{'s' if ahead > 1 else ''} ahead.")

    worker = chat_workers.get(chat_id)
    if worker is None or worker.done():
        chat_workers[chat_id] = asyncio.create_task(_chat_worker(chat_id, context))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return

    text = update.message.text.strip()
    if not text:
        return

    await _enqueue(text, update, context)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return

    msg = update.message
    caption = (msg.caption or "").strip()

    if msg.document:
        tg_file_id = msg.document.file_id
        file_name = msg.document.file_name or "uploaded_file"
    elif msg.photo:
        photo = msg.photo[-1]  # highest resolution
        tg_file_id = photo.file_id
        file_name = f"photo_{photo.file_id[-8:]}.jpg"
    else:
        return

    tg_file = await context.bot.get_file(tg_file_id)
    dest = UPLOADS_DIR / file_name
    await tg_file.download_to_drive(dest)

    note = f"File uploaded: {dest}"
    text = f"{caption}\n\n{note}" if caption else note

    await _enqueue(text, update, context)


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
    await _enqueue(text, update, context)


async def cmd_sendfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a file from the server to the user. Usage: /sendfile <path> [caption]"""
    if not auth(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /sendfile <path> [optional caption]")
        return
    path = Path(context.args[0])
    caption = " ".join(context.args[1:]) if len(context.args) > 1 else None
    if not path.exists():
        await update.message.reply_text(f"File not found: {path}")
        return
    size_mb = path.stat().st_size / 1_048_576
    write_timeout = max(60, int(size_mb * 30))  # 30s per MB, min 60s
    try:
        with open(path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename=path.name,
                caption=caption,
                write_timeout=write_timeout,
                read_timeout=write_timeout,
            )
    except Exception as e:
        await update.message.reply_text(f"Failed to send: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Telegram errors without crashing the bot. TimedOut is a normal network hiccup."""
    err = context.error
    if isinstance(err, TelegramTimedOut):
        log.warning("Telegram API timed out (transient): %s", err)
    elif isinstance(err, TelegramNetworkError):
        log.warning("Telegram network error (transient): %s", err)
    else:
        log.error("Unhandled Telegram error: %s", err, exc_info=err)


async def post_init(app) -> None:
    """Clean up leftover status badges from before a restart, then register commands."""
    # On restart, any in-flight status message was left orphaned — clean it up now.
    for session_file_path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(session_file_path.read_text())
            mid = data.get("status_msg_id")
            chat_id = int(session_file_path.stem)
            if mid is not None:
                try:
                    await app.bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception:
                    pass
                data.pop("status_msg_id", None)
                session_file_path.write_text(json.dumps(data))
        except Exception:
            pass

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
        BotCommand("sendfile", "Send a file from the server"),
    ]
    await app.bot.set_my_commands(bot_commands)
    log.info("Registered %d commands in Telegram menu", len(bot_commands))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(15)
        .read_timeout(30)
        .get_updates_connect_timeout(15)
        .get_updates_read_timeout(35)   # must exceed run_polling(timeout=...) — default is 0 but PTB adds buffer
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("id",      cmd_id))
    app.add_handler(CommandHandler("sendfile", cmd_sendfile))
    # Claude Code passthrough commands
    for cmd in CLAUDE_CODE_COMMANDS:
        app.add_handler(CommandHandler(cmd, cmd_claude_passthrough))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.PHOTO, handle_file))
    app.add_error_handler(error_handler)
    log.info("Bot started. Allowed user ID: %s", ALLOWED_USER_ID)
    app.run_polling()


if __name__ == "__main__":
    main()
