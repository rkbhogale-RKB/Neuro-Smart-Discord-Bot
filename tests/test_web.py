"""Tests for web search, image search/sending and GIFs. All network calls are replaced by fakes, so these
need no internet and no API keys. Run with:  python -m pytest tests   (or:  python tests/test_web.py)"""
import asyncio
import os
import sys
import tempfile
import types as pytypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("GEMINI_API_KEY", "x")

import agent  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import web  # noqa: E402
from tools import web_tools  # noqa: E402,F401
from tools.registry import registry  # noqa: E402

N = pytypes.SimpleNamespace
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


# ------------------------------------------------------------------ pure parsing / safety
async def check_safety():
    assert web.blocked_query("cute NSFW cats") == "nsfw" and web.blocked_query("a tiger in the snow") is None
    assert web.blocked_query("Essex") is None, "whole words only"
    for ip, ok in (("8.8.8.8", True), ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.0.9", False),
                   ("169.254.169.254", False), ("::1", False), ("0.0.0.0", False)):
        assert web._public_ip(ip) is ok, ip
    assert await web.is_public_url("http://169.254.169.254/latest/meta-data") is False
    assert await web.is_public_url("https://127.0.0.1/x.png") is False
    assert await web.is_public_url("file:///etc/passwd") is False
    assert await web.is_public_url("ftp://example.com/a.png") is False
    assert await web.is_public_url("https://8.8.8.8/a.png") is True
    assert web.sniff(PNG) == "png" and web.sniff(b"\xff\xd8\xff\xe0abc") == "jpg" and web.sniff(b"GIF89a....") == "gif"
    assert web.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp" and web.sniff(b"<html>nope") is None


def check_parsers():
    p = web.parse_pexels({"photos": [{"src": {"large": "https://i/p.jpg"}, "alt": "A tiger", "url": "https://pexels/1",
                                      "photographer": "Sam"}, {"src": {}}]})
    assert p == [{"url": "https://i/p.jpg", "title": "A tiger", "page": "https://pexels/1",
                  "credit": "Photo by Sam on Pexels"}]
    d = web.parse_ddg({"results": [{"image": "https://x/a.jpg", "title": "Cat", "url": "https://x", "source": "Bing"}, {}]})
    assert len(d) == 1 and d[0]["credit"] == "via Bing"
    c = web.parse_commons({"query": {"pages": {
        "2": {"index": 2, "title": "File:B.png", "imageinfo": [{"mime": "image/png", "url": "https://c/b.png"}]},
        "1": {"index": 1, "title": "File:Tiger.jpg", "imageinfo": [{
            "mime": "image/jpeg", "thumburl": "https://c/t.jpg", "descriptionurl": "https://c/File:Tiger.jpg",
            "extmetadata": {"Artist": {"value": "<a href='x'>Jane</a>"}}}]},
        "3": {"index": 3, "title": "File:v.ogv", "imageinfo": [{"mime": "video/ogg", "url": "https://c/v"}]}}}})
    assert [x["title"] for x in c] == ["Tiger", "B"] and c[0]["credit"] == "Wikimedia Commons · Jane"
    g = web.parse_giphy({"data": [{"url": "https://giphy.com/gifs/a", "title": "Wow"}, {"url": "http://insecure"}, {}]})
    assert g == [{"url": "https://giphy.com/gifs/a", "title": "Wow"}]
    assert web.pick_gif([]) is None and web.pick_gif(g)["title"] == "Wow"


async def check_provider_chain():
    calls = []

    async def ddg(q, n): calls.append("ddg"); raise RuntimeError("blocked")

    async def commons(q, n): calls.append("commons"); return [{"url": "https://c/1.jpg", "title": "T", "page": None, "credit": "c"}]

    async def pexels(q, n): calls.append("pexels"); return []
    web._ddg, web._commons, web._pexels = ddg, commons, pexels
    config.PEXELS_API_KEY = ""
    r = await web.search_images("tiger", 1)
    assert calls == ["ddg", "commons"] and len(r) == 1, "a failing provider falls through to the next"
    calls.clear()
    config.PEXELS_API_KEY = "k"
    await web.search_images("tiger", 1)
    assert calls[0] == "pexels"
    config.PEXELS_API_KEY = ""


async def check_web_answer():
    class FakeModels:
        async def generate_content(self, model, contents, config):
            assert model == web.config.SEARCH_MODEL and contents == "who won"
            web_ = N(uri="https://news.example/a", title="news.example")
            return N(text="The Reds won 2-1.", candidates=[N(grounding_metadata=N(grounding_chunks=[
                N(web=web_), N(web=web_), N(web=None)]))])
    llm.client = N(aio=N(models=FakeModels()))
    r = await web.web_answer("who won")
    assert r["text"] == "The Reds won 2-1." and r["sources"] == [{"title": "news.example", "url": "https://news.example/a"}]

    class Broken:
        async def generate_content(self, **kw): raise RuntimeError("quota")
    llm.client = N(aio=N(models=Broken()))
    assert "error" in await web.web_answer("x")


# ------------------------------------------------------------------ tools
class Chan:
    def __init__(self, perms=True):
        self.sent, self.perms = [], perms

    def permissions_for(self, m): return N(attach_files=self.perms, embed_links=self.perms)

    async def send(self, content=None, **kw): self.sent.append((content, kw))

    async def fetch_message(self, i): return None


def make_ctx(ch, reference=None):
    replies = []

    async def reply(content, **kw): replies.append(("msg", content, kw))
    msg = N(guild=N(me=N(id=1)), channel=ch, author=N(id=7), reply=reply, reference=reference, replies=replies)
    return N(user_id=7, channel_id=1, server_id=1, bot=None, message=msg), replies


async def check_tools():
    # web_search: one grounded call, answer posted directly with sources
    async def answer(q):
        return {"text": "Neuro bot news.", "sources": [{"title": "site", "url": "https://s.example/x"}]}
    web.web_answer = answer
    ch = Chan()
    ctx, _ = make_ctx(ch)
    r = await registry.execute("web_search", {"query": "latest news"}, ctx)
    assert r["direct_reply"].startswith("Neuro bot news.") and "[site](<https://s.example/x>)" in r["direct_reply"]
    assert agent.action_succeeded(r)

    async def failing(q): return {"error": "web search isn't available right now"}
    web.web_answer = failing
    assert "error" in await registry.execute("web_search", {"query": "x"}, ctx)

    # send_image: skips undownloadable candidates, uploads files, refuses blocked words and missing permissions
    async def search(q, n):
        return [{"url": f"https://i/{i}.png", "title": f"T{i}", "page": "https://p", "credit": "c"} for i in range(6)]

    async def fetch(url, max_bytes=None):
        return None if url.endswith(("0.png", "1.png")) else (PNG, "png")
    web.search_images, web.fetch_image = search, fetch
    r = await registry.execute("send_image", {"query": "tiger", "count": 2}, ctx)
    assert r["status"] == "sent" and r["images"] == 2 and r["direct_reply"] == ""
    content, kw = ch.sent[-1]
    assert len(kw["files"]) == 2 and kw["embeds"][0].image == "attachment://image1.png" and kw["files"][0].filename == "image1.png"
    assert kw["allowed_mentions"] is not None
    assert "error" in await registry.execute("send_image", {"query": "nsfw stuff"}, ctx)
    nope = Chan(perms=False)
    ctx_np, _ = make_ctx(nope)
    r = await registry.execute("send_image", {"query": "tiger"}, ctx_np)
    assert "Attach Files" in r["error"] and not nope.sent

    async def none_found(q, n): return []
    web.search_images = none_found
    assert "couldn't find" in (await registry.execute("send_image", {"query": "zzz"}, ctx))["error"]

    async def nofetch(url, max_bytes=None): return None
    web.search_images, web.fetch_image = search, nofetch
    assert "safely" in (await registry.execute("send_image", {"query": "tiger"}, ctx))["error"]

    # send_gif
    config.GIPHY_API_KEY = ""
    assert "GIPHY_API_KEY" in (await registry.execute("send_gif", {"query": "wow"}, ctx))["error"]
    config.GIPHY_API_KEY = "k"

    async def gifs(q, n=8): return [{"url": "https://giphy.com/gifs/wow-1", "title": "Wow"}]
    web.search_gifs = gifs
    r = await registry.execute("send_gif", {"query": "wow"}, ctx)
    assert r["status"] == "sent" and r["direct_reply"] == ""
    assert ctx.message.replies[-1][1] == "https://giphy.com/gifs/wow-1" and ctx.message.replies[-1][2]["mention_author"] is False

    target_replies = []

    async def treply(content, **kw): target_replies.append(content)
    ref = N(message_id=5, resolved=N(reply=treply))
    ctx2, own = make_ctx(ch, reference=ref)
    await registry.execute("send_gif", {"query": "facepalm", "reply_to_replied": True}, ctx2)
    assert target_replies == ["https://giphy.com/gifs/wow-1"] and not own, "reacts to the replied-to message"
    await registry.execute("send_gif", {"query": "facepalm"}, ctx2)
    assert len(own) == 1, "without the flag it replies to the asker"

    async def nogif(q, n=8): return []
    web.search_gifs = nogif
    assert "no gif found" in (await registry.execute("send_gif", {"query": "x"}, ctx))["error"]
    assert "error" in await registry.execute("send_gif", {"query": "xxx"}, ctx)
    config.GIPHY_API_KEY = ""

    for s in ("search the web for the latest iphone", "send a picture of a tiger", "react with a gif", "google it", "any news today"):
        assert agent.ACTION_RE.search(s), s
    assert agent.unbacked_claims("Sent you a gif!", set()) and not agent.unbacked_claims("Sent you a gif!", {"send_gif"})


async def _all():
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "w.db")
    await db.init()
    try:
        await check_safety()
        check_parsers()
        await check_provider_chain()
        await check_web_answer()
        await check_tools()
    finally:
        await db.close()


def test_web():
    asyncio.run(_all())


if __name__ == "__main__":
    asyncio.run(_all())
    print("ALL WEB CHECKS PASSED")
