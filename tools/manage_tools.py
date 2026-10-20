"""Editing the server on request: rename / re-topic / move a channel, change a role, hide or open a
channel for a role, give or take a role, delete something, post an announcement. Only used when the
user explicitly asks. Every change except delete and posting can be undone ("undo that").
Neuro never starts any of this on its own."""
import json
import logging

import discord

import builder
import config
import db
from tools import discord_utils as du
from tools.registry import registry

log = logging.getLogger("manage")


def _g(ctx):
    return ctx.message.guild, ctx.message.author


def _need(ctx, perm: str) -> str | None:
    guild, author = _g(ctx)
    if not builder.author_has(guild, author, perm):
        return f"you don't have the {perm} permission"
    if not builder.bot_has(guild, perm):
        return f"I'm missing the {perm} permission"
    return None


async def _record(ctx, title: str, kind: str, obj, name: str, data: dict) -> int:
    guild, author = _g(ctx)
    bid = await db.create_build(guild.id, author.id, title, "edit")
    await db.add_build_item(bid, kind, obj.id if obj is not None else None, name, json.dumps(data))
    return bid


CHANNEL_KINDS = {"text", "voice", "news", "forum", "stage_voice"}


def _chan(ctx, query, kinds=None):
    """Channels first; a category is only picked when no channel matches the name."""
    g = ctx.message.guild
    if kinds is not None:
        return builder.resolve_channel(g, query, kinds)
    ch, err = builder.resolve_channel(g, query, CHANNEL_KINDS)
    if ch is None and err and err.startswith("no channel matching"):
        cat, cerr = builder.resolve_channel(g, query, {"category"})
        if cat is not None:
            return cat, None
    return ch, err


def _fail(err):
    return {"error": err}


@registry.register(
    name="edit_channel",
    description=("Change an EXISTING channel when the user asks: rename it, change its topic, slowmode, or move it "
                 "into another existing category. Only the fields given are changed. Undoable."),
    parameters={"type": "object", "properties": {
        "channel": {"type": "string", "description": "mention, id or name"},
        "new_name": {"type": "string"}, "topic": {"type": "string"},
        "slowmode_seconds": {"type": "integer"},
        "category": {"type": "string", "description": "name of an existing category to move it into"}},
        "required": ["channel"]},
    permission=config.BUILD_TOOL_PERMISSION, daily_cap=40,
)
async def edit_channel(ctx, channel: str, new_name: str = None, topic: str = None,
                       slowmode_seconds: int = None, category: str = None):
    if (why := _need(ctx, "manage_channels")):
        return _fail(why)
    ch, err = _chan(ctx, channel)
    if err:
        return _fail(err)
    voice_like = str(ch.type) in ("voice", "stage_voice", "category")
    before, after = {}, {}
    if new_name:
        n = builder.clean_name("voice" if voice_like else "text", new_name)
        if not n:
            return _fail("that name isn't allowed")
        before["name"], after["name"] = ch.name, n
    if topic is not None:
        if not hasattr(ch, "topic"):
            return _fail("this channel type has no topic")
        before["topic"], after["topic"] = ch.topic, builder.clean_text(topic, 1024) or None
    if slowmode_seconds is not None:
        if not hasattr(ch, "slowmode_delay"):
            return _fail("this channel type has no slowmode")
        before["slowmode_delay"], after["slowmode_delay"] = ch.slowmode_delay, max(0, min(int(slowmode_seconds), 21600))
    if category is not None:
        cat = next((c for c in ctx.message.guild.categories if builder.key(c.name) == builder.key(category)), None)
        if cat is None:
            return _fail(f"no category named '{category}'")
        before["category_id"] = ch.category.id if ch.category else None
        after["category"] = cat
    if not after:
        return _fail("nothing to change: give new_name, topic, slowmode_seconds or category")
    old_name = ch.name
    await ch.edit(reason=f"Neuro edit requested by {ctx.message.author}"[:400], **after)
    bid = await _record(ctx, f"edit #{old_name}", "channel_edit", ch, old_name, before)
    await du.mod_log(ctx.message.guild, f"✏️ {ctx.message.author} edited channel #{old_name} (edit #{bid}): "
                                       + ", ".join(f"{k}" for k in before))
    return {"status": "edited", "edit_id": bid, "channel": ch.name, "changed": sorted(before)}


@registry.register(
    name="edit_role",
    description=("Change an EXISTING role when the user asks: rename, colour, show-separately, mentionable. "
                 "Never changes a role's permissions. Undoable."),
    parameters={"type": "object", "properties": {
        "role": {"type": "string"}, "new_name": {"type": "string"},
        "color": {"type": "string", "description": "name or hex"},
        "hoist": {"type": "boolean"}, "mentionable": {"type": "boolean"}}, "required": ["role"]},
    permission=config.BUILD_TOOL_PERMISSION, daily_cap=40,
)
async def edit_role(ctx, role: str, new_name: str = None, color: str = None,
                    hoist: bool = None, mentionable: bool = None):
    if (why := _need(ctx, "manage_roles")):
        return _fail(why)
    guild, author = _g(ctx)
    r, err = builder.resolve_role(guild, role)
    if err:
        return _fail(err)
    if (why := builder.role_problem(guild, author, r)):
        return _fail(why)
    before, after = {}, {}
    if new_name:
        n = builder.clean_role_name(new_name)
        if not n:
            return _fail("that role name isn't allowed")
        before["name"], after["name"] = r.name, n
    if color:
        c, cerr = builder.parse_color(color)
        if cerr:
            return _fail(cerr)
        before["colour"], after["colour"] = r.colour.value, discord.Colour(c or 0)
    if hoist is not None:
        before["hoist"], after["hoist"] = r.hoist, bool(hoist)
    if mentionable is not None:
        before["mentionable"], after["mentionable"] = r.mentionable, bool(mentionable)
    if not after:
        return _fail("nothing to change")
    old = r.name
    await r.edit(reason=f"Neuro edit requested by {author}"[:400], **after)
    bid = await _record(ctx, f"edit role {old}", "role_edit", r, old, before)
    await du.mod_log(guild, f"✏️ {author} edited role {old} (edit #{bid}): " + ", ".join(before))
    return {"status": "edited", "edit_id": bid, "role": r.name, "changed": sorted(before)}


@registry.register(
    name="set_channel_access",
    description=("Change who can see/write in an EXISTING channel for one role when the user asks: "
                 "hide (role can't see it), show (role can see and write), read_only (role can see, not write), "
                 "reset (remove that special rule). Undoable."),
    parameters={"type": "object", "properties": {
        "channel": {"type": "string"}, "role": {"type": "string", "description": "role name, or 'everyone'"},
        "access": {"type": "string", "enum": ["hide", "show", "read_only", "reset"]}},
        "required": ["channel", "role", "access"]},
    permission=config.BUILD_TOOL_PERMISSION, daily_cap=40,
)
async def set_channel_access(ctx, channel: str, role: str, access: str):
    if (why := _need(ctx, "manage_roles")):
        return _fail(why)
    guild, author = _g(ctx)
    ch, err = _chan(ctx, channel)
    if err:
        return _fail(err)
    r, err = builder.resolve_role(guild, role)
    if err:
        return _fail(err)
    if not r.is_default() and (why := builder.role_problem(guild, author, r)):
        return _fail(why)
    voice = str(ch.type) in ("voice", "stage_voice")
    PO = discord.PermissionOverwrite
    access = (access or "").lower()
    if access == "hide":
        ow = PO(view_channel=False)
    elif access == "show":
        ow = PO(view_channel=True, connect=True, speak=True) if voice else \
            PO(view_channel=True, read_message_history=True, send_messages=True)
    elif access == "read_only":
        ow = PO(view_channel=True, speak=False) if voice else PO(view_channel=True, send_messages=False)
    elif access == "reset":
        ow = None
    else:
        return _fail("access must be hide, show, read_only or reset")
    cur = ch.overwrites.get(r)
    prev = None if cur is None else {"allow": cur.pair()[0].value, "deny": cur.pair()[1].value}
    await ch.set_permissions(r, overwrite=ow, reason=f"Neuro edit requested by {author}"[:400])
    bid = await _record(ctx, f"access {ch.name}", "access", ch, ch.name, {"role_id": r.id, "before": prev})
    await du.mod_log(guild, f"🔐 {author} set {access} for @{r.name} on #{ch.name} (edit #{bid})")
    return {"status": "changed", "edit_id": bid, "channel": ch.name, "role": r.name, "access": access}


@registry.register(
    name="change_member_role",
    description="Give a role to a member or take it away, when the user asks. Undoable.",
    parameters={"type": "object", "properties": {
        "member": {"type": "string"}, "role": {"type": "string"},
        "action": {"type": "string", "enum": ["add", "remove"]}}, "required": ["member", "role", "action"]},
    permission=config.BUILD_TOOL_PERMISSION, daily_cap=60,
)
async def change_member_role(ctx, member: str, role: str, action: str):
    if (why := _need(ctx, "manage_roles")):
        return _fail(why)
    guild, author = _g(ctx)
    m, err = du.resolve_member(guild, member, author=author)
    if err:
        return _fail(err)
    r, err = builder.resolve_role(guild, role)
    if err:
        return _fail(err)
    if (why := builder.role_problem(guild, author, r)):
        return _fail(why)
    action = (action or "").lower()
    if action == "add":
        if r in m.roles:
            return _fail(f"{m.display_name} already has {r.name}")
        await m.add_roles(r, reason=f"Neuro: requested by {author}"[:400])
    elif action == "remove":
        if r not in m.roles:
            return _fail(f"{m.display_name} doesn't have {r.name}")
        await m.remove_roles(r, reason=f"Neuro: requested by {author}"[:400])
    else:
        return _fail("action must be add or remove")
    bid = await _record(ctx, f"{action} {r.name}", "member_role", m, f"{r.name} / {m.display_name}",
                        {"role_id": r.id, "action": action})
    await du.mod_log(guild, f"🏷️ {author} {'gave' if action == 'add' else 'removed'} role {r.name} "
                            f"{'to' if action == 'add' else 'from'} {m} (edit #{bid})")
    return {"status": "done", "edit_id": bid, "member": m.display_name, "role": r.name, "action": action}


@registry.register(
    name="delete_item",
    description=("Delete an existing channel, role or EMPTY category when the user clearly asks. Cannot be undone, "
                 "so the user must press a Confirm button."),
    parameters={"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["channel", "role", "category"]}, "name": {"type": "string"}},
        "required": ["kind", "name"]},
    permission=config.BUILD_TOOL_PERMISSION, daily_cap=15, cost=2,
)
async def delete_item(ctx, kind: str, name: str):
    guild, author = _g(ctx)
    kind = (kind or "").lower()
    if kind == "role":
        if (why := _need(ctx, "manage_roles")):
            return _fail(why)
        obj, err = builder.resolve_role(guild, name)
        if not err and (why := builder.role_problem(guild, author, obj)):
            return _fail(why)
        label = f"role **{getattr(obj, 'name', name)}**"
    elif kind in ("channel", "category"):
        if (why := _need(ctx, "manage_channels")):
            return _fail(why)
        kinds = {"category"} if kind == "category" else {"text", "voice", "news", "forum", "stage_voice"}
        obj, err = builder.resolve_channel(guild, name, kinds)
        if not err and kind == "category" and obj.channels:
            return _fail(f"category {obj.name} still has {len(obj.channels)} channel(s): delete or move them first")
        if not err and kind == "channel" and obj.id in (getattr(guild, "rules_channel", None) and guild.rules_channel.id,
                                                       getattr(guild, "public_updates_channel", None) and guild.public_updates_channel.id):
            return _fail("that is a required Community channel (rules / updates); change that in server settings first")
        label = f"{kind} **{getattr(obj, 'name', name)}**"
    else:
        return _fail("kind must be channel, role or category")
    if err:
        return _fail(err)
    if not await du.confirm(ctx.message, f"⚠️ Delete {label}? This can't be undone."):
        return {"status": "cancelled", "note": "user did not confirm; nothing deleted"}
    nm = obj.name
    await obj.delete(reason=f"Neuro: requested by {author}"[:400])
    await du.mod_log(guild, f"🗑️ {author} deleted {kind} {nm}")
    return {"status": "deleted", "kind": kind, "name": nm}


@registry.register(
    name="post_announcement",
    description=("Post a message in a channel as Neuro when the user asks (e.g. an announcement). Never pings "
                 "@everyone/@here or roles. In an announcement channel it can also be published to followers."),
    parameters={"type": "object", "properties": {
        "channel": {"type": "string"}, "text": {"type": "string"},
        "publish": {"type": "boolean", "description": "announcement channels only: also publish to followers"},
        "pin": {"type": "boolean"}}, "required": ["channel", "text"]},
    permission=config.BUILD_TOOL_PERMISSION, daily_cap=20,
)
async def post_announcement(ctx, channel: str, text: str, publish: bool = False, pin: bool = False):
    guild, author = _g(ctx)
    ch, err = _chan(ctx, channel, {"text", "news"})
    if err:
        return _fail(err)
    body = builder.clean_text(text, 1900)
    if not body:
        return _fail("nothing to post")
    if not ch.permissions_for(author).send_messages:
        return _fail("you can't write in that channel yourself")
    if not ch.permissions_for(guild.me).send_messages:
        return _fail("I can't write in that channel")
    msg = await ch.send(body, allowed_mentions=du.NO_PINGS)
    out = {"status": "posted", "channel": ch.name}
    for flag, fn, label in ((publish and str(ch.type) == "news", "publish", "published"), (pin, "pin", "pinned")):
        if flag:
            try:
                await getattr(msg, fn)()
                out[label] = True
            except discord.HTTPException as e:
                out[label] = f"failed: {getattr(e, 'text', '')[:80]}"
    await du.mod_log(guild, f"📣 {author} had me post in #{ch.name}")
    return out
