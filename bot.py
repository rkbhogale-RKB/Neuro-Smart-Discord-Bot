import asyncio
import collections
import logging
import time

import discord
from discord.ext import commands

import agent
import config
import db
import tools.memory_tools  # noqa: F401  (importing registers the tools)
from tools.registry import ToolContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.message_content = True   # privileged: enable in Developer Portal
intents.members = True           # privileged: needed later for name -> user resolution


class MemoryBot(commands.Bot):
    async def setup_hook(self) -> None:
        await db.init()
        # slash commands (/myprofile, /forget, ...) get added + synced here in step 2


bot = MemoryBot(command_prefix=commands.when_mentioned, intents=intents)

NO_PINGS = discord.AllowedMentions.none()   # a tricked model can never @everyone / ping people
_recent: dict[int, collections.deque] = collections.defaultdict(collections.deque)


def rate_ok(user_id: int) -> bool:
    now, q = time.time(), _recent[user_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= config.USER_REQUESTS_PER_MIN:
        return False
    q.append(now)
    return True


async def is_addressed(message: discord.Message) -> bool:
    if bot.user in message.mentions:
        return True
    ref = message.reference
    if ref and isinstance(ref.resolved, discord.Message):
        return ref.resolved.author.id == bot.user.id
    return False


def chunks(text: str, size: int = 1900):
    for i in range(0, len(text), size):
        yield text[i:i + size]


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.id == bot.user.id:
        return
    if message.author.bot:
        return

    # Log every human message (clean_content = mentions turned into names).
    if message.content:
        await db.log_message(message.id, message.guild.id, message.channel.id,
                             message.author.id, message.author.display_name, False,
                             message.clean_content, message.created_at.timestamp())

    if not await is_addressed(message):
        return

    # Strip the bot mention but keep other <@id> mentions: step 4 resolves them to users.
    text = message.content
    for tag in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        text = text.replace(tag, "")
    text = text.strip()
    if not text:
        return

    if not rate_ok(message.author.id):
        await message.add_reaction("⏳")
        return

    ctx = ToolContext(bot=bot, message=message)
    async with message.channel.typing():
        try:
            reply = await asyncio.wait_for(agent.run_agent(ctx, text), timeout=60)
        except Exception:
            log.exception("agent failed")
            reply = "Kuch gadbad ho gayi yaar, thodi der baad try kar."

    for part in chunks(reply):
        sent = await message.reply(part, mention_author=False, allowed_mentions=NO_PINGS)
        await db.log_message(sent.id, message.guild.id, message.channel.id, bot.user.id,
                             bot.user.display_name, True, part, time.time())


if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)
