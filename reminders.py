"""Reminder delivery. A background loop checks the database every few seconds and posts due
reminders in the channel where they were set (pinging only the target). If the channel is gone or
the bot can't post there, it falls back to a DM. Reminders that came due while the bot was offline
are delivered late, with a note. Repeating reminders ("daily" / "weekly") move to their next time."""
import asyncio
import datetime as dt
import logging
import time

import discord

import config
import db

log = logging.getLogger("reminders")
STEP = {"daily": 86400, "weekly": 7 * 86400}
LATE_AFTER = 120      # seconds: later than this and the message says it was delayed


def next_due(due: float, repeat: str, now: float) -> float | None:
    """Next occurrence strictly after `now` for a repeating reminder, else None."""
    step = STEP.get(repeat)
    if not step:
        return None
    nxt = due + step
    if nxt <= now:                       # missed several periods while offline: skip ahead
        nxt += ((now - nxt) // step + 1) * step
    return nxt


def fmt_when(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, config.TZ).strftime("%a %d %b, %I:%M %p")


async def deliver(bot: discord.Client, row, late: bool) -> bool:
    """Send one reminder. True if it reached the person (channel or DM)."""
    text = row["text"]
    target_id = row["target_id"]
    note = " _(sorry, I was offline)_" if late else ""
    by = "" if row["creator_id"] == target_id else f" (from <@{row['creator_id']}>)"
    body = f"⏰ <@{target_id}> reminder: {text}{by}{note}"
    ch = bot.get_channel(row["channel_id"])
    if ch is not None:
        try:
            await ch.send(body, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=target_id)]))
            return True
        except discord.HTTPException:
            pass
    user = bot.get_user(target_id)
    if user is None:
        try:
            user = await bot.fetch_user(target_id)
        except discord.HTTPException:
            return False
    try:
        await user.send(f"⏰ Reminder: {text}{note}")
        return True
    except discord.HTTPException:
        return False


async def tick(bot: discord.Client) -> int:
    """Deliver everything that is due. Returns how many were delivered."""
    now = time.time()
    sent = 0
    for row in await db.due_reminders(now):
        nxt = next_due(row["due_ts"], row["repeat"], now)
        if not await db.claim_reminder(row["id"], row["due_ts"], nxt):
            continue                              # already handled
        late = now - row["due_ts"] > LATE_AFTER
        try:
            if await deliver(bot, row, late):
                sent += 1
            else:
                log.warning("reminder %s could not be delivered", row["id"])
        except Exception:
            log.exception("reminder %s failed", row["id"])
    return sent


async def run(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await tick(bot)
        except Exception:
            log.exception("reminder loop error")
        await asyncio.sleep(config.REMINDER_POLL_SECONDS)
