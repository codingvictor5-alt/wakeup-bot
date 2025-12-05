#!/usr/bin/env python3
"""
Group-only Wakeup Bot (GitHub + Render ready)
Requires: python-telegram-bot v20+, aiosqlite
Reads BOT_TOKEN and GROUP_ID from environment variables.
"""

import os
import asyncio
from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo
import statistics
import aiosqlite
from typing import Optional, List, Tuple

from telegram import Update, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ---------- CONFIG from env ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GROUP_CHAT_ID_RAW = os.environ.get("GROUP_ID", "").strip()  # must be set
if not BOT_TOKEN:
    raise SystemExit("Error: BOT_TOKEN environment variable is required.")
if not GROUP_CHAT_ID_RAW:
    raise SystemExit("Error: GROUP_ID environment variable is required. Use -100... group id.")
try:
    GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW)
except:
    raise SystemExit("Error: GROUP_ID must be an integer (example: -1001234567890).")

DB_FILE = os.environ.get("DB_FILE", "wakeup_bot.db")
TZ = ZoneInfo("Asia/Kolkata")
LEADERBOARD_HOUR = int(os.environ.get("LEADERBOARD_HOUR", 5))    # 05:00
BEDTIME_HOUR = int(os.environ.get("BEDTIME_HOUR", 21))         # 21:00
WEEKLY_SUMMARY_DAY = int(os.environ.get("WEEKLY_SUMMARY_DAY", 6))  # Sunday=6 (0=Monday)
WEEKLY_SUMMARY_HOUR = int(os.environ.get("WEEKLY_SUMMARY_HOUR", 6))
LEADERBOARD_TOP = int(os.environ.get("LEADERBOARD_TOP", 5))
BADGE_THRESHOLDS = [3, 7, 21]  # Bronze, Silver, Gold
# ----------------------------------------

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

# ---------------- Utilities ----------------
def parse_time_string(s: str) -> Optional[time]:
    s = s.replace(".", ":").strip()
    for fmt in ("%H:%M", "%I:%M", "%H"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.time()
        except Exception:
            continue
    try:
        h = int(s)
        if 0 <= h <= 23:
            return time(h, 0)
    except Exception:
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
    if streak >= BADGE_THRESHOLDS[2]:
        return "Gold"
    if streak >= BADGE_THRESHOLDS[1]:
        return "Silver"
    if streak >= BADGE_THRESHOLDS[0]:
        return "Bronze"
    return None

# ---------------- DB helpers -----------------
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()

async def ensure_user_row(db, chat_id: int, user_id: int):
    await db.execute(
        "INSERT OR IGNORE INTO users(chat_id, user_id, streak, last_checkin, last_time, badge) VALUES (?, ?, 0, '', '', '')",
        (chat_id, user_id),
    )

async def get_user(db, chat_id: int, user_id: int):
    cur = await db.execute(
        "SELECT streak, last_checkin, last_time, badge FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    row = await cur.fetchone()
    return row

async def set_user_fields(db, chat_id:int, user_id:int, streak:int=None, last_checkin:str=None, last_time:str=None, badge:Optional[str]=None):
    parts = []
    vals = []
    if streak is not None:
        parts.append("streak=?"); vals.append(streak)
    if last_checkin is not None:
        parts.append("last_checkin=?"); vals.append(last_checkin)
    if last_time is not None:
        parts.append("last_time=?"); vals.append(last_time)
    if badge is not None:
        parts.append("badge=?"); vals.append(badge)
    if not parts:
        return
    vals.extend([chat_id, user_id])
    await db.execute(f"UPDATE users SET {', '.join(parts)} WHERE chat_id=? AND user_id=?", vals)

async def add_record(db, chat_id:int, user_id:int, iso_date:str, hhmm:str):
    await db.execute("INSERT INTO records(chat_id, user_id, date, time) VALUES (?, ?, ?, ?)",
                     (chat_id, user_id, iso_date, hhmm))

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

# --------------- Bot Handlers -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("This bot works only in the configured group.")
        return
    await update.message.reply_text(
        "👋 Group Wakeup Bot ready!\n\n"
        "Plain-text commands (type in group):\n"
        "• checkin → uses current time\n"
        "• checkin 5:30 → specify wake time (5:30 or 5.30 allowed)\n\n"
        "Slash commands:\n"
        "/reset — reset your streak to 0\n"
        "/setstreak <n> — set your streak manually\n"
        "/mystats — view streak, last check-in, average wake-up\n"
        "/leaderboard — show current leaderboard (manual)\n\n"
        "Rules: one check-in per day. Wake time outside 04:00–07:00 (Asia/Kolkata) resets streak to 0.\n"
        "Daily: leaderboard at 05:00 (pinned). Bedtime reminder at 21:00. Weekly summary Sunday 06:00."
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        await update.message.reply_text("This bot is configured to operate only in the group.")
        return
    user = update.effective_user
    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_user_row(db, GROUP_CHAT_ID, user.id)
        await set_user_fields(db, GROUP_CHAT_ID, user.id, streak=0, last_checkin=date.today().isoformat(), last_time="reset", badge="")
        await db.commit()
    await update.message.reply_text(f"🔁 {user.mention_html()} — your streak has been reset to 0.", parse_mode=ParseMode.HTML)

async def setstreak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        await update.message.reply_text("This bot is configured to operate only in the group.")
        return
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
    await update.message.reply_text(f"✅ {user.mention_html()} — streak set to {new}. Badge: {badge or 'None'}.", parse_mode=ParseMode.HTML)

async def mystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        await update.message.reply_text("This bot is configured to operate only in the group.")
        return
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
        f"📊 {user.mention_html()}'s stats:\n"
        f"• Current streak: <b>{streak}</b>\n"
        f"• Last check-in date: <b>{last_checkin or 'N/A'}</b>\n"
        f"• Last wake-up time: <b>{last_time or 'N/A'}</b>\n"
        f"• Average wake-up time: <b>{avg}</b>\n"
        f"• Badge: <b>{badge or 'None'}</b>",
        parse_mode=ParseMode.HTML
    )

async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        await update.message.reply_text("This bot is configured to operate only in the group.")
        return
    await send_leaderboard_internal(context)

async def checkin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if chat.id != GROUP_CHAT_ID:
        await msg.reply_text("This bot operates only in the configured group.")
        return
    user = update.effective_user
    text = msg.text.strip()
    parts = text.split()
    today_iso = datetime.now(TZ).date().isoformat()

    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_user_row(db, GROUP_CHAT_ID, user.id)
        if await user_checked_today(db, GROUP_CHAT_ID, user.id, today_iso):
            await msg.reply_text(f"⛔ {user.mention_html()} — you already checked in today.", parse_mode=ParseMode.HTML)
            return

        if len(parts) >= 2:
            t = parse_time_string(parts[1])
            if not t:
                await msg.reply_text("⛔ Invalid time. Use `checkin 5:30` or `checkin 5.30`.", parse_mode=ParseMode.HTML)
                return
        else:
            t = datetime.now(TZ).time()

        hhmm = t.strftime("%H:%M")
        if not valid_wakeup(t):
            await add_record(db, GROUP_CHAT_ID, user.id, today_iso, hhmm)
            await set_user_fields(db, GROUP_CHAT_ID, user.id, streak=0, last_checkin=today_iso, last_time=hhmm, badge="")
            await db.commit()
            await msg.reply_text(
                f"⚠️ {user.mention_html()} — wake-up {hhmm} is outside 04:00–07:00. Your streak is reset to <b>0</b>.",
                parse_mode=ParseMode.HTML
            )
            return

        row = await get_user(db, GROUP_CHAT_ID, user.id)
        prev_streak = row[0] if row else 0
        new_streak = prev_streak + 1
        await add_record(db, GROUP_CHAT_ID, user.id, today_iso, hhmm)
        badge = badge_for_streak(new_streak) or ""
        await set_user_fields(db, GROUP_CHAT_ID, user.id, streak=new_streak, last_checkin=today_iso, last_time=hhmm, badge=badge)
        await db.commit()

        await msg.reply_text(
            f"🔥 {user.mention_html()} — great! Streak is now <b>{new_streak}</b>. Wake-up: <b>{hhmm}</b>. Badge: <b>{badge or 'None'}</b>.",
            parse_mode=ParseMode.HTML
        )

# Scheduled tasks
async def send_leaderboard_internal(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_FILE) as db:
        rows = await get_top_streaks(db, GROUP_CHAT_ID, LEADERBOARD_TOP)
    if not rows:
        try:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text="🏆 No data for leaderboard yet.")
        except Exception as e:
            print("Leaderboard send failed:", e)
        return

    lines = []
    rank = 1
    for (uid, streak, badge) in rows:
        try:
            member = await context.bot.get_chat_member(GROUP_CHAT_ID, uid)
            mention = member.user.mention_html()
        except Exception:
            mention = str(uid)
        b = f" ({badge})" if badge else ""
        lines.append(f"{rank}. {mention} — <b>{streak}</b> days{b}")
        rank += 1

    text = "🏆 <b>Daily Leaderboard</b>\n\n" + "\n".join(lines) + f"\n\n📅 {datetime.now(TZ).date().isoformat()}\nKeep the streaks alive! 🌅"
    try:
        sent = await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
        try:
            await context.bot.pin_chat_message(chat_id=GROUP_CHAT_ID, message_id=sent.message_id, disable_notification=True)
        except Exception as e:
            print("Pin failed:", e)
    except Exception as e:
        print("Failed to send leaderboard:", e)

async def send_bedtime_reminder(context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🌙 <b>Bedtime Reminder</b>\n\n"
        "Champions, rest fuels tomorrow's wins. Wind down, sleep early, and wake up ready to defend your streak. 🛌✨\n\n"
        "Good night — see you at dawn. 🌅"
    )
    try:
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print("Bedtime reminder failed:", e)

async def send_weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    end = datetime.now(TZ).date()
    start = end - timedelta(days=6)
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    async with aiosqlite.connect(DB_FILE) as db:
        recs = await get_records_between(db, GROUP_CHAT_ID, start_iso, end_iso)
        from collections import defaultdict
        user_counts = defaultdict(int)
        user_times = defaultdict(list)
        for uid, d, hhmm in recs:
            user_counts[uid] += 1
            user_times[uid].append(hhmm)
    if not user_counts:
        try:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text="📈 Weekly summary: no activity in the last 7 days.")
        except Exception as e:
            print("Weekly summary failed:", e)
        return

    items = sorted(user_counts.items(), key=lambda x: x[1], reverse=True)
    lines = []
    rank = 1
    for uid, cnt in items[:10]:
        try:
            member = await context.bot.get_chat_member(GROUP_CHAT_ID, uid)
            mention = member.user.mention_html()
        except Exception:
            mention = str(uid)
        avg = average_time_str(user_times[uid])
        lines.append(f"{rank}. {mention} — {cnt} check-ins, avg wake {avg}")
        rank += 1

    text = (
        f"📅 <b>Weekly Summary</b> ({start_iso} to {end_iso})\n\n"
        + "\n".join(lines)
        + "\n\nKeep up the discipline — next week, aim higher! 💪"
    )
    try:
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print("Weekly summary send failed:", e)

# Boot & scheduler
async def main():
    await init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("setstreak", setstreak_cmd))
    app.add_handler(CommandHandler("mystats", mystats_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^checkin($|\s+)'), checkin_handler))

    jq = app.job_queue
    jq.run_daily(lambda ctx: asyncio.create_task(send_leaderboard_internal(ctx)), time=time(LEADERBOARD_HOUR, 0, tzinfo=TZ))
    jq.run_daily(lambda ctx: asyncio.create_task(send_bedtime_reminder(ctx)), time=time(BEDTIME_HOUR, 0, tzinfo=TZ))
    def weekly_sched(dt):
        async def wrapper(ctx):
            if datetime.now(TZ).weekday() == WEEKLY_SUMMARY_DAY:
                await send_weekly_summary(ctx)
        return wrapper
    jq.run_daily(lambda ctx: asyncio.create_task(weekly_sched(datetime.now(TZ))(ctx)), time=time(WEEKLY_SUMMARY_HOUR, 0, tzinfo=TZ))

    print("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
