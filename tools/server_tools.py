"""Read-only server awareness: members, channels, roles, voice, server info.
Channel lists only show channels the person asking can see."""
import discord

import config
from tools.registry import registry


async def _members(guild: discord.Guild):
    """The member list. Asks Discord for the full list if the cache is incomplete."""
    if not guild.chunked:
        try:
            await guild.chunk()
        except discord.HTTPException:
            pass
    return list(guild.members)


def _label(m: discord.Member) -> str:
    label = f"{m.display_name} ({m.id})"
    if m.bot:
        label += " [bot]"
    if config.PRESENCE_INTENT:
        label += f" - {m.status}"
    return label


@registry.register(
    name="list_members",
    description="List people in the server. scope='server' (everyone), 'channel' (people who can see "
                "the current channel) or give a role name in `role`. Bots are hidden unless "
                "include_bots is true. Returns the total count plus up to `limit` names.",
    parameters={"type": "object",
                "properties": {"scope": {"type": "string", "enum": ["server", "channel"]},
                               "role": {"type": "string", "description": "optional role name to filter by"},
                               "limit": {"type": "integer", "description": "1-100, default 50"},
                               "include_bots": {"type": "boolean"}}},
)
async def list_members(ctx, scope: str = "server", role: str = "", limit: int = 50,
                       include_bots: bool = False):
    guild, ch = ctx.message.guild, ctx.message.channel
    members = await _members(guild)
    if scope == "channel" and hasattr(ch, "members"):
        members = list(ch.members)
    if role:
        r = next((x for x in guild.roles if x.name.lower() == role.strip().lstrip("@").lower()), None)
        if r is None:
            return {"error": f"no role named '{role}'",
                    "roles": [x.name for x in guild.roles if x.name != "@everyone"][:30]}
        members = [m for m in members if r in m.roles]
    if not include_bots:
        members = [m for m in members if not m.bot]
    members.sort(key=lambda m: m.display_name.lower())
    limit = max(1, min(int(limit), 100))
    out = {"scope": role or scope, "total": len(members),
           "members": [_label(m) for m in members[:limit]]}
    if len(members) > limit:
        out["note"] = f"showing {limit} of {len(members)}"
    return out


@registry.register(
    name="list_channels",
    description="List the channels in this server that the person asking can see.",
    parameters={"type": "object", "properties": {}},
)
async def list_channels(ctx):
    guild, author = ctx.message.guild, ctx.message.author
    chans = [c for c in guild.channels
             if str(c.type) != "category" and c.permissions_for(author).view_channel]
    chans.sort(key=lambda c: (str(c.type), c.name))
    return {"total": len(chans), "channels": [f"#{c.name} ({c.type})" for c in chans[:60]]}


@registry.register(
    name="list_roles",
    description="List the server's roles with how many members have each.",
    parameters={"type": "object", "properties": {}},
)
async def list_roles(ctx):
    roles = [r for r in reversed(ctx.message.guild.roles) if r.name != "@everyone"]
    return {"total": len(roles), "roles": [f"{r.name} ({len(r.members)})" for r in roles[:40]]}


@registry.register(
    name="list_voice_members",
    description="Show who is currently in which voice channel.",
    parameters={"type": "object", "properties": {}},
)
async def list_voice_members(ctx):
    rooms = {vc.name: [m.display_name for m in vc.members]
             for vc in ctx.message.guild.voice_channels if vc.members}
    return {"voice_channels": rooms} if rooms else {"voice_channels": {}, "note": "nobody is in voice"}


@registry.register(
    name="server_info",
    description="Basic facts about this server: name, owner, member count, creation date, boosts.",
    parameters={"type": "object", "properties": {}},
)
async def server_info(ctx):
    g = ctx.message.guild
    return {"name": g.name, "id": g.id, "owner": str(g.owner) if g.owner else g.owner_id,
            "members": g.member_count, "created": g.created_at.strftime("%Y-%m-%d"),
            "text_channels": len(g.text_channels), "voice_channels": len(g.voice_channels),
            "roles": len(g.roles) - 1, "boost_level": g.premium_tier,
            "my_top_role": g.me.top_role.name}
