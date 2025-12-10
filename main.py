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
from telegram import Poll, Update
from telegram import PollAnswer
from telegram.ext import ContextTypes
from morningtag import tag_all_users
from telegram.ext import PollAnswerHandler


from dotenv import load_dotenv
from motivate import send_motivation,motivate_command
from tagger import generate_mentions
load_dotenv()

# Third-party DB + HTTP
import asyncpg
import aiohttp

# Telegram

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
BEDTIME_MINUTE = int(os.environ.get("BEDTIME_MINUTE", 0))
WEEKLY_SUMMARY_DAY = int(os.environ.get("WEEKLY_SUMMARY_DAY", 6))  # Sunday=6
WEEKLY_SUMMARY_HOUR = int(os.environ.get("WEEKLY_SUMMARY_HOUR", 6))
LEADERBOARD_TOP = int(os.environ.get("LEADERBOARD_TOP", 10))
BADGE_THRESHOLDS = [3, 7, 14, 21, 30, 50, 75, 100]
# Add this near the top with other globals
ACTIVE_POLL_CHAT = {}  # Store poll_id -> chat_id mapping

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
    time TEXT,
    source TEXT DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_records_chat_date ON records(chat_id, date);
"""

MIGRATION_SQL = """
-- Add source column if it doesn't exist
DO $ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name='records' AND column_name='source'
    ) THEN
        ALTER TABLE records ADD COLUMN source TEXT DEFAULT 'manual';
        UPDATE records SET source = 'manual' WHERE source IS NULL;
        RAISE NOTICE 'Added source column to records table';
    END IF;
END $;
"""

async def init_db():
    global db_pool
    print("🔄 Connecting to DB...")
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        # Create tables first
        await conn.execute(CREATE_TABLES_SQL)
        print("✅ Tables created/verified")
        
        # Check if source column exists
        check_column = await conn.fetchval(
            """SELECT COUNT(*) FROM information_schema.columns 
               WHERE table_name='records' AND column_name='source'"""
        )
        
        if check_column == 0:
            print("⚠️ Source column missing - adding it now...")
            await conn.execute("ALTER TABLE records ADD COLUMN source TEXT DEFAULT 'manual'")
            await conn.execute("UPDATE records SET source = 'manual' WHERE source IS NULL OR source = ''")
            print("✅ Migration completed - source column added and populated")
        else:
            print("✅ Source column already exists - no migration needed")
    
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

async def add_record(conn, chat_id:int, user_id:int, iso_date:str, hhmm:str, source:str='manual'):
    await conn.execute("INSERT INTO records(chat_id, user_id, date, time, source) VALUES ($1,$2,$3,$4,$5)", chat_id, user_id, iso_date, hhmm, source)

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
    """Calculate average time, filtering out non-time values like 'overslept', 'reset'"""
    if not times:
        return "N/A"
    
    valid_times = []
    for t in times:
        # Skip special markers
        if t in ['overslept', 'reset']:
            continue
        try:
            h, m = map(int, t.split(":"))
            valid_times.append(h * 60 + m)
        except:
            # Skip malformed times
            continue
    
    if not valid_times:
        return "N/A"
    
    avg = int(statistics.mean(valid_times))
    return f"{avg//60:02d}:{avg%60:02d}"

def badge_for_streak(streak:int) -> Optional[str]:
    """
    Award badges based on streak milestones:
    3+ days = Bronze
    7+ days = Silver  
    14+ days = Gold
    21+ days = Platinum
    30+ days = Diamond
    50+ days = Master
    75+ days = Grandmaster
    100+ days = Legend
    """
    if streak >= 100: return "Legend"
    if streak >= 75: return "Grandmaster"
    if streak >= 50: return "Master"
    if streak >= 30: return "Diamond"
    if streak >= 21: return "Platinum"
    if streak >= 14: return "Gold"
    if streak >= 7: return "Silver"
    if streak >= 3: return "Bronze"
    return None

# ------------- Safe send helper -------------
async def safe_send(bot, chat_id:int, *args, **kwargs):
    try:
        return await bot.send_message(chat_id, *args, **kwargs)
    except Exception as e:
        print("❌ send_message failed:", e)
        return None

# Add this function
async def cleanup_old_polls():
    """Remove poll mappings older than 24 hours"""
    ACTIVE_POLL_CHAT.clear()

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




async def send_wakeup_poll(context: ContextTypes.DEFAULT_TYPE):
    chat_id = GROUP_CHAT_ID  # Use the global GROUP_CHAT_ID
    bot = context.bot
    
    async with db_pool.acquire() as conn:
        # Generate tagging text
        tags_text = await tag_all_users(conn, bot, chat_id)

        poll_message = await bot.send_poll(
            chat_id=chat_id,
            question="🌅 Good morning! Did you wake up on time today between 4:00 AM and 7:00 AM?",
            options=["Yes, I woke up!", "No, I overslept 😴"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )

        # Store the poll_id -> chat_id mapping
        ACTIVE_POLL_CHAT[poll_message.poll.id] = chat_id

        # Pin poll
        try:
            await bot.pin_chat_message(
                chat_id=chat_id, 
                message_id=poll_message.message_id, 
                disable_notification=True
            )
        except Exception as e:
            print("Failed to pin poll:", e)

        # Send tagging message right after poll to alert everyone
        try:
            await bot.send_message(
                chat_id=chat_id, 
                text=f"{tags_text}\n\nPlease vote in the poll above!", 
                parse_mode="HTML"
            )
        except Exception as e:
            print("Failed to send tagging message:", e)


async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll answers and update streaks accordingly."""
    answer = update.poll_answer
    user_id = answer.user.id
    poll_id = answer.poll_id
    
    # Get the chat_id from our stored mapping
    chat_id = ACTIVE_POLL_CHAT.get(poll_id)
    
    if chat_id is None:
        print(f"⚠️ Poll answer received for unknown poll_id: {poll_id}")
        return
    
    # Determine if user voted "Yes" (first option)
    if not answer.option_ids:
        return
    
    option_index = answer.option_ids[0]
    voted_yes = (option_index == 0)
    
    today = datetime.now(TZ).date().isoformat()
    POLL_DEFAULT_TIME = "06:45"  # Default time for poll "Yes" votes
    
    async with db_pool.acquire() as conn:
        # Ensure user row exists
        await ensure_user_row(conn, chat_id, user_id)
        
        # Check if user already did MANUAL checkin today
        manual_checkin = await conn.fetchrow(
            "SELECT 1 FROM records WHERE chat_id=$1 AND user_id=$2 AND date=$3 AND source='manual' LIMIT 1",
            chat_id, user_id, today
        )
        
        if manual_checkin:
            # User already did manual checkin, ignore poll vote
            print(f"ℹ️ User {user_id} already did manual checkin today, ignoring poll")
            return
        
        # Check if already voted in poll today
        poll_vote_exists = await conn.fetchrow(
            "SELECT 1 FROM records WHERE chat_id=$1 AND user_id=$2 AND date=$3 AND source='poll' LIMIT 1",
            chat_id, user_id, today
        )
        
        if poll_vote_exists:
            # User already voted today, ignore duplicate votes
            print(f"ℹ️ User {user_id} already voted in poll today")
            return
        
        if voted_yes:
            # Get current streak
            row = await get_user(conn, chat_id, user_id)
            current_streak = row['streak'] if row else 0
            new_streak = current_streak + 1
            badge = badge_for_streak(new_streak) or ""
            
            # Update user stats with default poll time (6:45 AM)
            await set_user_fields(
                conn, chat_id, user_id,
                streak=new_streak,
                last_checkin=today,
                last_time=POLL_DEFAULT_TIME,
                badge=badge
            )
            
            # Add record with 6:45 AM time and source='poll'
            await add_record(conn, chat_id, user_id, today, POLL_DEFAULT_TIME, source='poll')
            
            # Send congratulations message
            try:
                user = await context.bot.get_chat_member(chat_id, user_id)
                mention = user.user.mention_html()
                
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🎉 {mention} woke up on time!\n"
                        f"🔥 Streak: <b>{new_streak}</b> days\n"
                        f"⏰ Time recorded: <b>{POLL_DEFAULT_TIME}</b>\n"
                        f"🏅 Badge: <b>{badge or 'None'}</b>\n\n"
                        f"💡 <i>You can still use /checkin to update your exact wake-up time!</i>"
                    ),
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                print(f"Failed to send success message: {e}")
                
        else:
            # User overslept - reset streak
            await set_user_fields(
                conn, chat_id, user_id,
                streak=0,
                last_checkin=today,
                last_time="overslept",
                badge=""
            )
            
            # Add oversleep record
            await add_record(conn, chat_id, user_id, today, "overslept", source='poll')
            
            # Send reset message
            try:
                user = await context.bot.get_chat_member(chat_id, user_id)
                mention = user.user.mention_html()
                
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"😴 {mention} overslept today. Streak reset to 0.",
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                print(f"Failed to send reset message: {e}")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID: return
    user = update.effective_user
    async with db_pool.acquire() as conn:
        await ensure_user_row(conn, GROUP_CHAT_ID, user.id)
        await set_user_fields(conn, GROUP_CHAT_ID, user.id, streak=0, last_checkin=date.today().isoformat(), last_time="reset", badge="")
        # Also add a record
        await add_record(conn, GROUP_CHAT_ID, user.id, date.today().isoformat(), "reset", source='manual')
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
    
    # Get earliest valid time (skip markers)
    valid_times = [t for t in recs if t not in ['overslept', 'reset']]
    earliest = min(valid_times) if valid_times else "N/A"
    
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
    try:
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
        
        print(f"📝 Checkin attempt: user={user.id}, time={hhmm}, date={today_iso}")

        async with db_pool.acquire() as conn:

            # Ensure user row exists
            await ensure_user_row(conn, GROUP_CHAT_ID, user.id)
            print(f"✅ User row ensured for {user.id}")

            # Check if already did MANUAL checkin today
            manual_checkin = await conn.fetchrow(
                "SELECT 1 FROM records WHERE chat_id=$1 AND user_id=$2 AND date=$3 AND source='manual' LIMIT 1",
                GROUP_CHAT_ID, user.id, today_iso
            )
            
            if manual_checkin:
                print(f"⚠️ User {user.id} already did manual checkin today")
                await msg.reply_text(
                    f"⛔ {user.mention_html()} you have already used your manual check-in for today.\n"
                    "You can check in only <b>once per day</b>.",
                    parse_mode=ParseMode.HTML
                )
                return

            # Delete any existing poll vote (we're replacing it with manual checkin)
            deleted = await conn.execute(
                "DELETE FROM records WHERE chat_id=$1 AND user_id=$2 AND date=$3 AND source='poll'",
                GROUP_CHAT_ID, user.id, today_iso
            )
            
            if deleted and "DELETE 0" not in str(deleted):
                print(f"✅ Deleted poll vote for user {user.id}")

            # If outside 4–7 AM → record + reset
            if not valid_wakeup(t):
                print(f"⚠️ Invalid wakeup time {hhmm} for user {user.id}")
                await add_record(conn, GROUP_CHAT_ID, user.id, today_iso, hhmm, source='manual')
                await set_user_fields(conn, GROUP_CHAT_ID , user.id,streak=0, last_checkin=today_iso,last_time=hhmm, badge="")

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

            print(f"✅ Valid checkin: user={user.id}, new_streak={new_streak}, badge={badge}")

            await add_record(conn, GROUP_CHAT_ID, user.id, today_iso, hhmm, source='manual')
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
            print(f"✅ Checkin complete for user {user.id}")
    except Exception as e:
        print(f"❌ CHECKIN ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        try:
            await msg.reply_text("⚠️ An error occurred during check-in. Please try again.")
        except:
            pass


# ------------- Leaderboards & summaries -------------
async def send_leaderboard(context: ContextTypes.DEFAULT_TYPE):
    """
    Send leaderboard with TOP 10 streaks and TOP 10 earliest wake-up times.
    """
    try:
        print("📊 Generating leaderboard...")
        
        # Get database connection
        async with db_pool.acquire() as conn:
            # Get top 10 streaks
            top_streaks = await conn.fetch(
                "SELECT user_id, streak, badge FROM users WHERE chat_id=$1 ORDER BY streak DESC LIMIT 10",
                GROUP_CHAT_ID
            )
            
            # Get all records with valid times
            all_records = await conn.fetch(
                "SELECT user_id, time FROM records WHERE chat_id=$1 AND time NOT IN ('overslept', 'reset')",
                GROUP_CHAT_ID
            )
        
        print(f"✅ Found {len(top_streaks)} users with streaks")
        print(f"✅ Found {len(all_records)} valid time records")
        
        # === TOP STREAKS SECTION ===
        streak_lines = []
        if top_streaks:
            for i, row in enumerate(top_streaks, 1):
                uid = row['user_id']
                streak = row['streak']
                badge = row['badge'] or ""
                
                try:
                    member = await context.bot.get_chat_member(GROUP_CHAT_ID, uid)
                    name = member.user.mention_html()
                except Exception as e:
                    print(f"⚠️ Could not fetch user {uid}: {e}")
                    name = f"User {uid}"
                
                badge_text = f" 🏅 {badge}" if badge else ""
                streak_lines.append(f"{i}. {name} — <b>{streak}</b> days{badge_text}")
        
        streak_section = "🏆 <b>TOP 10 STREAKS</b>\n\n" + (
            "\n".join(streak_lines) if streak_lines else "No streaks yet. Start checking in!"
        )
        
        # === EARLIEST RISERS SECTION ===
        # Calculate average wake-up time per user
        user_times = defaultdict(list)
        
        for record in all_records:
            uid = record['user_id']
            time_str = record['time']
            
            try:
                # Parse time and convert to minutes
                h, m = map(int, time_str.split(":"))
                if 0 <= h <= 23 and 0 <= m <= 59:
                    minutes = h * 60 + m
                    user_times[uid].append(minutes)
            except Exception as e:
                print(f"⚠️ Skipping invalid time '{time_str}': {e}")
                continue
        
        # Calculate averages
        user_averages = []
        for uid, times in user_times.items():
            if times:
                avg_minutes = sum(times) // len(times)
                user_averages.append((uid, avg_minutes))
        
        # Sort by earliest (lowest minutes) and take top 10
        user_averages.sort(key=lambda x: x[1])
        top_10_earliest = user_averages[:10]
        
        early_lines = []
        if top_10_earliest:
            for i, (uid, avg_mins) in enumerate(top_10_earliest, 1):
                try:
                    member = await context.bot.get_chat_member(GROUP_CHAT_ID, uid)
                    name = member.user.mention_html()
                except Exception as e:
                    print(f"⚠️ Could not fetch user {uid}: {e}")
                    name = f"User {uid}"
                
                h, m = divmod(avg_mins, 60)
                time_display = f"{h:02d}:{m:02d}"
                early_lines.append(f"{i}. {name} — ⏰ <b>{time_display}</b>")
        
        early_section = "🌅 <b>TOP 10 EARLIEST RISERS</b> (avg)\n\n" + (
            "\n".join(early_lines) if early_lines else "No wake-up data yet."
        )
        
        # === SEND MESSAGE ===
        final_message = f"{streak_section}\n\n{early_section}"
        
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=final_message,
            parse_mode=ParseMode.HTML
        )
        
        print("✅ Leaderboard sent successfully!")
        
    except Exception as e:
        print(f"❌ LEADERBOARD ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        
        try:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text="⚠️ Error generating leaderboard. Please try again later."
            )
        except:
            print("❌ Could not send error message to chat")

async def send_bedtime_reminder(context: ContextTypes.DEFAULT_TYPE):
    chat_id = GROUP_CHAT_ID  # Use the global GROUP_CHAT_ID
    bot = context.bot
    async with db_pool.acquire() as conn:
        # Generate tagging text
        tags_text = await tag_all_users(conn, bot, chat_id)
        text = (
        "🌙 <b>Bedtime Reminder</b>\n"
        "Wind down and protect your streak!\n\n"
        f"{tags_text}"
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
        await safe_send(context.bot, GROUP_CHAT_ID, "📈 Weekly summary: no activity in last 7 days.")
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
        try: 
            await func(ctx) if ctx else await func(None)
        except Exception as e: 
            print("❌ hourly fallback error:",e)
        await asyncio.sleep(9000)

# ------------- App setup & main -------------
async def main():
    await init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("setstreak", setstreak_cmd))
    app.add_handler(CommandHandler("mystats", mystats_cmd))
    app.add_handler(CommandHandler("leaderboard", lambda u, c: send_leaderboard(c)))
    app.add_handler(CommandHandler("checkin", checkin_handler))
    # Also accept plain "checkin" without slash (privacy may block plain messages unless disabled)
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^checkin($|\s+)'), checkin_handler))
    app.add_handler(CommandHandler("motivate", motivate_command))
    app.add_handler(PollAnswerHandler(poll_answer_handler))


    app.add_error_handler(error_handler)

    # Attempt to use JobQueue; if not present, fall back to our scheduler
    jq = app.job_queue
    if jq:
        try:
            jq.run_daily(send_leaderboard, time=time(LEADERBOARD_HOUR,0,tzinfo=TZ), chat_id=GROUP_CHAT_ID, name="daily_leaderboard")
            jq.run_daily(send_bedtime_reminder, time=time(BEDTIME_HOUR, BEDTIME_MINUTE,tzinfo=TZ), chat_id=GROUP_CHAT_ID, name="bedtime_reminder")
            jq.run_daily(send_weekly_summary, time=time(WEEKLY_SUMMARY_HOUR,0,tzinfo=TZ), chat_id=GROUP_CHAT_ID, name="weekly_summary")
            jq.run_daily(send_wakeup_poll, time=time(9,25, tzinfo=TZ), chat_id=GROUP_CHAT_ID, name="wakeup_poll")
            jq.run_repeating(send_motivation, interval=9000, first=0, chat_id=GROUP_CHAT_ID, name="hourly_motivation")
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
        asyncio.create_task(fallback_daily_runner(send_wakeup_poll, 9 ,25, ctx=None))
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
