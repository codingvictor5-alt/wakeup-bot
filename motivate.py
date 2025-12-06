# motivate.py

import os
import random
from datetime import datetime
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

# Load from .env
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))

# ---------------- QUOTES ---------------- #

QUOTES = [
    "Success is not final; failure is not fatal — it is the courage to continue that counts. — Winston Churchill",
    "Your future is created by what you do today, not tomorrow. — Robert Kiyosaki",
    "The secret of your success is found in your daily routine. — John C. Maxwell",
    "Dream big. Start small. Act now. — Robin Sharma",
    "Wake up early. Stay consistent. Win every day. — Jocko Willink",
    "Discipline equals freedom. — Jocko Willink",
    "If you want to be the best, you have to do things others aren’t willing to do. — Kobe Bryant",
    "Every morning you have two choices: continue to sleep with dreams or wake up and chase them. — Arnold Schwarzenegger",
    "Losers wait. Winners create. — Gary Vaynerchuk",
    "Small daily improvements lead to stunning long-term results. — James Clear"
]

# -----------------------------------------------------
# SAFE SEND  (prevents bot crash from Blocked/Left/etc)
# -----------------------------------------------------

async def safe_send(bot, chat_id, text, **kwargs):
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        print(f"⚠️ safe_send error: {e}")


# -----------------------------------------------------
# /motivate command
# -----------------------------------------------------

async def motivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User manually asks for a motivation quote."""
    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    quote = random.choice(QUOTES)
    text = f"💡 <b>Motivation:</b>\n{quote}"

    await safe_send(context.bot, GROUP_CHAT_ID, text, parse_mode=ParseMode.HTML)


# -----------------------------------------------------
# Hourly motivational push from JobQueue OR fallback
# -----------------------------------------------------

async def send_motivation(context: ContextTypes.DEFAULT_TYPE):
    """Automatic hourly quote sender."""
    quote = random.choice(QUOTES)
    text = f"💡 <b>Hourly Motivation:</b>\n{quote}"

    await safe_send(context.bot, GROUP_CHAT_ID, text, parse_mode=ParseMode.HTML)
    print(f"[{datetime.now()}] Sent hourly motivation.")
