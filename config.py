import os

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name} (see .env.example)")
    return v


DISCORD_TOKEN = _req("DISCORD_TOKEN")
GEMINI_API_KEY = _req("GEMINI_API_KEY")

ROUTER_MODEL = os.getenv("ROUTER_MODEL", "gemini-2.5-flash-lite")   # picks tools, cheap chat
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "gemini-2.5-flash")        # final answers after tools

BOT_NAME = os.getenv("BOT_NAME", "Yaar")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
DB_PATH = os.getenv("DB_PATH", "data/bot.db")

# Agent loop
MAX_STEPS = 4                 # hard cap on think -> tool -> think rounds
ESCALATE_CHAT = False         # True = even plain chat gets answered by ANSWER_MODEL (costlier)

# Memory
SHORT_TERM_N = 15             # last N channel messages given to the model
MAX_FACTS_IN_PROMPT = 25

# Limits
USER_REQUESTS_PER_MIN = 6
