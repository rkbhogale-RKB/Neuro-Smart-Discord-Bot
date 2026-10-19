"""Reaching people: ping someone in the channel, DM them, or look up who a name refers to.
Pings only ever hit the ONE resolved person: never @everyone, @here or roles."""
import logging

import discord

import config
from tools import discord_utils as du
from tools.registry import registry

log = logging.getLogger("social")


def _clean(text: str, limit: int) -> str:
    text = " ".join((text or "").split())[:limit]
    return text.replace("@everyone", "@​everyone").replace("@here", "@​here")


@registry.register(
    name="find_members",
    description="Look up server members by (partial or misspelled) name, mention or id. Returns up "
                "to 5 matches with ids. Use when you're unsure who someone is.",
    parameters={"type": "object",
                "properties": {"query": {"type": "string", "description": "name, mention or id"}},
                "required": ["query"]},
)
async def find_members(ctx, query: str):
    found = du.search_members(ctx.message.guild, query, 5)
    if not found:
        return {"matches": [], "note": f"nobody matches '{query}'"}
    return {"matches": [{"name": m.display_name, "username": m.name, "id": m.id, "is_bot": m.bot}
                        for m in found]}


@registry.register(
    name="mention_member",
    description="Call / ping / tag a person in the current channel so they get a notification. "
                "Use when the user says things like 'call hardik', 'ping @x', 'tell x to come here'. "
                "Pass the name or mention, and an optional short message to send with the ping.",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "name, mention or id"},
                               "message": {"type": "string",
                                           "description": "short text to send with the ping (optional)"}},
                "required": ["member"]},
    daily_cap=60,
)
async def mention_member(ctx, member: str, message: str = ""):
    m, err = du.resolve_member(ctx.message.guild, member, fuzzy=True)
    if err:
        return {"error": err}
    text = _clean(message, 300) or f"{ctx.message.author.display_name} is calling you!"
    try:
        await ctx.message.channel.send(f"{m.mention} {text}",
                                       allowed_mentions=discord.AllowedMentions(users=[m]))
    except discord.HTTPException as e:
        return {"error": f"couldn't send in this channel: {getattr(e, 'text', '')[:100]}"}
    return {"status": "pinged", "member": m.display_name}


@registry.register(
    name="dm_member",
    description="Send a private direct message to a person. Use when the user explicitly says to "
                "DM / privately message someone. Not for pinging in the channel (use mention_member).",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "name, mention or id"},
                               "text": {"type": "string", "description": "the message to send"}},
                "required": ["member", "text"]},
    daily_cap=20,
)
async def dm_member(ctx, member: str, text: str):
    m, err = du.resolve_member(ctx.message.guild, member, fuzzy=True)
    if err:
        return {"error": err}
    if m.bot:
        return {"error": "can't DM a bot"}
    body = _clean(text, 1500)
    if not body:
        return {"error": "empty message"}
    try:
        await m.send(f"📨 From **{ctx.message.author.display_name}** in **{ctx.message.guild.name}** "
                     f"(via {config.BOT_NAME}):\n{body}")
    except discord.Forbidden:
        return {"error": f"{m.display_name} has DMs closed for this server"}
    except discord.HTTPException as e:
        return {"error": f"couldn't DM: {getattr(e, 'text', '')[:100]}"}
    return {"status": "dm_sent", "member": m.display_name}
