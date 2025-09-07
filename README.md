# Telegram Feedback Bot (Render + Postgres + Flask keep-alive)

Production-ready Telegram bot that tracks `#feedback` posts (media or reply to media) inside **authorized groups**.
- PTB v21 (async), Render PostgreSQL, Flask keep-alive web endpoint.
- Owner-only `/addgroup`, admin tools, auto-cleanup, reminders, DB keep-alive.

## Features
- `#feedback` + media **or** reply `#feedback` to media → log.
- `/addgroup` (owner): authorize the group.
- `/fb_stats <days>` (admins): list all feedback in last *days* + unique count.
- `/fb_user <id|@username> [days]` (admins): per-user history.
- `/check` (admins) **or** raw `"/!"` text: quick 3-day check by reply/mention/user.
- Auto-delete logs older than 5 days.
- `/addreminder <text>` and `/removereminder` (admins): repeating reminder every 2h.
- `/cleardb` (admins/owner): inline confirm to wipe feedback logs.
- DB heartbeat every 10 min to avoid idle connection issues on free tier.
- Flask web server responds on `/` and `/health` (for Render pings).

## Important
- `/start` message: **Welcome to the HIJI's Private Bot**
- Owner Telegram ID: **7703188582**
- DB URL is **hardcoded** in `bot.py` (as requested). Prefer env var in real projects.

## Deploy on Render
1. Fork or upload this repo.
2. In Render dashboard → **New Web Service** → connect repo.
3. Render reads `render.yaml` (or use Dockerfile). Ensure **Environment**=Python.
4. Set **environment variables**:
   - `BOT_TOKEN` (required)
   - (optional) `REMINDER_INTERVAL_MINUTES`, `HEARTBEAT_INTERVAL_SECONDS`, `CLEANUP_INTERVAL_SECONDS`
5. Deploy. The web service stays awake thanks to Flask; the bot runs in a background thread.

## Local run
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=123456:ABC...
python web.py  # starts Flask (port 8000) and the bot in a thread
```
Or run bot only:
```bash
export BOT_TOKEN=123456:ABC...
python bot.py
```

## Commands (summary)
- `/start` → Welcome text.
- `/addgroup` (owner only) → authorize group.
- `/fb_stats <days>` (admins) → list entries & unique users.
- `/fb_user <id|@username> [days]` (admins) → user history.
- `/check` (admins) or raw `"/!"` as text → check last 3 days by reply/mention.
- `/addreminder <text>` (admins) → schedule 2-hour reminder.
- `/removereminder` (admins) → stop reminder.
- `/cleardb` (admins/owner) → confirm to wipe logs.

## Notes about `/!`
Telegram command names can only contain letters, numbers, and underscores.
So we implement **/check** as the official command **and** accept raw **"/!"** text via regex.
Use it by replying to a user's message with `/check`, or typing `/check @username`.

## Security
- Your DB URL is hardcoded per request; if public on GitHub, anyone can read it.
  Consider switching to `DATABASE_URL` env var later.
