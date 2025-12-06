# motivate.py
import random
from telegram import Update
from telegram.ext import ContextTypes

QUOTES = [
    "Your time is limited, so don’t waste it living someone else’s life. – Steve Jobs",
    "Success is not in what you have, but who you are. – Bo Bennett",
    "Don’t be afraid to give up the good to go for the great. – John D. Rockefeller",
    "The only limit to our realization of tomorrow is our doubts of today. – Franklin D. Roosevelt",
    "Strive not to be a success, but rather to be of value. – Albert Einstein",
    "Opportunities don't happen, you create them. – Chris Grosser",
    "Don’t let the fear of losing be greater than the excitement of winning. – Robert Kiyosaki",
    "Success usually comes to those who are too busy to be looking for it. – Henry David Thoreau",
    "Great minds discuss ideas; average minds discuss events; small minds discuss people. – Eleanor Roosevelt",
    "I find that the harder I work, the more luck I seem to have. – Thomas Jefferson"
]

async def send_motivation(context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    user = update.effective_user
    chat_id = context.job.chat_id
    quote = random.choice(QUOTES)
    await context.bot.send_message(chat_id, f"💡 Motivation: {quote}")

async def motivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    user = update.effective_user
    quote = random.choice(QUOTES)
    await update.message.reply_text(f"💡 Motivation: {quote}")
