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
_ORIG = (web._ddgs_images, web._ddg, web._commons, web._pexels, web._ddgs_text, web._html_text)
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

    async def ddgs(q, n): calls.append("ddgs"); raise RuntimeError("not installed")

    async def ddg(q, n): calls.append("ddg"); return []

    async def commons(q, n): calls.append("commons"); return [{"url": "https://c/1.jpg", "title": "T", "page": None, "credit": "c"}]

    async def pexels(q, n): calls.append("pexels"); return []
    web._ddgs_images, web._ddg, web._commons, web._pexels = ddgs, ddg, commons, pexels
    config.PEXELS_API_KEY = ""
    r = await web.search_images("tiger", 1)
    assert calls == ["ddgs", "ddg", "commons"] and len(r) == 1, "a failing provider falls through to the next"
    calls.clear()
    config.PEXELS_API_KEY = "k"
    await web.search_images("tiger", 1)
    assert calls[0] == "pexels"
    config.PEXELS_API_KEY = ""
    assert "NeuroDiscordBot" in web.ua() and "github.com" in web.ua(), "Wikimedia 403s anonymous user agents"


async def check_ddgs_package():
    """The real ddgs package is replaced by a fake module with the same call shape."""
    seen = {}

    class DDGS:
        def __init__(self, timeout=None): seen["timeout"] = timeout

        def images(self, q, safesearch=None, max_results=None):
            seen["img"] = (q, safesearch)
            return [{"image": "https://x/a.jpg", "title": "Mountain", "url": "https://x", "source": "Bing"}]

        def text(self, q, safesearch=None, max_results=None):
            seen["txt"] = (q, safesearch, max_results)
            return [{"title": "Result A", "href": "https://a.example/1", "body": "Alpha facts 2026"},
                    {"title": "Result B", "href": "https://b.example/2", "body": "Beta facts"}]
    web._ddgs_images, web._ddg, web._commons, web._pexels, web._ddgs_text, web._html_text = _ORIG
    sys.modules["ddgs"] = N(DDGS=DDGS)
    imgs = await web._ddgs_images("mountain", 1)
    assert imgs[0]["url"] == "https://x/a.jpg" and seen["img"] == ("mountain", "on"), "safe search is on"
    res, errs = await web.search_text("who won")
    assert [r["url"] for r in res] == ["https://a.example/1", "https://b.example/2"] and not errs
    # full answer path: results -> summarised by the normal model; sources come from the results
    prompts = []

    async def generate(model, contents, system, tools=None, allow_tools=True, temperature=0.8, max_tokens=700):
        prompts.append((model, contents[0].parts[0].text))
        return N(text="Alpha happened.")
    llm.generate = generate
    config.SEARCH_PROVIDER = "auto"
    r = await web.web_answer("who won")
    assert r["text"] == "Alpha happened." and r["sources"][0]["url"] == "https://a.example/1"
    assert prompts[0][0] == config.ANSWER_MODEL and "Alpha facts 2026" in prompts[0][1] and "<results>" in prompts[0][1]

    async def broken(*a, **k): raise RuntimeError("quota")
    llm.generate = broken
    r = await web.web_answer("who won")
    assert "Result A" in r["text"] and r["sources"], "if the model fails, the raw results are still returned"
    del sys.modules["ddgs"]


def check_html_fallback():
    html = ('<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fx&rut=1">Title <b>A</b></a>'
            '<a class="result__snippet" href="x">Snippet <b>one</b></a>'
            '<a class="result__a" href="https://b.example/y">Title B</a>')
    r = web.parse_html_results(html)
    assert r[0] == {"title": "Title A", "url": "https://a.example/x", "snippet": "Snippet one"}
    assert r[1]["url"] == "https://b.example/y" and r[1]["snippet"] == ""
    assert web.parse_html_results("") == []


async def check_web_answer():
    async def no_text(q, n): return []

    async def err_text(q, n): raise RuntimeError("blocked")
    web._ddgs_text, web._html_text = err_text, no_text

    class FakeModels:
        async def generate_content(self, model, contents, config):
            assert model == web.config.SEARCH_MODEL and contents == "who won"
            web_ = N(uri="https://news.example/a", title="news.example")
            return N(text="The Reds won 2-1.", candidates=[N(grounding_metadata=N(grounding_chunks=[
                N(web=web_), N(web=web_), N(web=None)]))])
    llm.client = N(aio=N(models=FakeModels()))
    config.SEARCH_PROVIDER = "auto"
    r = await web.web_answer("who won")                       # ddgs fails -> falls back to Gemini grounding
    assert r["text"] == "The Reds won 2-1." and r["sources"] == [{"title": "news.example", "url": "https://news.example/a"}]
    config.SEARCH_PROVIDER = "ddgs"
    r = await web.web_answer("who won")
    assert "error" in r and "ddgs" in r["error"] and "pip install" in r["error"], "the real reason is reported"

    class Broken:
        async def generate_content(self, **kw): raise RuntimeError("quota exceeded")
    llm.client = N(aio=N(models=Broken()))
    config.SEARCH_PROVIDER = "auto"
    r = await web.web_answer("x")
    assert "quota exceeded" in r["error"] and "blocked" in r["error"], "both reasons are shown"
    config.SEARCH_PROVIDER = "auto"


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
        await check_ddgs_package()
        check_html_fallback()
        await check_web_answer()
        await check_tools()
    finally:
        await db.close()


def test_web():
    asyncio.run(_all())


if __name__ == "__main__":
    asyncio.run(_all())
    print("ALL WEB CHECKS PASSED")
