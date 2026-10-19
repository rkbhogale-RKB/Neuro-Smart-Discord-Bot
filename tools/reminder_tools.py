"""Reminder tools. Anyone can set reminders for themselves or ping someone else later."""
import datetime as dt
import time

import config
import db
import reminders
from tools import discord_utils as du
from tools import moderation_tools as mt
from tools.registry import registry

FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def parse_at(at: str) -> float | None:
    """'2026-10-21 18:00' (24h, in the bot's timezone) -> unix time, or None if unreadable."""
    at = (at or "").strip()
    for f in FORMATS:
        try:
            return dt.datetime.strptime(at, f).replace(tzinfo=config.TZ).timestamp()
        except ValueError:
            continue
    return None


@registry.register(
    name="set_reminder",
    description=(
        "Set a reminder that will be posted in this channel at the right time, pinging the person. "
        "Give EITHER in_minutes (relative: 'in 2 hours' = 120) OR at (a clock time as 'YYYY-MM-DD HH:MM', "
        "24-hour, local server time; use the <now> line of the prompt to work out dates like 'tomorrow 6pm'). "
        "member is who to remind ('me' by default). repeat may be 'daily' or 'weekly'."),
    parameters={"type": "object",
                "properties": {"text": {"type": "string", "description": "what to remind about"},
                               "in_minutes": {"type": "integer", "description": "minutes from now"},
                               "at": {"type": "string", "description": "YYYY-MM-DD HH:MM, 24-hour local time"},
                               "member": {"type": "string", "description": "who to remind; default me"},
                               "repeat": {"type": "string", "enum": ["", "daily", "weekly"]}},
                "required": ["text"]},
    daily_cap=40,
)
async def set_reminder(ctx, text: str, in_minutes: int = 0, at: str = "", member: str = "me",
                       repeat: str = ""):
    text = " ".join((text or "").split())[:300]
    if not text:
        return {"error": "what should the reminder say?"}
    now = time.time()
    if in_minutes and at:
        return {"error": "give either in_minutes or at, not both"}
    if in_minutes:
        due = now + int(in_minutes) * 60
    elif at:
        due = parse_at(at)
        if due is None:
            return {"error": "couldn't read that time; use 'YYYY-MM-DD HH:MM' in 24-hour format"}
    else:
        return {"error": "when? give in_minutes or at"}
    if due < now + 30:
        return {"error": "that time is in the past or less than a minute away"}
    if due > now + 366 * 86400:
        return {"error": "reminders can be at most a year ahead"}
    repeat = (repeat or "").strip().lower()
    if repeat not in ("", "daily", "weekly"):
        return {"error": "repeat must be daily or weekly"}

    guild, author = ctx.message.guild, ctx.message.author
    m, err = du.resolve_member(guild, member or "me", fuzzy=True, author=author)
    if err:
        return {"error": err}
    if m.bot:
        return {"error": "can't remind a bot"}
    if await db.count_pending_reminders(guild.id, ctx.user_id) >= config.MAX_PENDING_REMINDERS:
        return {"error": f"you already have {config.MAX_PENDING_REMINDERS} pending reminders; cancel some first"}
    rid = await db.add_reminder(guild.id, ctx.channel_id, ctx.user_id, m.id, text, due, repeat)
    return {"status": "reminder_set", "reminder_id": rid, "for": m.display_name, "text": text,
            "when": reminders.fmt_when(due), "repeat": repeat or "no"}


@registry.register(
    name="list_reminders",
    description="Show pending reminders you set (moderators can pass all_users=true to see everyone's).",
    parameters={"type": "object",
                "properties": {"all_users": {"type": "boolean"}}},
)
async def list_reminders(ctx, all_users: bool = False):
    guild = ctx.message.guild
    if all_users and not mt._author_can(ctx, "manage_messages"):
        return {"error": "only moderators can see everyone's reminders"}
    rows = await db.pending_reminders(guild.id, None if all_users else ctx.user_id)
    items = []
    for r in rows:
        tgt = guild.get_member(r["target_id"])
        items.append({"id": r["id"], "for": tgt.display_name if tgt else str(r["target_id"]),
                      "text": r["text"], "when": reminders.fmt_when(r["due_ts"]),
                      "repeat": r["repeat"] or "no"})
    return {"total": len(items), "reminders": items} if items else {"total": 0, "note": "no pending reminders"}


@registry.register(
    name="cancel_reminder",
    description="Cancel a pending reminder by its id (see list_reminders). You can cancel your own; "
                "moderators can cancel any.",
    parameters={"type": "object",
                "properties": {"reminder_id": {"type": "integer"}},
                "required": ["reminder_id"]},
)
async def cancel_reminder(ctx, reminder_id: int):
    r = await db.get_reminder(int(reminder_id))
    if not r or r["sent"] or r["server_id"] != ctx.message.guild.id:
        return {"error": "no pending reminder with that id"}
    if r["creator_id"] != ctx.user_id and not mt._author_can(ctx, "manage_messages"):
        return {"error": "that isn't your reminder"}
    await db.cancel_reminder(r["id"])
    return {"status": "reminder_cancelled", "reminder_id": r["id"], "text": r["text"]}
