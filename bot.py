import asyncio
import collections
import logging
import re
import time
import types
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import agent
import config
import db
import memory
import automod
import app_profiles
import diagnostics
import event_log
import reminders
from tools import app_tools, moderation_tools, server_tools, social_tools   # noqa: F401  (importing registers the tools)
from tools import reminder_tools, warning_tools   # noqa: F401
from tools import builder_tools, manage_tools   # noqa: F401
from tools import discord_utils as du
from tools.registry import ToolContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.message_content = True   # privileged: enable in Developer Portal
intents.members = True           # privileged: name -> user resolution, member lists, moderation
intents.presences = config.PRESENCE_INTENT   # privileged, optional: online/offline status in lists


class MemoryBot(commands.Bot):
    _reminder_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        await db.init()
        event_log.setup(self)    # message / member / role / channel / ban logging
        self._reminder_task = asyncio.create_task(reminders.run(self))
        await self.tree.sync()   # registers /whatdoyouknowaboutme, /forget, /noshare, /neuro ...

    async def close(self) -> None:
        if self._reminder_task:
            self._reminder_task.cancel()
        await db.close()         # flush the SQLite connection on shutdown
        await super().close()


bot = MemoryBot(command_prefix=commands.when_mentioned, intents=intents)

NO_PINGS = discord.AllowedMentions.none()   # a tricked model can never @everyone / ping people
_recent: dict[int, collections.deque] = collections.defaultdict(collections.deque)
_tasks: set[asyncio.Task] = set()           # keep references so background tasks aren't GC'd
_user_locks: dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)


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
        # One request at a time per person, so two quick messages can't run the same action twice.
        async with _user_locks[message.author.id]:
            return await asyncio.wait_for(agent.run_agent(ctx, text, reason),
                                          timeout=config.AGENT_TIMEOUT)

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
        try:
            sent = await message.reply(part, mention_author=False, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            try:                    # the original message may be deleted: post normally instead
                sent = await message.channel.send(part, allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                log.warning("could not send a reply in channel %s", message.channel.id)
                return
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


# ---------------- /neuro: diagnostics + settings (typed backup that needs no AI) ----------------
class NeuroGroup(app_commands.Group):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the Manage Server permission for that."
        else:
            log.exception("/neuro command failed", exc_info=error)
            msg = "That command failed, check the bot console."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


neuro = NeuroGroup(name="neuro", description="Neuro diagnostics and settings", guild_only=True)


@neuro.command(name="status", description="Check my permissions, role position and setup")
@app_commands.checks.has_permissions(manage_guild=True)
async def neuro_status(interaction: discord.Interaction):
    await interaction.response.send_message(
        diagnostics.status_report(bot, interaction.guild, await db.get_log_channel(interaction.guild_id)),
        ephemeral=True, allowed_mentions=NO_PINGS)


@neuro.command(name="debug", description="Show which tools I used for the last few requests")
@app_commands.checks.has_permissions(manage_guild=True)
async def neuro_debug(interaction: discord.Interaction):
    await interaction.response.send_message(
        diagnostics.debug_report(agent.TRACES.get(interaction.guild_id)),
        ephemeral=True, allowed_mentions=NO_PINGS)


@neuro.command(name="members", description="List server members (works without the AI)")
@app_commands.describe(role="Only people with this role", channel_only="Only people who can see this channel")
async def neuro_members(interaction: discord.Interaction, role: Optional[str] = None,
                        channel_only: bool = False):
    await interaction.response.defer(ephemeral=True)
    fake = types.SimpleNamespace(message=types.SimpleNamespace(
        guild=interaction.guild, channel=interaction.channel))
    res = await server_tools.list_members(fake, "channel" if channel_only else "server", role or "")
    if "error" in res:
        text = res["error"] + ("\nRoles: " + ", ".join(res["roles"]) if res.get("roles") else "")
    else:
        text = f"**{res['scope']}: {res['total']} people**\n" + "\n".join(f"- {m}" for m in res["members"])
    await interaction.followup.send(text[:1900], ephemeral=True, allowed_mentions=NO_PINGS)


@neuro.command(name="appprofile", description="Tell me how to talk to another bot (its prefix etc.)")
@app_commands.describe(app="The other bot", prefix="Command prefix it uses, e.g. !",
                       mention="Does it only answer when @mentioned?",
                       slash_only="Does it only have slash commands?",
                       wait="Seconds to wait for its reply (1-30)")
@app_commands.checks.has_permissions(manage_guild=True)
async def neuro_appprofile(interaction: discord.Interaction, app: discord.Member,
                           prefix: Optional[str] = None, mention: Optional[bool] = None,
                           slash_only: Optional[bool] = None, wait: Optional[int] = None):
    if not app.bot:
        await interaction.response.send_message("Pick a bot, not a person.", ephemeral=True)
        return
    p = app_profiles.set_profile(app.name, prefix=prefix, mention=mention,
                                 slash_only=slash_only, wait=wait)
    await interaction.response.send_message(
        f"Saved for **{app.display_name}**: {app_profiles.describe(p)}", ephemeral=True,
        allowed_mentions=NO_PINGS)


def _ctx_from(interaction: discord.Interaction):
    """A tool context built from a slash command, so typed commands reuse the same tool code."""
    return types.SimpleNamespace(
        bot=bot, user_id=interaction.user.id, channel_id=interaction.channel_id,
        message=types.SimpleNamespace(guild=interaction.guild, author=interaction.user,
                                      channel=interaction.channel, id=0))


@neuro.command(name="setlog", description="Choose the channel for the moderation + event log")
@app_commands.describe(channel="Where to post logs. Leave empty to turn the setting off.")
@app_commands.checks.has_permissions(manage_guild=True)
async def neuro_setlog(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if channel is None:
        await db.set_log_channel(interaction.guild_id, None)
        await interaction.response.send_message(
            "Log channel setting cleared (the .env value, if any, is used again).", ephemeral=True)
        return
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages):
        await interaction.response.send_message(
            f"I can't post in {channel.mention}. Give me View Channel + Send Messages there first.",
            ephemeral=True)
        return
    await db.set_log_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(f"Logs will go to {channel.mention}.", ephemeral=True)
    await channel.send("📝 Neuro log channel is set. Moderation actions and server events will appear here.",
                       allowed_mentions=NO_PINGS)


@neuro.command(name="warn", description="Give someone a warning (strike)")
@app_commands.describe(member="Who to warn", reason="Why")
@app_commands.checks.has_permissions(manage_messages=True)
async def neuro_warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    await interaction.response.defer(ephemeral=True)
    res = await warning_tools.warn_member(_ctx_from(interaction), str(member.id), reason)
    if "error" in res:
        text = f"Couldn't warn: {res['error']}"
    else:
        text = f"Warned **{res['member']}** (#{res['warning_id']}, now {res['active_warnings']} active)."
        text += " DM sent." if res.get("dm") == "sent" else " They have DMs closed."
        if res.get("auto_timeout_minutes"):
            text += f" Auto-timeout: {res['auto_timeout_minutes']} min."
        if res.get("auto_timeout_failed"):
            text += f" Auto-timeout failed: {res['auto_timeout_failed']}."
    await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)


@neuro.command(name="warnings", description="Show a member's active warnings")
@app_commands.describe(member="Whose warnings")
@app_commands.checks.has_permissions(manage_messages=True)
async def neuro_warnings(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    res = await warning_tools.list_warnings(_ctx_from(interaction), str(member.id))
    if "error" in res:
        text = res["error"]
    elif not res["warnings"]:
        text = f"**{res['member']}** has no active warnings."
    else:
        text = f"**{res['member']}**: {res['active_warnings']} active warning(s)\n" + "\n".join(
            f"#{w['id']} {w['date']} by {w['by']}: {w['reason']}" for w in res["warnings"])
    await interaction.followup.send(text[:1900], ephemeral=True, allowed_mentions=NO_PINGS)


@neuro.command(name="reminders", description="Show your pending reminders")
async def neuro_reminders(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    from tools import reminder_tools as rt
    res = await rt.list_reminders(_ctx_from(interaction))
    if not res.get("reminders"):
        text = "You have no pending reminders."
    else:
        text = "**Your reminders**\n" + "\n".join(
            f"#{r['id']} {r['when']} ({r['repeat']}) for {r['for']}: {r['text']}" for r in res["reminders"])
    await interaction.followup.send(text[:1900], ephemeral=True, allowed_mentions=NO_PINGS)


@neuro.command(name="builds", description="Show recent builds and edits (use 'undo build' in chat to reverse one)")
async def neuro_builds(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    perms = interaction.user.guild_permissions
    if not (perms.manage_channels or perms.administrator or interaction.user.id == config.OWNER_ID):
        await interaction.followup.send("You need the Manage Channels permission.", ephemeral=True)
        return
    rows = await db.recent_builds(interaction.guild.id, 10)
    text = "**Recent builds / edits**\n" + "\n".join(
        f"#{r['id']} {r['kind']}: {r['title']}" + (" (undone)" if r["undone"] else "") for r in rows) \
        if rows else "No builds yet."
    await interaction.followup.send(text[:1900], ephemeral=True, allowed_mentions=NO_PINGS)


bot.tree.add_command(neuro)


if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)