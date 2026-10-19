"""Event log: posts what happens in the server to the log channel (set with /neuro setlog, or
EVENT_LOG_CHANNEL_ID / MOD_LOG_CHANNEL_ID in .env). Which categories are logged is controlled by
EVENT_LOG_EVENTS (messages, members, roles, channels, bans, voice).

Privacy: content of messages in /noshare channels is never written to the log channel.
Who did it (kick / ban / timeout / role change) comes from Discord's audit log when the bot has the
"View Audit Log" permission; otherwise the line simply has no "by"."""
import datetime as dt
import logging

import discord

import config
import db
from tools import discord_utils as du

log = logging.getLogger("eventlog")
APP_SUFFIX = " [app]"          # bot.py labels other apps' messages this way in the message log


def enabled(category: str) -> bool:
    return category in config.EVENT_LOG_EVENTS


async def post(guild: discord.Guild, text: str, category: str, skip_channel_id: int | None = None) -> None:
    if guild is None or not enabled(category):
        return
    ch = await du.log_channel(guild, events=True)
    if ch is None or (skip_channel_id is not None and ch.id == skip_channel_id):
        return                                   # no log channel, or the event happened IN it
    try:
        await ch.send(text[:1900], allowed_mentions=du.NO_PINGS)
    except discord.HTTPException:
        pass


def _quote(text: str, limit: int = 500) -> str:
    text = (text or "").strip() or "(no text)"
    if len(text) > limit:
        text = text[:limit] + "…"
    return "\n".join("> " + line for line in text.splitlines()[:8])


def _name(user) -> str:
    return f"{getattr(user, 'display_name', None) or getattr(user, 'name', user)} ({user.id})"


async def audit_entry(guild: discord.Guild, action, target_id: int, within: float = 20.0):
    """Newest audit-log entry for this action against this target in the last few seconds."""
    try:
        if not guild.me.guild_permissions.view_audit_log:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        async for e in guild.audit_logs(limit=8, action=action):
            if e.target is not None and getattr(e.target, "id", None) == target_id \
                    and (now - e.created_at).total_seconds() <= within:
                return e
    except Exception:
        log.debug("audit log lookup failed", exc_info=True)
    return None


def _by(entry) -> str:
    if entry is None or entry.user is None:
        return ""
    reason = f" — {entry.reason}" if getattr(entry, "reason", None) else ""
    return f" by {_name(entry.user)}{reason}"


# ------------------------------------------------------------------ messages
async def message_deleted(bot, payload) -> None:
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if guild is None:
        return
    cached = payload.cached_message
    author_id = author_label = content = None
    if cached is not None:
        author_id, author_label = cached.author.id, _name(cached.author)
        content = cached.content or ("[attachment] " + ", ".join(a.filename for a in cached.attachments)
                                     if cached.attachments else "")
        if cached.author.bot:
            return
    else:
        row = await db.get_logged_message(payload.message_id)
        if row is not None:
            if row["is_bot"] or (row["author_name"] or "").endswith(APP_SUFFIX):
                return
            author_id, author_label, content = row["user_id"], f"{row['author_name']} ({row['user_id']})", row["content"]
    if author_id == bot.user.id:
        return
    chan = guild.get_channel(payload.channel_id)
    chan_name = f"#{chan.name}" if chan else f"channel {payload.channel_id}"
    if author_label is None:
        await post(guild, f"🗑️ A message was deleted in {chan_name} (it wasn't in my log, so I don't know who wrote it).",
                   "messages", payload.channel_id)
        return
    if await db.is_no_share(payload.channel_id):
        content = "(hidden: this channel is marked private)"
    await post(guild, f"🗑️ Message by **{author_label}** deleted in {chan_name}:\n{_quote(content)}",
               "messages", payload.channel_id)


async def messages_bulk_deleted(bot, payload) -> None:
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if guild is None:
        return
    chan = guild.get_channel(payload.channel_id)
    await post(guild, f"🧹 {len(payload.message_ids)} messages were bulk-deleted in "
                      f"{('#' + chan.name) if chan else payload.channel_id}.", "messages", payload.channel_id)


async def message_edited(bot, payload) -> None:
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if guild is None:
        return
    data = payload.data or {}
    after = data.get("content")
    if after is None:                             # embed/link-preview update, not a text edit
        return
    author = data.get("author") or {}
    if author.get("bot") or author.get("id") == bot.user.id:
        return
    cached = payload.cached_message
    before = author_label = None
    if cached is not None:
        if cached.author.bot:
            return
        before, author_label = cached.content, _name(cached.author)
    else:
        row = await db.get_logged_message(payload.message_id)
        if row is not None:
            if row["is_bot"] or (row["author_name"] or "").endswith(APP_SUFFIX):
                return
            before, author_label = row["content"], f"{row['author_name']} ({row['user_id']})"
    if before is not None and before.strip() == after.strip():
        return
    chan = guild.get_channel(payload.channel_id)
    chan_name = f"#{chan.name}" if chan else f"channel {payload.channel_id}"
    who = author_label or (f"user {author['id']}" if author.get("id") else "someone")
    if await db.is_no_share(payload.channel_id):
        await post(guild, f"✏️ **{who}** edited a message in {chan_name} (content hidden: private channel).",
                   "messages", payload.channel_id)
        return
    text = f"✏️ **{who}** edited a message in {chan_name}:\n"
    text += (f"Before:\n{_quote(before, 400)}\n" if before is not None else "Before: (not in my log)\n")
    text += f"After:\n{_quote(after, 400)}"
    await post(guild, text, "messages", payload.channel_id)


# ------------------------------------------------------------------ members
async def member_joined(bot, member) -> None:
    age = (dt.datetime.now(dt.timezone.utc) - member.created_at).days
    flag = f"  ⚠️ **new account** (under {config.NEW_ACCOUNT_DAYS} days)" if age < config.NEW_ACCOUNT_DAYS else ""
    await post(member.guild, f"📥 **{_name(member)}** joined. Account is {age} days old.{flag}", "members")


async def member_left(bot, member) -> None:
    guild = member.guild
    kick = await audit_entry(guild, discord.AuditLogAction.kick, member.id)
    if kick is not None:
        await post(guild, f"👢 **{_name(member)}** was kicked{_by(kick)}", "bans")
        return
    if await audit_entry(guild, discord.AuditLogAction.ban, member.id) is not None:
        return                                   # the ban event reports this one
    roles = [r.name for r in getattr(member, "roles", []) if r.name != "@everyone"]
    joined = member.joined_at.strftime("%d %b %Y") if getattr(member, "joined_at", None) else "unknown"
    await post(guild, f"📤 **{_name(member)}** left. Joined {joined}."
                      + (f" Roles: {', '.join(roles[:8])}" if roles else ""), "members")


async def member_updated(bot, before, after) -> None:
    guild = after.guild
    if before.nick != after.nick:
        await post(guild, f"✏️ Nickname of **{_name(after)}**: {before.nick or '(none)'} → {after.nick or '(none)'}",
                   "members")
    if before.roles != after.roles:
        gained = [r.name for r in after.roles if r not in before.roles]
        lost = [r.name for r in before.roles if r not in after.roles]
        entry = await audit_entry(guild, discord.AuditLogAction.member_role_update, after.id)
        parts = ([f"gained {', '.join(gained)}"] if gained else []) + ([f"lost {', '.join(lost)}"] if lost else [])
        if parts:
            await post(guild, f"🎭 **{_name(after)}** {' and '.join(parts)}{_by(entry)}", "roles")
    if before.timed_out_until != after.timed_out_until:
        entry = await audit_entry(guild, discord.AuditLogAction.member_update, after.id)
        if after.timed_out_until:
            until = after.timed_out_until.astimezone(config.TZ).strftime("%d %b %H:%M")
            await post(guild, f"🔇 **{_name(after)}** timed out until {until}{_by(entry)}", "members")
        else:
            await post(guild, f"🔊 Timeout of **{_name(after)}** ended or was removed{_by(entry)}", "members")


# ------------------------------------------------------------------ bans
async def member_banned(bot, guild, user) -> None:
    entry = await audit_entry(guild, discord.AuditLogAction.ban, user.id)
    await post(guild, f"🔨 **{_name(user)}** was banned{_by(entry)}", "bans")


async def member_unbanned(bot, guild, user) -> None:
    entry = await audit_entry(guild, discord.AuditLogAction.unban, user.id)
    await post(guild, f"✅ **{_name(user)}** was unbanned{_by(entry)}", "bans")


# ------------------------------------------------------------------ roles + channels
async def role_created(bot, role) -> None:
    await post(role.guild, f"🎭 Role created: **{role.name}**", "roles")


async def role_deleted(bot, role) -> None:
    await post(role.guild, f"🎭 Role deleted: **{role.name}**", "roles")


async def channel_created(bot, channel) -> None:
    entry = await audit_entry(channel.guild, discord.AuditLogAction.channel_create, channel.id)
    await post(channel.guild, f"📁 Channel created: **#{channel.name}** ({channel.type}){_by(entry)}", "channels")


async def channel_deleted(bot, channel) -> None:
    entry = await audit_entry(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
    await post(channel.guild, f"📁 Channel deleted: **#{channel.name}** ({channel.type}){_by(entry)}", "channels")


# ------------------------------------------------------------------ voice
async def voice_changed(bot, member, before, after) -> None:
    if before.channel == after.channel:
        return
    if before.channel is None:
        text = f"🎙️ **{_name(member)}** joined voice **{after.channel.name}**"
    elif after.channel is None:
        text = f"🎙️ **{_name(member)}** left voice **{before.channel.name}**"
    else:
        text = f"🎙️ **{_name(member)}** moved from **{before.channel.name}** to **{after.channel.name}**"
    await post(member.guild, text, "voice")


HANDLERS = {
    "on_raw_message_delete": message_deleted,
    "on_raw_bulk_message_delete": messages_bulk_deleted,
    "on_raw_message_edit": message_edited,
    "on_member_join": member_joined,
    "on_member_remove": member_left,
    "on_member_update": member_updated,
    "on_member_ban": member_banned,
    "on_member_unban": member_unbanned,
    "on_guild_role_create": role_created,
    "on_guild_role_delete": role_deleted,
    "on_guild_channel_create": channel_created,
    "on_guild_channel_delete": channel_deleted,
    "on_voice_state_update": voice_changed,
}


def setup(bot) -> None:
    """Register all listeners on the bot. A failure in one never affects the others or the bot."""
    for event, handler in HANDLERS.items():
        def make(handler=handler, event=event):
            async def listener(*args):
                try:
                    await handler(bot, *args)
                except Exception:
                    log.exception("event log handler %s failed", event)
            return listener
        bot.add_listener(make(), event)
