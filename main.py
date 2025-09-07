import os
import asyncio
import logging
import aiosqlite
from datetime import datetime

from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ENV Vars ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
REMINDER_INTERVAL = int(os.getenv("REMINDER_INTERVAL_MINUTES", "120"))

# --- Flask App (keep alive) ---
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "OK - Telegram Feedback Bot running", 200

@flask_app.route("/health")
def health():
    return {"status": "ok"}, 200


# --- SQLite Database ---
class Database:
    def __init__(self, path="feedback.db"):
        self.path = path
        self.conn = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        await self._create_tables()

    async def _create_tables(self):
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS feedback_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                display_name TEXT,
                group_id INTEGER,
                group_name TEXT,
                message_link TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS allowed_groups (
                group_id INTEGER PRIMARY KEY
            );
        """)
        await self.conn.commit()

    async def log_feedback(self, user_id, username, display_name, gid, gname, msg_link):
        await self.conn.execute("""
            INSERT INTO feedback_logs (user_id, username, display_name, group_id, group_name, message_link)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, username, display_name, gid, gname, msg_link))
        await self.conn.commit()

    async def feedback_in_last_days(self, gid, days):
        query = """
            SELECT * FROM feedback_logs
            WHERE group_id = ? AND ts >= datetime('now', ?)
            ORDER BY ts DESC
        """
        async with self.conn.execute(query, (gid, f'-{days} days')) as cursor:
            return await cursor.fetchall()

    async def has_feedback(self, user_id, gid, days=3):
        query = """
            SELECT * FROM feedback_logs
            WHERE group_id = ? AND user_id = ? AND ts >= datetime('now', ?)
        """
        async with self.conn.execute(query, (gid, user_id, f'-{days} days')) as cursor:
            return await cursor.fetchall()

    async def clear_feedback(self):
        await self.conn.execute("DELETE FROM feedback_logs")
        await self.conn.commit()

    async def cleanup_old_feedback(self, days=5):
        await self.conn.execute(
            "DELETE FROM feedback_logs WHERE ts < datetime('now', ?)",
            (f'-{days} days',)
        )
        await self.conn.commit()

    async def add_group(self, gid):
        await self.conn.execute("INSERT OR IGNORE INTO allowed_groups (group_id) VALUES (?)", (gid,))
        await self.conn.commit()

    async def is_group_allowed(self, gid):
        async with self.conn.execute("SELECT 1 FROM allowed_groups WHERE group_id = ?", (gid,)) as cursor:
            return await cursor.fetchone() is not None


DB = Database("feedback.db")


# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the HIJI's Private Bot")


async def addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    gid = update.effective_chat.id
    await DB.add_group(gid)
    await update.message.reply_text("✅ Group authorized for feedback tracking.")


async def fb_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id
    if not await DB.is_group_allowed(gid):
        return
    rows = await DB.feedback_in_last_days(gid, 3)
    if not rows:
        await update.message.reply_text("No feedback in the last 3 days.")
        return
    msg = f"📊 Feedback in last 3 days ({len(rows)} entries):\n"
    for r in rows:
        msg += f"- {r[2]} (@{r[1]}) → {r[6]} [{r[7]}]\n"
    await update.message.reply_text(msg)


async def check_user_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id
    if not await DB.is_group_allowed(gid):
        return
    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif update.message.entities:
        for ent in update.message.entities:
            if ent.type == "mention":
                uname = update.message.text[ent.offset:ent.offset+ent.length].lstrip("@")
                target_user = await context.bot.get_chat_member(gid, uname)
    if not target_user:
        await update.message.reply_text("⚠️ Could not identify user.")
        return
    rows = await DB.has_feedback(target_user.id, gid, 3)
    if rows:
        msg = f"✅ Feedback from {target_user.full_name} (@{target_user.username}):\n"
        for r in rows:
            msg += f"- {r[6]} [{r[7]}]\n"
    else:
        msg = "No feedback was received from him in the last 3 days"
    await update.message.reply_text(msg)


async def clear_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await DB.clear_feedback()
    await update.message.reply_text("🗑️ All feedback data cleared.")


REMINDER_TEXT = None
async def add_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global REMINDER_TEXT
    user = update.effective_user
    member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
    if not member.status in ["administrator", "creator"]:
        return
    if not context.args:
        await update.message.reply_text("Usage: /addreminder <text>")
        return
    REMINDER_TEXT = " ".join(context.args)
    await update.message.reply_text(f"⏰ Reminder set: {REMINDER_TEXT}")


async def reminder_task(app: Application):
    global REMINDER_TEXT
    while True:
        if REMINDER_TEXT:
            for chat in app.chat_data.keys():
                try:
                    await app.bot.send_message(chat, REMINDER_TEXT)
                except Exception as e:
                    logger.warning(f"Reminder send failed: {e}")
        await asyncio.sleep(REMINDER_INTERVAL * 60)


async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    gid = update.effective_chat.id
    if not await DB.is_group_allowed(gid):
        return
    if "#feedback" in msg.text.lower() if msg.text else "":
        if msg.photo or msg.video or msg.document or (msg.reply_to_message and (msg.reply_to_message.photo or msg.reply_to_message.video or msg.reply_to_message.document)):
            user = msg.from_user
            link = msg.link or ""
            await DB.log_feedback(
                user.id, user.username, user.full_name,
                gid, update.effective_chat.title or "",
                link
            )
            await msg.reply_text("✅ Feedback recorded.")


# --- Build Bot ---
def build_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addgroup", addgroup))
    app.add_handler(CommandHandler("fb_stats", fb_stats))
    app.add_handler(CommandHandler("!", check_user_feedback))
    app.add_handler(CommandHandler("cleardb", clear_db))
    app.add_handler(CommandHandler("addreminder", add_reminder))
    app.add_handler(MessageHandler(filters.ALL, feedback_handler))

    return app


# --- Runner ---
async def main():
    await DB.connect()

    # Auto-clean task
    async def cleanup_task():
        while True:
            await DB.cleanup_old_feedback(5)
            logger.info("🧹 Old feedback (5+ days) deleted")
            await asyncio.sleep(24 * 60 * 60)

    bot_app = build_bot()
    asyncio.create_task(cleanup_task())
    asyncio.create_task(reminder_task(bot_app))

    bot_task = asyncio.create_task(bot_app.run_polling())

    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    config = Config()
    config.bind = [f"0.0.0.0:{os.getenv('PORT', 8000)}"]
    flask_task = asyncio.create_task(serve(flask_app, config))

    await asyncio.gather(bot_task, flask_task)


if __name__ == "__main__":
    asyncio.run(main())