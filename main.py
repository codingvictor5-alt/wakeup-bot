#!/usr/bin/env python3
"""
Group-only Wakeup Bot
Deploy-ready for GitHub + Render
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

# ---------- CONFIG via ENV ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GROUP_CHAT_ID_RAW = os.environ.get("GROUP_ID", "").strip()

if not BOT_TOKEN:
    raise SystemExit("Error: BOT_TOKEN env var missing.")
if not GROUP_CHAT_ID_RAW:
    raise SystemExit("Error: GROUP_ID env var missing.")

try:
    GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW)
except:
    raise SystemExit("GROUP_ID must be -100... integer")

DB_FILE = os.environ.get("DB_FILE", "wakeup_bot.db")
TZ = ZoneInfo("Asia/Kolkata")

LEADERBOARD_HOUR = int(os.environ.get("LEADERBOARD_HOUR", 5))
BEDTIME_HOUR = int(os.environ.get("BEDTIME_HOUR", 21))
WEEKLY_SUMMARY_DAY = int(os.environ.get("WEEKLY_SUMMARY_DAY", 6))  # Sunday
WEEKLY_SUMMARY_HOUR = int(os.environ.get("WEEKLY_SUMMARY_HOUR", 6))
LEADERBOARD_TOP = int(os.environ.get("LEADERBOARD_TOP", 5))

BADGE_THRESHOLDS = [3, 7, 21]  # Bronze, Silver, Gold
# ------------------------------------

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
"""

# ---------------- Utilities ----------------
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
        return time(h, 0)
    except:
        return None

def valid_wakeup(t: time) -> bool:
    return time(4, 0) <= t <= time(7, 0)

def average_time_str(times: List[str]) -> str:
    if not times:
        return "N/A"
    mins = [int(t.split(":")[0]) * 60 + int(t.split(":")[1]) for t in times]
    avg = int(statistics.mean(mins))
    return f"{avg//60:02d}:{avg%60:02d}"

def badge_for_streak(streak: int) -> Optional[str]:
    if streak >= 21:
        return "Gold"
    if streak >= 7:
        return "Silver"
    if streak >= 3:
        return "Bronze"
    return None

# ---------------- DB helpers -----------------
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()

async def ensure_user_row(db, chat_id: int, user_id: int):
    await db.execute(
        "INSERT OR IGNORE INTO users(chat_id, user_id, streak, last_checkin, last_time, badge) "
        "VALUES (?, ?, 0, '', '', '')",
        (chat_id, user_id),
    )

async def get_user(db, chat_id: int, user_id: int):
    cur = await db.execute(
        "SELECT streak, last_checkin, last_time, badge FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    return await cur.fetchone()

async def set_user_fields(db, chat_id: int, user_id: int, **fields):
    parts = []
    vals = []
    for k, v in fields.items():
        if v is not None:
            parts.append(f"{k}=?")
            vals.append(v)
    if not parts:
        return
    vals.extend([chat_id, user_id])
    await db.execute(f"UPDATE users SET {', '.join(parts)} WHERE chat_id=? AND user_id=?", vals)

async def add_record(db, chat_id: int, user_id: int, iso_date: str, hhmm: str):
    await db.execute(
        "INSERT INTO records(chat_id, user_id, date, time) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, iso_date, hhmm),
    )

async def user_checked_today(db, chat_id: int, user_id: int, iso_date: str):
    cur = await db.execute(
        "SELECT 1 FROM records WHERE chat_id=? AND user_id=? AND date=? LIMIT 1",
        (chat_id, user_id, iso_date),
    )
    return await cur.fetchone() is not None

async def get_user_records(db, chat_id: int, user_id: int):
    cur = await db.execute(
        "SELECT time FROM records WHERE chat_id=? AND user_id=? ORDER BY date",
        (chat_id, user_id),
    )
    rows = await cur.fetchall()
    return [r[0] for r in rows]

async def get_top_streaks(db, chat_id: int, top: int):
    cur = await db.execute(
        "SELECT user_id, streak, badge FROM users WHERE chat_id=? ORDER BY streak DESC LIMIT ?",
        (chat_id, top),
    )
    return await cur.fetchall()

async def get_records_between(db, chat_id: int, start_date: str, end_date: str):
    cur = await db.execute(
        "SELECT user_id, date, time FROM records WHERE chat_id=? AND date BETWEEN ? AND ?",
        (chat_id, start_date, end_date),
    )
    return await cur.fetchall()

# --------------- Bot Handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Wakeup Group Bot Ready!\n\n"
        "Commands:\n"
        "• checkin → mark using current time\n"
        "• checkin 5:30 → custom wake time\n"
        "/reset — reset your streak\n"
        "/setstreak N — manually set streak\n"
        "/mystats — your stats\n"
        "/leaderboard — show leaderboard\n\n"
        "Daily: 5 AM leaderboard, 9 PM bedtime reminder.\n"
    )

async def reset_cmd(update, context):
    user = update.effective_user
    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_user_row(db, GROUP_CHAT_ID, user.id)
        await set_user_fields(db, GROUP_CHAT_ID, user.id,
                              streak=0, last_checkin=date.today().isoformat(),
                              last_time="reset", badge="")
        await db.commit()
    await update.message.reply_text(
        f"🔁 {user.mention_html()} — streak reset.", parse_mode="HTML"
    )

async def setstreak_cmd(update, context):
    user = update.effective_user
    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        return await update.message.reply_text("Usage: /setstreak 8")

    new = int(args[0])
    badge = badge_for_streak(new) or ""
    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_user_row(db, GROUP_CHAT_ID, user.id)
        await set_user_fields(db, GROUP_CHAT_ID, user.id, streak=new, badge=badge)
        await db.commit()

    await update.message.reply_text(
        f"✅ Streak set to {new}. Badge: {badge or 'None'}.",
        parse_mode="HTML",
    )

async def mystats_cmd(update, context):
    user = update.effective_user
    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_user_row(db, GROUP_CHAT_ID, user.id)
        row = await get_user(db, GROUP_CHAT_ID, user.id)
        recs = await get_user_records(db, GROUP_CHAT_ID, user.id)

    streak, last_checkin, last_time, badge = row
    avg = average_time_str(recs)

    await update.message.reply_text(
        f"📊 Stats:\n"
        f"• Streak: {streak}\n"
        f"• Last check-in: {last_checkin}\n"
        f"• Last wake: {last_time}\n"
        f"• Avg wake: {avg}\n"
        f"• Badge: {badge or 'None'}",
        parse_mode="HTML",
    )

async def leaderboard_cmd(update, context):
    await send_leaderboard(context)

async def checkin_handler(update, context):
    msg = update.message
    user = update.effective_user
    text = msg.text.strip()
    parts = text.split()
    today = datetime.now(TZ).date().isoformat()

    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_user_row(db, GROUP_CHAT_ID, user.id)
        if await user_checked_today(db, GROUP_CHAT_ID, user.id, today):
            return await msg.reply_text("⛔ Already checked in today.")

        if len(parts) >= 2:
            t = parse_time_string(parts[1])
            if not t:
                return await msg.reply_text("Invalid time. Use: checkin 5:30")
        else:
            t = datetime.now(TZ).time()

        hhmm = t.strftime("%H:%M")

        if not valid_wakeup(t):
            await add_record(db, GROUP_CHAT_ID, user.id, today, hhmm)
            await set_user_fields(db, GROUP_CHAT_ID, user.id,
                                  streak=0, last_checkin=today,
                                  last_time=hhmm, badge="")
            await db.commit()
            return await msg.reply_text(
                f"⚠️ Wake {hhmm} outside 4–7 AM. Streak reset."
            )

        row = await get_user(db, GROUP_CHAT_ID, user.id)
        prev = row[0]
        new = prev + 1
        b = badge_for_streak(new) or ""

        await add_record(db, GROUP_CHAT_ID, user.id, today, hhmm)
        await set_user_fields(db, GROUP_CHAT_ID, user.id,
                              streak=new, last_checkin=today,
                              last_time=hhmm, badge=b)
        await db.commit()

        await msg.reply_text(
            f"🔥 Streak {new}! Wake: {hhmm}. Badge: {b or 'None'}"
        )

# ------------ Scheduled Jobs -------------
async def send_leaderboard(context):
    async with aiosqlite.connect(DB_FILE) as db:
        rows = await get_top_streaks(db, GROUP_CHAT_ID, LEADERBOARD_TOP)

    if not rows:
        return await context.bot.send_message(
            GROUP_CHAT_ID, "No leaderboard yet."
        )

    lines = []
    rank = 1
    for uid, streak, badge in rows:
        try:
            member = await context.bot.get_chat_member(GROUP_CHAT_ID, uid)
            name = member.user.mention_html()
        except:
            name = str(uid)

        lines.append(
            f"{rank}. {name} — {streak} days ({badge or 'None'})"
        )
        rank += 1

    sent = await context.bot.send_message(
        GROUP_CHAT_ID,
        "🏆 <b>Leaderboard</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )

    try:
        await context.bot.pin_chat_message(
            GROUP_CHAT_ID, sent.message_id, disable_notification=True
        )
    except:
        pass

async def bedtime_reminder(context):
    await context.bot.send_message(
        GROUP_CHAT_ID,
        "🌙 Sleep early, champions.\nTomorrow’s streak depends on tonight’s discipline.",
        parse_mode="HTML",
    )

async def weekly_summary(context):
    end = datetime.now(TZ).date()
    start = end - timedelta(days=6)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    async with aiosqlite.connect(DB_FILE) as db:
        recs = await get_records_between(
            db, GROUP_CHAT_ID, start_iso, end_iso
        )

    from collections import defaultdict
    count = defaultdict(int)
    avg_times = defaultdict(list)

    for uid, d, t in recs:
        count[uid] += 1
        avg_times[uid].append(t)

    if not count:
        return

    items = sorted(count.items(), key=lambda x: x[1], reverse=True)
    lines = []
    rank = 1
    for uid, c in items[:10]:
        avg = average_time_str(avg_times[uid])
        try:
            member = await context.bot.get_chat_member(GROUP_CHAT_ID, uid)
            name = member.user.mention_html()
        except:
            name = str(uid)

        lines.append(f"{rank}. {name} — {c} check-ins, avg {avg}")
        rank += 1

    await context.bot.send_message(
        GROUP_CHAT_ID,
        f"📅 <b>Weekly Summary</b>\n({start_iso} to {end_iso})\n\n"
        + "\n".join(lines),
        parse_mode="HTML",
    )

# --------------- Boot ---------------
async def main():
    await init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("setstreak", setstreak_cmd))
    app.add_handler(CommandHandler("mystats", mystats_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^checkin"), checkin_handler))

    jq = app.job_queue

    jq.run_daily(
        lambda c: asyncio.create_task(send_leaderboard(c)),
        time=time(LEADERBOARD_HOUR, 0, tzinfo=TZ),
    )

    jq.run_daily(
        lambda c: asyncio.create_task(bedtime_reminder(c)),
        time=time(BEDTIME_HOUR, 0, tzinfo=TZ),
    )

    jq.run_daily(
        lambda c: asyncio.create_task(weekly_summary(c)),
        time=time(WEEKLY_SUMMARY_HOUR, 0, tzinfo=TZ),
    )

    print("Bot running on Render...")
    app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
