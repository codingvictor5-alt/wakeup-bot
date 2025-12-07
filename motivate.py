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
    "“If you win the morning, you win the day.” – Tim Ferriss",
    "“Your first hour sets the tone for everything that follows.” – Robin Sharma",
    "“The way you start your day determines how well you live your day.” – Hal Elrod",
    "“Success is built on the back of consistent mornings.” – James Clear",
    "“Wake up early. Show up earlier. Do more than anyone expects.” – Gary Vaynerchuk",
    "“Discipline is choosing what you want most over what you want now.” – Angela Duckworth",
    "“The day is yours if you take the morning.” – Jocko Willink",
    "“Small daily improvements over time lead to stunning results.” – Jeff Bezos",
    "“Get up early, work hard, and don’t quit.” – Elon Musk",
    "“Your habits determine your future more than your goals do.” – Naval Ravikant",
    "“You have to put in the work before the world is awake.” – Kobe Bryant",
    "“Energy is created by action, not motivation.” – Mel Robbins",
    "“Own your morning. Elevate your life.” – Jay Shetty",
    "“The early hours are your unfair advantage.” – Cal Newport",
    "“If you want different results, you have to do things differently—starting with your mornings.” – Ray Dalio",
    "“Success is the sum of small efforts repeated day in and day out.” – Satya Nadella",
    "“Wake up and attack the day with ambition.” – Dwayne Johnson",
    "“Consistency is harder when no one is watching. That’s why it counts.” – James Clear",
    "“Your future self is built in the quiet hours when others are sleeping.” – Simon Sinek",
    "“The earlier you rise, the more life you get to live.” – Mark Manson"
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
    text = f"💡 <b>Motivation:</b>\n{quote}"

    await safe_send(context.bot, GROUP_CHAT_ID, text, parse_mode=ParseMode.HTML)
    print(f"[{datetime.now()}] Sent hourly motivation.")
