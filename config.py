import os

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name} (see .env.example)")
    return v


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


DISCORD_TOKEN = _req("DISCORD_TOKEN")
GEMINI_API_KEY = _req("GEMINI_API_KEY")

ROUTER_MODEL = os.getenv("ROUTER_MODEL", "gemini-2.5-flash-lite")   # cheap chat / tool picking
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "gemini-2.5-flash")        # final answers after tools
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL") or ROUTER_MODEL           # saves facts in the background

BOT_NAME = os.getenv("BOT_NAME", "Neuro")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
DB_PATH = os.getenv("DB_PATH", "data/bot.db")

# Agent loop
MAX_STEPS = 4                 # hard cap on think -> tool -> think rounds
ESCALATE_CHAT = False         # True = even plain chat gets answered by ANSWER_MODEL (costlier)

# ---- Memory ----
SHORT_TERM_N = 20             # last N messages of the current channel given to the model
MAX_FACTS_IN_PROMPT = 25
MESSAGE_RETENTION_DAYS = int(os.getenv("MESSAGE_RETENTION_DAYS", "30"))   # raw chat log kept this long

# Fact saving (background extractor, no tool calling)
EXTRACT_FROM_ALL_MESSAGES = _flag("EXTRACT_FROM_ALL_MESSAGES", False)  # False = only messages aimed at the bot
EXTRACT_PER_HOUR = 20         # max extraction calls per user per hour
MAX_FACTS_PER_USER = 60
REACT_ON_SAVE = _flag("REACT_ON_SAVE", True)   # add a 🧠 reaction when a fact was saved

# Cross-channel recall: recent chat with the same person in OTHER channels (permission-checked)
CROSS_CHANNEL_HOURS = 6
CROSS_CHANNEL_CHANNELS = 2
CROSS_CHANNEL_MESSAGES = 6

# Limits
USER_REQUESTS_PER_MIN = 6

# Reply triggers (besides @mention and replying to the bot)
# Words that count as calling the bot, comma separated, whole-word match ("neurology" won't trigger).
TRIGGER_NAMES = [n.strip().lower() for n in os.getenv("TRIGGER_NAMES", "neuro,neuro smart").split(",") if n.strip()]
# After the bot answers someone, their next messages in that channel count as follow-ups for this long.
FOLLOWUP_SECONDS = int(os.getenv("FOLLOWUP_SECONDS", "90"))

# ---- Moderation + talking to other apps ----
AGENT_TIMEOUT = 90            # seconds; tools may wait for a confirm button or another app's reply
CONFIRM_SECONDS = 25          # how long the requester has to press Confirm on kick / ban / purge
MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID") or 0)   # 0 = log to console only
APP_WAIT_SECONDS = int(os.getenv("APP_WAIT_SECONDS", "8"))       # how long to wait for another app's reply
# Commands containing these words need a button confirmation before being sent to another app.
DANGEROUS_APP_WORDS = [w.strip().lower() for w in os.getenv(
    "DANGEROUS_APP_WORDS", "ban,kick,nuke,purge,wipe,delete,reset,remove,clear,mute,lockdown"
).split(",") if w.strip()]

# Auto-moderation (off by default). Deletes + short timeout only, never bans.
AUTOMOD = _flag("AUTOMOD", False)
AUTOMOD_SPAM_COUNT = int(os.getenv("AUTOMOD_SPAM_COUNT", "6"))        # this many messages...
AUTOMOD_SPAM_SECONDS = int(os.getenv("AUTOMOD_SPAM_SECONDS", "8"))    # ...within this many seconds
AUTOMOD_MENTION_LIMIT = int(os.getenv("AUTOMOD_MENTION_LIMIT", "5"))  # mentions in one message
AUTOMOD_BLOCK_INVITES = _flag("AUTOMOD_BLOCK_INVITES", False)
AUTOMOD_TIMEOUT_MINUTES = int(os.getenv("AUTOMOD_TIMEOUT_MINUTES", "10"))