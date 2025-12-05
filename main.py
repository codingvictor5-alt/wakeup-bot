#!/usr/bin/env python3
"""
Group-only Wakeup Bot (Render-ready, python-telegram-bot v20+)
Requires: python-telegram-bot>=20.0, aiosqlite
"""

import os
import asyncio
from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo
import statistics
import aiosqlite
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

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GROUP_CHAT_ID_RAW = os.environ.get("GROUP_ID", "").strip()
if not BOT_TOKEN:
    raise SystemExit("Error: BOT_TOKEN env var required.")
if not GROUP_CHAT_ID_RAW:
    raise SystemExit("Error: GROUP_ID env var required.")
try:
    GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW)
except:
    raise SystemExit("Error: GROUP_ID must be integer, e.g., -1001234567890")

DB_FILE = os.environ.get("DB_FILE", "wakeup_bot.db")
TZ = ZoneInfo("Asia/Kolkata")
LEADERBOARD_HOUR = int(os.environ.get("LEADERBOARD_HOUR", 5))
BEDTIME_HOUR = int(os.environ.get("BEDTIME_HOUR", 21))
WEEKLY_SUMMARY_DAY = int(os.environ.get("WEEKLY_SUMMARY_DAY", 6))
WEEKLY_SUMMARY_HOUR = int(os.environ.get("WEEKLY_SUMMARY_HOUR", 6))
LEADERBOARD_TOP = int(os.environ.get("LEADERBOARD_TOP", 5))
BADGE_THRESHOLDS = [3, 7, 21]  # Bronze, Silver, Gold

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER,
    user_id INTEGER,
    streak INTEGER DEFAULT 0,
    last_checkin TEXT,
    last_time TEXT,
    badge TEXT,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS records (
    chat_id INTEGER,
    user_id INTEGER,
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
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()

async def ensure_user_row(db, chat_id: int, user_id: int):
    await db.execute(
        "INSERT OR IGNORE INTO users(chat_id, user_id, streak, last_checkin, last_time, badge) VALUES (?, ?, 0, '', '', '')",
        (chat_id, user_id)
    )

async def get_user(db, chat_id: int, user_id: int):
    cur = await db.execute("SELECT streak, last_checkin, last_time, badge FROM users WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    return await cur.fetchone()

async def set_user_fields(db, chat_id:int, user_id:int, streak:int=None, last_checkin:str=None, last_time:str=None, badge:Optional[str]=None):
    parts, vals = [], []
    if streak is not None: parts.append("streak=?"); vals.append(streak)
    if last_checkin is not None: parts.append("last_checkin=?"); vals.append(last_checkin)
    if last_time is not None: parts.append("last_time=?"); vals.append(last_time)
    if badge is not None: parts.append("badge=?"); vals.append(badge)
    if not parts: return
    vals.extend([chat_id, user_id])
    await db.execute(f"UPDATE users SET {', '.join(parts)} WHERE chat_id=? AND user_id=?", vals)

async def add_record(db, chat_id:int, user_id:int, iso_date:str, hhmm:str):
    await db.execute("INSERT INTO records(chat_id, user_id, date, time) VALUES (?, ?, ?, ?)", (chat_id, user_id, iso_date, hhmm))

async def user_checked_today(db, chat_id:int, user_id:int, iso_date:str) -> bool:
    cur = await db.execute("SELECT 1 FROM records WHERE chat_id=? AND user_id=? AND date=? LIMIT 1", (chat_id, user_id, iso_date))
    return await cur.fetchone() is not None

async def get_user_records(db, chat_id:int, user_id:int):
    cur = await db.execute("SELECT time FROM records WHERE chat_id=? AND user_id=? ORDER BY date", (chat_id, user_id))
    rows = await cur.fetchall()
    return [r[0] for r in rows]

async def get_top_streaks(db, chat_id:int, top:int=5) -> List[Tuple[int,int,str]]:
    cur = await db.execute("SELECT user_id, streak, badge FROM users WHERE chat_id=? ORDER BY streak DESC LIMIT ?", (chat_id, top))
    return await cur.fetchall()

async def get_records_between(db, chat_id:int, start_date:str, end_date:str):
    cur = await db.execute("SELECT user_id, date, time FROM records WHERE chat_id=? AND date BETWEEN ? AND ?", (chat_id, start_date, end_date))
    return await cur.fetchall()

# ---------------- Handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "group":
        await update.message.reply_text("This bot works only in the configured group.")
        return
    await update.message.reply_text(
        "👋 Wakeup Bot ready!\nCommands:\ncheckin, /reset, /setstreak <n>, /mystats, /leaderboard"
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID: return
    user = update.effective_user
    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_user_row(db, GROUP_CHAT_ID, user.id)
        await set_user_fields(db, GROUP_CHAT_ID, user.id, streak=0, last_checkin=date.today().isoformat(), last_time="reset", badge="")
        await db.commit()
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
    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_user_row(db, GROUP_CHAT_ID, user.id)
        await set_user_fields(db, GROUP_CHAT_ID, user.id, streak=new, badge=badge)
        await db.commit()
    await update.message.reply_text(f"✅ {user.mention_html()} streak set to {new}. Badge: {badge or 'None'}.", parse_mode=ParseMode.HTML)

async def mystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID: return
    user = update.effective_user
    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_user_row(db, GROUP_CHAT_ID, user.id)
        row = await get_user(db, GROUP_CHAT_ID, user.id)
        recs = await get_user_records(db, GROUP_CHAT_ID, user.id)
    if not row:
        await update.message.reply_text("No stats found. Do your first checkin!")
        return
    streak, last_checkin, last_time, badge = row
    avg = average_time_str(recs)
    await update.message.reply_text(
        f"📊 {user.mention_html()}'s stats:\n• Current streak: <b>{streak}</b>\n• Last check-in: <b>{last_checkin}</b>\n• Last wake-up: <b>{last_time}</b>\n• Avg wake-up: <b>{avg}</b>\n• Badge: <b>{badge or 'None'}</b>",
        parse_mode=ParseMode.HTML
    )

# ---------------- Checkin -----------------
async def checkin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.chat.id != GROUP_CHAT_ID: return
    user = update.effective_user
    parts = msg.text.strip().split()
    today_iso = datetime.now(TZ).date().isoformat()

    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_user_row(db, GROUP_CHAT_ID, user.id)
        if await user_checked_today(db, GROUP_CHAT_ID, user.id, today_iso):
            await msg.reply_text(f"⛔ {user.mention_html()} already checked in today.", parse_mode=ParseMode.HTML)
            return
        t = parse_time_string(parts[1]) if len(parts) > 1 else datetime.now(TZ).time()
        if not t:
            await msg.reply_text("⛔ Invalid time format.", parse_mode=ParseMode.HTML)
            return
        hhmm = t.strftime("%H:%M")
        if not valid_wakeup(t):
            await add_record(db, GROUP_CHAT_ID, user.id, today_iso, hhmm)
            await set_user_fields(db, GROUP_CHAT_ID, user.id, streak=0, last_checkin=today_iso, last_time=hhmm, badge="")
            await db.commit()
            await msg.reply_text(f"⚠️ {user.mention_html()} — wake-up {hhmm} outside 04:00–07:00. Streak reset to 0.", parse_mode=ParseMode.HTML)
            return
        row = await get_user(db, GROUP_CHAT_ID, user.id)
        prev_streak = row[0] if row else 0
        new_streak = prev_streak + 1
        badge = badge_for_streak(new_streak) or ""
        await add_record(db, GROUP_CHAT_ID, user.id, today_iso, hhmm)
        await set_user_fields(db, GROUP_CHAT_ID, user.id, streak=new_streak, last_checkin=today_iso, last_time=hhmm, badge=badge)
        await db.commit()
        await msg.reply_text(f"🔥 {user.mention_html()} — streak now <b>{new_streak}</b>, wake-up: <b>{hhmm}</b>, badge: <b>{badge or 'None'}</b>", parse_mode=ParseMode.HTML)

# ---------------- Scheduler Tasks -----------------
async def send_leaderboard(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_FILE) as db:
        rows = await get_top_streaks(db, GROUP_CHAT_ID, LEADERBOARD_TOP)
    if not rows:
        await context.bot.send_message(GROUP_CHAT_ID, "🏆 No leaderboard data yet.")
        return
    lines = []
    for i, (uid, streak, badge) in enumerate(rows, 1):
        try: mention = (await context.bot.get_chat_member(GROUP_CHAT_ID, uid)).user.mention_html()
        except: mention = str(uid)
        b = f" ({badge})" if badge else ""
        lines.append(f"{i}. {mention} — <b>{streak}</b> days{b}")
    text = "🏆 <b>Daily Leaderboard</b>\n\n" + "\n".join(lines)
    await context.bot.send_message(GROUP_CHAT_ID, text, parse_mode=ParseMode.HTML)

async def send_bedtime_reminder(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(GROUP_CHAT_ID, "🌙 <b>Bedtime Reminder</b>\nSleep early and protect your streak!", parse_mode=ParseMode.HTML)

async def send_weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    end = datetime.now(TZ).date()
    start = end - timedelta(days=6)
    async with aiosqlite.connect(DB_FILE) as db:
        recs = await get_records_between(db, GROUP_CHAT_ID, start.isoformat(), end.isoformat())
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
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^checkin($|\s+)'), checkin_handler))

    jq = app.job_queue
    jq.run_daily(lambda c: asyncio.create_task(send_leaderboard(c)), time=time(LEADERBOARD_HOUR,0,tzinfo=TZ))
    jq.run_daily(lambda c: asyncio.create_task(send_bedtime_reminder(c)), time=time(BEDTIME_HOUR,0,tzinfo=TZ))
    jq.run_daily(lambda c: asyncio.create_task(send_weekly_summary(c)), time=time(WEEKLY_SUMMARY_HOUR,0,tzinfo=TZ))

    print("Bot starting...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
