"""Server builder tools: propose a plan (1-6 new channels under a new category, plus roles), change the
plan in chat, cancel it, undo a finished build. Nothing is created until the requester presses the
Confirm button on the preview."""
import datetime as dt

import builder
import config
import db
from tools import discord_utils as du
from tools.registry import registry

_ROLE_PROPS = {
    "name": {"type": "string"},
    "color": {"type": "string", "description": "name (red, gold, blue...) or hex like #3498db"},
    "hoist": {"type": "boolean", "description": "show members separately in the list"},
    "mentionable": {"type": "boolean"},
    "members": {"type": "array", "items": {"type": "string"},
                "description": "people who should get this role (names/mentions), only if the user named them"},
}
_CHANNEL_PROPS = {
    "name": {"type": "string", "description": "short, like 'match-lobby'"},
    "type": {"type": "string", "enum": ["text", "voice", "announcement", "forum"]},
    "topic": {"type": "string", "description": "one-line description"},
    "visibility": {"type": "string", "enum": ["public", "private"],
                   "description": "private = only the listed roles can see it"},
    "roles": {"type": "array", "items": {"type": "string"},
              "description": "roles that can see a private channel: new roles from this plan or existing ones"},
    "read_only": {"type": "boolean", "description": "only mods/Neuro can post (good for announcements)"},
    "slowmode_seconds": {"type": "integer"},
    "intro": {"type": "string", "description": "optional first message posted in the channel"},
    "pin": {"type": "boolean", "description": "pin the intro message"},
}


def _ctx(ctx):
    return ctx.message.guild, ctx.message.author


def _can_build(ctx, need_roles: bool = True) -> str | None:
    guild, author = _ctx(ctx)
    if not builder.author_has(guild, author, "manage_channels"):
        return "you need the Manage Channels permission for that"
    if need_roles and not builder.author_has(guild, author, "manage_roles"):
        return "you need the Manage Roles permission for that"
    return None


async def _show(ctx, raw: dict, what: str):
    guild, author = _ctx(ctx)
    plan, errs = builder.validate(guild, raw)
    if errs:
        return {"error": "plan not valid, fix and call again", "problems": errs}
    if (why := builder.check_permissions(guild, author, plan)):
        return {"error": why}
    draft = await builder.set_draft(ctx.bot, ctx.message.channel, guild.id, author.id, raw, plan)
    return {"status": what, "version": draft.version, "channels": len(plan["channels"]),
            "roles": len(plan["roles"]),
            "note": "A preview with Confirm/Cancel buttons was posted. Tell the user briefly what you planned "
                    "and to press Confirm. Nothing is built yet."}


@registry.register(
    name="propose_build",
    description=("Plan new channels and roles for a topic/event and show a preview with Confirm/Cancel buttons. "
                 f"You decide what is useful: ONE new category, 1 to {config.BUILD_MAX_CHANNELS} channels "
                 f"(fewer is fine), up to {config.BUILD_MAX_ROLES} new roles. Use only when the user asks to "
                 "create/set up/make channels or a section. Never builds by itself."),
    parameters={"type": "object", "properties": {
        "title": {"type": "string", "description": "short name of this build"},
        "category_name": {"type": "string", "description": "new category (the #tag/section), e.g. 'Summer Cup'"},
        "use_existing_category": {"type": "boolean", "description": "only if the user asked to add to an existing category"},
        "channels": {"type": "array", "items": {"type": "object", "properties": _CHANNEL_PROPS, "required": ["name"]}},
        "roles": {"type": "array", "items": {"type": "object", "properties": _ROLE_PROPS, "required": ["name"]}},
    }, "required": ["category_name", "channels"]},
    permission=config.BUILD_TOOL_PERMISSION, daily_cap=30, cost=2,
)
async def propose_build(ctx, category_name: str, channels: list, title: str = "", roles: list = None,
                        use_existing_category: bool = False):
    if (why := _can_build(ctx, need_roles=False)):
        return {"error": why}
    raw = {"title": title, "category_name": category_name, "channels": channels, "roles": roles or [],
           "use_existing_category": use_existing_category}
    return await _show(ctx, raw, "preview_posted")


@registry.register(
    name="revise_build",
    description=("Change the user's current unconfirmed build plan (shown in <build_draft>) and post a new preview. "
                 "Each operation: action + name (+ new_name or value). Actions: rename_category, set_title, "
                 "add_channel (name, channel_type, value=topic), remove_channel, rename_channel, set_channel_topic, "
                 "set_channel_type, set_channel_visibility (value public/private), set_channel_roles (value comma list), "
                 "set_channel_read_only (value true/false), set_channel_intro, set_channel_slowmode, add_role "
                 "(name, value=color), remove_role, rename_role, set_role_color, set_role_hoist, set_role_mentionable, "
                 "add_role_members, remove_role_members (value comma list)."),
    parameters={"type": "object", "properties": {"operations": {"type": "array", "items": {
        "type": "object", "properties": {
            "action": {"type": "string"}, "name": {"type": "string"}, "new_name": {"type": "string"},
            "value": {"type": "string"}, "channel_type": {"type": "string"}}, "required": ["action"]}}},
        "required": ["operations"]},
    permission=config.BUILD_TOOL_PERMISSION, daily_cap=60,
)
async def revise_build(ctx, operations: list):
    guild, author = _ctx(ctx)
    draft = builder.active_draft(guild.id, author.id)
    if draft is None:
        return {"error": "no open build plan for this user (it may have expired). Use propose_build to plan again"}
    raw, errs = builder.apply_ops(draft.raw, operations)
    if errs:
        return {"error": "couldn't apply: " + "; ".join(errs)}
    return await _show(ctx, raw, "revised_preview_posted")


@registry.register(
    name="cancel_build_draft",
    description="Throw away the user's current unconfirmed build plan (they said never mind / cancel / forget it).",
    parameters={"type": "object", "properties": {}},
    permission=config.BUILD_TOOL_PERMISSION,
)
async def cancel_build_draft(ctx):
    guild, author = _ctx(ctx)
    d = builder.active_draft(guild.id, author.id)
    msg = d.message if d else None
    if not builder.cancel_draft(guild.id, author.id):
        return {"error": "no open build plan"}
    if msg is not None:
        await builder._edit(msg, content="❌ Build plan cancelled. Nothing was created.", embed=None, view=None)
    return {"status": "cancelled"}


def _when(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, config.TZ).strftime("%d %b %H:%M")


@registry.register(
    name="list_builds",
    description="Show recent builds and edits made by Neuro in this server (ids, titles, whether undone).",
    parameters={"type": "object", "properties": {}},
    permission=config.BUILD_TOOL_PERMISSION,
)
async def list_builds(ctx):
    rows = await db.recent_builds(ctx.server_id, 8)
    return {"builds": [{"id": r["id"], "title": r["title"], "kind": r["kind"], "when": _when(r["created_ts"]),
                        "undone": bool(r["undone"])} for r in rows] or "none yet"}


@registry.register(
    name="undo_build",
    description=("Undo a finished build or edit: deletes the channels/roles/category it created (or restores the old "
                 "values for an edit). Asks the user to confirm with a button. Without build_id undoes the newest one."),
    parameters={"type": "object", "properties": {"build_id": {"type": "integer"}}},
    permission=config.BUILD_TOOL_PERMISSION, daily_cap=10, cost=2,
)
async def undo_build(ctx, build_id: int = None):
    guild, author = _ctx(ctx)
    row = await db.get_build(build_id) if build_id else await db.last_build(guild.id)
    if row is None or row["server_id"] != guild.id:
        return {"error": "no such build in this server"}
    if row["undone"]:
        return {"error": f"build #{row['id']} was already undone"}
    if (why := _can_build(ctx)):
        return {"error": why}
    items = await db.build_items(row["id"])
    summary = ", ".join(f"{i['kind'].replace('_', ' ')} {i['name']}" for i in items if i["kind"] != "role_member")[:600]
    ok = await du.confirm(ctx.message, f"⚠️ Undo build #{row['id']} “{row['title']}”? This will remove/restore: {summary or 'nothing'}.")
    if not ok:
        return {"status": "cancelled", "note": "user did not confirm; nothing changed"}
    res = await builder.undo(guild, row)
    await du.mod_log(guild, f"↩️ {author} undid build #{row['id']} “{row['title']}”: {res['undone']} change(s)")
    return {"status": "undone", **res}
