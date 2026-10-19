"""Helpers shared by the moderation and app tools."""
import difflib
import logging
import re

import discord

import config

log = logging.getLogger("discord_utils")

NO_PINGS = discord.AllowedMentions.none()
_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def describe_message(m: discord.Message, limit: int = 1500) -> str:
    """Plain-text view of a message incl. embeds, attachments and button/menu labels, so the
    model can read what another app said (most apps answer with embeds, not plain text)."""
    parts = []
    if m.content:
        parts.append(m.clean_content)
    for e in m.embeds:
        bits = [x for x in (e.title, e.description) if x]
        bits += [f"{f.name}: {f.value}" for f in e.fields]
        if e.footer and e.footer.text:
            bits.append(e.footer.text)
        if bits:
            parts.append("[embed] " + " | ".join(bits))
    for a in m.attachments:
        parts.append(f"[attachment] {a.filename}")
    for row in m.components:
        labels = [getattr(c, "label", None) or getattr(c, "placeholder", None)
                  for c in getattr(row, "children", [])]
        labels = [x for x in labels if x]
        if labels:
            parts.append("[buttons/menus] " + ", ".join(labels))
    return "\n".join(parts)[:limit]


def _names(x: discord.Member) -> set[str]:
    return {n for n in (x.name.lower(), x.display_name.lower(), (x.global_name or "").lower()) if n}


def _match(guild: discord.Guild, ql: str) -> tuple[str, list]:
    """Candidates for a name: ('exact'|'partial'|'fuzzy'|'none', members)."""
    members = list(guild.members)
    exact = [x for x in members if ql in _names(x)]
    if exact:
        return "exact", exact
    partial = [x for x in members if any(ql in n for n in _names(x))]
    if partial:
        return "partial", partial
    index: dict[str, list] = {}
    for x in members:
        for n in _names(x):
            index.setdefault(n, []).append(x)
    out: list = []
    for n in difflib.get_close_matches(ql, list(index), n=5, cutoff=0.7):
        out += [x for x in index[n] if x not in out]
    return ("fuzzy", out) if out else ("none", [])


def search_members(guild: discord.Guild, query: str, limit: int = 5) -> list:
    """Members whose name looks like the query (typos allowed). For lookups, never for punishments."""
    q = (query or "").strip()
    m = _MENTION_RE.match(q)
    if m or q.isdigit():
        mem = guild.get_member(int(m.group(1) if m else q))
        return [mem] if mem else []
    return _match(guild, q.lstrip("@").lower())[1][:limit]


def resolve_member(guild: discord.Guild, query: str,
                   fuzzy: bool = False) -> tuple[discord.Member | None, str | None]:
    """Find ONE member from a mention, an id, or a name. Returns (member, error).
    fuzzy=True also accepts a single typo-tolerant match (fine for pinging, never for bans)."""
    q = (query or "").strip()
    if not q:
        return None, "no member given"
    m = _MENTION_RE.match(q)
    if m:
        q = m.group(1)
    if q.isdigit():
        mem = guild.get_member(int(q))
        return (mem, None) if mem else (None, "no member with that id in this server")
    kind, pool = _match(guild, q.lstrip("@").lower())
    if kind == "none":
        return None, f"no member matching '{q}'"
    if len(pool) == 1 and (kind != "fuzzy" or fuzzy):
        return pool[0], None
    listing = ", ".join(f"{x.display_name} ({x.id})" for x in pool[:5])
    if kind == "fuzzy":
        return None, f"no exact match for '{q}'. Did you mean: {listing}?"
    return None, "ambiguous, could be: " + listing


async def mod_log(guild: discord.Guild, text: str) -> None:
    """Post to the mod-log channel if MOD_LOG_CHANNEL_ID is set. Never raises."""
    log.info("modlog: %s", text)
    if not config.MOD_LOG_CHANNEL_ID:
        return
    ch = guild.get_channel(config.MOD_LOG_CHANNEL_ID)
    if ch is None:
        return
    try:
        await ch.send(text[:1900], allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass


class ActionConfirm(discord.ui.View):
    """Yes/No buttons that only the requester can press."""

    def __init__(self, user_id: int):
        super().__init__(timeout=config.CONFIRM_SECONDS)
        self.user_id = user_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()


async def confirm(message: discord.Message, prompt: str) -> bool:
    """Ask the message author to confirm a destructive action with a button.
    The model can't press it, and text from other users/apps can't either."""
    view = ActionConfirm(message.author.id)
    sent = await message.reply(prompt, view=view, mention_author=False, allowed_mentions=NO_PINGS)
    await view.wait()
    try:
        await sent.edit(content=prompt + ("\n✅ Confirmed." if view.confirmed else "\n❌ Cancelled."),
                        view=None)
    except discord.HTTPException:
        pass
    return view.confirmed
