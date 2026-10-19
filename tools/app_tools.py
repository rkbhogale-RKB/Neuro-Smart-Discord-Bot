"""Talk to the other bots/apps in the server.

Discord only lets *humans* invoke slash commands and press buttons, so a bot can't do those.
What a bot CAN do is what this file does: post a normal message that another app listens to
(a prefix command like "!help", or an @mention to chat with an AI app), then read what the app
answers, including embeds. The model gets the answer back and can explain or analyze it."""
import asyncio
import logging

import discord

import app_profiles
import config
from tools import discord_utils as du
from tools.registry import registry

log = logging.getLogger("apps")


def _find_app(guild: discord.Guild, me_id: int, query: str):
    q = (query or "").strip()
    m = du._MENTION_RE.match(q)
    if m:
        q = m.group(1)
    apps = [x for x in guild.members if x.bot and x.id != me_id]
    if q.isdigit():
        hit = [x for x in apps if x.id == int(q)]
    else:
        ql = q.lstrip("@").lower()
        hit = [x for x in apps if ql in (x.name.lower(), x.display_name.lower())] or \
              [x for x in apps if ql in x.name.lower() or ql in x.display_name.lower()]
    if len(hit) == 1:
        return hit[0], None
    if not hit:
        return None, f"no app matching '{q}' in this server"
    return None, "ambiguous, could be: " + ", ".join(f"{x.display_name} ({x.id})" for x in hit[:5])


@registry.register(
    name="list_apps",
    description="List the other bots/apps in this server that you can talk to.",
    parameters={"type": "object", "properties": {}},
)
async def list_apps(ctx):
    apps = [{"name": m.display_name, "id": m.id, "how_to_talk_to_it": app_profiles.describe(app_profiles.get(m))}
            for m in ctx.message.guild.members if m.bot and m.id != ctx.bot.user.id]
    return {"apps": apps[:25]}


@registry.register(
    name="read_app_messages",
    description="Read what a specific app/bot recently said in THIS channel (text, embeds, button "
                "labels). Use to explain, summarize or analyze another app's reply.",
    parameters={"type": "object",
                "properties": {"app": {"type": "string", "description": "app name, mention or id"},
                               "count": {"type": "integer", "description": "1-5, default 3"}},
                "required": ["app"]},
)
async def read_app_messages(ctx, app: str, count: int = 3):
    target, err = _find_app(ctx.message.guild, ctx.bot.user.id, app)
    if err:
        return {"error": err}
    out = []
    async for m in ctx.message.channel.history(limit=50):
        if m.author.id == target.id:
            out.append(du.describe_message(m, 800))
            if len(out) >= max(1, min(int(count), 5)):
                break
    return {"app": target.display_name, "messages": out} if out else \
           {"app": target.display_name, "messages": [], "note": "nothing recent from it in this channel"}


async def _collect(bot: discord.Client, channel_id: int, app_id: int, after_id: int,
                   wait: float) -> list[discord.Message]:
    """Wait for the app's first reply, then a short grace period for follow-up messages."""
    loop = asyncio.get_running_loop()
    deadline, out = loop.time() + wait, []
    while len(out) < 3:
        left = deadline - loop.time() if not out else min(1.5, deadline - loop.time())
        if left <= 0:
            break
        try:
            m = await bot.wait_for(
                "message", timeout=left,
                check=lambda m: m.channel.id == channel_id and m.author.id == app_id and m.id > after_id)
        except asyncio.TimeoutError:
            break
        out.append(m)
        if len(out) == 1:
            deadline = loop.time() + 1.5
    refreshed = []
    for m in out:                       # many apps send a placeholder, then edit in the real answer
        try:
            await asyncio.sleep(0.8)
            m = await m.channel.fetch_message(m.id)
        except discord.HTTPException:
            pass
        refreshed.append(m)
    return refreshed


@registry.register(
    name="run_app_command",
    description=(
        "Send a message to another app/bot in the current channel and read its answer. Use it to "
        "run an app's text commands or to chat with an AI app. The app's known prefix / @mention "
        "style is applied automatically from its saved profile (see list_apps), so pass just the "
        "command or question, e.g. 'help' or 'what is a function'. It CANNOT run slash commands "
        "or press buttons (Discord forbids that for bots): for slash-only apps, tell the user the "
        "exact /command to run themselves. Don't invent commands the user didn't ask for."),
    parameters={"type": "object",
                "properties": {"app": {"type": "string", "description": "app name, mention or id"},
                               "text": {"type": "string", "description": "the message to send, max 300 chars"},
                               "mention": {"type": "boolean", "description": "prefix the text with an @mention of the app"}},
                "required": ["app", "text"]},
    permission=config.APP_TOOL_PERMISSION, daily_cap=60,
)
async def run_app_command(ctx, app: str, text: str, mention: bool = False):
    guild, ch = ctx.message.guild, ctx.message.channel
    target, err = _find_app(guild, ctx.bot.user.id, app)
    if err:
        return {"error": err}
    text = (text or "").strip()
    if not text or len(text) > 300:
        return {"error": "text must be 1-300 characters"}
    if "@everyone" in text or "@here" in text:
        return {"error": "refusing to send a mass mention"}

    prof = app_profiles.get(target)
    if prof.get("slash_only") or text.startswith("/"):
        cmd = "/" + text.lstrip("/")
        return {"slash_only": True,
                "note": f"{target.display_name} works through slash commands, which bots can't run. "
                        f"Ask the user to run `{cmd}` themselves; afterwards I can read its reply."}
    mention = bool(mention or prof.get("mention"))
    prefix = str(prof.get("prefix") or "")
    if prefix and not mention and not text.startswith(prefix):
        text = prefix + text
    wait = float(prof.get("wait") or config.APP_WAIT_SECONDS)
    lowered = text.lower()
    if any(w in lowered for w in config.DANGEROUS_APP_WORDS):
        if not await du.confirm(ctx.message, f"Send `{text[:150]}` to **{target.display_name}**? "
                                             "It looks destructive."):
            return {"status": "cancelled by requester"}
    content = f"{target.mention} {text}" if mention else text
    after_id = ch.last_message_id or 0
    collector = asyncio.create_task(_collect(ctx.bot, ch.id, target.id, after_id, wait))
    try:
        await ch.send(content, allowed_mentions=discord.AllowedMentions(users=[target]) if mention
                      else du.NO_PINGS)
    except discord.HTTPException as e:
        collector.cancel()
        return {"error": f"couldn't send: {e.text[:100]}"}
    replies = await collector
    await du.mod_log(guild, f"🤖 {ctx.message.author} sent `{text[:100]}` to app {target}")
    if not replies:
        return {"sent": content, "replies": [],
                "note": f"no reply in {wait:.0f}s: the prefix may be wrong, the app may need an "
                        "@mention, or it only supports slash commands. A profile for it can be set "
                        "with /neuro appprofile."}
    return {"app": target.display_name, "replies": [du.describe_message(m, 800) for m in replies]}
