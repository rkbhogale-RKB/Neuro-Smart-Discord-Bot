"""Moderation tools. Only visible to mods (manage_messages / admin / OWNER_ID), and every
call is re-checked in code: the requester needs the real Discord permission, role hierarchy
is respected on both sides, and kick / ban / big purges need a button press from the requester."""
import datetime as dt
import logging

import discord

import config
import db
from tools import discord_utils as du
from tools.registry import registry

log = logging.getLogger("moderation")


def _author_can(ctx, perm: str) -> bool:
    a = ctx.message.author
    gp = a.guild_permissions
    return ctx.user_id == config.OWNER_ID or gp.administrator or getattr(gp, perm)


def _check_target(ctx, target: discord.Member, perm: str) -> str | None:
    """None = allowed, otherwise the reason it isn't."""
    guild, author = ctx.message.guild, ctx.message.author
    me = guild.me
    if not _author_can(ctx, perm):
        return f"you don't have the {perm} permission"
    if target.id == ctx.user_id:
        return "can't do that to yourself"
    if target.id == ctx.bot.user.id:
        return "can't do that to me"
    if target.id == guild.owner_id:
        return "can't act on the server owner"
    if not (me.guild_permissions.administrator or getattr(me.guild_permissions, perm)):
        return f"I'm missing the {perm} permission"
    if target.top_role >= me.top_role:
        return "their top role is above or equal to mine (move my role higher)"
    if ctx.user_id not in (config.OWNER_ID, guild.owner_id) and target.top_role >= author.top_role:
        return "their top role is above or equal to yours"
    return None


def _reason(ctx, reason: str) -> str:
    return f"{(reason or 'no reason given').strip()[:300]} (by {ctx.message.author} via {config.BOT_NAME})"[:512]


def _member(ctx, query: str):
    return du.resolve_member(ctx.message.guild, query, author=ctx.message.author)


@registry.register(
    name="get_member_info",
    description="Look up a server member: roles, when they joined, account age, timeout status. "
                "Use before moderating someone or when asked about a person.",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "mention, id or name"}},
                "required": ["member"]},
    permission=config.MOD_TOOL_PERMISSION,
)
async def get_member_info(ctx, member: str):
    m, err = _member(ctx, member)
    if err:
        return {"error": err}
    return {
        "name": m.display_name, "id": m.id, "is_bot": m.bot,
        "joined_server": m.joined_at.strftime("%Y-%m-%d") if m.joined_at else None,
        "account_created": m.created_at.strftime("%Y-%m-%d"),
        "roles": [r.name for r in m.roles if r.name != "@everyone"][-10:],
        "timed_out_until": m.timed_out_until.isoformat() if m.timed_out_until else None,
    }


@registry.register(
    name="get_member_recent_messages",
    description="Read the most recent messages a member sent in this server (from the bot's own "
                "short-lived chat log). Use to check what someone said before acting on a report.",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "mention, id or name"},
                               "count": {"type": "integer", "description": "1-20, default 10"}},
                "required": ["member"]},
    permission=config.MOD_TOOL_PERMISSION,
)
async def get_member_recent_messages(ctx, member: str, count: int = 10):
    m, err = _member(ctx, member)
    if err:
        return {"error": err}
    rows = await db.recent_messages_by_user(ctx.server_id, m.id, max(1, min(int(count), 20)))
    return {"member": m.display_name,
            "messages": [r["content"][:200] for r in rows]}


@registry.register(
    name="timeout_member",
    description="Mute (Discord timeout) a member for some minutes. minutes=0 removes an existing "
                "timeout. Max 40320 (28 days).",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "mention, id or name"},
                               "minutes": {"type": "integer"},
                               "reason": {"type": "string"}},
                "required": ["member", "minutes"]},
    permission=config.MOD_TOOL_PERMISSION, daily_cap=50,
)
async def timeout_member(ctx, member: str, minutes: int, reason: str = ""):
    m, err = _member(ctx, member)
    if err:
        return {"error": err}
    if (why := _check_target(ctx, m, "moderate_members")):
        return {"error": why}
    minutes = max(0, min(int(minutes), 40320))
    until = dt.timedelta(minutes=minutes) if minutes else None
    try:
        await m.timeout(until, reason=_reason(ctx, reason))
    except discord.HTTPException as e:
        return {"error": f"discord refused: {e.text[:100]}"}
    await du.mod_log(ctx.message.guild,
                     f"🔇 {ctx.message.author} {'timed out' if minutes else 'un-timed-out'} "
                     f"{m} ({m.id}){f' for {minutes} min' if minutes else ''}: {reason or 'no reason'}")
    return {"status": "timed_out" if minutes else "timeout_removed", "member": m.display_name,
            "minutes": minutes}


@registry.register(
    name="kick_member",
    description="Kick a member from the server (they can rejoin). The requester must confirm "
                "with a button before it happens.",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "mention, id or name"},
                               "reason": {"type": "string"}},
                "required": ["member"]},
    permission=config.MOD_TOOL_PERMISSION, daily_cap=20,
)
async def kick_member(ctx, member: str, reason: str = ""):
    m, err = _member(ctx, member)
    if err:
        return {"error": err}
    if (why := _check_target(ctx, m, "kick_members")):
        return {"error": why}
    if not await du.confirm(ctx.message, f"Kick **{m.display_name}** ({m.id})? Reason: {reason or 'none'}"):
        return {"status": "not confirmed (cancelled, or no button press in time), nothing was done"}
    try:
        await m.kick(reason=_reason(ctx, reason))
    except discord.HTTPException as e:
        return {"error": f"discord refused: {e.text[:100]}"}
    await du.mod_log(ctx.message.guild, f"👢 {ctx.message.author} kicked {m} ({m.id}): {reason or 'no reason'}")
    return {"status": "kicked", "member": m.display_name}


@registry.register(
    name="ban_member",
    description="Ban a person from the server. Accepts a mention, id or name; an id also works "
                "for someone who already left. delete_message_days (0-7) also removes their recent "
                "messages. The requester must confirm with a button before it happens.",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "mention, id or name"},
                               "reason": {"type": "string"},
                               "delete_message_days": {"type": "integer"}},
                "required": ["member"]},
    permission=config.MOD_TOOL_PERMISSION, daily_cap=20,
)
async def ban_member(ctx, member: str, reason: str = "", delete_message_days: int = 0):
    guild = ctx.message.guild
    m, err = _member(ctx, member)
    target: discord.abc.Snowflake
    label: str
    if m is not None:
        if (why := _check_target(ctx, m, "ban_members")):
            return {"error": why}
        target, label = m, f"{m.display_name} ({m.id})"
    else:
        q = member.strip().strip("<@!>")
        if not q.isdigit():
            return {"error": err}
        if not _author_can(ctx, "ban_members"):
            return {"error": "you don't have the ban_members permission"}
        if int(q) in (ctx.user_id, ctx.bot.user.id, guild.owner_id):
            return {"error": "can't ban that account"}
        target, label = discord.Object(id=int(q)), f"user id {q} (not in server)"
    if not await du.confirm(ctx.message, f"Ban **{label}**? Reason: {reason or 'none'}"):
        return {"status": "not confirmed (cancelled, or no button press in time), nothing was done"}
    days = max(0, min(int(delete_message_days), 7))
    try:
        await guild.ban(target, reason=_reason(ctx, reason), delete_message_seconds=days * 86400)
    except discord.HTTPException as e:
        return {"error": f"discord refused: {e.text[:100]}"}
    await du.mod_log(guild, f"🔨 {ctx.message.author} banned {label}: {reason or 'no reason'}")
    return {"status": "banned", "target": label}


@registry.register(
    name="unban_user",
    description="Unban a previously banned user, by id or name.",
    parameters={"type": "object",
                "properties": {"user": {"type": "string", "description": "user id or name"},
                               "reason": {"type": "string"}},
                "required": ["user"]},
    permission=config.MOD_TOOL_PERMISSION, daily_cap=30,
)
async def unban_user(ctx, user: str, reason: str = ""):
    if not _author_can(ctx, "ban_members"):
        return {"error": "you don't have the ban_members permission"}
    guild, q = ctx.message.guild, user.strip().strip("<@!>").lower()
    try:
        async for entry in guild.bans(limit=None):
            u = entry.user
            if q in (str(u.id), u.name.lower(), (u.global_name or "").lower()):
                await guild.unban(u, reason=_reason(ctx, reason))
                await du.mod_log(guild, f"✅ {ctx.message.author} unbanned {u} ({u.id})")
                return {"status": "unbanned", "user": u.name}
    except discord.HTTPException as e:
        return {"error": f"discord refused: {e.text[:100]}"}
    return {"error": "no banned user matches that"}


@registry.register(
    name="purge_messages",
    description="Delete the latest N messages (1-100) in the current channel, optionally only "
                "from one member. Over 10 messages needs a button confirmation from the requester.",
    parameters={"type": "object",
                "properties": {"count": {"type": "integer"},
                               "member": {"type": "string", "description": "optional: only this member"}},
                "required": ["count"]},
    permission=config.MOD_TOOL_PERMISSION, daily_cap=20,
)
async def purge_messages(ctx, count: int, member: str = ""):
    ch = ctx.message.channel
    if not _author_can(ctx, "manage_messages"):
        return {"error": "you don't have the manage_messages permission"}
    if not ch.permissions_for(ctx.message.guild.me).manage_messages:
        return {"error": "I can't delete messages in this channel"}
    count = max(1, min(int(count), 100))
    target = None
    if member:
        target, err = _member(ctx, member)
        if err:
            return {"error": err}
    if count > 10 and not await du.confirm(
            ctx.message, f"Delete the last {count} messages{f' from {target.display_name}' if target else ''}?"):
        return {"status": "not confirmed (cancelled, or no button press in time), nothing was done"}
    try:
        deleted = await ch.purge(limit=count, check=(lambda m: m.author.id == target.id) if target else None)
    except discord.HTTPException as e:
        return {"error": f"discord refused: {e.text[:100]}"}
    await du.mod_log(ctx.message.guild,
                     f"🧹 {ctx.message.author} purged {len(deleted)} messages in #{ch.name}")
    return {"status": "purged", "deleted": len(deleted)}
