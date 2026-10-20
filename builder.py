"""Server builder: turns a request like "make group channels for the tournament" into a plan
(one new category, 1-6 channels, a few roles), shows it with Confirm / Cancel buttons, builds it
step by step on Confirm, and can undo exactly what it made.

Nothing is created until the person who asked presses Confirm. The plan can be changed in chat
(rename a channel, change a role...) as often as wanted; every change posts a fresh preview.

Safety: new roles get NO permissions (they only matter through channel visibility), @everyone and
@here can't be used in names or messages, channel/role counts are capped, and everything created
is recorded so "undo" removes only what the builder made."""
import asyncio
import collections
import copy
import dataclasses
import difflib
import json
import logging
import re
import time
import unicodedata

import discord

import config
import db
from tools import discord_utils as du

log = logging.getLogger("builder")

CHANNEL_TYPES = {"text": "text", "chat": "text", "announcement": "announcement",
                 "announcements": "announcement", "news": "announcement", "voice": "voice",
                 "forum": "forum"}
TEXT_LIKE = ("text", "announcement", "forum")
ICON = {"text": "💬", "announcement": "📢", "voice": "🔊", "forum": "🗂️"}
EMOJI = "☀-➿\U0001f300-\U0001faff"
_TEXT_BAD = re.compile(rf"[^\w\-{EMOJI}]")
_OTHER_BAD = re.compile(rf"[^\w \-&'.|{EMOJI}]")
COLORS = {"red": 0xE74C3C, "orange": 0xE67E22, "gold": 0xF1C40F, "yellow": 0xF1C40F,
          "green": 0x2ECC71, "teal": 0x1ABC9C, "blue": 0x3498DB, "purple": 0x9B59B6,
          "pink": 0xE91E63, "grey": 0x95A5A6, "gray": 0x95A5A6, "white": 0xFFFFFF,
          "blurple": 0x5865F2}
HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


# ================================================================== cleaning helpers
def clean_name(kind: str, name, limit: int = 90) -> str:
    """Channel/category name the way Discord wants it. Text-like channels: lowercase-with-hyphens;
    voice channels and categories keep capitals and spaces."""
    n = unicodedata.normalize("NFKC", str(name or "")).strip().replace("@", "")
    if kind in TEXT_LIKE:
        n = _TEXT_BAD.sub("", re.sub(r"\s+", "-", n.lower()))
        n = re.sub(r"-{2,}", "-", n).strip("-_")
    else:
        n = re.sub(r"\s{2,}", " ", _OTHER_BAD.sub("", n)).strip()
    return n[:limit].strip("-_ ")


def clean_role_name(name) -> str:
    n = re.sub(r"\s{2,}", " ", unicodedata.normalize("NFKC", str(name or "")).replace("@", "").replace("#", "")).strip()[:80]
    return "" if n.casefold() in ("everyone", "here") else n


def clean_text(text, limit: int) -> str:
    """Free text (topic, first message): no control characters, no live @everyone/@here."""
    t = "".join(ch for ch in str(text or "") if ch == "\n" or ch >= " ").strip()
    t = t.replace("@everyone", "@​everyone").replace("@here", "@​here")
    return t[:limit]


def parse_color(value) -> tuple[int | None, str | None]:
    v = str(value or "").strip().lower()
    if not v or v in ("none", "default"):
        return None, None
    if v in COLORS:
        return COLORS[v], None
    m = HEX_RE.match(v)
    if m:
        return int(m.group(1), 16), None
    return None, f"unknown color '{value}' (use a name like red/gold/blue or a hex like #3498db)"


def _s(v, limit: int = 300) -> str:
    return "" if v is None else str(v).strip()[:limit]


def _b(v) -> bool:
    return v if isinstance(v, bool) else str(v or "").strip().lower() in ("true", "yes", "1", "y")


def _list(v) -> list:
    if v is None or v == "":
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def key(name: str) -> str:
    """Comparison key for names: '#Match Lobby', 'match-lobby' and 'match_lobby' are the same."""
    return re.sub(r"[\s\-_]+", "-", str(name or "").casefold().lstrip("#")).strip("-")


# ================================================================== lookups on a real guild
def resolve_channel(guild, query: str, kinds=None):
    """Find ONE channel by mention, id or name. kinds: e.g. {"text","voice"}. Returns (channel, error)."""
    q = (query or "").strip()
    if not q:
        return None, "no channel given"
    m = re.match(r"^<#(\d+)>$", q)
    if m or q.isdigit():
        ch = guild.get_channel(int(m.group(1) if m else q))
        return (ch, None) if ch else (None, "no channel with that id")
    pool = [c for c in guild.channels if kinds is None or str(c.type) in kinds]
    k = key(q)
    exact = [c for c in pool if key(c.name) == k]
    hits = exact or [c for c in pool if k and k in key(c.name)]
    if not hits:
        close = difflib.get_close_matches(k, [key(c.name) for c in pool], n=3, cutoff=0.7)
        hits = [c for c in pool if key(c.name) in close]
        if hits:
            return None, "no exact match. Did you mean: " + ", ".join(f"#{c.name}" for c in hits[:4]) + "?"
        return None, f"no channel matching '{query}'"
    if len(hits) > 1:
        return None, "ambiguous, could be: " + ", ".join(f"{c.name} ({str(c.type)}, {c.id})" for c in hits[:5])
    return hits[0], None


def resolve_role(guild, query: str):
    """Find ONE role by mention, id or name ('everyone' = @everyone). Returns (role, error)."""
    q = (query or "").strip()
    if not q:
        return None, "no role given"
    if q.lstrip("@").lower() == "everyone":
        return guild.default_role, None
    m = re.match(r"^<@&(\d+)>$", q)
    if m or q.isdigit():
        r = guild.get_role(int(m.group(1) if m else q))
        return (r, None) if r else (None, "no role with that id")
    k = key(q)
    exact = [r for r in guild.roles if key(r.name) == k]
    hits = exact or [r for r in guild.roles if k and k in key(r.name) and not r.is_default()]
    if not hits:
        close = difflib.get_close_matches(k, [key(r.name) for r in guild.roles], n=3, cutoff=0.7)
        hits = [r for r in guild.roles if key(r.name) in close]
        if hits:
            return None, "no exact match. Did you mean: " + ", ".join(r.name for r in hits[:4]) + "?"
        return None, f"no role matching '{query}'"
    if len(hits) > 1:
        return None, "ambiguous, could be: " + ", ".join(f"{r.name} ({r.id})" for r in hits[:5])
    return hits[0], None


def role_problem(guild, author, role) -> str | None:
    """Why this role can't be changed / handed out by this person right now (None = fine)."""
    if role.is_default():
        return "that's @everyone, it can't be changed this way"
    if role.managed:
        return "that role belongs to a bot or integration"
    if role >= guild.me.top_role:
        return "that role is above or equal to mine; move Neuro's role higher in Server Settings > Roles"
    if author.id not in (guild.owner_id, config.OWNER_ID) and role >= author.top_role:
        return "that role is above or equal to yours"
    return None


def author_has(guild, author, perm: str) -> bool:
    gp = author.guild_permissions
    return author.id in (guild.owner_id, config.OWNER_ID) or gp.administrator or getattr(gp, perm)


def bot_has(guild, perm: str) -> bool:
    gp = guild.me.guild_permissions
    return gp.administrator or getattr(gp, perm)


# ================================================================== plans
def normalize_raw(raw) -> dict:
    """Coerce whatever the model sent into the plain shape the builder works with."""
    raw = raw if isinstance(raw, dict) else {}
    cat = raw.get("category")
    out = {"title": _s(raw.get("title"), 60),
           "category_name": _s(raw.get("category_name") or (cat.get("name") if isinstance(cat, dict) else cat), 90),
           "use_existing_category": _b(raw.get("use_existing_category")),
           "roles": [], "channels": []}
    for r in _list(raw.get("roles")):
        r = {"name": r} if isinstance(r, str) else r
        if isinstance(r, dict):
            out["roles"].append({
                "name": _s(r.get("name"), 80), "color": _s(r.get("color"), 20), "hoist": _b(r.get("hoist")),
                "mentionable": _b(r.get("mentionable")),
                "members": [_s(x, 80) for x in _list(r.get("members")) if _s(x, 80)][:25]})
    for c in _list(raw.get("channels")):
        c = {"name": c} if isinstance(c, str) else c
        if isinstance(c, dict):
            try:
                slow = int(c.get("slowmode_seconds") or c.get("slowmode") or 0)
            except (TypeError, ValueError):
                slow = 0
            out["channels"].append({
                "name": _s(c.get("name"), 90), "type": _s(c.get("type") or "text", 20).lower(),
                "topic": _s(c.get("topic"), 300), "visibility": _s(c.get("visibility") or "public", 10).lower(),
                "roles": [_s(x, 80) for x in _list(c.get("roles")) if _s(x, 80)],
                "read_only": _b(c.get("read_only")), "slowmode": max(0, min(slow, 21600)),
                "intro": _s(c.get("intro"), config.BUILD_MAX_INTRO), "pin": _b(c.get("pin"))})
    return out


def validate(guild, raw) -> tuple[dict | None, list[str]]:
    """Check a plan against the real server. Returns (plan, []) or (None, [problems])."""
    raw = normalize_raw(raw)
    errors: list[str] = []

    cat_name = clean_name("category", raw["category_name"])
    existing_cat = next((c for c in guild.categories if c.name.casefold() == cat_name.casefold()), None) \
        if cat_name else None
    if not cat_name:
        errors.append("the category needs a name")
    elif existing_cat and not raw["use_existing_category"]:
        errors.append(f"a category called '{existing_cat.name}' already exists. Choose another name "
                      "(only if the user asked to add to it, set use_existing_category=true)")
    elif raw["use_existing_category"] and not existing_cat:
        errors.append(f"use_existing_category is set but there is no category named '{cat_name}'")

    n_ch = len(raw["channels"])
    if n_ch == 0:
        errors.append("a build needs at least 1 channel")
    if n_ch > config.BUILD_MAX_CHANNELS:
        errors.append(f"at most {config.BUILD_MAX_CHANNELS} channels per build, but {n_ch} were given: drop the least useful")
    if len(raw["roles"]) > config.BUILD_MAX_ROLES:
        errors.append(f"at most {config.BUILD_MAX_ROLES} new roles per build, but {len(raw['roles'])} were given")

    guild_roles = {r.name.casefold(): r for r in guild.roles}
    roles, plan_role_keys = [], {}
    for r in raw["roles"]:
        name = clean_role_name(r["name"])
        if not name:
            errors.append("a role has an empty or not-allowed name"); continue
        if name.casefold() in plan_role_keys:
            errors.append(f"role '{name}' is listed twice"); continue
        if name.casefold() in guild_roles:
            errors.append(f"a role called '{name}' already exists: don't create it, just use it in a channel's roles list")
            continue
        color, cerr = parse_color(r["color"])
        if cerr:
            errors.append(f"role '{name}': {cerr}")
        ids, names = [], []
        for nm in r["members"]:
            mem, merr = du.resolve_member(guild, nm, fuzzy=True)
            if merr:
                errors.append(f"role '{name}' member: {merr}")
            elif mem.id not in ids:
                ids.append(mem.id); names.append(mem.display_name)
        plan_role_keys[name.casefold()] = name
        roles.append({"name": name, "color": color, "hoist": r["hoist"], "mentionable": r["mentionable"],
                      "member_ids": ids, "member_names": names})

    community = "COMMUNITY" in (getattr(guild, "features", None) or [])
    taken = {key(c.name) for c in existing_cat.channels} if existing_cat else set()
    channels, seen = [], set()
    for c in raw["channels"]:
        ctype = CHANNEL_TYPES.get(c["type"])
        if ctype is None:
            errors.append(f"channel type '{c['type']}' isn't supported (use text, voice, announcement or forum)"); continue
        name = clean_name(ctype, c["name"])
        if not name:
            errors.append("a channel has an empty or not-allowed name"); continue
        if key(name) in seen:
            errors.append(f"channel '{name}' is listed twice"); continue
        seen.add(key(name))
        if key(name) in taken:
            errors.append(f"the category already has a channel called '{name}'"); continue
        if ctype in ("announcement", "forum") and not community:
            errors.append(f"'{name}': {ctype} channels need the Community feature (Server Settings > Enable Community). "
                          "Use a text channel instead, or ask the user to enable Community")
            continue
        vis = "private" if c["visibility"] == "private" else "public"
        crole_names = []
        for rn in c["roles"]:
            k = rn.casefold().lstrip("@")
            if k in plan_role_keys:
                crole_names.append(plan_role_keys[k])
            elif k in guild_roles and not guild_roles[k].is_default():
                crole_names.append(guild_roles[k].name)
            else:
                errors.append(f"channel '{name}' uses role '{rn}', which isn't in the plan or on the server")
        if vis == "private" and not crole_names:
            errors.append(f"channel '{name}' is private but no role may see it: give it a roles list")
        channels.append({
            "name": name, "type": ctype, "visibility": vis, "roles": list(dict.fromkeys(crole_names)),
            "read_only": c["read_only"], "topic": clean_text(c["topic"], 300) if ctype != "voice" else "",
            "slowmode": c["slowmode"] if ctype in ("text", "forum") else 0,
            "intro": clean_text(c["intro"], config.BUILD_MAX_INTRO) if ctype in ("text", "announcement") else "",
            "pin": c["pin"] and ctype in ("text", "announcement")})

    if len(guild.channels) + len(channels) + 1 > 500:
        errors.append("the server would go over Discord's 500 channel limit")
    if len(guild.roles) + len(roles) > 250:
        errors.append("the server would go over Discord's 250 role limit")
    if errors:
        return None, errors
    return {"title": raw["title"] or f"{cat_name} setup",
            "category": {"name": cat_name, "existing": existing_cat is not None},
            "roles": roles, "channels": channels}, []


# ================================================================== changing a draft in chat
def _find(items: list[dict], name: str):
    k = key(name)
    for it in items:
        if key(it["name"]) == k or key(clean_name("voice", it["name"])) == k:
            return it
    for it in items:
        if k and (k in key(it["name"]) or key(it["name"]) in k):
            return it
    return None


def _names(items) -> str:
    return ", ".join(i["name"] for i in items) or "(none)"


def apply_ops(raw, ops) -> tuple[dict, list[str]]:
    """Apply simple edit operations to a plan. Returns (new raw plan, problems)."""
    raw = copy.deepcopy(normalize_raw(raw))
    errors: list[str] = []
    for op in _list(ops):
        if not isinstance(op, dict):
            continue
        act, name, new, val = (_s(op.get("action"), 40).lower(), _s(op.get("name"), 90),
                               _s(op.get("new_name"), 90), _s(op.get("value"), config.BUILD_MAX_INTRO))
        if act == "rename_category":
            raw["category_name"] = new or val or raw["category_name"]; continue
        if act == "set_title":
            raw["title"] = new or val; continue
        if act == "add_channel":
            raw["channels"].append({"name": name or new, "type": _s(op.get("channel_type") or "text", 20).lower(),
                                    "topic": val, "visibility": "public", "roles": [], "read_only": False,
                                    "slowmode": 0, "intro": "", "pin": False}); continue
        if act == "add_role":
            raw["roles"].append({"name": name or new, "color": val, "hoist": False, "mentionable": False, "members": []}); continue
        if act.endswith("channel") or act.startswith("set_channel") or "channel" in act:
            ch = _find(raw["channels"], name)
            if ch is None:
                errors.append(f"no channel '{name}' in the plan. Channels: {_names(raw['channels'])}"); continue
            if act == "remove_channel":
                raw["channels"].remove(ch)
            elif act == "rename_channel":
                ch["name"] = new or val
            elif act == "set_channel_topic":
                ch["topic"] = val
            elif act == "set_channel_type":
                ch["type"] = val.lower()
            elif act == "set_channel_visibility":
                ch["visibility"] = val.lower()
            elif act == "set_channel_roles":
                ch["roles"] = [x.strip() for x in val.split(",") if x.strip()]
            elif act == "set_channel_read_only":
                ch["read_only"] = _b(val)
            elif act == "set_channel_intro":
                ch["intro"] = val
            elif act == "set_channel_slowmode":
                ch["slowmode"] = int(val) if val.isdigit() else 0
            else:
                errors.append(f"unknown operation '{act}'")
            continue
        if "role" in act:
            r = _find(raw["roles"], name)
            if r is None:
                errors.append(f"no new role '{name}' in the plan. Roles: {_names(raw['roles'])}"); continue
            if act == "remove_role":
                raw["roles"].remove(r)
                for c in raw["channels"]:
                    c["roles"] = [x for x in c["roles"] if key(x) != key(r["name"])]
            elif act == "rename_role":
                old = r["name"]; r["name"] = new or val
                for c in raw["channels"]:
                    c["roles"] = [(r["name"] if key(x) == key(old) else x) for x in c["roles"]]
            elif act == "set_role_color":
                r["color"] = val
            elif act == "set_role_hoist":
                r["hoist"] = _b(val)
            elif act == "set_role_mentionable":
                r["mentionable"] = _b(val)
            elif act == "add_role_members":
                r["members"] = list(dict.fromkeys(r["members"] + [x.strip() for x in val.split(",") if x.strip()]))
            elif act == "remove_role_members":
                gone = {key(x) for x in val.split(",")}
                r["members"] = [m for m in r["members"] if key(m) not in gone]
            else:
                errors.append(f"unknown operation '{act}'")
            continue
        errors.append(f"unknown operation '{act}'")
    return raw, errors


# ================================================================== preview text
def _chan_line(c: dict) -> str:
    bits = [c["type"]]
    if c["read_only"]:
        bits.append("read-only" if c["type"] != "voice" else "listen-only")
    bits.append("visible to " + ", ".join(c["roles"]) if c["visibility"] == "private" else "everyone can see it")
    label = c["name"] if c["type"] == "voice" else f"#{c['name']}"
    line = f"{ICON[c['type']]} **{label}**: {' · '.join(bits)}"
    if c["topic"]:
        line += f"\n    _{c['topic'][:90]}_"
    if c["intro"]:
        line += "\n    first message" + (" (pinned)" if c["pin"] else "") + f": “{c['intro'][:70]}{'…' if len(c['intro']) > 70 else ''}”"
    return line


def render_preview(plan: dict, version: int) -> tuple[str, str]:
    """(title, body) for the preview message."""
    cat = plan["category"]
    lines = [f"**Category:** {cat['name']} ({'existing, channels are added to it' if cat['existing'] else 'new'})"]
    if plan["roles"]:
        lines.append(f"\n**New roles ({len(plan['roles'])}):** (no special permissions, they only unlock channels)")
        for r in plan["roles"]:
            extra = [f"#{r['color']:06x}" if r["color"] is not None else "no color"]
            if r["hoist"]:
                extra.append("shown separately")
            if r["mentionable"]:
                extra.append("mentionable")
            who = f" → will be given to: {', '.join(r['member_names'])}" if r["member_names"] else ""
            lines.append(f"• **{r['name']}**: {', '.join(extra)}{who}")
    lines.append(f"\n**Channels ({len(plan['channels'])} of max {config.BUILD_MAX_CHANNELS}):**")
    lines += [_chan_line(c) for c in plan["channels"]]
    lines.append("\nNothing is created until you press **Confirm**. To change something, just tell me "
                 "(e.g. “rename that channel”, “change that role”).")
    return f"🛠️ Build plan: {plan['title']} (v{version})", "\n".join(lines)[:3900]


def describe_for_prompt(raw: dict) -> str:
    cat = raw["category_name"] + (" (existing)" if raw["use_existing_category"] else " (new)")
    roles = "; ".join(f"{r['name']}" + (f" [{r['color']}]" if r["color"] else "") +
                      (f" members={r['members']}" if r["members"] else "") for r in raw["roles"]) or "none"
    chans = "; ".join(f"#{c['name']} [{c['type']}{', private for ' + '/'.join(c['roles']) if c['visibility'] == 'private' else ''}"
                      f"{', read-only' if c['read_only'] else ''}]" for c in raw["channels"])
    return f"category: {cat}\nroles: {roles}\nchannels: {chans}"


# ================================================================== drafts (waiting for Confirm)
@dataclasses.dataclass
class Draft:
    guild_id: int
    user_id: int
    channel_id: int
    raw: dict
    plan: dict
    version: int
    created: float
    message: object = None
    state: str = "open"            # open | building | done | cancelled | replaced


DRAFTS: dict[tuple[int, int], Draft] = {}
_guild_locks: dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)


def active_draft(guild_id: int, user_id: int) -> Draft | None:
    d = DRAFTS.get((guild_id, user_id))
    if d is None:
        return None
    if d.state != "open" or time.time() - d.created > config.BUILD_DRAFT_MINUTES * 60:
        if d.state == "open":
            d.state = "expired"
        DRAFTS.pop((guild_id, user_id), None)
        return None
    return d


def prompt_block(guild_id: int, user_id: int) -> str:
    d = active_draft(guild_id, user_id)
    if d is None:
        return ""
    return (f"<build_draft version=\"{d.version}\">\n{describe_for_prompt(d.raw)}\n</build_draft>\n"
            "(This person has an unconfirmed build plan, shown above. To change it call revise_build; "
            "to start over call propose_build. They build it by pressing Confirm on the preview.)\n\n")


async def _edit(message, **kw) -> None:
    try:
        await message.edit(allowed_mentions=du.NO_PINGS, **kw)
    except Exception:
        log.debug("could not edit preview message", exc_info=True)


class BuildPreview(discord.ui.View):
    """Confirm / Cancel buttons under a plan preview. Only the person who asked can press them."""

    def __init__(self, bot, draft: Draft):
        super().__init__(timeout=config.BUILD_DRAFT_MINUTES * 60)
        self.bot, self.draft = bot, draft

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.draft.user_id:
            await interaction.response.send_message("This plan isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm & build", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction, button):
        await interaction.response.defer()
        self.stop()
        await execute_draft(self.bot, self.draft, interaction.message)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.defer()
        self.stop()
        if DRAFTS.get((self.draft.guild_id, self.draft.user_id)) is self.draft:
            DRAFTS.pop((self.draft.guild_id, self.draft.user_id), None)
        self.draft.state = "cancelled"
        await _edit(interaction.message, content="❌ Build cancelled. Nothing was created.", embed=None, view=None)

    async def on_timeout(self) -> None:
        d = self.draft
        if d.state == "open":
            d.state = "expired"
            if DRAFTS.get((d.guild_id, d.user_id)) is d:
                DRAFTS.pop((d.guild_id, d.user_id), None)
            if d.message is not None:
                await _edit(d.message, content="⌛ This build plan expired. Ask me again if you still want it.",
                            embed=None, view=None)


async def post_preview(bot, channel, draft: Draft) -> None:
    title, body = render_preview(draft.plan, draft.version)
    embed = discord.Embed(title=title, description=body)
    draft.message = await channel.send(embed=embed, view=BuildPreview(bot, draft), allowed_mentions=du.NO_PINGS)


async def set_draft(bot, channel, guild_id: int, user_id: int, raw: dict, plan: dict) -> Draft:
    """Store a plan as the person's current draft, retire the previous preview, post the new one."""
    old = DRAFTS.get((guild_id, user_id))
    version = (old.version + 1) if old else 1
    if old is not None:
        old.state = "replaced"
        if old.message is not None:
            await _edit(old.message, content=f"↪️ Replaced by a newer plan (v{version}).", embed=None, view=None)
    draft = Draft(guild_id, user_id, channel.id, normalize_raw(raw), plan, version, time.time())
    DRAFTS[(guild_id, user_id)] = draft
    await post_preview(bot, channel, draft)
    return draft


def cancel_draft(guild_id: int, user_id: int) -> bool:
    d = DRAFTS.pop((guild_id, user_id), None)
    if d is None:
        return False
    d.state = "cancelled"
    return True


# ================================================================== building
class BuildError(Exception):
    pass


def overwrites_for(guild, ch: dict, role_objs: dict, text_like: bool) -> dict:
    """Who can see / write in a channel, from its plan entry."""
    PO = discord.PermissionOverwrite
    ow = {}
    voice = ch["type"] == "voice"
    me = guild.me
    if ch["visibility"] == "private":
        ow[guild.default_role] = PO(view_channel=False)
        for rn in ch["roles"]:
            role = role_objs[rn.casefold()]
            ow[role] = (PO(view_channel=True, connect=True, speak=not ch["read_only"]) if voice else
                        PO(view_channel=True, read_message_history=True, send_messages=not ch["read_only"]))
        ow[me] = (PO(view_channel=True, connect=True, speak=True) if voice else
                  PO(view_channel=True, read_message_history=True, send_messages=True))
    elif ch["read_only"]:
        ow[guild.default_role] = PO(speak=False) if voice else PO(send_messages=False)
        ow[me] = PO(speak=True) if voice else PO(send_messages=True)
    return ow


def category_overwrites(guild, plan: dict, role_objs: dict) -> dict:
    """If every channel is private, hide the category from everyone else too."""
    chans = plan["channels"]
    if not chans or any(c["visibility"] != "private" for c in chans):
        return {}
    PO = discord.PermissionOverwrite
    ow = {guild.default_role: PO(view_channel=False), guild.me: PO(view_channel=True)}
    for c in chans:
        for rn in c["roles"]:
            ow[role_objs[rn.casefold()]] = PO(view_channel=True)
    return ow


def check_permissions(guild, owner, plan: dict) -> str | None:
    """None if the requester and Neuro are both allowed to build this plan."""
    needs_roles = bool(plan["roles"]) or any(c["visibility"] == "private" for c in plan["channels"])
    for who, has in (("you", lambda p: author_has(guild, owner, p)), ("I", lambda p: bot_has(guild, p))):
        if not has("manage_channels"):
            return f"{who} don't have the Manage Channels permission" if who == "you" else \
                   "I'm missing the Manage Channels permission"
        if needs_roles and not has("manage_roles"):
            return f"{who} don't have the Manage Roles permission" if who == "you" else \
                   "I'm missing the Manage Roles permission"
    return None


async def execute_draft(bot, draft: Draft, message) -> dict:
    """Run after Confirm: re-check everything, build, report. Never raises."""
    key_ = (draft.guild_id, draft.user_id)
    guild = bot.get_guild(draft.guild_id)
    owner = guild.get_member(draft.user_id) if guild else None
    if guild is None or owner is None:
        return {"error": "server or member not found"}
    if draft.state != "open" or DRAFTS.get(key_) is not draft:
        await _edit(message, content="This plan was already used or replaced by a newer one.", embed=None, view=None)
        return {"error": "stale draft"}
    async with _guild_locks[guild.id]:
        # The server may have changed since the preview: validate again, and check permissions again.
        plan, errs = validate(guild, draft.raw)
        problem = ("; ".join(errs) if errs else None) or check_permissions(guild, owner, plan)
        if not problem and await db.count_builds_since(guild.id, time.time() - 86400) >= config.BUILD_PER_DAY:
            problem = f"this server already ran {config.BUILD_PER_DAY} builds in the last 24 hours"
        if problem:
            await _edit(message, content=f"⚠️ I can't build this right now: {problem}\nThe plan is still open, "
                                         "fix that and press Confirm again, or ask me to change it.",
                        embed=None, view=None)
            await post_preview(bot, message.channel, draft)
            return {"error": problem}
        draft.state = "building"
        DRAFTS.pop(key_, None)
        result = await build(guild, owner, plan, lambda t: _edit(message, content=t, embed=None, view=None))
        draft.state = "done"
        await _edit(message, content=result["report"], embed=None, view=None)
        await du.mod_log(guild, f"🛠️ {owner} built “{plan['title']}” (build #{result['build_id']}): "
                                f"{len(result['channels'])} channels, {len(result['roles'])} roles"
                                + (f", STOPPED: {result['failed']}" if result.get("failed") else ""))
        return result


async def build(guild, owner, plan: dict, progress) -> dict:
    """Create everything in the plan, recording each item the moment it exists."""
    build_id = await db.create_build(guild.id, owner.id, plan["title"], "build")
    reason = f"Neuro build #{build_id} requested by {owner}"[:400]
    created = {"channels": [], "roles": [], "category": None}
    role_objs: dict = {r.name.casefold(): r for r in guild.roles}
    failed = None
    delay = config.BUILD_STEP_DELAY

    async def record(kind, obj, data=""):
        await db.add_build_item(build_id, kind, obj.id, obj.name, data)

    try:
        for r in plan["roles"]:
            await progress(f"🛠️ Building… creating role **{r['name']}**")
            role = await guild.create_role(
                name=r["name"], permissions=discord.Permissions.none(),
                colour=discord.Colour(r["color"] or 0), hoist=r["hoist"], mentionable=r["mentionable"], reason=reason)
            role_objs[r["name"].casefold()] = role
            created["roles"].append(role)
            await record("role", role)
            await asyncio.sleep(delay)

        if plan["category"]["existing"]:
            category = next(c for c in guild.categories if c.name.casefold() == plan["category"]["name"].casefold())
        else:
            await progress(f"🛠️ Building… creating category **{plan['category']['name']}**")
            category = await guild.create_category(
                plan["category"]["name"], overwrites=category_overwrites(guild, plan, role_objs), reason=reason)
            created["category"] = category
            await record("category", category)
            await asyncio.sleep(delay)

        for c in plan["channels"]:
            await progress(f"🛠️ Building… creating {ICON[c['type']]} **{c['name']}**")
            ow = overwrites_for(guild, c, role_objs, c["type"] != "voice")
            kw = dict(category=category, overwrites=ow, reason=reason)
            if c["type"] == "voice":
                ch = await guild.create_voice_channel(c["name"], **kw)
            elif c["type"] == "forum":
                ch = await guild.create_forum(c["name"], topic=c["topic"] or None, slowmode_delay=c["slowmode"], **kw)
            else:
                ch = await guild.create_text_channel(
                    c["name"], news=(c["type"] == "announcement"), topic=c["topic"] or None,
                    slowmode_delay=c["slowmode"], **kw)
            created["channels"].append(ch)
            await record("channel", ch)
            await asyncio.sleep(delay)
            if c["intro"]:
                try:
                    msg = await ch.send(c["intro"], allowed_mentions=du.NO_PINGS)
                    if c["pin"]:
                        await msg.pin()
                except discord.HTTPException as e:       # a missing first message must not stop the build
                    log.warning("intro message in %s failed: %s", ch.name, e)

        for r in plan["roles"]:
            role = role_objs[r["name"].casefold()]
            for mid in r["member_ids"]:
                mem = guild.get_member(mid)
                if mem is not None:
                    await progress(f"🛠️ Building… giving **{r['name']}** to {mem.display_name}")
                    await mem.add_roles(role, reason=reason)
                    await db.add_build_item(build_id, "role_member", mid, f"{role.name} → {mem.display_name}",
                                            json.dumps({"role_id": role.id}))
                    await asyncio.sleep(delay)
    except discord.HTTPException as e:
        failed = f"{getattr(e, 'text', '') or e}"[:200] or "Discord refused a step"
    except Exception as e:                                           # never leave the person guessing
        log.exception("build failed")
        failed = f"{type(e).__name__}: {e}"[:200]

    lines = []
    if failed:
        lines.append(f"⚠️ **Build stopped half-way** ({failed}).")
        lines.append("What was already created is listed below. Say “undo build” to remove it, or fix the "
                     "problem (permissions / role order) and ask me to continue.")
    else:
        lines.append(f"✅ **Built “{plan['title']}”** (build #{build_id})")
    if created["category"] is not None or plan["category"]["existing"]:
        lines.append(f"Category: **{plan['category']['name']}**")
    if created["channels"]:
        lines.append("Channels: " + ", ".join(f"<#{c.id}>" if str(c.type) != "voice" else f"🔊 {c.name}"
                                              for c in created["channels"]))
    if created["roles"]:
        lines.append("Roles: " + ", ".join(f"<@&{r.id}>" for r in created["roles"]))
    if not failed:
        lines.append("Say “undo build” to remove everything it made.")
    return {"build_id": build_id, "channels": created["channels"], "roles": created["roles"],
            "failed": failed, "report": "\n".join(lines)[:1900]}


# ================================================================== undo
async def undo(guild, build_row) -> dict:
    """Reverse one build or edit. Builds: delete what it created (a category only if it is now empty).
    Edits: put the old values back. Returns counts and notes."""
    items = list(await db.build_items(build_row["id"]))
    done, notes = [], []
    reason = f"Neuro undo of build #{build_row['id']}"
    for it in reversed(items):
        kind, did = it["kind"], it["discord_id"]
        data = json.loads(it["data"]) if it["data"] else {}
        try:
            if kind == "channel":
                ch = guild.get_channel(did)
                if ch is not None:
                    await ch.delete(reason=reason); done.append(f"deleted {it['name']}")
            elif kind == "category":
                cat = guild.get_channel(did)
                if cat is not None:
                    if getattr(cat, "channels", None):
                        notes.append(f"kept category {it['name']}: it still has channels in it")
                    else:
                        await cat.delete(reason=reason); done.append(f"deleted category {it['name']}")
            elif kind == "role":
                role = guild.get_role(did)
                if role is not None:
                    await role.delete(reason=reason); done.append(f"deleted role {it['name']}")
            elif kind == "role_member":
                mem, role = guild.get_member(did), guild.get_role(data.get("role_id", 0))
                if mem is not None and role is not None and role in mem.roles:
                    await mem.remove_roles(role, reason=reason)
            elif kind == "channel_edit":
                ch = guild.get_channel(did)
                if ch is not None:
                    before = dict(data)
                    if "category_id" in before:
                        before["category"] = guild.get_channel(before.pop("category_id")) if before["category_id"] else None
                    await ch.edit(reason=reason, **before); done.append(f"restored {it['name']}")
            elif kind == "role_edit":
                role = guild.get_role(did)
                if role is not None:
                    before = dict(data)
                    if "colour" in before:
                        before["colour"] = discord.Colour(before["colour"])
                    await role.edit(reason=reason, **before); done.append(f"restored role {it['name']}")
            elif kind == "access":
                ch, role = guild.get_channel(did), guild.get_role(data.get("role_id", 0))
                if ch is not None and role is not None:
                    prev = data.get("before")
                    ow = None if prev is None else discord.PermissionOverwrite.from_pair(
                        discord.Permissions(prev["allow"]), discord.Permissions(prev["deny"]))
                    await ch.set_permissions(role, overwrite=ow, reason=reason); done.append(f"restored access of {it['name']}")
            elif kind == "member_role":
                mem, role = guild.get_member(did), guild.get_role(data.get("role_id", 0))
                if mem is not None and role is not None:
                    if data.get("action") == "add":
                        await mem.remove_roles(role, reason=reason)
                    else:
                        await mem.add_roles(role, reason=reason)
                    done.append(f"reverted {it['name']}")
        except discord.HTTPException as e:
            notes.append(f"couldn't undo {it['name']}: {getattr(e, 'text', '') or e}"[:120])
        await asyncio.sleep(config.BUILD_STEP_DELAY)
    if not any(n.startswith("couldn't") for n in notes):
        await db.mark_build_undone(build_row["id"])
    return {"undone": len(done), "details": done[:12], "notes": notes}
