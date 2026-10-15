"""Agent loop: think -> call tool -> see result -> reply, capped at MAX_STEPS.
Step 0 uses the cheap ROUTER_MODEL (Flash-Lite). Once tools have run, the follow-up
calls use ANSWER_MODEL (Flash/Pro) for the final reply. The last step forbids tools."""
import datetime as dt
import json
import logging

from google.genai import errors, types

import config
import db
import llm
import memory
import personality
from tools.registry import ToolContext, registry

log = logging.getLogger("agent")

FALLBACK = "My brain just froze, yaar. Try again in a bit."
# Used when a tool already ran fine but the model failed to phrase the reply.
TOOL_DONE_FALLBACK = "Done, yaar. Couldn't phrase a proper reply, but it went through."


def _transcript(rows) -> str:
    lines = []
    for r in rows:
        t = dt.datetime.fromtimestamp(r["ts"]).strftime("%H:%M")
        who = config.BOT_NAME if r["is_bot"] else (r["author_name"] or "user")
        lines.append(f"[{t}] {who}: {r['content'][:400]}")
    return "\n".join(lines)


SOFT_HINT = (
    "\n\n(Note: you were not @mentioned; {why}. If the message is clearly NOT meant for you "
    "(they're talking to someone else, or it's just noise), output exactly NO_REPLY and nothing "
    "else. If it is calling you, asking you something, or about you, reply normally.)"
)


async def run_agent(ctx: ToolContext, user_text: str, reason: str = "mention") -> str:
    msg = ctx.message
    facts = await db.current_facts(ctx.server_id, ctx.user_id, config.MAX_FACTS_IN_PROMPT)
    history = await db.recent_messages(ctx.channel_id, config.SHORT_TERM_N, exclude_msg_id=msg.id)

    system = personality.build_system_prompt(msg.author.display_name, facts)
    elsewhere = await memory.cross_channel_context(msg)
    elsewhere_block = (
        "<elsewhere note=\"recent chat with this person in OTHER channels they can also see\">\n"
        f"{elsewhere}\n</elsewhere>\n\n" if elsewhere else ""
    )
    prompt = (
        f"<chat_history>\n{_transcript(history)}\n</chat_history>\n\n"
        f"{elsewhere_block}"
        f"New message from {msg.author.display_name}: {user_text}"
    )
    if reason == "name":
        prompt += SOFT_HINT.format(why="someone used your name in their message")
    elif reason == "followup":
        prompt += SOFT_HINT.format(why="this person kept talking right after your last reply")
    contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
    decls = registry.declarations(ctx)

    tools_ok = False
    for step in range(config.MAX_STEPS):
        last = step == config.MAX_STEPS - 1
        model = config.ROUTER_MODEL if step == 0 else config.ANSWER_MODEL
        log.info("step=%d model=%s", step, model)
        try:
            resp = await llm.generate(model, contents, system, decls, allow_tools=not last)
        except errors.APIError:
            if tools_ok:                 # the tool already ran fine, don't lose that
                log.exception("model failed after a successful tool call")
                return TOOL_DONE_FALLBACK
            raise

        if not resp.candidates:                      # blocked / empty
            return FALLBACK
        calls = resp.function_calls or []

        if not calls:
            if step == 0 and config.ESCALATE_CHAT:  # optional: better voice, 2x cost
                resp = await llm.generate(config.ANSWER_MODEL, contents, system, decls,
                                          allow_tools=False)
            return (resp.text or FALLBACK).strip()

        # Run the tools, then hand the results back as plain TEXT instead of functionCall /
        # functionResponse parts. Replaying those structured parts is what many models
        # (Gemma especially) answer with a 500 INTERNAL; plain text works on every model.
        lines = []
        for c in calls:
            args = dict(c.args or {})
            result = await registry.execute(c.name, args, ctx)
            log.info("tool %s(%s) -> %s", c.name, args, str(result)[:200])
            if not (isinstance(result, dict) and "error" in result):
                tools_ok = True
            lines.append(f"- {c.name}({json.dumps(args, ensure_ascii=False)}) -> "
                         f"{json.dumps(result, ensure_ascii=False, default=str)[:800]}")
        contents[0].parts.append(types.Part(text=(
            "\n\n<tool_results>\n" + "\n".join(lines) + "\n</tool_results>\n"
            "These tools were already run for you. Now reply to the user in character, using "
            "the results. Don't mention tool names and don't repeat a call that succeeded.")))

    return FALLBACK