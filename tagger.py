# tagger.py
import os
from telegram import ChatMember, Chat
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from dotenv import load_dotenv

load_dotenv()
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))


async def get_all_members(context: ContextTypes.DEFAULT_TYPE):
    """
    Fetch all group members.
    Telegram doesn't allow fetching all members directly, 
    so we use administrators + recent active members.
    """
    bot = context.bot
    members = set()

    try:
        # Fetch chat admins
        admins = await bot.get_chat_administrators(GROUP_CHAT_ID)
        for admin in admins:
            members.add(admin.user.id)

        # Fetch recent active members (recommended method)
        chat = await bot.get_chat(GROUP_CHAT_ID)
        recent_members = await bot.get_chat_menu_button(GROUP_CHAT_ID)

    except Exception as e:
        print("❌ Error fetching members:", e)

    return list(members)


async def generate_mentions(context: ContextTypes.DEFAULT_TYPE):
    """Returns a string containing @tags of all known members."""
    member_ids = await get_all_members(context)
    if not member_ids:
        return ""

    bot = context.bot
    tags = []

    for uid in member_ids:
        try:
            user = await bot.get_chat_member(GROUP_CHAT_ID, uid)
            username = user.user.username

            if username:
                tags.append(f"@{username}")
            else:
                tags.append(f"<a href='tg://user?id={uid}'>User</a>")

        except Exception:
            continue

    return " ".join(tags)
