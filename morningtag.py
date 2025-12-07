from telegram import ChatMember
from typing import List, Tuple

async def tag_all_users(conn, bot, chat_id: int) -> str:
    """
    Returns a string tagging all users from DB and group admins.
    """
    # Fetch all users from DB
    rows = await conn.fetch("SELECT user_id FROM users WHERE chat_id=$1", chat_id)
    user_ids = [r['user_id'] for r in rows]

    # Fetch admins
    admins = await bot.get_chat_administrators(chat_id)
    admin_ids = [a.user.id for a in admins]

    all_ids = set(user_ids + admin_ids)
    mentions = []

    for uid in all_ids:
        try:
            member = await bot.get_chat_member(chat_id, uid)
            mentions.append(member.user.mention_html())
        except Exception:
            mentions.append(f"<a href='tg://user?id={uid}'>user</a>")  # fallback

    return " ".join(mentions)
