"""Web tools: search the web, send a picture, send a GIF (also as a reaction to a message).
Each answers the user directly (`direct_reply`), so no second model call is needed.
Used only when someone asks for it: nothing here runs on its own."""
import io
import logging

import discord

import config
import web
from tools import discord_utils as du
from tools.registry import registry

log = logging.getLogger("webtools")


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


@registry.register(
    name="web_search",
    description=("Search the web (Google) and answer from the results. Use when the user asks you to search/google/"
                 "look something up, or needs current facts you can't know (news, scores, prices, releases, "
                 "'what's the latest'). Not for chit-chat or things you know well. The answer goes straight to the chat."),
    parameters={"type": "object", "properties": {"query": {"type": "string", "description": "a clear search query"}},
                "required": ["query"]},
    permission=config.WEB_TOOL_PERMISSION, daily_cap=12, cost=3,
)
async def web_search(ctx, query: str):
    q = " ".join((query or "").split())[:200]
    if not q:
        return {"error": "nothing to search for"}
    res = await web.web_answer(q)
    if "error" in res:
        return res
    text = res["text"]
    if res["sources"]:
        text += "\n\n**Sources:** " + " · ".join(f"[{_clip(s['title'], 40)}](<{s['url']}>)" for s in res["sources"])
    return {"status": "answered", "direct_reply": _clip(text, 1900)}


@registry.register(
    name="send_image",
    description=("Find a picture on the web and post it in this channel. Use when the user asks you to show/send a "
                 "picture/photo/image of something. 1 image by default, up to 3."),
    parameters={"type": "object", "properties": {
        "query": {"type": "string", "description": "what the picture should show"},
        "count": {"type": "integer", "description": "1-3, default 1"}}, "required": ["query"]},
    permission=config.WEB_TOOL_PERMISSION, daily_cap=15, cost=2,
)
async def send_image(ctx, query: str, count: int = 1):
    q = " ".join((query or "").split())[:100]
    if not q:
        return {"error": "what should the picture show?"}
    if (bad := web.blocked_query(q)):
        return {"error": f"I won't search for that ('{bad}')"}
    ch = ctx.message.channel
    perms = ch.permissions_for(ctx.message.guild.me)
    for flag, label in (("attach_files", "Attach Files"), ("embed_links", "Embed Links")):
        if not getattr(perms, flag, True):
            return {"error": f"I need the {label} permission in this channel to post pictures"}
    n = max(1, min(int(count or 1), 3))
    cands = await web.search_images(q, n)
    if not cands:
        return {"error": f"couldn't find any pictures for '{q}'"}
    files, embeds = [], []
    for c in cands[:12]:
        if len(files) >= n:
            break
        got = await web.fetch_image(c["url"])
        if got is None:
            continue
        data, ext = got
        name = f"image{len(files) + 1}.{ext}"
        e = discord.Embed(title=_clip(c["title"], 120), url=c.get("page") or None, description=_clip(c["credit"], 100))
        e.set_image(url=f"attachment://{name}")
        files.append(discord.File(io.BytesIO(data), filename=name))
        embeds.append(e)
    if not files:
        return {"error": f"found results for '{q}' but none could be downloaded safely"}
    await ch.send(embeds=embeds, files=files, allowed_mentions=du.NO_PINGS)
    return {"status": "sent", "images": len(files), "direct_reply": ""}


@registry.register(
    name="send_gif",
    description=("Send a GIF in this channel. Use when the user asks for a gif, or asks you to react to a message "
                 "(theirs, or the one they replied to) with a gif. Pick a short search phrase that captures the "
                 "mood or topic (e.g. 'facepalm', 'celebration', 'good night'). Set reply_to_replied=true to post "
                 "the gif as a reply to the message the user replied to."),
    parameters={"type": "object", "properties": {
        "query": {"type": "string", "description": "short phrase, e.g. 'mind blown'"},
        "reply_to_replied": {"type": "boolean", "description": "post as a reply to the message the user replied to"}},
        "required": ["query"]},
    permission=config.WEB_TOOL_PERMISSION, daily_cap=25, cost=1,
)
async def send_gif(ctx, query: str, reply_to_replied: bool = False):
    q = " ".join((query or "").split())[:60]
    if not q:
        return {"error": "what kind of gif?"}
    if (bad := web.blocked_query(q)):
        return {"error": f"I won't search for that ('{bad}')"}
    if not config.GIPHY_API_KEY:
        return {"error": "GIFs aren't set up: the bot owner needs to add a free GIPHY_API_KEY (developers.giphy.com)"}
    gif = web.pick_gif(await web.search_gifs(q))
    if gif is None:
        return {"error": f"no gif found for '{q}'"}
    msg = ctx.message
    target = msg
    ref = getattr(msg, "reference", None)
    if reply_to_replied and ref is not None and getattr(ref, "message_id", None):
        resolved = getattr(ref, "resolved", None)
        if resolved is None or not hasattr(resolved, "reply"):
            try:
                resolved = await msg.channel.fetch_message(ref.message_id)
            except Exception:
                resolved = None
        if resolved is not None and hasattr(resolved, "reply"):
            target = resolved
    try:
        await target.reply(gif["url"], mention_author=False, allowed_mentions=du.NO_PINGS)
    except discord.HTTPException:
        await msg.channel.send(gif["url"], allowed_mentions=du.NO_PINGS)
    return {"status": "sent", "gif": gif["title"], "direct_reply": ""}
