import re

import db
from tools.registry import registry

# Cheap code-level guard on top of the prompt rule (health, religion, family, relationships...)
SENSITIVE = (
    "health", "medical", "disease", "illness", "diagnos", "medicine", "therapy",
    "religio", "god", "caste", "family", "mother", "father", "sister", "brother",
    "girlfriend", "boyfriend", "wife", "husband", "relationship", "dating",
    "sexual", "politic", "salary", "password", "otp",
)


@registry.register(
    name="remember_fact",
    description=(
        "Save a DURABLE fact the current user stated about THEMSELVES (e.g. favorite_color, "
        "city, job, hobby). Call it right when they say it. Do NOT save jokes, sarcasm, "
        "moods, one-off events, or sensitive topics (health, religion, family, relationships). "
        "A newer value replaces the older one."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "snake_case label, e.g. favorite_color"},
            "value": {"type": "string", "description": "short value, e.g. blue"},
        },
        "required": ["key", "value"],
    },
    cost=1,
    daily_cap=30,
)
async def remember_fact(ctx, key: str, value: str):
    key = re.sub(r"[^a-z0-9_]+", "_", key.lower()).strip("_")[:40]
    value = " ".join(value.split())[:200]          # no newlines, bounded size
    if not key or not value:
        return {"error": "empty key or value"}
    blob = f"{key} {value}".lower()
    if any(s in blob for s in SENSITIVE):
        return {"status": "refused", "reason": "sensitive topic, not stored"}
    # user_id comes from Discord, so a prompt-injected model cannot write facts for someone else
    status = await db.save_fact(ctx.server_id, ctx.user_id, key, value, ctx.channel_id)
    return {"status": status, "key": key}
