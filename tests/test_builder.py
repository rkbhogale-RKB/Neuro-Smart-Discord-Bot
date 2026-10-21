"""Tests for the server builder: plan checks (6-channel cap, names, roles), preview + Confirm flow,
revising a plan, building with a fake server, undo, and the edit tools. Fake Discord objects and a
real SQLite database, so no Discord connection is needed.
Run with:  python -m pytest tests   (or:  python tests/test_builder.py)"""
import asyncio
import os
import sys
import tempfile
import types as pytypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("OWNER_ID", "999")

import agent  # noqa: E402
import builder  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
from tools import builder_tools, manage_tools, discord_utils as du  # noqa: E402,F401
from tools.registry import registry  # noqa: E402

N = pytypes.SimpleNamespace
config.BUILD_STEP_DELAY = 0


class AllPerms:
    def __getattr__(self, n):
        return True


class Role:
    def __init__(self, g, id, name, pos, managed=False, default=False):
        self.guild, self.id, self.name, self.position, self.managed = g, id, name, pos, managed
        self.hoist, self.mentionable, self._default = False, False, default
        self.colour = builder.discord.Colour(0)

    def is_default(self): return self._default
    def __ge__(self, o): return self.position >= o.position
    def __lt__(self, o): return self.position < o.position
    def __hash__(self): return self.id
    def __eq__(self, o): return self is o

    async def edit(self, reason=None, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    async def delete(self, reason=None):
        self.guild.roles.remove(self)


class Chan:
    def __init__(self, g, id, name, type="text", category=None, overwrites=None, topic=None, slowmode_delay=0):
        self.guild, self.id, self.name, self.type, self.category = g, id, name, type, category
        self.overwrites, self.topic, self.slowmode_delay, self.sent = overwrites or {}, topic, slowmode_delay, []
        self.pinned = []

    @property
    def channels(self):
        return [c for c in self.guild.channels if c.category is self and c.type != "category"]

    def permissions_for(self, m): return AllPerms()

    async def send(self, text=None, **kw):
        msg = N(content=text, pinned=False)

        async def pin(): self.pinned.append(msg)
        msg.pin = pin
        self.sent.append(msg)
        return msg

    async def delete(self, reason=None): self.guild.channels.remove(self)

    async def edit(self, reason=None, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    async def set_permissions(self, target, overwrite=None, reason=None):
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite


class Member:
    def __init__(self, g, id, name, rank, admin=False):
        self.guild, self.id, self.name, self.display_name, self.global_name, self.bot = g, id, name, name.title(), None, False
        self.top_role = Role(g, 1000 + id, "top", rank)
        self.roles = []
        self.guild_permissions = AllPerms() if admin else N(administrator=False, manage_channels=False,
                                                           manage_roles=False, manage_messages=True)

    async def add_roles(self, r, reason=None): self.roles.append(r)
    async def remove_roles(self, r, reason=None): self.roles.remove(r)


class Guild:
    def __init__(self):
        self.id, self.name, self.owner_id, self._n = 1, "Test", 2, 100
        self.features = ["COMMUNITY"]
        self.default_role = Role(self, 1, "@everyone", 0, default=True)
        self.roles = [self.default_role, Role(self, 5, "Staff", 30)]
        self.channels = [Chan(self, 10, "General", "category"), Chan(self, 11, "general")]
        self.channels[1].category = self.channels[0]
        self.me = Member(self, 50, "neuro", 50, admin=True)
        self.boss = Member(self, 2, "boss", 100, admin=True)
        self.bob = Member(self, 4, "bob", 10)
        self.hardik = Member(self, 9, "hardik", 5)
        self.members = [self.me, self.boss, self.bob, self.hardik]
        self.rules_channel = self.public_updates_channel = None

    def _id(self):
        self._n += 1
        return self._n

    @property
    def categories(self): return [c for c in self.channels if c.type == "category"]
    def get_member(self, i): return next((m for m in self.members if m.id == i), None)
    def get_channel(self, i): return next((c for c in self.channels if c.id == i), None)
    def get_role(self, i): return next((r for r in self.roles if r.id == i), None)

    async def create_role(self, name, permissions=None, colour=None, hoist=False, mentionable=False, reason=None):
        r = Role(self, self._id(), name, 20)
        r.hoist, r.mentionable, r.colour, r.perms = hoist, mentionable, colour, permissions
        self.roles.append(r)
        return r

    async def create_category(self, name, overwrites=None, reason=None):
        c = Chan(self, self._id(), name, "category", overwrites=overwrites)
        self.channels.append(c)
        return c

    async def _mk(self, name, type, category=None, overwrites=None, topic=None, slowmode_delay=0, reason=None):
        c = Chan(self, self._id(), name, type, category, overwrites, topic, slowmode_delay)
        self.channels.append(c)
        return c

    async def create_text_channel(self, name, news=False, **kw):
        return await self._mk(name, "news" if news else "text", **kw)

    async def create_voice_channel(self, name, **kw): return await self._mk(name, "voice", **kw)
    async def create_forum(self, name, **kw): return await self._mk(name, "forum", **kw)


class FakeBot:
    def __init__(self, g): self.g = g
    def get_guild(self, i): return self.g if i == 1 else None


def setup():
    g = Guild()
    chat = Chan(g, 20, "bot-chat")
    g.channels.append(chat)
    return g, chat, FakeBot(g)


def ctx_for(g, bot, author, chat):
    return N(user_id=author.id, channel_id=chat.id, server_id=1, bot=bot,
             message=N(guild=g, author=author, channel=chat, id=1000, reply=_reply))


async def _reply(text, **kw):
    return N(edit=_noop)


async def _noop(**kw): pass


def plan(**over):
    p = {"title": "Summer Cup", "category_name": "Summer Cup",
         "roles": [{"name": "Cup Player", "color": "gold", "members": ["bob"]}],
         "channels": [
             {"name": "Cup Announcements", "type": "announcement", "read_only": True, "topic": "News"},
             {"name": "match lobby", "type": "text", "visibility": "private", "roles": ["Cup Player"],
              "intro": "Welcome! @everyone read this", "pin": True},
             {"name": "Team Voice", "type": "voice", "visibility": "private", "roles": ["Cup Player"]}]}
    p.update(over)
    return p


# ------------------------------------------------------------------ plan checks
def check_validation():
    g, chat, bot = setup()
    p, errs = builder.validate(g, plan())
    assert not errs, errs
    names = [c["name"] for c in p["channels"]]
    assert names == ["cup-announcements", "match-lobby", "Team Voice"], names
    assert p["roles"][0]["member_ids"] == [4] and p["roles"][0]["color"] == 0xF1C40F

    seven = plan(channels=[{"name": f"c{i}"} for i in range(7)], roles=[])
    _, errs = builder.validate(g, seven)
    assert any("at most 6" in e for e in errs), errs
    six = plan(channels=[{"name": f"c{i}"} for i in range(6)], roles=[])
    assert not builder.validate(g, six)[1]
    assert any("at least 1" in e for e in builder.validate(g, plan(channels=[]))[1])

    _, errs = builder.validate(g, plan(roles=[{"name": "Staff"}]))
    assert any("already exists" in e for e in errs)
    _, errs = builder.validate(g, plan(category_name="general"))
    assert any("already exists" in e for e in errs)
    _, errs = builder.validate(g, plan(channels=[{"name": "x", "visibility": "private"}], roles=[]))
    assert any("private" in e for e in errs)
    _, errs = builder.validate(g, plan(roles=[{"name": "everyone"}]))
    assert errs
    _, errs = builder.validate(g, plan(channels=[{"name": "a b"}, {"name": "a-b"}], roles=[]))
    assert any("twice" in e for e in errs)
    g.features = []
    _, errs = builder.validate(g, plan())
    assert any("Community" in e for e in errs)
    assert builder.clean_name("text", "@everyone Match Lobby!!") == "everyone-match-lobby"
    assert "@everyone" not in builder.clean_text("hi @everyone", 50)
    assert builder.parse_color("#3498db")[0] == 0x3498DB and builder.parse_color("nope")[1]


def check_ops():
    raw = plan()
    new, errs = builder.apply_ops(raw, [
        {"action": "rename_channel", "name": "match lobby", "new_name": "game room"},
        {"action": "rename_role", "name": "cup player", "new_name": "Finalist"},
        {"action": "remove_channel", "name": "Team Voice"}])
    assert not errs, errs
    g, _, _ = setup()
    p, errs = builder.validate(g, new)
    assert not errs, errs
    assert [c["name"] for c in p["channels"]] == ["cup-announcements", "game-room"]
    assert p["channels"][1]["roles"] == ["Finalist"] and p["roles"][0]["name"] == "Finalist"
    _, errs = builder.apply_ops(raw, [{"action": "rename_channel", "name": "nothing here", "new_name": "x"}])
    assert errs and "Channels:" in errs[0]


# ------------------------------------------------------------------ flow
async def check_flow():
    g, chat, bot = setup()
    ctx = ctx_for(g, bot, g.boss, chat)
    res = await registry.execute("propose_build", plan(), ctx)
    assert res.get("status") == "preview_posted", res
    assert not [c for c in g.channels if c.name == "cup-announcements"], "nothing may be built before Confirm"
    assert len(chat.sent) == 1 and "Confirm" in chat.sent[0].content
    d = builder.active_draft(1, 2)
    assert d and d.version == 1 and "<build_draft" in builder.prompt_block(1, 2)

    # change the plan: new preview, old one retired
    res = await registry.execute("revise_build", {"operations": [
        {"action": "rename_role", "name": "Cup Player", "new_name": "Finalist"},
        {"action": "rename_channel", "name": "Cup Announcements", "new_name": "cup news"}]}, ctx)
    assert res.get("status") == "revised_preview_posted" and res["version"] == 2, res
    assert builder.active_draft(1, 2).plan["roles"][0]["name"] == "Finalist"
    bad = await registry.execute("revise_build", {"operations": [{"action": "remove_channel", "name": "zzz"}]}, ctx)
    assert "error" in bad

    # someone else can't confirm; the owner can
    result = await builder.execute_draft(bot, builder.active_draft(1, 2), N(channel=chat, edit=_noop))
    assert not result.get("failed"), result
    cat = next(c for c in g.channels if c.name == "Summer Cup")
    kids = [c for c in g.channels if c.category is cat]
    assert [c.name for c in kids] == ["cup-news", "match-lobby", "Team Voice"] and len(kids) <= 6
    assert kids[0].type == "news" and kids[1].type == "text" and kids[2].type == "voice"
    lobby = kids[1]
    assert lobby.pinned and "@​everyone" in lobby.sent[0].content
    ow = {r.name: o for r, o in lobby.overwrites.items()}
    assert ow["@everyone"].view_channel is False and ow["Finalist"].view_channel is True
    role = next(r for r in g.roles if r.name == "Finalist")
    assert role in g.bob.roles and role.perms.value == 0, "role gets no permissions"
    assert builder.active_draft(1, 2) is None

    # undo removes exactly what was built
    out = await registry.execute("list_builds", {}, ctx)
    assert out["builds"][0]["title"] == "Summer Cup"
    before = len(g.channels)
    from tools import discord_utils
    discord_utils.confirm = _yes
    res = await registry.execute("undo_build", {}, ctx)
    assert res["status"] == "undone", res
    assert len(g.channels) == before - 4 and not [r for r in g.roles if r.name == "Finalist"]
    assert [r.name for r in g.roles] == ["@everyone", "Staff"] and role not in g.bob.roles
    assert "error" in await registry.execute("undo_build", {}, ctx)

    # cancel
    await registry.execute("propose_build", plan(), ctx)
    assert (await registry.execute("cancel_build_draft", {}, ctx))["status"] == "cancelled"
    assert builder.active_draft(1, 2) is None

    # permission: a plain member can't build
    plain = ctx_for(g, bot, g.bob, chat)
    assert "error" in await registry.execute("propose_build", plan(), plain)

    # stale draft is refused after the server changed (name taken)
    await registry.execute("propose_build", plan(), ctx)
    d = builder.active_draft(1, 2)
    await g.create_category("Summer Cup")
    r = await builder.execute_draft(bot, d, N(channel=chat, edit=_noop))
    assert "error" in r and not [c for c in g.channels if c.name == "cup-news"]


async def _yes(message, prompt):
    return True


# ------------------------------------------------------------------ edits
async def check_manage():
    g, chat, bot = setup()
    ctx = ctx_for(g, bot, g.boss, chat)
    from tools import discord_utils
    discord_utils.confirm = _yes
    ch = g.channels[1]
    r = await registry.execute("edit_channel", {"channel": "general", "new_name": "Lounge Room", "topic": "hi"}, ctx)
    assert r["status"] == "edited" and ch.name == "lounge-room" and ch.topic == "hi", r
    assert (await registry.execute("undo_build", {}, ctx))["status"] == "undone"
    assert ch.name == "general" and ch.topic is None

    staff = g.roles[1]
    r = await registry.execute("edit_role", {"role": "staff", "new_name": "Crew", "color": "red"}, ctx)
    assert r["status"] == "edited" and staff.name == "Crew" and staff.colour.value == 0xE74C3C, r
    await registry.execute("undo_build", {}, ctx)
    assert staff.name == "Staff"

    r = await registry.execute("set_channel_access", {"channel": "general", "role": "everyone", "access": "hide"}, ctx)
    assert r["status"] == "changed" and g.default_role in ch.overwrites
    await registry.execute("undo_build", {}, ctx)
    assert g.default_role not in ch.overwrites

    r = await registry.execute("change_member_role", {"member": "bob", "role": "Staff", "action": "add"}, ctx)
    assert r["status"] == "done" and staff in g.bob.roles
    assert "error" in await registry.execute("change_member_role", {"member": "bob", "role": "Staff", "action": "add"}, ctx)
    await registry.execute("undo_build", {}, ctx)
    assert staff not in g.bob.roles

    r = await registry.execute("post_announcement", {"channel": "general", "text": "Hi @everyone"}, ctx)
    assert r["status"] == "posted" and "@​everyone" in ch.sent[-1].content

    cat = g.channels[0]
    assert "error" in await registry.execute("delete_item", {"kind": "category", "name": "General"}, ctx)
    assert (await registry.execute("delete_item", {"kind": "channel", "name": "general"}, ctx))["status"] == "deleted"
    assert (await registry.execute("delete_item", {"kind": "category", "name": "General"}, ctx))["status"] == "deleted"
    assert "error" in await registry.execute("delete_item", {"kind": "role", "name": "everyone"}, ctx)

    # a normal member can't use any of it, and roles above the bot / author are refused
    plain = ctx_for(g, bot, g.bob, chat)
    assert "error" in await registry.execute("edit_channel", {"channel": "bot-chat", "new_name": "x"}, plain)
    g.roles.append(Role(g, 77, "Boss Role", 90))
    assert "error" in await registry.execute("edit_role", {"role": "Boss Role", "color": "red"}, ctx)


def check_agent_rules():
    for s in ("make channels for the tournament", "rename that role", "undo build", "set up a section"):
        assert agent.ACTION_RE.search(s), s
    assert agent.unbacked_claims("Done! I created the channels for you.", {"propose_build"})
    assert agent.unbacked_claims("I've built the category and roles.", set())
    assert not agent.unbacked_claims("Here's the plan, press Confirm to build it.", {"propose_build"})
    assert agent.unbacked_claims("Here is the plan, press Confirm below.", set())
    assert not agent.unbacked_claims("Here is the plan, press Confirm below.", {"propose_build"})
    assert agent.unbacked_claims("Renamed the channel.", set()) and not agent.unbacked_claims("Renamed it.", {"edit_channel"})


async def _all():
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "b.db")
    await db.init()
    try:
        await check_flow()
        await check_manage()
    finally:
        await db.close()
    check_validation()
    check_ops()
    check_agent_rules()


def test_builder():
    asyncio.run(_all())


if __name__ == "__main__":
    asyncio.run(_all())
    print("ALL BUILDER CHECKS PASSED")
