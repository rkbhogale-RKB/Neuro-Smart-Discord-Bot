"""Agent loop: think -> call tool -> see result -> reply, capped at MAX_STEPS.
Step 0 uses the cheap ROUTER_MODEL (Flash-Lite). Once tools have run, the follow-up
calls use ANSWER_MODEL (Flash/Pro) for the final reply. The last step forbids tools."""
import datetime as dt
import logging

from google.genai import types

import config
import db
import llm
import personality
from tools.registry import ToolContext, registry

log = logging.getLogger("agent")

FALLBACK = "Dimaag ghoom gaya yaar, ek baar phir try kar."


def _transcript(rows) -> str:
    lines = []
    for r in rows:
        t = dt.datetime.fromtimestamp(r["ts"]).strftime("%H:%M")
        who = config.BOT_NAME if r["is_bot"] else (r["author_name"] or "user")
        lines.append(f"[{t}] {who}: {r['content'][:400]}")
    return "\n".join(lines)


async def run_agent(ctx: ToolContext, user_text: str) -> str:
    msg = ctx.message
    facts = await db.current_facts(ctx.server_id, ctx.user_id, config.MAX_FACTS_IN_PROMPT)
    history = await db.recent_messages(ctx.channel_id, config.SHORT_TERM_N, exclude_msg_id=msg.id)

    system = personality.build_system_prompt(msg.author.display_name, facts)
    prompt = (
        f"<chat_history>\n{_transcript(history)}\n</chat_history>\n\n"
        f"New message from {msg.author.display_name}: {user_text}"
    )
    contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
    decls = registry.declarations(ctx)

    for step in range(config.MAX_STEPS):
        last = step == config.MAX_STEPS - 1
        model = config.ROUTER_MODEL if step == 0 else config.ANSWER_MODEL
        resp = await llm.generate(model, contents, system, decls, allow_tools=not last)

        if not resp.candidates:                      # blocked / empty
            return FALLBACK
        calls = resp.function_calls or []

        if not calls:
            if step == 0 and config.ESCALATE_CHAT:  # optional: better voice, 2x cost
                resp = await llm.generate(config.ANSWER_MODEL, contents, system, decls,
                                          allow_tools=False)
            return (resp.text or FALLBACK).strip()

        # Keep the model turn verbatim (preserves thought signatures), then answer its calls.
        contents.append(resp.candidates[0].content)
        responses = []
        for c in calls:
            result = await registry.execute(c.name, dict(c.args or {}), ctx)
            log.info("tool %s(%s) -> %s", c.name, dict(c.args or {}), str(result)[:200])
            responses.append(types.Part.from_function_response(
                name=c.name, response={"result": result}))
        contents.append(types.Content(role="user", parts=responses))

    return FALLBACK
