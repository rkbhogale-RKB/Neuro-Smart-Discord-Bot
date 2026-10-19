import asyncio
import collections
import logging
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import agent
import config
import db
import memory
import automod
from tools import app_tools, moderation_tools   # noqa: F401  (importing registers the tools)
from tools import discord_utils as du
from tools.registry import ToolContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.message_content = True   # privileged: enable in Developer Portal
intents.members = True           # privileged: needed later for name -> user resolution


class MemoryBot(commands.Bot):
    async def setup_hook(self) -> None:
        await db.init()
        await self.tree.sync()   # registers /whatdoyouknowaboutme, /forget, /noshare


bot = MemoryBot(command_prefix=commands.when_mentioned, intents=intents)

NO_PINGS = discord.AllowedMentions.none()   # a tricked model can never @everyone / ping people
_recent: dict[int, collections.deque] = collections.defaultdict(collections.deque)
_tasks: set[asyncio.Task] = set()           # keep references so background tasks aren't GC'd


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def rate_ok(user_id: int) -> bool:
    now, q = time.time(), _recent[user_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= config.USER_REQUESTS_PER_MIN:
        return False
    q.append(now)
    return True


# ---------------- when should the bot reply? ----------------
NAME_RE = (re.compile(r"\b(" + "|".join(re.escape(n) for n in config.TRIGGER_NAMES) + r")\b", re.I)
           if config.TRIGGER_NAMES else None)
FILLER_RE = re.compile(r"^\W*(lol|lmao|haha+|hehe+|ok|okay|k|hmm+|yes|no|nice|true)\W*$", re.I)
EXPLICIT = {"mention", "reply", "name"}     # clearly aimed at the bot (vs. a soft "followup")
_last_reply: dict[tuple[int, int], float] = {}   # (channel_id, user_id) -> when the bot last answered


async def addressed_reason(message: discord.Message) -> str | None:
    """Why should the bot answer this message? None = stay out of it."""
    if bot.user in message.mentions:
        return "mention"

    replying_to_other = False
    ref = message.reference
    if ref and ref.message_id:
        target = ref.resolved
        if not isinstance(target, discord.Message):      # not cached: fetch it
            try:
                target = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                target = None
        if isinstance(target, discord.Message):
            if target.author.id == bot.user.id:
                return "reply"
            replying_to_other = True

    if NAME_RE and NAME_RE.search(message.content):
        return "name"

    # Follow-up: same person keeps talking right after the bot answered them, and the
    # message isn't aimed at someone else (reply/mention) or pure filler like "lol".
    if replying_to_other or message.mentions or message.role_mentions or message.mention_everyone:
        return None
    last = _last_reply.get((message.channel.id, message.author.id))
    if last and time.time() - last < config.FOLLOWUP_SECONDS:
        text = message.content.strip()
        if text and not FILLER_RE.match(text):
            return "followup"
    return None


def chunks(text: str, size: int = 1900):
    for i in range(0, len(text), size):
        yield text[i:i + size]


async def save_facts_in_background(message: discord.Message, text: str) -> None:
    saved = await memory.extract_and_save(message, text)
    if saved and config.REACT_ON_SAVE:
        try:
            await message.add_reaction("🧠")       # honest feedback: something was really saved
        except discord.HTTPException:
            pass                                    # missing "Add Reactions" permission: ignore


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.id == bot.user.id:
        return
    if message.author.bot:
        # Other apps never trigger a reply (no bot-to-bot loops), but what they say is logged
        # (embeds included, labelled "[app]") so the model can read and analyze it later.
        desc = du.describe_message(message)
        if desc:
            await db.log_message(message.id, message.guild.id, message.channel.id,
                                 message.author.id, f"{message.author.display_name} [app]", False,
                                 desc, message.created_at.timestamp())
        return

    if await automod.check(message):
        return

    # Log every human message (clean_content = mentions turned into names).
    if message.content:
        await db.log_message(message.id, message.guild.id, message.channel.id,
                             message.author.id, message.author.display_name, False,
                             message.clean_content, message.created_at.timestamp())

    reason = await addressed_reason(message)
    if not reason:
        if config.EXTRACT_FROM_ALL_MESSAGES and message.content:
            spawn(save_facts_in_background(message, message.clean_content))
        return
    log.info("trigger=%s user=%s", reason, message.author.display_name)

    # Strip the bot mention but keep other <@id> mentions: step 4 resolves them to users.
    text = message.content
    for tag in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        text = text.replace(tag, "")
    text = text.strip()
    if not text:
        return

    if not rate_ok(message.author.id):
        if reason in EXPLICIT:
            try:
                await message.add_reaction("⏳")
            except discord.HTTPException:
                pass
        return

    ctx = ToolContext(bot=bot, message=message)

    async def _run() -> str:
        return await asyncio.wait_for(agent.run_agent(ctx, text, reason), timeout=config.AGENT_TIMEOUT)

    try:
        if reason in EXPLICIT:
            async with message.channel.typing():
                reply = await _run()
        else:                       # soft trigger: don't show "typing" if it ends up silent
            reply = await _run()
    except Exception:
        log.exception("agent failed")
        if reason not in EXPLICIT:
            return                  # never post error noise for a message that may not be for us
        reply = "My brain glitched for a sec. Try again in a bit, yaar."

    if reply.strip().upper().startswith("NO_REPLY"):
        if reason not in EXPLICIT:
            return                  # the model decided this wasn't meant for the bot
        reply = "Hmm? Say that again?"

    for part in chunks(reply):
        sent = await message.reply(part, mention_author=False, allowed_mentions=NO_PINGS)
        await db.log_message(sent.id, message.guild.id, message.channel.id, bot.user.id,
                             bot.user.display_name, True, part, time.time())
    _last_reply[(message.channel.id, message.author.id)] = time.time()
    if len(_last_reply) > 2000:     # keep the dict small
        cutoff = time.time() - config.FOLLOWUP_SECONDS
        for k in [k for k, v in _last_reply.items() if v < cutoff]:
            del _last_reply[k]

    await db.record_interaction(message.guild.id, message.channel.id, message.author.id)
    # Save facts AFTER replying, in the background: a failure here can't break the reply.
    spawn(save_facts_in_background(message, message.clean_content))


# ---------------- slash commands ----------------
class ConfirmView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()


@bot.tree.command(name="whatdoyouknowaboutme",
                  description="Show the facts I've saved about you in this server")
@app_commands.guild_only()
async def whatdoyouknowaboutme(interaction: discord.Interaction):
    facts = await db.current_facts(interaction.guild_id, interaction.user.id, 100)
    if not facts:
        text = ("I haven't saved anything about you yet. Tell me things like "
                "\"my favorite color is blue\" when you talk to me.")
    else:
        text = "**What I know about you** (server-wide):\n" + \
               "\n".join(f"- {f['key']}: {f['value']}" for f in facts)
    await interaction.response.send_message(text[:1900], ephemeral=True,
                                            allowed_mentions=NO_PINGS)


@bot.tree.command(name="forget", description="Delete what I saved about you (one key, or everything)")
@app_commands.describe(key="Fact to delete, e.g. favorite_color. Leave empty to delete everything.")
@app_commands.guild_only()
async def forget(interaction: discord.Interaction, key: Optional[str] = None):
    if key:
        k = memory.clean_key(key)
        n = await db.delete_facts(interaction.guild_id, interaction.user.id, k)
        await interaction.response.send_message(
            f"Deleted `{k}`." if n else f"I had nothing saved under `{k}`.", ephemeral=True)
        return
    view = ConfirmView(interaction.user.id)
    await interaction.response.send_message(
        "Delete EVERYTHING I've saved about you in this server? This can't be undone.",
        view=view, ephemeral=True)
    await view.wait()
    if view.confirmed:
        n = await db.delete_facts(interaction.guild_id, interaction.user.id)
        await interaction.edit_original_response(content=f"Done. Deleted {n} saved entries.", view=None)
    else:
        await interaction.edit_original_response(content="Cancelled.", view=None)


@bot.tree.command(name="noshare",
                  description="Make this channel private: its chats are never used in other channels")
@app_commands.describe(enabled="True = private, False = normal")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def noshare(interaction: discord.Interaction, enabled: bool):
    await db.set_no_share(interaction.channel_id, enabled)
    await interaction.response.send_message(
        "This channel is now private: I won't learn facts from it or mention its chats elsewhere."
        if enabled else "This channel is back to normal.", ephemeral=True)


@noshare.error
async def noshare_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the Manage Channels permission.",
                                                ephemeral=True)
    else:
        log.exception("noshare failed", exc_info=error)


if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)