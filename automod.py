"""Optional, conservative auto-moderation. OFF unless AUTOMOD=true in .env.

It only ever deletes the offending message and (for spam / mass mentions) times the person out
for a few minutes. It never kicks or bans on its own: a human decides that, through the bot's
ban_member tool. Admins, mods (manage_messages / moderate_members) and OWNER_ID are exempt."""
import collections
import datetime as dt
import logging
import re
import time

import discord

import config
from tools import discord_utils as du

log = logging.getLogger("automod")
INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/\w+", re.I)
_times: dict[tuple[int, int], collections.deque] = collections.defaultdict(collections.deque)


def _exempt(member: discord.Member) -> bool:
    gp = member.guild_permissions
    return (member.id == config.OWNER_ID or gp.administrator or gp.manage_messages
            or gp.moderate_members)


async def check(message: discord.Message) -> bool:
    """True if the message was moderated (the caller should then ignore it)."""
    if not config.AUTOMOD or message.guild is None or _exempt(message.author):
        return False

    key, now = (message.guild.id, message.author.id), time.time()
    q = _times[key]
    while q and now - q[0] > config.AUTOMOD_SPAM_SECONDS:
        q.popleft()
    q.append(now)

    reason, punish = None, False
    if len(q) >= config.AUTOMOD_SPAM_COUNT:
        reason, punish = "spam", True
    elif len(message.mentions) + len(message.role_mentions) >= config.AUTOMOD_MENTION_LIMIT:
        reason, punish = "mass mentions", True
    elif config.AUTOMOD_BLOCK_INVITES and INVITE_RE.search(message.content):
        reason = "invite link"
    if not reason:
        return False

    q.clear()
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    action = "deleted message"
    if punish:
        try:
            await message.author.timeout(dt.timedelta(minutes=config.AUTOMOD_TIMEOUT_MINUTES),
                                         reason=f"automod: {reason}")
            action += f" + {config.AUTOMOD_TIMEOUT_MINUTES} min timeout"
        except discord.HTTPException:
            action += " (couldn't time out: role too high or missing permission)"
    await du.mod_log(message.guild,
                     f"🛡️ automod ({reason}) on {message.author} ({message.author.id}) "
                     f"in #{message.channel}: {action}")
    return True
