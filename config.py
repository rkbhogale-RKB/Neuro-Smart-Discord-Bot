import datetime
import os
import zoneinfo

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
MAX_STEPS = 5                 # hard cap on think -> tool -> think rounds
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
AGENT_TIMEOUT = 120           # seconds for one whole request (several tool rounds)
TOOL_TIMEOUT = 60             # seconds for a single tool call
# Messages that look like actions (ban, ping, list, ask a bot...) go straight to the stronger
# model for tool picking; plain chat stays on the cheap router model.
ACTION_MODEL_FOR_TOOLS = _flag("ACTION_MODEL_FOR_TOOLS", True)
PRESENCE_INTENT = _flag("PRESENCE_INTENT", False)   # privileged; lets member lists show online status
# Who may use "talk to other apps" and moderation tools: mod | everyone | owner
APP_TOOL_PERMISSION = os.getenv("APP_TOOL_PERMISSION", "mod").strip().lower()
MOD_TOOL_PERMISSION = os.getenv("MOD_TOOL_PERMISSION", "mod").strip().lower()
# Known prefixes / quirks of the other bots (see app_profiles.example.json)
APP_PROFILES_PATH = os.getenv("APP_PROFILES_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "app_profiles.json")
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

# ---- Warnings (strikes) ----
WARN_EXPIRE_DAYS = int(os.getenv("WARN_EXPIRE_DAYS", "30"))   # warnings older than this stop counting (0 = never)


def _escalation(raw: str) -> list[tuple[int, int]]:
    """'3:60,5:1440' -> [(3, 60), (5, 1440)] = at 3 warnings time out 60 min, at 5 time out 1 day."""
    out = []
    for part in raw.split(","):
        try:
            n, minutes = part.strip().split(":")
            if int(n) > 0 and int(minutes) > 0:
                out.append((int(n), min(int(minutes), 40320)))
        except ValueError:
            continue
    return sorted(out)


WARN_ESCALATION = _escalation(os.getenv("WARN_ESCALATION", "3:60,5:1440"))   # empty = never auto-punish

# ---- Reminders ----
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Kolkata")
try:
    TZ = zoneinfo.ZoneInfo(TIMEZONE_NAME)               # how "6pm" is understood
    TZ_OK = True
except (zoneinfo.ZoneInfoNotFoundError, ValueError):    # no tz database on this host: pip install tzdata
    TZ = datetime.timezone.utc
    TZ_OK = False
REMINDER_POLL_SECONDS = 15
MAX_PENDING_REMINDERS = 25    # per person

# ---- Event log (posted to the log channel: /neuro setlog, EVENT_LOG_CHANNEL_ID or MOD_LOG_CHANNEL_ID) ----
EVENT_LOG_CHANNEL_ID = int(os.getenv("EVENT_LOG_CHANNEL_ID") or 0)
# categories: messages, members, roles, channels, bans, voice
EVENT_LOG_EVENTS = {e.strip().lower() for e in os.getenv(
    "EVENT_LOG_EVENTS", "messages,members,roles,channels,bans").split(",") if e.strip()}
NEW_ACCOUNT_DAYS = int(os.getenv("NEW_ACCOUNT_DAYS", "7"))    # joins from accounts younger than this are flagged
