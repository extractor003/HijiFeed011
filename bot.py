# bot.py
"""
Telegram Feedback Bot — Render Postgres (PTB v21, async)
- Uses Flask web service (see web.py) to keep Render free tier awake.
- Bot runs in a background thread alongside Flask (started by web.create_app()).
- Database: Render PostgreSQL (hardcoded per user's request).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

import asyncpg
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Chat,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    AIORateLimiter,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import os

# ---------------------------
# Config & Logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("feedback-bot")

# Owner & DB Configuration (hardcoded as requested)
OWNER_ID = int(os.getenv("OWNER_ID"))  # HIJI's owner Telegram ID

# HARD-CODED DB URL (user requested this). In production, prefer environment variables!
import os
DATABASE_URL = os.getenv("DATABASE_URL")

# Bot token must still come from environment for safety
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Intervals
REMINDER_INTERVAL_MINUTES = int(os.getenv("REMINDER_INTERVAL_MINUTES", "120"))
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "600"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))


# ---------------------------
# Database Layer
# ---------------------------
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
            await self._create_tables()
            logger.info("DB pool created and tables ensured.")

    async def _create_tables(self):
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    group_id BIGINT PRIMARY KEY,
                    group_name TEXT,
                    date_added TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS feedback_logs (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    display_name TEXT,
                    group_id BIGINT NOT NULL,
                    group_name TEXT,
                    message_id BIGINT,
                    message_link TEXT,
                    ts TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback_logs(ts);
                CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback_logs(user_id);
                CREATE INDEX IF NOT EXISTS idx_feedback_group ON feedback_logs(group_id);

                CREATE TABLE IF NOT EXISTS reminders (
                    group_id BIGINT PRIMARY KEY,
                    reminder_text TEXT NOT NULL,
                    date_added TIMESTAMPTZ DEFAULT NOW(),
                    last_sent TIMESTAMPTZ
                );
                """
            )

    async def heartbeat(self):
        if self.pool is None:
            await self.connect()
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT 1;")

    async def add_group(self, group_id: int, group_name: str):
        assert self.pool is not None
        q = """
            INSERT INTO groups (group_id, group_name)
            VALUES ($1, $2)
            ON CONFLICT (group_id) DO UPDATE SET group_name = EXCLUDED.group_name
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, group_id, group_name)

    async def is_group_authorized(self, group_id: int) -> bool:
        assert self.pool is not None
        q = "SELECT 1 FROM groups WHERE group_id=$1"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, group_id)
            return row is not None

    async def log_feedback(
        self,
        user_id: int,
        username: Optional[str],
        display_name: str,
        group_id: int,
        group_name: str,
        message_id: int,
        message_link: str,
        ts: Optional[datetime] = None,
    ):
        assert self.pool is not None
        if ts is None:
            ts = datetime.now(timezone.utc)
        q = """
            INSERT INTO feedback_logs (user_id, username, display_name, group_id, group_name, message_id, message_link, ts)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                user_id,
                username,
                display_name,
                group_id,
                group_name,
                message_id,
                message_link,
                ts,
            )

    async def feedback_in_last_days(self, group_id: int, days: int) -> Sequence[asyncpg.Record]:
        assert self.pool is not None
        q = """
            SELECT user_id, username, display_name, message_link, ts
            FROM feedback_logs
            WHERE group_id=$1 AND ts >= NOW() - ($2::INT || ' days')::INTERVAL
            ORDER BY ts DESC
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(q, group_id, days)

    async def user_feedback(
        self, group_id: int, user_id: Optional[int] = None, username: Optional[str] = None, days: Optional[int] = None
    ) -> Sequence[asyncpg.Record]:
        assert self.pool is not None
        # Dynamic where
        cond = ["group_id=$1"]
        params: list = [group_id]
        param_idx = 2
        if user_id is not None:
            cond.append(f"user_id=${param_idx}")
            params.append(user_id)
            param_idx += 1
        elif username is not None:
            cond.append(f"LOWER(username)=LOWER(${param_idx})")
            params.append(username.lstrip("@"))
            param_idx += 1
        if days is not None:
            cond.append(f"ts >= NOW() - (${param_idx}::INT || ' days')::INTERVAL")
            params.append(days)
        where = " AND ".join(cond)
        q = f"""
            SELECT user_id, username, display_name, message_link, ts
            FROM feedback_logs
            WHERE {where}
            ORDER BY ts DESC
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(q, *params)

    async def count_unique_users_last_days(self, group_id: int, days: int) -> int:
        assert self.pool is not None
        q = """
            SELECT COUNT(DISTINCT user_id) AS c
            FROM feedback_logs
            WHERE group_id=$1 AND ts >= NOW() - ($2::INT || ' days')::INTERVAL
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, group_id, days)
            return int(row["c"]) if row else 0

    async def clear_feedback(self):
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM feedback_logs;")

    async def cleanup_old_feedback(self, days: int = 5):
        assert self.pool is not None
        q = "DELETE FROM feedback_logs WHERE ts < NOW() - ($1::INT || ' days')::INTERVAL"
        async with self.pool.acquire() as conn:
            await conn.execute(q, days)

    async def set_reminder(self, group_id: int, text: str):
        assert self.pool is not None
        q = """
            INSERT INTO reminders (group_id, reminder_text, last_sent)
            VALUES ($1,$2,NULL)
            ON CONFLICT (group_id) DO UPDATE SET reminder_text = EXCLUDED.reminder_text
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, group_id, text)

    async def remove_reminder(self, group_id: int):
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM reminders WHERE group_id=$1", group_id)

    async def get_reminder(self, group_id: int) -> Optional[str]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT reminder_text FROM reminders WHERE group_id=$1", group_id)
            return row["reminder_text"] if row else None

    async def due_reminder_groups(self, interval_minutes: int) -> Sequence[asyncpg.Record]:
        assert self.pool is not None
        q = """
            SELECT r.group_id, r.reminder_text, g.group_name, r.last_sent
            FROM reminders r
            JOIN groups g ON g.group_id = r.group_id
            WHERE (
                r.last_sent IS NULL OR r.last_sent <= NOW() - ($1::INT || ' minutes')::INTERVAL
            )
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(q, interval_minutes)

    async def update_last_sent(self, group_id: int):
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE reminders SET last_sent=NOW() WHERE group_id=$1", group_id)


DB = Database(DATABASE_URL)

# ---------------------------
# Helpers
# ---------------------------
def is_media(msg: Message) -> bool:
    return bool(msg and (msg.photo or msg.video or msg.document))

def has_feedback_hashtag(msg: Message) -> bool:
    text = (msg.text or msg.caption or "").lower()
    return "#feedback" in text

def build_message_link(chat: Chat, message_id: int) -> str:
    if chat.username:  # public group
        return f"https://t.me/{chat.username}/{message_id}"
    cid = str(chat.id)
    if cid.startswith("-100"):
        cid = cid[4:]
    else:
        cid = cid.lstrip("-")
    return f"https://t.me/c/{cid}/{message_id}"

async def ensure_authorized_group(update: Update) -> bool:
    if update.effective_chat and update.effective_chat.id:
        return await DB.is_group_authorized(update.effective_chat.id)
    return False

async def is_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id == OWNER_ID:
        return True
    chat = update.effective_chat
    if not chat:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        logger.warning(f"Admin check failed: {e}")
        return False

async def send_paginated(update: Update, text: str):
    MAX = 4000
    if len(text) <= MAX:
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    start = 0
    while start < len(text):
        chunk = text[start : start + MAX]
        await update.effective_chat.send_message(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        start += MAX

# ---------------------------
# Command Handlers
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Custom welcome text as requested
    welcome = "Welcome to the HIJI's Private Bot"
    if update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        auth = await ensure_authorized_group(update)
        if auth:
            await update.message.reply_text(welcome)
        else:
            await update.message.reply_text("🚫 This group is not authorized. Ask the owner to run /addgroup here.")
    else:
        await update.message.reply_text(welcome)

async def addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("❌ Run this inside a group you want to authorize.")
        return
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Only the owner can authorize groups for this bot.")
        return
    chat = update.effective_chat
    await DB.add_group(chat.id, chat.title or chat.username or str(chat.id))
    await update.message.reply_text("✅ This group has been authorized. Feedback tracking is now active.")

async def fb_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized_group(update):
        return
    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be an admin to use this command.")
        return
    days = 3
    if context.args:
        try:
            days = max(1, min(90, int(context.args[0])))
        except Exception:
            pass
    chat = update.effective_chat
    rows = await DB.feedback_in_last_days(chat.id, days)
    unique_count = await DB.count_unique_users_last_days(chat.id, days)

    if not rows:
        await update.message.reply_text(f"📊 No feedback found in the last {days} days.")
        return

    lines = [
        f"<b>📊 Feedback Report (Last {days} days)</b>",
        f"<b>Total unique senders:</b> {unique_count}",
        "",
    ]
    for i, r in enumerate(rows, start=1):
        name = r["display_name"] or "-"
        uname = f"@{r['username']}" if r["username"] else "-"
        uid = r["user_id"]
        date_str = r["ts"].strftime("%Y-%m-%d %H:%M UTC")
        link = r["message_link"] or "-"
        lines.append(f"{i}. <b>{name}</b> ({uname}, ID: <code>{uid}</code>)\n   Date: {date_str}\n   Link: {link}")
    await send_paginated(update, "\n".join(lines))

async def fb_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized_group(update):
        return
    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be an admin to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /fb_user <user_id|@username> [days]")
        return

    target = context.args[0]
    days = None
    if len(context.args) > 1:
        try:
            days = max(1, min(365, int(context.args[1])))
        except Exception:
            days = None

    user_id = None
    username = None
    if target.startswith("@"):
        username = target.lstrip("@")
    else:
        try:
            user_id = int(target)
        except Exception:
            await update.message.reply_text("❌ Invalid identifier. Provide a numeric ID or @username.")
            return

    chat = update.effective_chat
    rows = await DB.user_feedback(chat.id, user_id=user_id, username=username, days=days)
    if not rows:
        when = f" in the last {days} days" if days else ""
        who = f"@{username}" if username else f"ID {user_id}"
        await update.message.reply_text(f"❌ No feedback found for {who}{when}.")
        return

    head_user = rows[0]
    name = head_user["display_name"] or "-"
    uname = f"@{head_user['username']}" if head_user["username"] else "-"
    uid = head_user["user_id"]

    lines = [
        f"<b>📌 Feedback history for {name} ({uname}, ID: <code>{uid}</code>)</b>",
        "",
    ]
    for i, r in enumerate(rows, start=1):
        date_str = r["ts"].strftime("%Y-%m-%d %H:%M UTC")
        link = r["message_link"] or "-"
        lines.append(f"{i}. Date: {date_str}\n   Link: {link}")
    await send_paginated(update, "\n".join(lines))

async def bang_check_core(update: Update, context: ContextTypes.DEFAULT_TYPE, text_source: str):
    if not await ensure_authorized_group(update):
        return
    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be an admin to use this command.")
        return

    chat = update.effective_chat
    user_id = None
    username = None

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        user_id = update.message.reply_to_message.from_user.id
        username = update.message.reply_to_message.from_user.username
    else:
        # parse mention from entities
        if update.message.entities:
            for ent in update.message.entities:
                if ent.type == "mention":
                    username = (update.message.text or "")[ent.offset + 1 : ent.offset + ent.length]
                    break
                if ent.type == "text_mention" and ent.user:
                    user_id = ent.user.id
                    username = ent.user.username
                    break
        # fallback: first arg
        if not user_id and not username and context.args:
            arg = context.args[0]
            if arg.startswith("@"):
                username = arg[1:]
            else:
                try:
                    user_id = int(arg)
                except Exception:
                    pass

    if not user_id and not username:
        await update.message.reply_text("Usage: reply with /check (or type /check @username or user_id).\nTip: I also accept raw '/!' text.")
        return

    rows = await DB.user_feedback(chat.id, user_id=user_id, username=username, days=3)
    who = f"@{username}" if username else f"ID {user_id}"

    if not rows:
        await update.message.reply_text(f"❌ No feedback was received from {who} in the last 3 days.")
        return

    head = rows[0]
    name = head["display_name"] or "-"
    uname = f"@{head['username']}" if head["username"] else "-"
    uid = head["user_id"]

    lines = [
        f"<b>📌 Feedback history for {name} ({uname}, ID: <code>{uid}</code>) — last 3 days</b>",
        "",
    ]
    for i, r in enumerate(rows, start=1):
        date_str = r["ts"].strftime("%Y-%m-%d %H:%M UTC")
        link = r["message_link"] or "-"
        lines.append(f"{i}. Date: {date_str}\n   Link: {link}")
    await send_paginated(update, "\n".join(lines))

async def bang_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await bang_check_core(update, context, "command")

async def bang_check_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Accept raw "/!" text as a convenience, even though it's not a formal Telegram command name.
    await bang_check_core(update, context, "text")

async def addreminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized_group(update):
        return
    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be an admin to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addreminder <text>")
        return
    text = " ".join(context.args).strip()
    await DB.set_reminder(update.effective_chat.id, text)
    await update.message.reply_text(
        f"✅ Reminder saved. I’ll send it every {REMINDER_INTERVAL_MINUTES} minutes in this group."
    )

async def removereminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized_group(update):
        return
    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be an admin to use this command.")
        return
    await DB.remove_reminder(update.effective_chat.id)
    await update.message.reply_text("🗑 Reminder removed for this group.")

async def cleardb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized_group(update):
        return
    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ You don’t have permission to use this command.")
        return
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, clear everything", callback_data="confirm_clear"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_clear"),
            ]
        ]
    )
    await update.message.reply_text(
        "⚠️ Are you sure you want to delete <b>all stored feedback data</b>? This action cannot be undone.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )

async def cleardb_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_or_owner(update, context):
        await update.callback_query.answer("Not allowed.")
        return
    query = update.callback_query
    data = query.data
    if data == "confirm_clear":
        await DB.clear_feedback()
        await query.edit_message_text("🗑 All feedback data has been cleared successfully.")
    else:
        await query.edit_message_text("❌ Operation cancelled.")

# ---------------------------
# Feedback Detection
# ---------------------------
async def feedback_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not await ensure_authorized_group(update):
        return

    msg = update.effective_message
    chat = update.effective_chat

    # Case 1: #feedback + media in same message
    if has_feedback_hashtag(msg) and is_media(msg):
        media_msg = msg
    # Case 2: reply with #feedback to a media message
    elif has_feedback_hashtag(msg) and msg.reply_to_message and is_media(msg.reply_to_message):
        media_msg = msg.reply_to_message
    else:
        return  # not a feedback event

    user = update.effective_user
    if not user:
        return

    user_id = user.id
    username = user.username
    display_name = (user.full_name or "").strip() or (user.first_name or "-")

    message_id = media_msg.message_id
    message_link = build_message_link(chat, message_id)

    await DB.log_feedback(
        user_id=user_id,
        username=username,
        display_name=display_name,
        group_id=chat.id,
        group_name=chat.title or chat.username or str(chat.id),
        message_id=message_id,
        message_link=message_link,
        ts=datetime.now(timezone.utc),
    )

    try:
        await msg.reply_text("✅ Thanks! Your feedback has been recorded.")
    except Exception:
        pass

# ---------------------------
# Background Tasks
# ---------------------------
async def heartbeat_task(app: Application):
    while True:
        try:
            await DB.heartbeat()
            logger.debug("DB heartbeat OK")
        except Exception as e:
            logger.warning(f"Heartbeat failed: {e}")
            try:
                await DB.connect()
            except Exception as e2:
                logger.error(f"DB reconnect failed: {e2}")
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

async def cleanup_task(app: Application):
    while True:
        try:
            await DB.cleanup_old_feedback(days=5)
            logger.info("Cleanup: removed logs older than 5 days")
        except Exception as e:
            logger.warning(f"Cleanup error: {e}")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

async def reminder_task(app: Application):
    while True:
        try:
            due = await DB.due_reminder_groups(REMINDER_INTERVAL_MINUTES)
            for r in due:
                group_id = int(r["group_id"])
                text = r["reminder_text"]
                try:
                    await app.bot.send_message(group_id, f"⏰ Reminder: {text}")
                    await DB.update_last_sent(group_id)
                except Exception as e:
                    logger.warning(f"Failed to send reminder to {group_id}: {e}")
        except Exception as e:
            logger.warning(f"Reminder task error: {e}")
        await asyncio.sleep(60)  # check every minute

async def post_init(app: Application):
    await DB.connect()
    app.create_task(heartbeat_task(app))
    app.create_task(cleanup_task(app))
    app.create_task(reminder_task(app))

def build_application() -> Application:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .post_init(post_init)
        .build()
    )

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addgroup", addgroup))
    application.add_handler(CommandHandler("fb_stats", fb_stats))
    application.add_handler(CommandHandler("fb_user", fb_user))
    application.add_handler(CommandHandler("addreminder", addreminder))
    application.add_handler(CommandHandler("removereminder", removereminder))
    application.add_handler(CommandHandler("cleardb", cleardb))
    application.add_handler(CallbackQueryHandler(cleardb_callback, pattern="^(confirm_clear|cancel_clear)$"))

    # Alias for quick check: /check is a valid Telegram command.
    application.add_handler(CommandHandler("check", bang_check_command))
    # Also accept raw "/!" text via regex (since "/!" is not a valid Telegram command name).
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/!(\\s|$)"), bang_check_text))

    # Feedback listener
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO | filters.Document.ALL),
            feedback_listener,
        )
    )
    return application

async def run_bot_polling():
    app = build_application()
    await app.run_polling(close_loop=False)


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_bot_polling())
