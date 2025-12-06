import os
from dotenv import load_dotenv
from telegram.constants import ParseMode

load_dotenv()
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))

# Memory store (you can replace with database later)
KNOWN_USERS = set()


def store_user(update):
    """Store user ID whenever a message arrives."""
    if update.effective_chat.id == GROUP_CHAT_ID:
        user_id = update.effective_user.id
        KNOWN_USERS.add(user_id)


async def generate_mentions(context):
    """Generate @mentions for all remembered users."""
    bot = context.bot
    tags = []

    for uid in KNOWN_USERS:
        try:
            member = await bot.get_chat_member(GROUP_CHAT_ID, uid)
            username = member.user.username

            if username:
                tags.append(f"@{username}")
            else:
                tags.append(f"<a href='tg://user?id={uid}'>User</a>")
        except Exception:
            continue

    return " ".join(tags)
