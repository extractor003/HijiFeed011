"""
Telegram Feedback Bot + Flask keep-alive (SQLite DB)
"""

import os
import logging
import threading
import asyncio
from datetime import datetime, timedelta
from flask import Flask, jsonify
import aiosqlite
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, AIORateLimiter,
    CommandHandler, MessageHandler, ContextTypes, filters
)

# =====================
# Config
# =====================
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("feedback-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))

REMINDER_INTERVAL_MINUTES = int(os.getenv("REMINDER_INTERVAL_MINUTES", "120"))

# =====================
# Database (SQLite)
# =====================
class Database:
    def __init__(self, path="feedback.db"):
        self.path = path
        self.conn = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        await self._create_tables()

    async def _create_tables(self):
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS groups (
                group_id INTEGER PRIMARY KEY,
                group_name TEXT,
                date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
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
            CREATE TABLE IF NOT EXISTS reminders (
                group_id INTEGER PRIMARY KEY,
                reminder_text TEXT NOT NULL,
                last_sent TIMESTAMP
            );
        """)
        await self.conn.commit()

    async def add_group(self, gid, gname):
        await self.conn.execute(
            "INSERT OR REPLACE INTO groups (group_id, group_name) VALUES (?, ?)",
            (gid, gname)
        )
        await self.conn.commit()

    async def is_group_authorized(self, gid):
        async with self.conn.execute("SELECT 1 FROM groups WHERE group_id=?", (gid,)) as cur:
            return await cur.fetchone() is not None

    async def log_feedback(self, user_id, username, display_name, gid, gname, msg_link):
        await self.conn.execute("""
            INSERT INTO feedback_logs (user_id, username, display_name, group_id, group_name, message_link)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, username, display_name, gid, gname, msg_link))
        await self.conn.commit()

    async def feedback_in_last_days(self, gid, days):
        query = """
            SELECT * FROM feedback_logs
            WHERE group_id=? AND ts >= datetime('now', ?)
            ORDER BY ts DESC
        """
        async with self.conn.execute(query, (gid, f'-{days} days')) as cur:
            return await cur.fetchall()

    async def clear_feedback(self):
        await self.conn.execute("DELETE FROM feedback_logs")
        await self.conn.commit()

    async def cleanup_old_feedback(self, days=5):
        """Delete feedback older than X days"""
        await self.conn.execute(
            "DELETE FROM feedback_logs WHERE ts < datetime('now', ?)",
            (f'-{days} days',)
        )
        await self.conn.commit()
        logger.info(f"🧹 Old feedback older than {days} days deleted")

DB = Database("feedback.db")

# =====================
# Helpers
# =====================
def make_link(chat, message_id):
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    cid = str(chat.id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{message_id}"

# =====================
# Bot Handlers
# =====================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the HIJI's Private Bot")

async def addgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("❌ Only owner can authorize.")
    chat = update.effective_chat
    await DB.add_group(chat.id, chat.title or str(chat.id))
    await update.message.reply_text("✅ Group authorized.")

async def fb_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await DB.is_group_authorized(update.effective_chat.id):
        return
    rows = await DB.feedback_in_last_days(update.effective_chat.id, 3)
    if not rows:
        return await update.message.reply_text("No feedback in last 3 days.")
    msg = [f"📊 Feedback in last 3 days ({len(rows)} entries):"]
    for r in rows:
        msg.append(f"- {r[3]} (@{r[2]}) → {r[6]}")
    await update.message.reply_text("\n".join(msg), disable_web_page_preview=True)

async def cleardb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await DB.clear_feedback()
    await update.message.reply_text("🗑 All feedback data cleared.")

async def feedback_listener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await DB.is_group_authorized(update.effective_chat.id):
        return
    msg = update.effective_message
    txt = (msg.text or msg.caption or "").lower()
    if "#feedback" not in txt:
        return
    if not (msg.photo or msg.video or msg.document) and not (
        msg.reply_to_message and (msg.reply_to_message.photo or msg.reply_to_message.video or msg.reply_to_message.document)
    ):
        return
    user = update.effective_user
    link = make_link(update.effective_chat, msg.message_id)
    await DB.log_feedback(user.id, user.username, user.full_name,
                          update.effective_chat.id, update.effective_chat.title, link)
    await msg.reply_text("✅ Feedback recorded.")

# =====================
# Build Bot
# =====================
def build_app():
    app = (ApplicationBuilder()
           .token(BOT_TOKEN)
           .rate_limiter(AIORateLimiter())
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addgroup", addgroup))
    app.add_handler(CommandHandler("fb_stats", fb_stats))
    app.add_handler(CommandHandler("cleardb", cleardb))
    app.add_handler(MessageHandler(filters.ALL, feedback_listener))
    return app

# =====================
# Flask App
# =====================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "OK - Telegram Feedback Bot running", 200

@flask_app.route("/health")
def health():
    return jsonify(status="ok"), 200

# =====================
# Main entry
# =====================
if __name__ == "__main__":
    async def run_bot():
        await DB.connect()
        logger.info("✅ SQLite DB ready")

        # Background auto-clean task
        async def cleanup_task():
            while True:
                await DB.cleanup_old_feedback(5)  # delete >5 days old
                await asyncio.sleep(24 * 60 * 60)  # run every 24h

        asyncio.create_task(cleanup_task())

        bot_app = build_app()
        await bot_app.run_polling()

    def bot_thread():
        asyncio.run(run_bot())

    t = threading.Thread(target=bot_thread, daemon=True)
    t.start()

    port = int(os.getenv("PORT", 8000))
    flask_app.run(host="0.0.0.0", port=port)
