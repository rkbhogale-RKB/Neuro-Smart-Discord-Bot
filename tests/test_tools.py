"""Tests that run without Discord or Gemini: fake members/channels stand in for the real objects.
Run with:  python -m pytest tests   (or just:  python tests/test_tools.py)
Needs the same packages as the bot (discord.py etc.) and dummy env vars, set below."""
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

import agent  # noqa: E402,F401  (also checks that the whole agent module imports)
import app_profiles  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import diagnostics  # noqa: E402
from tools import app_tools, discord_utils as du, moderation_tools as mt  # noqa: E402
from tools import server_tools, social_tools  # noqa: E402,F401
from tools.registry import registry  # noqa: E402

N = pytypes.SimpleNamespace


class Rank:
    name = "Top"

    def __init__(self, p):
        self.position = p

    def __ge__(self, o):
        return self.position >= o.position


def mem(id, name, rank, bot=False, roles=(), **perms):
    gp = N(administrator=perms.get("admin", False), ban_members=perms.get("ban", False),
           kick_members=perms.get("kick", False), moderate_members=perms.get("mod", False),
           manage_messages=perms.get("msgs", False), read_message_history=True)
    sent = []

    async def send(text, **kw):
        sent.append(text)

    return N(id=id, name=name, display_name=name.title(), global_name=None, bot=bot,
             top_role=Rank(rank), guild_permissions=gp, roles=list(roles), mention=f"<@{id}>",
             status="online", dms=sent, send=send)


me = mem(1, "neuro", 50, bot=True, admin=True)
owner = mem(2, "boss", 100, admin=True)
modguy = mem(3, "modguy", 20, ban=True, msgs=True)
bob = mem(4, "bob", 10)
bobby = mem(5, "bobby", 10)
high = mem(6, "highrole", 60)
peer = mem(7, "peer", 20)
bolt = mem(8, "bolt", 5, bot=True)
hardik = mem(9, "hardik", 5)
everyone = [me, owner, modguy, bob, bobby, high, peer, bolt, hardik]
by_id = {m.id: m for m in everyone}

sent_in_channel = []


async def ch_send(text, **kw):
    sent_in_channel.append((text, kw.get("allowed_mentions")))


channel = N(id=100, name="general", type="text", members=[bob, hardik, modguy], send=ch_send,
            last_message_id=1, permissions_for=lambda m: N(view_channel=True, manage_messages=True))
secret = N(id=101, name="secret", type="text", members=[], send=ch_send,
           permissions_for=lambda m: N(view_channel=False))
voice = N(id=102, name="lounge", type="voice", members=[hardik],
          permissions_for=lambda m: N(view_channel=True))
admin_role = N(name="Admin", position=40, managed=False, members=[owner])
guild = N(id=1, name="Test", members=everyone, owner_id=2, owner=owner, me=me, chunked=True,
          get_member=by_id.get, roles=[N(name="@everyone", position=0, managed=False, members=everyone), admin_role],
          channels=[channel, secret, voice], voice_channels=[voice], text_channels=[channel, secret],
          member_count=len(everyone), created_at=__import__("datetime").datetime(2024, 1, 1),
          premium_tier=0, get_channel=lambda i: None)
admin_role.position = 40
owner.roles.append(admin_role)


class FakeBot:
    user = me

    def __init__(self, reply=True):
        self.reply = reply
        self.served = False

    async def wait_for(self, event, check=None, timeout=None):
        if not self.reply or self.served:          # an app answers once, then goes quiet
            await asyncio.sleep(min(timeout or 0.05, 0.05))
            raise asyncio.TimeoutError
        self.served = True
        await asyncio.sleep(0.01)

        async def fetch(_id):
            return m
        ch = N(id=100, fetch_message=fetch)
        m = N(id=500, channel=ch, author=bolt, content="pong", clean_content="pong", embeds=[],
              attachments=[], components=[])
        return m


def ctx_for(author, bot=None):
    return N(user_id=author.id, channel_id=100, server_id=1, bot=bot or FakeBot(),
             message=N(guild=guild, author=author, channel=channel, id=1000))


async def _noop(*a, **k):
    return 0

db.record_tool_call = _noop
db.count_tool_calls = _noop
config.APP_WAIT_SECONDS = 1


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- member lookup
def test_resolve_member():
    assert du.resolve_member(guild, "<@4>")[0] is bob
    assert du.resolve_member(guild, "peer")[0] is peer
    assert du.resolve_member(guild, "bob")[0] is bob                 # exact beats partial 'bobby'
    assert "ambiguous" in du.resolve_member(guild, "bo")[1]
    assert du.resolve_member(guild, "nobody")[0] is None


def test_fuzzy_only_when_allowed():
    m, err = du.resolve_member(guild, "hardick")                     # typo, strict: never guess
    assert m is None and "hardik" in err.lower()
    assert du.resolve_member(guild, "hardick", fuzzy=True)[0] is hardik
    assert hardik in du.search_members(guild, "hardick")


# ---------------------------------------------------------------- moderation rules
def test_hierarchy_checks():
    chk = lambda a, t, p="ban_members": mt._check_target(ctx_for(a), t, p)
    assert chk(modguy, bob) is None
    assert "mine" in chk(modguy, high)
    assert "yours" in chk(modguy, peer)
    assert "owner" in chk(modguy, owner)
    assert "yourself" in chk(modguy, modguy)
    assert "permission" in chk(bob, peer)
    assert chk(owner, bob, "kick_members") is None


# ---------------------------------------------------------------- pinging
def test_mention_member_pings_only_that_person():
    sent_in_channel.clear()
    r = run(registry.execute("mention_member", {"member": "hardick", "message": "come here @everyone"},
                             ctx_for(bob)))
    assert r["status"] == "pinged", r
    text, allowed = sent_in_channel[0]
    assert text.startswith("<@9> ") and "@everyone" not in text.replace("@​everyone", "")
    assert getattr(allowed, "users") == [hardik]


def test_dm_member():
    r = run(registry.execute("dm_member", {"member": "hardik", "text": "hi"}, ctx_for(bob)))
    assert r["status"] == "dm_sent" and "From **Bob**" in hardik.dms[0]
    assert "error" in run(registry.execute("dm_member", {"member": "bolt", "text": "hi"}, ctx_for(bob)))


# ---------------------------------------------------------------- server awareness
def test_list_members_variants():
    r = run(registry.execute("list_members", {}, ctx_for(bob)))
    assert r["total"] == 7 and not any("[bot]" in m for m in r["members"])       # bots hidden
    r = run(registry.execute("list_members", {"scope": "channel"}, ctx_for(bob)))
    assert r["total"] == 3
    r = run(registry.execute("list_members", {"role": "Admin"}, ctx_for(bob)))
    assert r["total"] == 1 and "Boss" in r["members"][0]
    r = run(registry.execute("list_members", {"role": "Nope"}, ctx_for(bob)))
    assert "error" in r and "Admin" in r["roles"]
    r = run(registry.execute("list_members", {"include_bots": True, "limit": 3}, ctx_for(bob)))
    assert r["total"] == 9 and len(r["members"]) == 3 and "note" in r


def test_channels_roles_voice_info():
    r = run(registry.execute("list_channels", {}, ctx_for(bob)))
    assert r["total"] == 2                                            # 'secret' is hidden from bob
    assert run(registry.execute("list_voice_members", {}, ctx_for(bob)))["voice_channels"] == {"lounge": ["Hardik"]}
    assert run(registry.execute("list_roles", {}, ctx_for(bob)))["total"] == 1
    assert run(registry.execute("server_info", {}, ctx_for(bob)))["members"] == 9


# ---------------------------------------------------------------- talking to apps
def test_app_profile_prefix_and_slash():
    config.APP_PROFILES_PATH = os.path.join(tempfile.mkdtemp(), "p.json")
    assert app_profiles.get(bolt) == {}
    app_profiles.set_profile("Bolt", prefix="!", wait=3)
    assert app_profiles.get(bolt) == {"prefix": "!", "wait": 3.0}

    sent_in_channel.clear()
    r = run(registry.execute("run_app_command", {"app": "bolt", "text": "help"}, ctx_for(owner)))
    assert r["replies"] == ["pong"], r
    assert sent_in_channel[0][0] == "!help"                           # prefix applied from profile

    r = run(registry.execute("run_app_command", {"app": "bolt", "text": "/ping"}, ctx_for(owner)))
    assert r["slash_only"] and "/ping" in r["note"]
    app_profiles.set_profile("bolt", slash_only=True)
    r = run(registry.execute("run_app_command", {"app": "bolt", "text": "help"}, ctx_for(owner)))
    assert r["slash_only"]

    app_profiles.set_profile("bolt", slash_only=False, mention=True)
    sent_in_channel.clear()
    run(registry.execute("run_app_command", {"app": "bolt", "text": "what is a function"}, ctx_for(owner)))
    assert sent_in_channel[0][0] == "<@8> what is a function"         # mention instead of prefix


def test_app_no_reply_has_helpful_note():
    r = run(registry.execute("run_app_command", {"app": "bolt", "text": "hello"},
                             ctx_for(owner, FakeBot(reply=False))))
    assert r["replies"] == [] and "prefix" in r["note"]


def test_apps_hidden_from_non_mods():
    names = [d.name for d in registry.declarations(ctx_for(bob))]
    assert "run_app_command" not in names and "ban_member" not in names
    assert "mention_member" in names and "list_members" in names


# ---------------------------------------------------------------- registry robustness
def test_registry_arg_handling():
    # unknown extra argument from the model is dropped, not fatal
    r = run(registry.execute("find_members", {"query": "hardik", "bogus": 1}, ctx_for(bob)))
    assert r["matches"][0]["name"] == "Hardik"
    # missing required argument gives a clear error
    assert "bad arguments" in run(registry.execute("find_members", {}, ctx_for(bob)))["error"]
    # unknown tool / not allowed
    assert run(registry.execute("ban_member", {"member": "bob"}, ctx_for(bob))) == {"error": "tool not available here"}


# ---------------------------------------------------------------- diagnostics + routing
def test_status_report():
    bot = N(user=me, intents=N(message_content=True))
    text = diagnostics.status_report(bot, guild)
    assert "Administrator permission" in text and "Bolt" in text


def test_action_router_regex():
    for s in ("call hardik", "ban <@5> for spam", "list members", "ask bolt what is a function"):
        assert agent.ACTION_RE.search(s), s
    assert not agent.ACTION_RE.search("lol that was great")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for n, f in tests:
        f()
        print("ok  ", n)
    print(f"ALL {len(tests)} PASSED")
