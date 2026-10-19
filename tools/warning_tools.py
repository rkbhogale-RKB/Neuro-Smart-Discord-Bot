"""Warnings (strikes). A moderator warns someone with a reason; at the configured counts the
person is automatically timed out (WARN_ESCALATION, default 3 warnings -> 1 hour, 5 -> 1 day).
It never kicks or bans on its own. Warnings expire after WARN_EXPIRE_DAYS (default 30)."""
import datetime as dt
import logging

import discord

import config
import db
from tools import discord_utils as du
from tools import moderation_tools as mt
from tools.registry import registry

log = logging.getLogger("warnings")


def _member(ctx, query):
    return du.resolve_member(ctx.message.guild, query, author=ctx.message.author)


def _fmt_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, config.TZ).strftime("%d %b %Y")


def escalation_for(count: int) -> int | None:
    """Minutes of timeout to apply when a person reaches exactly this many active warnings."""
    for n, minutes in config.WARN_ESCALATION:
        if n == count:
            return minutes
    return None


@registry.register(
    name="warn_member",
    description="Give a member an official warning (a strike) with a reason. They are told by DM. "
                "At the configured number of warnings they are automatically timed out.",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "mention, id or name"},
                               "reason": {"type": "string", "description": "why they are being warned"}},
                "required": ["member", "reason"]},
    permission=config.MOD_TOOL_PERMISSION, daily_cap=60,
)
async def warn_member(ctx, member: str, reason: str):
    m, err = _member(ctx, member)
    if err:
        return {"error": err}
    if m.bot:
        return {"error": "can't warn a bot"}
    if (why := mt._check_target(ctx, m, "manage_messages")):
        return {"error": why}
    reason = " ".join((reason or "").split())[:300]
    if not reason:
        return {"error": "a warning needs a reason"}
    guild = ctx.message.guild
    wid = await db.add_warning(guild.id, m.id, ctx.user_id, reason)
    count = len(await db.active_warnings(guild.id, m.id))
    out = {"status": "warned", "member": m.display_name, "warning_id": wid, "active_warnings": count}

    try:
        await m.send(f"⚠️ You were warned in **{guild.name}**: {reason}\n"
                     f"You now have {count} active warning(s).")
        out["dm"] = "sent"
    except (discord.Forbidden, discord.HTTPException):
        out["dm"] = "closed"

    minutes = escalation_for(count)
    if minutes:
        try:
            await m.timeout(dt.timedelta(minutes=minutes),
                            reason=f"{count} warnings (latest: {reason})"[:512])
            out["auto_timeout_minutes"] = minutes
        except discord.HTTPException as e:
            out["auto_timeout_failed"] = getattr(e, "text", "")[:100] or "discord refused"
    await du.mod_log(guild, f"⚠️ {ctx.message.author} warned {m} ({m.id}) [#{wid}, now {count}]: {reason}"
                            + (f" → auto-timeout {minutes} min" if out.get("auto_timeout_minutes") else ""))
    return out


@registry.register(
    name="list_warnings",
    description="Show a member's active warnings (id, reason, date, who gave it).",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "mention, id, name or 'me'"}},
                "required": ["member"]},
    permission="everyone",
)
async def list_warnings(ctx, member: str):
    m, err = _member(ctx, member)
    if err:
        return {"error": err}
    # Anyone may look at their OWN warnings; looking at someone else's needs moderator rights.
    if m.id != ctx.user_id and not mt._author_can(ctx, "manage_messages"):
        return {"error": "only moderators can see other people's warnings"}
    rows = await db.active_warnings(ctx.message.guild.id, m.id)
    guild = ctx.message.guild
    items = []
    for r in rows[:15]:
        who = guild.get_member(r["moderator_id"])
        items.append({"id": r["id"], "reason": r["reason"], "date": _fmt_ts(r["ts"]),
                      "by": who.display_name if who else str(r["moderator_id"])})
    return {"member": m.display_name, "active_warnings": len(rows), "warnings": items,
            "expire_days": config.WARN_EXPIRE_DAYS}


@registry.register(
    name="remove_warning",
    description="Remove one warning from a member: by warning_id, or the newest one if no id is given.",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "mention, id or name"},
                               "warning_id": {"type": "integer", "description": "optional id from list_warnings"}},
                "required": ["member"]},
    permission=config.MOD_TOOL_PERMISSION, daily_cap=60,
)
async def remove_warning(ctx, member: str, warning_id: int = 0):
    m, err = _member(ctx, member)
    if err:
        return {"error": err}
    if not mt._author_can(ctx, "manage_messages"):
        return {"error": "you don't have the manage_messages permission"}
    n = await db.delete_warning(ctx.message.guild.id, m.id, int(warning_id) or None)
    if not n:
        return {"error": "no such warning"}
    left = len(await db.active_warnings(ctx.message.guild.id, m.id))
    await du.mod_log(ctx.message.guild, f"🧽 {ctx.message.author} removed a warning from {m} ({m.id}), {left} left")
    return {"status": "warning_removed", "member": m.display_name, "active_warnings": left}


@registry.register(
    name="clear_warnings",
    description="Delete ALL warnings of a member. The requester must confirm with a button.",
    parameters={"type": "object",
                "properties": {"member": {"type": "string", "description": "mention, id or name"}},
                "required": ["member"]},
    permission=config.MOD_TOOL_PERMISSION, daily_cap=20,
)
async def clear_warnings(ctx, member: str):
    m, err = _member(ctx, member)
    if err:
        return {"error": err}
    if not mt._author_can(ctx, "manage_messages"):
        return {"error": "you don't have the manage_messages permission"}
    if not await du.confirm(ctx.message, f"Delete ALL warnings of **{m.display_name}**?"):
        return {"status": "not confirmed (cancelled, or no button press in time), nothing was done"}
    n = await db.clear_warnings(ctx.message.guild.id, m.id)
    await du.mod_log(ctx.message.guild, f"🧽 {ctx.message.author} cleared {n} warnings of {m} ({m.id})")
    return {"status": "warnings_cleared", "member": m.display_name, "removed": n}
