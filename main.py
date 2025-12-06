#!/usr/bin/env python3
"""
Group-only Wakeup Bot (PostgreSQL-ready for Render)
Requires: python-telegram-bot>=20.0, asyncpg, python-dotenv
"""
from dotenv import load_dotenv
import os
import asyncio
from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo
import statistics
import asyncpg
from typing import Optional, List, Tuple
from collections import defaultdict

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

load_dotenv()  # This reads your .env file

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID_RAW = os.getenv("GROUP_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")  # NEW: PostgreSQL connection string

if not BOT_TOKEN:
    raise SystemExit("Error: BOT_TOKEN env var required.")
if not GROUP_CHAT_ID_RAW:
    raise SystemExit("Error: GROUP_CHAT_ID env var required.")
if not DATABASE_URL:
    raise SystemExit("Error: DATABASE_URL env var required.")

try:
    GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW)
except:
    raise SystemExit("Error: GROUP_CHAT_ID must be integer, e.g., -1001234567890")

TZ = ZoneInfo("Asia/Kolkata")
LEADERBOARD_HOUR = int(os.environ.get("LEADERBOARD_HOUR", 5))
BEDTIME_HOUR = int(os.environ.get("BEDTIME_HOUR", 21))
WEEKLY_SUMMARY_DAY = int(os.environ.get("WEEKLY_SUMMARY_DAY", 6))
WEEKLY_SUMMARY_HOUR = int(os.environ.get("WEEKLY_SUMMARY_HOUR", 6))
LEADERBOARD_TOP = int(os.environ.get("LEADERBOARD_TOP", 5))
BADGE_THRESHOLDS = [3, 7, 21]  # Bronze, Silver, Gold

# Global connection pool
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

# ----------------- Utilities -----------------
def parse_time_string(s: str) -> Optional[time]:
    s = s.replace(".", ":").strip()
    for fmt in ("%H:%M", "%I:%M", "%H"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.time()
        except:
            continue
    try:
        h = int(s)
        if 0 <= h <= 23:
            return time(h, 0)
    except:
        pass
    return None

def valid_wakeup(t: time) -> bool:
    return time(4, 0) <= t <= time(7, 0)

def average_time_str(times: List[str]) -> str:
    if not times:
        return "N/A"
    mins = []
    for t in times:
        h, m = map(int, t.split(":"))
        mins.append(h * 60 + m)
    avg = int(statistics.mean(mins))
    return f"{avg//60:02d}:{avg%60:02d}"

def badge_for_streak(streak: int) -> Optional[str]:
    if streak >= BADGE_THRESHOLDS[2]: return "Gold"
    if streak >= BADGE_THRESHOLDS[1]: return "Silver"
    if streak >= BADGE_THRESHOLDS[0]: return "Bronze"
    return None

# ---------------- DB -----------------
async def init_db():
    """Initialize PostgreSQL connection pool and create tables"""
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute(CREATE_TABLES_SQL)
    print("✅ Database connected and tables created!")

async def ensure_user_row(conn, chat_id: int, user_id: int):
    await conn.execute(
        """INSERT INTO users(chat_id, user_id, streak, last_checkin, last_time, badge) 
           VALUES ($1, $2, 0, '', '', '') ON CONFLICT DO NOTHING""",
        chat_id, user_id
    )

async def get_user(conn, chat_id: int, user_id: int):
    return await conn.fetchrow(
        "SELECT streak, last_checkin, last_time, badge FROM users WHERE chat_id=$1 AND user_id=$2",
        chat_id, user_id
    )

async def set_user_fields(conn, chat_id:int, user_id:int, streak:int=None, last_checkin:str=None, last_time:str=None, badge:Optional[str]=None):
    parts, vals = [], []
    idx = 1
    if streak is not None: 
        parts.append(f"streak=${idx}"); vals.append(streak); idx += 1
    if last_checkin is not None: 
        parts.append(f"last_checkin=${idx}"); vals.append(last_checkin); idx += 1
    if last_time is not None: 
        parts.append(f"last_time=${idx}"); vals.append(last_time); idx += 1
    if badge is not None: 
        parts.append(f"badge=${idx}"); vals.append(badge); idx += 1
    if not parts: return
    vals.extend([chat_id, user_id])
    await conn.execute(
        f"UPDATE users SET {', '.join(parts)} WHERE chat_id=${idx} AND user_id=${idx+1}",
        *vals
    )

async def add_record(conn, chat_id:int, user_id:int, iso_date:str, hhmm:str):
    await conn.execute(
        "INSERT INTO records(chat_id, user_id, date, time) VALUES ($1, $2, $3, $4)",
        chat_id, user_id, iso_date, hhmm
    )

async def user_checked_today(conn, chat_id:int, user_id:int, iso_date:str) -> bool:
    row = await conn.fetchrow(
        "SELECT 1 FROM records WHERE chat_id=$1 AND user_id=$2 AND date=$3 LIMIT 1",
        chat_id, user_id, iso_date
    )
    return row is not None

async def get_user_records(conn, chat_id:int, user_id:int):
    rows = await conn.fetch(
        "SELECT time FROM records WHERE chat_id=$1 AND user_id=$2 ORDER BY date",
        chat_id, user_id
    )
    return [r['time'] for r in rows]

async def get_top_streaks(conn, chat_id:int, top:int=5) -> List[Tuple[int,int,str]]:
    rows = await conn.fetch(
        "SELECT user_id, streak, badge FROM users WHERE chat_id=$1 ORDER BY streak DESC LIMIT $2",
        chat_id, top
    )
    return [(r['user_id'], r['streak'], r['badge']) for r in rows]

async def get_records_between(conn, chat_id:int, start_date:str, end_date:str):
    rows = await conn.fetch(
        "SELECT user_id, date, time FROM records WHERE chat_id=$1 AND date BETWEEN $2 AND $3",
        chat_id, start_date, end_date
    )
    return [(r['user_id'], r['date'], r['time']) for r in rows]

# ---------------- Handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        await update.message.reply_text("This bot works only in the configured group.")
        return
    await update.message.reply_text(
        "👋 Wakeup Bot ready!\nCommands:\ncheckin, /reset, /setstreak <n>, /mystats, /leaderboard"
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID: return
    user = update.effective_user
    async with db_pool.acquire() as conn:
        await ensure_user_row(conn, GROUP_CHAT_ID, user.id)
        await set_user_fields(conn, GROUP_CHAT_ID, user.id, streak=0, last_checkin=date.today().isoformat(), last_time="reset", badge="")
    await update.message.reply_text(f"🔁 {user.mention_html()} streak reset to 0.", parse_mode=ParseMode.HTML)

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
    await update.message.reply_text(f"✅ {user.mention_html()} streak set to {new}. Badge: {badge or 'None'}.", parse_mode=ParseMode.HTML)

async def mystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID: return
    user = update.effective_user
    async with db_pool.acquire() as conn:
        await ensure_user_row(conn, GROUP_CHAT_ID, user.id)
        row = await get_user(conn, GROUP_CHAT_ID, user.id)
        recs = await get_user_records(conn, GROUP_CHAT_ID, user.id)
        max_streak_row = await conn.fetchrow("SELECT MAX(streak) FROM users WHERE chat_id=$1", GROUP_CHAT_ID)
        max_streak = max_streak_row['max'] if max_streak_row and max_streak_row['max'] else 0
    if not row:
        await update.message.reply_text("No stats found. Do your first checkin!")
        return
    streak, last_checkin, last_time, badge = row['streak'], row['last_checkin'], row['last_time'], row['badge']
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

async def checkin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.chat.id != GROUP_CHAT_ID: return
    user = update.effective_user
    parts = msg.text.strip().split()
    today_iso = datetime.now(TZ).date().isoformat()

    async with db_pool.acquire() as conn:
        await ensure_user_row(conn, GROUP_CHAT_ID, user.id)
        if await user_checked_today(conn, GROUP_CHAT_ID, user.id, today_iso):
            await msg.reply_text(f"⛔ {user.mention_html()} already checked in today.", parse_mode=ParseMode.HTML)
            return
        t = parse_time_string(parts[1]) if len(parts) > 1 else datetime.now(TZ).time()
        if not t:
            await msg.reply_text("⛔ Invalid time format.", parse_mode=ParseMode.HTML)
            return
        hhmm = t.strftime("%H:%M")
        if not valid_wakeup(t):
            await add_record(conn, GROUP_CHAT_ID, user.id, today_iso, hhmm)
            await set_user_fields(conn, GROUP_CHAT_ID, user.id, streak=0, last_checkin=today_iso, last_time=hhmm, badge="")
            await msg.reply_text(f"⚠️ {user.mention_html()} — wake-up {hhmm} outside 04:00–07:00. Streak reset to 0.", parse_mode=ParseMode.HTML)
            return
        row = await get_user(conn, GROUP_CHAT_ID, user.id)
        prev_streak = row['streak'] if row else 0
        new_streak = prev_streak + 1
        badge = badge_for_streak(new_streak) or ""
        await add_record(conn, GROUP_CHAT_ID, user.id, today_iso, hhmm)
        await set_user_fields(conn, GROUP_CHAT_ID, user.id, streak=new_streak, last_checkin=today_iso, last_time=hhmm, badge=badge)
        await msg.reply_text(f"🔥 {user.mention_html()} — streak now <b>{new_streak}</b>, wake-up: <b>{hhmm}</b>, badge: <b>{badge or 'None'}</b>", parse_mode=ParseMode.HTML)

# ---------------- Scheduler Tasks -----------------
async def send_leaderboard(context: ContextTypes.DEFAULT_TYPE):
    async with db_pool.acquire() as conn:
        top_streaks = await get_top_streaks(conn, GROUP_CHAT_ID, LEADERBOARD_TOP)
        records = await conn.fetch("SELECT user_id, time FROM records WHERE chat_id=$1", GROUP_CHAT_ID)
    
    times_by_user = defaultdict(list)
    for rec in records:
        times_by_user[rec['user_id']].append(rec['time'])
    
    avg_times_by_user = {}
    for uid, times in times_by_user.items():
        mins = [int(h)*60 + int(m) for h, m in (map(int, x.split(":")) for x in times)]
        avg_mins = sum(mins)//len(mins)
        avg_times_by_user[uid] = avg_mins
    
    earliest_risers = sorted(avg_times_by_user.items(), key=lambda x: x[1])[:LEADERBOARD_TOP]

    streak_lines = []
    for i, (uid, streak, badge) in enumerate(top_streaks, 1):
        try:
            mention = (await context.bot.get_chat_member(GROUP_CHAT_ID, uid)).user.mention_html()
        except:
            mention = str(uid)
        b = f" ({badge})" if badge else ""
        streak_lines.append(f"{i}. {mention} — <b>{streak}</b> days{b}")
    streak_text = "🏆 <b>Top Streaks</b>\n\n" + "\n".join(streak_lines) if streak_lines else "No streak data yet."

    early_lines = []
    for i, (uid, mins) in enumerate(earliest_risers, 1):
        try:
            mention = (await context.bot.get_chat_member(GROUP_CHAT_ID, uid)).user.mention_html()
        except:
            mention = str(uid)
        h, m = divmod(mins, 60)
        early_lines.append(f"{i}. {mention} — ⏰ <b>{h:02d}:{m:02d}</b>")
    early_text = "🌅 <b>Earliest Risers</b>\n\n" + "\n".join(early_lines) if early_lines else "No wake-up data yet."

    await context.bot.send_message(GROUP_CHAT_ID, f"{streak_text}\n\n{early_text}", parse_mode=ParseMode.HTML)

async def send_bedtime_reminder(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(GROUP_CHAT_ID, "🌙 <b>Bedtime Reminder</b>\nSleep early and protect your streak!", parse_mode=ParseMode.HTML)

async def send_weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    end = datetime.now(TZ).date()
    start = end - timedelta(days=6)
    async with db_pool.acquire() as conn:
        recs = await get_records_between(conn, GROUP_CHAT_ID, start.isoformat(), end.isoformat())
    user_counts = defaultdict(int)
    user_times = defaultdict(list)
    for uid, d, hhmm in recs:
        user_counts[uid] += 1
        user_times[uid].append(hhmm)
    if not user_counts: return
    items = sorted(user_counts.items(), key=lambda x: x[1], reverse=True)
    lines = []
    for i, (uid, cnt) in enumerate(items[:10], 1):
        try: mention = (await context.bot.get_chat_member(GROUP_CHAT_ID, uid)).user.mention_html()
        except: mention = str(uid)
        avg = average_time_str(user_times[uid])
        lines.append(f"{i}. {mention} — {cnt} check-ins, avg wake {avg}")
    text = f"📅 <b>Weekly Summary</b> ({start} to {end})\n\n" + "\n".join(lines)
    await context.bot.send_message(GROUP_CHAT_ID, text, parse_mode=ParseMode.HTML)

# ---------------- Main -----------------
async def main():
    await init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("setstreak", setstreak_cmd))
    app.add_handler(CommandHandler("mystats", mystats_cmd))
    app.add_handler(CommandHandler("leaderboard", lambda u,c: asyncio.create_task(send_leaderboard(c))))
    app.add_handler(CommandHandler("checkin", checkin_handler))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^checkin($|\s+)'), checkin_handler))

    jq = app.job_queue
    jq.run_daily(lambda c: asyncio.create_task(send_leaderboard(c)), time=time(LEADERBOARD_HOUR,0,tzinfo=TZ))
    jq.run_daily(lambda c: asyncio.create_task(send_bedtime_reminder(c)), time=time(BEDTIME_HOUR,0,tzinfo=TZ))
    jq.run_daily(lambda c: asyncio.create_task(send_weekly_summary(c)), time=time(WEEKLY_SUMMARY_HOUR,0,tzinfo=TZ))

    print("Bot starting...")
    await app.run_polling()

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    loop = asyncio.get_event_loop()
    loop.create_task(main())
    loop.run_forever()
