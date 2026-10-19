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

## Pinging, DMs and server info
Anyone can ask: "call hardik", "tell bob to come here" (pings only that person, never @everyone/roles;
small typos in names are tolerated), "DM hardik ...", "who is in the server", "list members",
"list channels/roles", "who is in voice", "server info". Member lists need the **Server Members** intent.

## Other bots: predefined prefixes
Copy `app_profiles.example.json` to `app_profiles.json`, or use `/neuro appprofile app:@Bolt prefix:!`.
- `prefix`: added automatically ("ask bolt help" sends `!help`)
- `mention`: true if the bot only answers when @mentioned
- `slash_only`: true if it has only slash commands. Discord forbids bots from running slash
  commands or pressing buttons, so Neuro then tells you the exact `/command` to run yourself,
  and can read and explain the reply afterwards.
- `wait`: seconds to wait for its reply

## /neuro commands (work without the AI)
- `/neuro status`: checks permissions, role position, member list, intents, mod-log channel, other bots
- `/neuro debug`: which tools were used for the last requests, and what they returned
- `/neuro members [role] [channel_only]`: member list
- `/neuro appprofile`: save how to talk to another bot

## Robustness
Action-looking messages use the stronger model for tool choice; the model can chain tools (look up,
then act); tool errors are explained plainly; bad tool arguments are handled; tools time out instead
of hanging; one request per person at a time; clean shutdown. Tests: `python tests/test_tools.py`.
