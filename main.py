"""
Telegram Feedback Bot + Flask keep-alive in ONE file
- PostgreSQL (asyncpg) for logs
- PTB v21 (async Application)
- Flask endpoint for Render uptime
"""

import os
import logging
import threading
import asyncio
from datetime import datetime, timezone
from flask import Flask, jsonify
import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, AIORateLimiter,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# =====================
# Config
# =====================
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("feedback-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

REMINDER_INTERVAL_MINUTES = int(os.getenv("REMINDER_INTERVAL_MINUTES", "120"))

# =====================
# Database
# =====================
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
            await self._create_tables()

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
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
                    message_link TEXT,
                    ts TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback_logs(ts);
                CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback_logs(user_id);
                CREATE TABLE IF NOT EXISTS reminders (
                    group_id BIGINT PRIMARY KEY,
                    reminder_text TEXT NOT NULL,
                    last_sent TIMESTAMPTZ
                );
            """)

    async def add_group(self, gid, gname):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO groups (group_id, group_name)
                VALUES ($1,$2)
                ON CONFLICT (group_id) DO UPDATE SET group_name=EXCLUDED.group_name
            """, gid, gname)

    async def is_group_authorized(self, gid):
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT 1 FROM groups WHERE group_id=$1", gid)

    async def log_feedback(self, user_id, username, display_name,
                           gid, gname, msg_link):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO feedback_logs (user_id, username, display_name, group_id, group_name, message_link, ts)
                VALUES ($1,$2,$3,$4,$5,$6,NOW())
            """, user_id, username, display_name, gid, gname, msg_link)

    async def feedback_in_last_days(self, gid, days):
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT * FROM feedback_logs
                WHERE group_id=$1 AND ts >= NOW() - ($2::INT || ' days')::INTERVAL
                ORDER BY ts DESC
            """, gid, days)

    async def clear_feedback(self):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM feedback_logs;")

DB = Database(DATABASE_URL)

# =====================
# Bot Handlers
# =====================
def make_link(chat, message_id):
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    cid = str(chat.id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{message_id}"

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the HIJI's Private Bot")

async def addgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
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
        msg.append(f"- {r['display_name']} (@{r['username']}) → {r['message_link']}")
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
        bot_app = build_app()
        await bot_app.run_polling()

    def bot_thread():
        asyncio.run(run_bot())

    t = threading.Thread(target=bot_thread, daemon=True)
    t.start()

    port = int(os.getenv("PORT", 8000))
    flask_app.run(host="0.0.0.0", port=port)