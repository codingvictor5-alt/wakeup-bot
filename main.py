#!/usr/bin/env python3
"""
RiseEarly Wakeup Bot — Render-ready, asyncpg, self-ping, JobQueue-safe.
Drop into your repo, set environment variables on Render, push, deploy.
"""

import os
import asyncio
from datetime import datetime, timedelta, date
from datetime import time
from zoneinfo import ZoneInfo
import statistics
from typing import Optional, List, Tuple, Dict, Any
from collections import defaultdict

from dotenv import load_dotenv
from motivate import send_motivation,motivate_command
from tagger import generate_mentions
load_dotenv()

# Third-party DB + HTTP
import asyncpg
import aiohttp

# Telegram
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------- CONFIG -------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID_RAW = os.getenv("GROUP_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")  # PostgreSQL connection string (Neon/Heroku/etc.)
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # e.g. https://your-service.onrender.com
TZ = ZoneInfo("Asia/Kolkata")

LEADERBOARD_HOUR = int(os.environ.get("LEADERBOARD_HOUR", 5))
BEDTIME_HOUR = int(os.environ.get("BEDTIME_HOUR", 21))
BEDTIME_MINUTE = int(os.environ.get("BEDTIME_MINUTE", 0O))
WEEKLY_SUMMARY_DAY = int(os.environ.get("WEEKLY_SUMMARY_DAY", 6))  # Sunday=6
WEEKLY_SUMMARY_HOUR = int(os.environ.get("WEEKLY_SUMMARY_HOUR", 6))
LEADERBOARD_TOP = int(os.environ.get("LEADERBOARD_TOP", 5))
BADGE_THRESHOLDS = [3, 7, 21]

if not BOT_TOKEN:
    raise SystemExit("Error: BOT_TOKEN env var required.")
if not GROUP_CHAT_ID_RAW:
    raise SystemExit("Error: GROUP_CHAT_ID env var required.")
if not DATABASE_URL:
    raise SystemExit("Error: DATABASE_URL env var required.")

try:
    GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW)
except Exception:
    raise SystemExit("Error: GROUP_CHAT_ID must be integer (e.g. -1001234567890).")

# ------------- DB (asyncpg pool) -------------
db_pool: Optional[asyncpg.Pool] = None

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    chat_id BIGINT,
    user_id BIGINT,
    streak INTEGER DEFAULT 0,
    last_checkin TEXT,
    last_time TEXT,
    badge TEXT,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS records (
    chat_id BIGINT,
    user_id BIGINT,
    date TEXT,
    time TEXT
);

CREATE INDEX IF NOT EXISTS idx_records_chat_date ON records(chat_id, date);
"""

async def init_db():
    global db_pool
    print("🔄 Connecting to DB...")
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute(CREATE_TABLES_SQL)
    print("✅ DB ready")

# DB helpers
async def ensure_user_row(conn, chat_id: int, user_id: int):
    await conn.execute(
        """INSERT INTO users(chat_id, user_id, streak, last_checkin, last_time, badge)
           VALUES ($1,$2,0,'','', '') ON CONFLICT DO NOTHING""",
        chat_id, user_id
    )

async def get_user(conn, chat_id:int, user_id:int):
    return await conn.fetchrow("SELECT streak, last_checkin, last_time, badge FROM users WHERE chat_id=$1 AND user_id=$2", chat_id, user_id)

async def set_user_fields(conn, chat_id:int, user_id:int, streak=None, last_checkin=None, last_time=None, badge=None):
    parts = []
    vals = []
    idx = 1
    if streak is not None:
        parts.append(f"streak=${idx}"); vals.append(streak); idx+=1
    if last_checkin is not None:
        parts.append(f"last_checkin=${idx}"); vals.append(last_checkin); idx+=1
    if last_time is not None:
        parts.append(f"last_time=${idx}"); vals.append(last_time); idx+=1
    if badge is not None:
        parts.append(f"badge=${idx}"); vals.append(badge); idx+=1
    if not parts: return
    vals.extend([chat_id, user_id])
    await conn.execute(f"UPDATE users SET {', '.join(parts)} WHERE chat_id=${idx} AND user_id=${idx+1}", *vals)

async def add_record(conn, chat_id:int, user_id:int, iso_date:str, hhmm:str):
    await conn.execute("INSERT INTO records(chat_id, user_id, date, time) VALUES ($1,$2,$3,$4)", chat_id, user_id, iso_date, hhmm)

async def user_checked_today(conn, chat_id:int, user_id:int, iso_date:str):
    row = await conn.fetchrow("SELECT 1 FROM records WHERE chat_id=$1 AND user_id=$2 AND date=$3 LIMIT 1", chat_id, user_id, iso_date)
    return row is not None

async def get_user_records(conn, chat_id:int, user_id:int):
    rows = await conn.fetch("SELECT time FROM records WHERE chat_id=$1 AND user_id=$2 ORDER BY date", chat_id, user_id)
    return [r['time'] for r in rows]

async def get_top_streaks(conn, chat_id:int, top:int=5):
    rows = await conn.fetch("SELECT user_id, streak, badge FROM users WHERE chat_id=$1 ORDER BY streak DESC LIMIT $2", chat_id, top)
    return [(r['user_id'], r['streak'], r['badge']) for r in rows]

async def get_records_between(conn, chat_id:int, start_date:str, end_date:str):
    rows = await conn.fetch("SELECT user_id, date, time FROM records WHERE chat_id=$1 AND date BETWEEN $2 AND $3", chat_id, start_date, end_date)
    return [(r['user_id'], r['date'], r['time']) for r in rows]

# ------------- Utilities -------------
def parse_time_string(s: str):
    s = s.strip()

    # Format: "5" → 5:00
    if s.isdigit():
        h = int(s)
        if 0 <= h <= 23:
            return time(h, 0)
        return None

    # Replace "." with ":" for flexibility
    s = s.replace(".", ":")

    # Format: "5:30"
    try:
        parts = s.split(":")
        if len(parts) != 2:
            return None
        h = int(parts[0])
        m = int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return time(h, m)
    except:
        return None

    return None

def valid_wakeup(t: time) -> bool:
    return time(4,0) <= t <= time(7,0)

def average_time_str(times: List[str]) -> str:
    if not times: return "N/A"
    mins = []
    for t in times:
        h,m = map(int, t.split(":"))
        mins.append(h*60 + m)
    avg = int(statistics.mean(mins))
    return f"{avg//60:02d}:{avg%60:02d}"

def badge_for_streak(streak:int) -> Optional[str]:
    if streak >= BADGE_THRESHOLDS[2]: return "Gold"
    if streak >= BADGE_THRESHOLDS[1]: return "Silver"
    if streak >= BADGE_THRESHOLDS[0]: return "Bronze"
    return None

# ------------- Safe send helper -------------
async def safe_send(bot, chat_id:int, *args, **kwargs):
    try:
        return await bot.send_message(chat_id, *args, **kwargs)
    except Exception as e:
        print("❌ send_message failed:", e)
        return None

# ------------- Self-ping keepalive (for Render) -------------
async def self_ping_task():
    if not RENDER_EXTERNAL_URL:
        print("ℹ️ RENDER_EXTERNAL_URL not set; self-ping disabled")
        return
    url = RENDER_EXTERNAL_URL.rstrip("/") + "/health"
    await asyncio.sleep(120)
    print(f"🔁 self-ping enabled -> {url} every 10 minutes")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, timeout=30) as r:
                    print(f"🔔 self-ping {r.status} @ {datetime.now(TZ)}")
            except Exception as e:
                print("⚠️ self-ping error:", e)
            await asyncio.sleep(200)  # 4 minutes

# ------------- Command Handlers -------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # only respond inside the configured group
    if update.effective_chat.id != GROUP_CHAT_ID:
        await update.message.reply_text("This bot works only in the configured group.")
        return
    await update.message.reply_text(
        "👋 Wakeup Bot ready!\nCommands:\n(checkin) or /checkin, /mystats, /leaderboard, /reset, /setstreak <n>"
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID: return
    user = update.effective_user
    async with db_pool.acquire() as conn:
        await ensure_user_row(conn, GROUP_CHAT_ID, user.id)
        await set_user_fields(conn, GROUP_CHAT_ID, user.id, streak=0, last_checkin=date.today().isoformat(), last_time="reset", badge="")
    await update.message.reply_text(f"🔁 {user.mention_html()} — streak reset to 0.", parse_mode=ParseMode.HTML)

async def setstreak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID: return
    user = update.effective_user
    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("Usage: /setstreak 8")
        return
    new = int(args[0])
    badge = badge_for_streak(new) or ""
    async with db_pool.acquire() as conn:
        await ensure_user_row(conn, GROUP_CHAT_ID, user.id)
        await set_user_fields(conn, GROUP_CHAT_ID, user.id, streak=new, badge=badge)
    await update.message.reply_text(f"✅ {user.mention_html()} — streak set to {new}. Badge: {badge or 'None'}.", parse_mode=ParseMode.HTML)

async def mystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID: return
    user = update.effective_user
    async with db_pool.acquire() as conn:
        await ensure_user_row(conn, GROUP_CHAT_ID, user.id)
        row = await get_user(conn, GROUP_CHAT_ID, user.id)
        recs = await get_user_records(conn, GROUP_CHAT_ID, user.id)
        max_row = await conn.fetchrow("SELECT MAX(streak) as maxst FROM users WHERE chat_id=$1", GROUP_CHAT_ID)
        max_streak = max_row['maxst'] if max_row and max_row['maxst'] else 0
    if not row:
        await update.message.reply_text("No stats found. Do your first checkin!")
        return
    streak = row['streak']
    last_checkin = row['last_checkin'] or "N/A"
    last_time = row['last_time'] or "N/A"
    badge = row['badge']
    avg = average_time_str(recs)
    earliest = min(recs) if recs else "N/A"
    await update.message.reply_text(
        f"📊 <b>{user.full_name}'s Stats</b>\n"
        f"• Current streak: <b>{streak}</b>\n"
        f"• Max streak ever: <b>{max_streak}</b>\n"
        f"• Last check-in: <b>{last_checkin}</b>\n"
        f"• Last wake-up: <b>{last_time}</b>\n"
        f"• Avg wake-up: <b>{avg}</b>\n"
        f"• Earliest wake-up: <b>{earliest}</b>\n"
        f"• Badge: <b>{badge or 'None'}</b>",
        parse_mode=ParseMode.HTML
    )

# Checkin supports both /checkin and plain "checkin"
async def checkin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.chat.id != GROUP_CHAT_ID:
        return

    user = update.effective_user
    parts = msg.text.strip().split()

    # Must include time argument
    if len(parts) < 2:
        await msg.reply_text(
            "❗ <b>Invalid format</b>\n\n"
            "Use:\n"
            "👉 <code>/checkin 5</code>\n"
            "👉 <code>/checkin 5.30</code>\n"
            "👉 <code>/checkin 5:30</code>\n\n"
            "⏰ <b>IMPORTANT:</b>\n"
            "• Streak is counted only if wake-up time is between <b>4:00 AM and 7:00 AM</b>.\n"
            "• You can check in only <b>once per day</b>.",
            parse_mode=ParseMode.HTML
        )
        return

    # Parse time (supports 5, 5.30, 5:30 etc.)
    raw_time = parts[1]
    t = parse_time_string(raw_time)

    if not t:
        await msg.reply_text(
            "❗ <b>Invalid time format</b>\n\n"
            "Use:\n"
            "👉 <code>/checkin 5</code>\n"
            "👉 <code>/checkin 5.30</code>\n"
            "👉 <code>/checkin 5:30</code>\n\n"
            "⏰ <b>Streak only counts between 4 AM and 7 AM</b>.",
            parse_mode=ParseMode.HTML
        )
        return

    today_iso = datetime.now(TZ).date().isoformat()
    hhmm = t.strftime("%H:%M")

    async with db_pool.acquire() as conn:

        # Ensure user row exists
        await ensure_user_row(conn, GROUP_CHAT_ID, user.id)

        # Check if already checked in today
        if await user_checked_today(conn, GROUP_CHAT_ID, user.id, today_iso):
            await msg.reply_text(
                f"⛔ {user.mention_html()} you have already used your valid check-in for today.\n"
                "You can check in only <b>once per day</b>.",
                parse_mode=ParseMode.HTML
            )
            return

        # If outside 4–7 AM → record + reset
        if not valid_wakeup(t):
            await add_record(conn, GROUP_CHAT_ID, user.id, today_iso, hhmm)
            await set_user_fields(conn, GROUP_CHAT_ID , user.id,streak=0, last_checkin=today_iso,last_time=hhmm, badge="")
            await conn.execute("COMMIT")

            await msg.reply_text(
                f"⚠️ {user.mention_html()} — wake-up <b>{hhmm}</b> is outside 04:00–07:00.\n"
                "Your streak has been <b>reset to 0</b>.",
                parse_mode=ParseMode.HTML
            )
            return

        # Valid check-in
        row = await get_user(conn, GROUP_CHAT_ID, user.id)
        prev_streak = row['streak'] if row else 0

        new_streak = prev_streak + 1
        badge = badge_for_streak(new_streak) or ""

        await add_record(conn, GROUP_CHAT_ID, user.id, today_iso, hhmm)
        await set_user_fields(conn , GROUP_CHAT_ID, user.id,
                              streak=new_streak,
                              last_checkin=today_iso,
                              last_time=hhmm,
                              badge=badge)
        await msg.reply_text(
            f"🔥 {user.mention_html()} — streak now <b>{new_streak}</b>\n"
            f"⏰ Wake-up time: <b>{hhmm}</b>\n"
            f"🏅 Badge: <b>{badge or 'None'}</b>",
            parse_mode=ParseMode.HTML
        )


# ------------- Leaderboards & summaries -------------
async def send_leaderboard(context: ContextTypes.DEFAULT_TYPE):
    async with db_pool.acquire() as conn:
        top_streaks = await get_top_streaks(conn, GROUP_CHAT_ID, LEADERBOARD_TOP)
        recs = await conn.fetch("SELECT user_id, time FROM records WHERE chat_id=$1", GROUP_CHAT_ID)
    # earliest risers by average
    times_by_user = defaultdict(list)
    for r in recs:
        times_by_user[r['user_id']].append(r['time'])
    avg_times = {}
    for uid, times in times_by_user.items():
        mins = [int(h)*60 + int(m) for h,m in (map(int, x.split(":")) for x in times)]
        avg_times[uid] = sum(mins)//len(mins)
    earliest = sorted(avg_times.items(), key=lambda x: x[1])[:LEADERBOARD_TOP]

    # format
    streak_lines = []
    for i, (uid, streak, badge) in enumerate(top_streaks, 1):
        try:
            mention = (await context.bot.get_chat_member(GROUP_CHAT_ID, uid)).user.mention_html()
        except:
            mention = str(uid)
        b = f" ({badge})" if badge else ""
        streak_lines.append(f"{i}. {mention} — <b>{streak}</b> days{b}")
    streak_text = "🏆 <b>Top Streaks</b>\n\n" + ("\n".join(streak_lines) if streak_lines else "No streaks yet.")

    early_lines = []
    for i, (uid, mins) in enumerate(earliest, 1):
        try:
            mention = (await context.bot.get_chat_member(GROUP_CHAT_ID, uid)).user.mention_html()
        except:
            mention = str(uid)
        h,m = divmod(mins, 60)
        early_lines.append(f"{i}. {mention} — ⏰ <b>{h:02d}:{m:02d}</b>")
    early_text = "🌅 <b>Earliest Risers (avg)</b>\n\n" + ("\n".join(early_lines) if early_lines else "No wake-up data yet.")

    await safe_send(context.bot, GROUP_CHAT_ID, f"{streak_text}\n\n{early_text}", parse_mode=ParseMode.HTML)

async def send_bedtime_reminder(context: ContextTypes.DEFAULT_TYPE):
    tags = await generate_mentions(context)

    text = (
        "🌙 <b>Bedtime Reminder</b>\n"
        "Wind down and protect your streak!\n\n"
        f"{tags}"
    )

    await safe_send(
        context.bot,
        GROUP_CHAT_ID,
        text,
        parse_mode=ParseMode.HTML
    )


async def send_weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    end = datetime.now(TZ).date()
    start = end - timedelta(days=6)
    async with db_pool.acquire() as conn:
        recs = await get_records_between(conn, GROUP_CHAT_ID, start.isoformat(), end.isoformat())
    if not recs:
        await safe_send(None, GROUP_CHAT_ID, "📈 Weekly summary: no activity in last 7 days.")
        return
    user_counts = defaultdict(int)
    user_times = defaultdict(list)
    for uid, d, hhmm in recs:
        user_counts[uid] += 1
        user_times[uid].append(hhmm)
    items = sorted(user_counts.items(), key=lambda x: x[1], reverse=True)
    lines = []
    for i, (uid, cnt) in enumerate(items[:10], 1):
        try:
            mention = (await context.bot.get_chat_member(GROUP_CHAT_ID, uid)).user.mention_html()
        except:
            mention = str(uid)
        avg = average_time_str(user_times[uid])
        lines.append(f"{i}. {mention} — {cnt} check-ins, avg wake {avg}")
    text = f"📅 <b>Weekly Summary</b> ({start} to {end})\n\n" + "\n".join(lines)
    await safe_send(context.bot, GROUP_CHAT_ID, text, parse_mode=ParseMode.HTML)

# ------------- Scheduler fallback if JobQueue missing -------------
async def fallback_daily_runner(func, hour, minute, ctx=None):
    """Runs daily fallback tasks when JobQueue is unavailable."""
    while True:
        try:
            now = datetime.now(TZ)
            target = datetime.combine(now.date(), time(hour, minute, tzinfo=TZ))

            if now > target:
                target = target + timedelta(days=1)

            wait_seconds = (target - now).total_seconds()
            print(f"⏳ Waiting {wait_seconds} seconds for {func.__name__}")

            await asyncio.sleep(wait_seconds)
            await func(ctx)

        except Exception as e:
            print(f"❌ Error in fallback_daily_runner for {func.__name__}: {e}")

        # After running, schedule next day
        await asyncio.sleep(1)


# ------------- Global error handler -------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("❌ Unhandled error:", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ An error occurred. The admin has been notified.")
    except Exception:
        pass


async def fallback_hourly_runner(func,ctx=None):
    while True:
        try: await func(ctx) if ctx else await func(None)
        except Exception as e: print("❌ hourly fallback error:",e)
        await asyncio.sleep(3600)

# ------------- App setup & main -------------
async def main():
    await init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("setstreak", setstreak_cmd))
    app.add_handler(CommandHandler("mystats", mystats_cmd))
    app.add_handler(CommandHandler("leaderboard", lambda u,c: asyncio.create_task(send_leaderboard(c))))
    app.add_handler(CommandHandler("checkin", checkin_handler))
    # Also accept plain "checkin" without slash (privacy may block plain messages unless disabled)
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^checkin($|\s+)'), checkin_handler))
    app.add_handler(CommandHandler("motivate", motivate_command))

    app.add_error_handler(error_handler)

    # Attempt to use JobQueue; if not present, fall back to our scheduler
    jq = app.job_queue
    if jq:
        try:
            jq.run_daily(lambda ctx: asyncio.create_task(send_leaderboard(ctx)), time=time(LEADERBOARD_HOUR,0,tzinfo=TZ))
            jq.run_daily(lambda ctx: asyncio.create_task(send_bedtime_reminder(ctx)), time=time(BEDTIME_HOUR, BEDTIME_MINUTE,tzinfo=TZ))
            jq.run_daily(lambda ctx: asyncio.create_task(send_weekly_summary(ctx)), time=time(WEEKLY_SUMMARY_HOUR,0,tzinfo=TZ))
            jq.run_repeating(lambda c: asyncio.create_task(send_motivation(c)), interval=3600, first=0)
            print("✅ JobQueue scheduled tasks registered.")
        except Exception as e:
            print("⚠️ JobQueue scheduling failed, falling back:", e)
            # fallback below
            jq = None

    if not jq:
        # schedule fallback tasks using create_task
        asyncio.create_task(fallback_daily_runner(send_leaderboard, LEADERBOARD_HOUR, 0, ctx=None))
        asyncio.create_task(fallback_daily_runner(send_bedtime_reminder, BEDTIME_HOUR,  BEDTIME_MINUTE, ctx=None))
        asyncio.create_task(fallback_daily_runner(send_weekly_summary, WEEKLY_SUMMARY_HOUR, 0, ctx=None))
        asyncio.create_task(fallback_hourly_runner(send_motivation))
        if RENDER_EXTERNAL_URL: asyncio.create_task(self_ping_task())
        print("ℹ️ Fallback scheduler running for daily jobs.")

    # start keep-alive self-ping if configured
    asyncio.create_task(self_ping_task())

    print("🚀 Bot starting (polling)...")
    await app.run_polling(drop_pending_updates=True)

# ------------- ENTRY POINT (Render + Local friendly) -------------


# Combined Render-friendly entry with periodic server pings

if __name__ == "__main__":
    import threading, asyncio
    import time as time_module
    from server import run as start_server

    # Start Flask server in a separate thread
    threading.Thread(target=start_server, daemon=True).start()

    # Keep server alive by pinging every 3 minutes
    def keep_alive():
        import requests
        while True:
            try:
                requests.get("http://localhost:10000")  # adjust port if needed
            except Exception:
                pass
            time_module.sleep(180)  # 3 minutes

    threading.Thread(target=keep_alive, daemon=True).start()

    # Start Telegram bot
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except:
        pass

    loop = asyncio.get_event_loop()
    loop.create_task(main())
    loop.run_forever()
