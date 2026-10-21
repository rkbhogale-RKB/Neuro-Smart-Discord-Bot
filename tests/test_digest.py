"""Tests for the server digest (real SQLite, fake Discord objects).
Run with:  python -m pytest tests   (or:  python tests/test_digest.py)"""
import asyncio
import os
import re
import sys
import tempfile
import time
import types as pytypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("GEMINI_API_KEY", "x")

import agent  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
from tools import digest_tools  # noqa: E402,F401
from tools.registry import registry  # noqa: E402

N = pytypes.SimpleNamespace


def chan(id, name, cat, can_view=True):
    c = N(id=id, name=name, type="text", category=cat)
    c.permissions_for = lambda m: N(view_channel=can_view, read_message_history=can_view)
    return c


async def _all():
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "d.db")
    await db.init()
    try:
        game, chat = N(name="Games"), N(name="Chat")
        chans = [chan(1, "scrims", game), chan(2, "general", chat), chan(3, "secret", chat, can_view=False),
                 chan(4, "private", chat), chan(5, "quiet", chat)]
        guild = N(id=1, channels=chans, get_channel=lambda i: None)
        author = N(id=7)
        ctx = N(user_id=7, channel_id=2, server_id=1, bot=None, message=N(guild=guild, author=author))
        now, mid = time.time(), 1
        for cid, n in ((1, 30), (2, 5), (3, 4), (4, 3)):
            for i in range(n):
                await db.log_message(mid, 1, cid, 7, "ann", False, f"msg {i} in {cid}", now - 1000 + mid)
                mid += 1
        await db.log_message(mid, 1, 1, 8, "Bolt [app]", False, "app noise", now); mid += 1
        await db.log_message(mid + 1, 1, 2, 7, "ann", False, "!help", now)
        await db.set_no_share(4, True)

        r = await registry.execute("server_digest", {}, ctx)
        text = r["summary_input"]
        assert "## Games" in text and "## Chat" in text
        assert len(re.findall(r"msg \d+ in 1$", text, re.M)) == 12, "per-channel cap"
        assert "msg 29 in 1" in text and "msg 0 in 1" not in text, "newest messages kept"
        assert "30 msgs" in text and "last 12 shown" in text
        assert "secret" not in text and "msg 0 in 3" not in text, "unreadable channel hidden"
        assert not re.search(r"msg \d+ in 4$", text, re.M), "/noshare channel skipped"
        assert "app noise" not in text and "!help" not in text
        assert r["hidden_channels"] == 2

        only = await registry.execute("server_digest", {"category": "games"}, ctx)
        assert "## Chat" not in only["summary_input"]
        none = await registry.execute("server_digest", {"channel": "quiet"}, ctx)
        assert none["summary_input"] == "" and "No recent" in none["note"]

        # token cap on a large server
        for i in range(40):
            await db.log_message(5000 + i, 1, 5, 7, "ann", False, "x" * 300, now)
        big = await registry.execute("server_digest", {"messages_per_channel": 15}, ctx)
        assert len(big["summary_input"]) < 11000, len(big["summary_input"])
    finally:
        await db.close()
    for s in ("hey neuro what happened across channels", "summarize the server", "catch me up", "recap please"):
        assert agent.ACTION_RE.search(s), s


def test_digest():
    asyncio.run(_all())


if __name__ == "__main__":
    asyncio.run(_all())
    print("DIGEST CHECKS PASSED")
