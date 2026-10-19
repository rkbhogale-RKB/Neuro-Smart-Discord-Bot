"""Tool registry: function + schema + metadata (cost, permission, channels, daily cap).
The model only SEES tools allowed for this user/channel, and execute() re-checks
everything, because the model can hallucinate a tool name."""
import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import discord
from google.genai import types

import config
import db

log = logging.getLogger("registry")


@dataclass
class ToolContext:
    """Identity comes from Discord, NEVER from model-supplied arguments."""
    bot: discord.Client
    message: discord.Message

    @property
    def server_id(self) -> int:
        return self.message.guild.id

    @property
    def channel_id(self) -> int:
        return self.message.channel.id

    @property
    def user_id(self) -> int:
        return self.message.author.id


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Awaitable[Any]]
    cost: int = 1                              # relative cost units (for budgets)
    permission: str = "everyone"               # everyone | mod | owner
    allowed_channels: set[int] | None = None   # None = all channels
    daily_cap: int | None = None               # per-user calls / 24h


def _has_permission(tool: Tool, ctx: ToolContext) -> bool:
    if tool.permission == "everyone":
        return True
    if tool.permission == "owner":
        return ctx.user_id == config.OWNER_ID
    if tool.permission == "mod":
        perms = ctx.message.author.guild_permissions
        return perms.manage_messages or perms.administrator or ctx.user_id == config.OWNER_ID
    return False


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, **meta):
        def deco(fn):
            t = Tool(fn=fn, **meta)
            self._tools[t.name] = t
            return fn
        return deco

    def _allowed(self, t: Tool, ctx: ToolContext) -> bool:
        if t.allowed_channels is not None and ctx.channel_id not in t.allowed_channels:
            return False
        return _has_permission(t, ctx)

    def declarations(self, ctx: ToolContext) -> list[types.FunctionDeclaration]:
        return [
            types.FunctionDeclaration(
                name=t.name, description=t.description, parameters_json_schema=t.parameters
            )
            for t in self._tools.values() if self._allowed(t, ctx)
        ]

    async def execute(self, name: str, args: dict, ctx: ToolContext) -> Any:
        t = self._tools.get(name)
        if t is None or not self._allowed(t, ctx):
            return {"error": "tool not available here"}
        if t.daily_cap is not None:
            used = await db.count_tool_calls(ctx.user_id, name, time.time() - 86400)
            if used >= t.daily_cap:
                return {"error": f"daily limit reached for {name}"}
        # Models sometimes add unknown arguments or forget required ones. Drop the unknown ones and
        # check the rest BEFORE running, so a real TypeError inside a tool isn't mistaken for this.
        sig = inspect.signature(t.fn)
        args = {k: v for k, v in (args or {}).items() if k in sig.parameters}
        try:
            sig.bind(ctx, **args)
        except TypeError as e:
            return {"error": f"bad arguments: {str(e)[:120]}"}
        await db.record_tool_call(ctx.user_id, name, t.cost)
        try:
            return await asyncio.wait_for(t.fn(ctx, **args), timeout=config.TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            return {"error": f"{name} timed out"}
        except Exception as e:
            log.exception("tool %s failed", name)
            return {"error": f"{name} failed ({type(e).__name__})"}


registry = Registry()
