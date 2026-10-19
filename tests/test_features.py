"""Tests for warnings, reminders and the event log. They use a REAL SQLite database in a temp
folder and fake Discord objects, so they need no Discord connection or API key.
Run with:  python -m pytest tests   (or just:  python tests/test_features.py)"""
import asyncio
import datetime as dt
import os
import sys
import tempfile
import time
import types as pytypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("OWNER_ID", "999")

import agent  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import event_log  # noqa: E402
import reminders  # noqa: E402
from tools import discord_utils as du  # noqa: E402
from tools import reminder_tools, warning_tools  # noqa: E402,F401
from tools.registry import registry  # noqa: E402

N = pytypes.SimpleNamespace
UTC = dt.timezone.utc


# ------------------------------------------------------------------ fakes
class Rank:
    name = "Top"

    def __init__(self, p):
        self.position = p

    def __ge__(self, o):
        return self.position >= o.position


class AllPerms:
    """The bot is admin: every permission is True."""
    def __getattr__(self, name):
        return True


def member(id, name, rank, bot=False, **perms):
    gp = AllPerms() if perms.get("admin") else N(
        administrator=False, ban_members=False, kick_members=False, moderate_members=False,
        manage_messages=perms.get("msgs", False), read_message_history=True, view_audit_log=False)
    m = N(id=id, name=name, display_name=name.title(), global_name=None, bot=bot, top_role=Rank(rank),
          guild_permissions=gp, roles=[], mention=f"<@{id}>", dms=[], timeouts=[],
          created_at=dt.datetime.now(UTC) - dt.timedelta(days=400),
          joined_at=dt.datetime.now(UTC) - dt.timedelta(days=30), nick=None, timed_out_until=None)

    async def send(text, **kw):
        m.dms.append(text)

    async def timeout(delta, reason=None):
        m.timeouts.append((delta, reason))

    m.send, m.timeout = send, timeout
    return m


me = member(1, "neuro", 50, bot=True, admin=True)
owner = member(2, "boss", 100, admin=True)
modguy = member(3, "modguy", 20, msgs=True)
bob = member(4, "bob", 10)
hardik = member(9, "hardik", 5)
high = member(6, "highrole", 60)
bolt = member(8, "bolt", 5, bot=True)
people = [me, owner, modguy, bob, hardik, high, bolt]


class Chan:
    def __init__(self, id, name):
        self.id, self.name, self.type, self.sent = id, name, "text", []

    async def send(self, text, **kw):
        self.sent.append((text, kw))

    def permissions_for(self, m):
        return AllPerms()


log_chan = Chan(500, "mod-log")
general = Chan(100, "general")
channels = {500: log_chan, 100: general}
audit = []                                   # entries returned by guild.audit_logs


class Guild:
    id, name, owner_id, owner, me = 1, "Test", 2, owner, me
    members = people

    def get_member(self, i):
        return next((m for m in people if m.id == i), None)

    def get_channel(self, i):
        return channels.get(i)

    async def audit_logs(self, limit=None, action=None):
        for e in list(audit):
            if e.action == action:
                yield e


guild = Guild()
for m in people:
    m.guild = guild


class FakeBot:
    user = me

    def __init__(self):
        self.listeners = {}

    def get_guild(self, i):
        return guild if i == 1 else None

    def get_channel(self, i):
        return channels.get(i)

    def get_user(self, i):
        return guild.get_member(i)

    async def fetch_user(self, i):
        return guild.get_member(i)

    def add_listener(self, fn, name):
        self.listeners[name] = fn


bot = FakeBot()


def ctx_for(author, channel=general):
    return N(user_id=author.id, channel_id=channel.id, server_id=1, bot=bot,
             message=N(guild=guild, author=author, channel=channel, id=1000))


def logged():
    return [t for t, _ in log_chan.sent]


def last_log():
    return logged()[-1] if log_chan.sent else ""


def entry(action, target, by=None, reason=None):
    audit.append(N(action=action, target=target, user=by or modguy, reason=reason,
                   created_at=dt.datetime.now(UTC)))


def pl(**kw):
    base = dict(guild_id=1, channel_id=100, message_id=1, cached_message=None, data={}, message_ids=set())
    base.update(kw)
    return N(**base)


async def X(name, args, author, **kw):
    return await registry.execute(name, args, ctx_for(author, **kw))


# ------------------------------------------------------------------ warnings
async def check_warnings():
    config.WARN_ESCALATION = [(2, 30)]
    config.WARN_EXPIRE_DAYS = 30
    await db.set_log_channel(1, 500)

    r = await X("warn_member", {"member": "bob", "reason": "spamming"}, modguy)
    assert r["status"] == "warned" and r["active_warnings"] == 1 and r["dm"] == "sent", r
    assert "spamming" in bob.dms[-1] and not bob.timeouts
    assert "warned" in last_log().lower() and "spamming" in last_log()

    r = await X("warn_member", {"member": "bob", "reason": "again"}, modguy)
    assert r["active_warnings"] == 2 and r["auto_timeout_minutes"] == 30, r
    assert bob.timeouts[-1][0] == dt.timedelta(minutes=30)
    assert "auto-timeout" in last_log()

    # who may see / do what
    assert "warn_member" not in [d.name for d in registry.declarations(ctx_for(bob))]
    assert {"set_reminder", "list_warnings"} <= {d.name for d in registry.declarations(ctx_for(bob))}
    r = await X("list_warnings", {"member": "bob"}, modguy)
    assert r["active_warnings"] == 2 and r["warnings"][0]["by"] == "Modguy"
    assert (await X("list_warnings", {"member": "me"}, bob))["active_warnings"] == 2   # own warnings
    assert "only moderators" in (await X("list_warnings", {"member": "hardik"}, bob))["error"]

    # refusals
    assert "reason" in (await X("warn_member", {"member": "bob", "reason": "  "}, modguy))["error"]
    assert "mine" in (await X("warn_member", {"member": "highrole", "reason": "x"}, modguy))["error"]
    assert "bot" in (await X("warn_member", {"member": "bolt", "reason": "x"}, modguy))["error"]
    assert "yourself" in (await X("warn_member", {"member": "me", "reason": "x"}, modguy))["error"]

    # remove / clear
    wid = (await X("list_warnings", {"member": "bob"}, modguy))["warnings"][1]["id"]
    r = await X("remove_warning", {"member": "bob", "warning_id": wid}, modguy)
    assert r["active_warnings"] == 1, r
    assert "no such warning" in (await X("remove_warning", {"member": "bob", "warning_id": 9999}, modguy))["error"]
    r = await X("remove_warning", {"member": "bob"}, modguy)               # newest
    assert r["active_warnings"] == 0

    await X("warn_member", {"member": "bob", "reason": "one"}, modguy)
    real_confirm = du.confirm

    async def no(*a, **k):
        return False

    async def yes(*a, **k):
        return True
    du.confirm = no
    r = await X("clear_warnings", {"member": "bob"}, modguy)
    assert r["status"].startswith("not confirmed") and not agent.action_succeeded(r)
    assert len(await db.active_warnings(1, 4)) == 1
    du.confirm = yes
    r = await X("clear_warnings", {"member": "bob"}, modguy)
    assert r["removed"] == 1 and len(await db.active_warnings(1, 4)) == 0
    du.confirm = real_confirm

    # expiry: an old warning stops counting
    await db.add_warning(1, 4, 3, "ancient")
    await db._db.execute("UPDATE warnings SET ts=? WHERE reason='ancient'", (time.time() - 40 * 86400,))
    await db._db.commit()
    assert len(await db.active_warnings(1, 4)) == 0
    config.WARN_EXPIRE_DAYS = 0
    assert len(await db.active_warnings(1, 4)) == 1                        # 0 = never expires
    config.WARN_EXPIRE_DAYS = 30

    assert warning_tools.escalation_for(2) == 30 and warning_tools.escalation_for(1) is None
    assert config._escalation("3:60, 5:1440,bad,0:5") == [(3, 60), (5, 1440)]


# ------------------------------------------------------------------ reminders
async def check_reminders():
    assert reminder_tools.parse_at("2026-10-21 18:00") is not None
    assert reminder_tools.parse_at("tomorrow") is None
    t = reminder_tools.parse_at("2026-10-21 18:00")
    assert dt.datetime.fromtimestamp(t, config.TZ).strftime("%H:%M") == "18:00"   # read in the bot's timezone

    r = await X("set_reminder", {"text": "drink water", "in_minutes": 60}, bob)
    assert r["status"] == "reminder_set" and r["for"] == "Bob" and r["repeat"] == "no", r
    rid = r["reminder_id"]
    r2 = await X("set_reminder", {"text": "standup", "in_minutes": 30, "member": "hardik", "repeat": "daily"}, bob)
    assert r2["for"] == "Hardik" and r2["repeat"] == "daily"

    errs = {
        "past": ({"text": "x", "in_minutes": -5}, "past"),
        "both": ({"text": "x", "in_minutes": 5, "at": "2030-01-01 10:00"}, "not both"),
        "none": ({"text": "x"}, "when"),
        "badat": ({"text": "x", "at": "next friday"}, "couldn't read"),
        "badrep": ({"text": "x", "in_minutes": 5, "repeat": "hourly"}, "daily or weekly"),
        "bot": ({"text": "x", "in_minutes": 5, "member": "bolt"}, "bot"),
        "far": ({"text": "x", "in_minutes": 60 * 24 * 400}, "year"),
        "empty": ({"text": " ", "in_minutes": 5}, "say"),
    }
    for k, (args, word) in errs.items():
        r = await X("set_reminder", args, bob)
        assert word in r["error"], (k, r)

    r = await X("list_reminders", {}, bob)
    assert r["total"] == 2
    assert (await X("list_reminders", {}, hardik))["total"] == 0
    assert "moderators" in (await X("list_reminders", {"all_users": True}, hardik))["error"]
    assert (await X("list_reminders", {"all_users": True}, modguy))["total"] == 2
    assert "isn't your" in (await X("cancel_reminder", {"reminder_id": rid}, hardik))["error"]
    assert (await X("cancel_reminder", {"reminder_id": rid}, bob))["status"] == "reminder_cancelled"
    assert "no pending" in (await X("cancel_reminder", {"reminder_id": rid}, bob))["error"]
    assert (await X("cancel_reminder", {"reminder_id": r2["reminder_id"]}, modguy))["status"] == "reminder_cancelled"

    # per-person cap
    config.MAX_PENDING_REMINDERS = 2
    await X("set_reminder", {"text": "a", "in_minutes": 5}, hardik)
    await X("set_reminder", {"text": "b", "in_minutes": 5}, hardik)
    assert "pending" in (await X("set_reminder", {"text": "c", "in_minutes": 5}, hardik))["error"]
    config.MAX_PENDING_REMINDERS = 25

    # next_due: repeating reminders skip periods missed while offline
    now = 1_000_000.0
    assert reminders.next_due(now - 10, "", now) is None
    assert reminders.next_due(now - 10, "daily", now) == now - 10 + 86400
    nxt = reminders.next_due(now - 5 * 86400 - 10, "daily", now)
    assert nxt > now and nxt - now <= 86400

    # delivery: on time, then never again
    general.sent.clear()
    await db.add_reminder(1, 100, 4, 4, "stretch", time.time() - 10, "")
    assert await reminders.tick(bot) == 1
    text, kw = general.sent[-1]
    assert text.startswith("⏰ <@4> reminder: stretch") and "offline" not in text
    assert [u.id for u in kw["allowed_mentions"].users] == [4]            # pings only the target
    assert await reminders.tick(bot) == 0

    # late delivery says so
    await db.add_reminder(1, 100, 4, 4, "old news", time.time() - 3000, "")
    await reminders.tick(bot)
    assert "offline" in general.sent[-1][0]

    # reminder from someone else mentions who set it
    await db.add_reminder(1, 100, 3, 9, "call mom", time.time() - 5, "")
    await reminders.tick(bot)
    assert "<@9>" in general.sent[-1][0] and "from <@3>" in general.sent[-1][0]

    # repeating: delivered, then rescheduled for later
    rid = await db.add_reminder(1, 100, 4, 4, "daily thing", time.time() - 10, "daily")
    await reminders.tick(bot)
    row = await db.get_reminder(rid)
    assert row["sent"] == 0 and row["due_ts"] > time.time() + 80000

    # exactly once even if two loops race
    general.sent.clear()
    await db.add_reminder(1, 100, 4, 4, "race", time.time() - 5, "")
    a, b = await asyncio.gather(reminders.tick(bot), reminders.tick(bot))
    assert a + b == 1 and len(general.sent) == 1

    # channel gone -> falls back to a DM
    channels.pop(100)
    before = len(bob.dms)
    await db.add_reminder(1, 100, 4, 4, "dm fallback", time.time() - 5, "")
    assert await reminders.tick(bot) == 1 and len(bob.dms) == before + 1 and "dm fallback" in bob.dms[-1]
    channels[100] = general


# ------------------------------------------------------------------ event log
async def check_event_log():
    log_chan.sent.clear()
    audit.clear()
    # a message we logged earlier, now deleted (not cached)
    await db.log_message(7001, 1, 100, 4, "Bob", False, "secret plan", time.time())
    await event_log.message_deleted(bot, pl(message_id=7001))
    assert "Bob (4)" in last_log() and "secret plan" in last_log() and "#general" in last_log()

    n = len(log_chan.sent)
    await db.log_message(7002, 1, 100, 8, "Bolt [app]", False, "pong", time.time())
    await event_log.message_deleted(bot, pl(message_id=7002))                 # other apps: ignored
    await db.log_message(7003, 1, 100, 1, "Neuro", True, "hi", time.time())
    await event_log.message_deleted(bot, pl(message_id=7003))                 # our own: ignored
    assert len(log_chan.sent) == n
    await event_log.message_deleted(bot, pl(message_id=424242))               # unknown message
    assert "wasn't in my log" in last_log()
    await event_log.message_deleted(bot, pl(message_id=7001, channel_id=500))  # inside the log channel
    assert len(log_chan.sent) == n + 1

    # private (/noshare) channels never leak content
    await db.set_no_share(100, True)
    await event_log.message_deleted(bot, pl(message_id=7001))
    assert "secret plan" not in last_log() and "private" in last_log()
    # edits
    await event_log.message_edited(bot, pl(message_id=7001, data={"content": "new text", "author": {"id": 4}}))
    assert "new text" not in last_log() and "private" in last_log()
    await db.set_no_share(100, False)

    await event_log.message_edited(bot, pl(message_id=7001, data={"content": "new text", "author": {"id": 4}}))
    assert "Before" in last_log() and "secret plan" in last_log() and "new text" in last_log()
    n = len(log_chan.sent)
    await event_log.message_edited(bot, pl(message_id=7001, data={"content": "secret plan"}))      # unchanged
    await event_log.message_edited(bot, pl(message_id=7001, data={"embeds": []}))                  # preview only
    await event_log.message_edited(bot, pl(message_id=7001, data={"content": "x", "author": {"id": 8, "bot": True}}))
    assert len(log_chan.sent) == n
    await event_log.messages_bulk_deleted(bot, pl(message_ids={1, 2, 3}))
    assert "3 messages" in last_log()

    # members
    new = member(20, "newbie", 1)
    new.created_at = dt.datetime.now(UTC) - dt.timedelta(days=1)
    new.guild = guild
    await event_log.member_joined(bot, new)
    assert "new account" in last_log()
    await event_log.member_joined(bot, bob)
    assert "new account" not in last_log() and "joined" in last_log()

    audit.clear()
    await event_log.member_left(bot, bob)
    assert "left" in last_log()
    entry(event_log.discord.AuditLogAction.kick, bob, reason="spam")
    await event_log.member_left(bot, bob)
    assert "kicked by Modguy (3)" in last_log() and "spam" in last_log()
    audit.clear()
    entry(event_log.discord.AuditLogAction.ban, bob)
    n = len(log_chan.sent)
    await event_log.member_left(bot, bob)                                       # the ban event reports it
    assert len(log_chan.sent) == n
    await event_log.member_banned(bot, guild, bob)
    assert "banned by Modguy" in last_log()
    await event_log.member_unbanned(bot, guild, bob)
    assert "unbanned" in last_log()

    # member updates
    before = N(nick=None, roles=[], timed_out_until=None, guild=guild, id=4)
    r1 = N(name="Gamer")
    after = N(nick="Bobby", roles=[r1], timed_out_until=dt.datetime.now(UTC) + dt.timedelta(minutes=10),
              guild=guild, id=4, display_name="Bob", name="bob")
    audit.clear()
    entry(event_log.discord.AuditLogAction.member_role_update, bob)
    entry(event_log.discord.AuditLogAction.member_update, bob, reason="spam")
    await event_log.member_updated(bot, before, after)
    texts = "\n".join(logged()[-3:])
    assert "Nickname" in texts and "gained Gamer" in texts and "timed out until" in texts and "by Modguy" in texts

    # roles / channels / categories
    await event_log.role_created(bot, N(guild=guild, name="Helper"))
    assert "Role created" in last_log()
    await event_log.channel_deleted(bot, N(guild=guild, name="old", type="text", id=77))
    assert "Channel deleted" in last_log()
    vs_before, vs_after = N(channel=None), N(channel=N(name="Lounge"))
    n = len(log_chan.sent)
    await event_log.voice_changed(bot, bob, vs_before, vs_after)
    assert len(log_chan.sent) == n                                              # voice is off by default
    config.EVENT_LOG_EVENTS.add("voice")
    await event_log.voice_changed(bot, bob, vs_before, vs_after)
    assert "joined voice" in last_log()
    config.EVENT_LOG_EVENTS.discard("voice")
    config.EVENT_LOG_EVENTS.discard("messages")
    n = len(log_chan.sent)
    await event_log.message_deleted(bot, pl(message_id=7001))
    assert len(log_chan.sent) == n                                              # category switched off
    config.EVENT_LOG_EVENTS.add("messages")

    # no log channel anywhere -> silent, no crash
    await db.set_log_channel(1, None)
    config.MOD_LOG_CHANNEL_ID = config.EVENT_LOG_CHANNEL_ID = 0
    n = len(log_chan.sent)
    await event_log.role_created(bot, N(guild=guild, name="Nobody sees"))
    await du.mod_log(guild, "also silent")
    assert len(log_chan.sent) == n
    config.EVENT_LOG_CHANNEL_ID = 500                                           # env fallback works
    await event_log.role_created(bot, N(guild=guild, name="Seen"))
    assert "Seen" in last_log()
    config.EVENT_LOG_CHANNEL_ID = 0
    await db.set_log_channel(1, 500)

    # setup registers every listener, and a crashing handler can't break the bot
    fb = FakeBot()
    event_log.setup(fb)
    assert set(fb.listeners) == set(event_log.HANDLERS)
    real = event_log.HANDLERS["on_member_join"]

    async def boom(*a):
        raise RuntimeError("x")
    fb2 = FakeBot()
    event_log.HANDLERS["on_member_join"] = boom
    event_log.setup(fb2)
    await fb2.listeners["on_member_join"](bob)                                  # must not raise
    event_log.HANDLERS["on_member_join"] = real


# ------------------------------------------------------------------ agent routing
def check_agent_rules():
    for s in ("remind me at 6pm to eat", "warn bob for spam", "show bob's warnings", "cancel reminder 3"):
        assert agent.ACTION_RE.search(s), s
    assert agent.unbacked_claims("Okay, I'll remind you at 6!", set())
    assert not agent.unbacked_claims("Okay, I'll remind you at 6!", {"set_reminder"})
    assert agent.unbacked_claims("Hardik has been warned.", {"set_reminder"})
    assert not agent.unbacked_claims("Hardik has been warned.", {"warn_member"})


async def _all():
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    await db.init()
    try:
        await check_warnings()
        await check_reminders()
        await check_event_log()
    finally:
        await db.close()
    check_agent_rules()


def test_warnings_reminders_event_log():
    asyncio.run(_all())


if __name__ == "__main__":
    asyncio.run(_all())
    print("ALL FEATURE CHECKS PASSED")
