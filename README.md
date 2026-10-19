# Neuro-Smart-Discord-Bot
LLM Powered Smart Discord Bot (Gemini + discord.py)

## Setup
1. `pip install -r requirements.txt` (Python 3.10+)
2. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` and `GEMINI_API_KEY`.
3. In the Discord Developer Portal enable **Message Content** and **Server Members** intents.
4. `python bot.py`

## Talking to other apps
Neuro can read other bots' messages (embeds included) and talk to them. Mods can say things like:
- `@Neuro ask Bolt "!help" and explain what it says`
- `@Neuro @Bolt what's the weather` (Neuro @mentions Bolt and reads the answer)
- Reply to any bot's message with `@Neuro analyze this`

**Limit:** Discord only lets humans run slash commands and press buttons. Neuro can send normal
messages (prefix commands like `!help`, or an @mention) and read the reply, but not `/commands`.

## Moderation (mods only)
Needs the Discord permission on the requester *and* on the bot, and the bot's role must be above the target's.
Say it in plain language: timeout, kick, ban, unban, purge messages, member info, a member's recent messages.
Kick, ban and purges over 10 messages show a **Confirm** button that only the requester can press.
Actions are logged to `MOD_LOG_CHANNEL_ID` if set.

Optional auto-moderation (`AUTOMOD=true`): deletes spam / mass-mention / invite messages and times
the sender out briefly. It never kicks or bans by itself.
