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

## Warnings (moderators)
"warn bob for spamming" gives an official strike. Bob gets a DM, and at the configured counts is
timed out automatically (`WARN_ESCALATION`, default 3 warnings = 1 hour, 5 = 1 day). It never kicks or
bans by itself. Warnings expire after `WARN_EXPIRE_DAYS` (default 30).
- "show bob's warnings" (anyone can see their own: "show my warnings")
- "remove bob's last warning" / "remove warning 12 from bob"
- "clear bob's warnings" (asks for a Confirm button)
- Typed backups: `/neuro warn`, `/neuro warnings`

## Reminders (anyone)
- "remind me in 30 minutes to stretch"
- "remind me tomorrow at 6pm about the match"
- "remind hardik at 9pm to join voice" (pings only him)
- "remind me every day at 9am to check the server" (daily / weekly)
- "show my reminders", "cancel reminder 4" (moderators can cancel any)

They are saved in the database, survive restarts, and are delivered late with a note if the bot was
offline. Times use `TIMEZONE` (default Asia/Kolkata). `/neuro reminders` lists yours.

## Event log
`/neuro setlog channel:#mod-log` picks the channel (or set `EVENT_LOG_CHANNEL_ID` / `MOD_LOG_CHANNEL_ID`).
It then records, depending on `EVENT_LOG_EVENTS`:
- **messages:** deleted messages (with who wrote them and what they said), edits (before and after), bulk deletes
- **members:** joins (new accounts flagged), leaves, nickname changes, timeouts
- **roles:** role created/deleted, roles gained/lost
- **channels:** created/deleted
- **bans:** bans, unbans, kicks
- **voice:** joins, leaves, moves (off by default)

With the **View Audit Log** permission it also says who did it (kick, ban, timeout, role change).
Messages in `/noshare` channels are logged without their content. Moderation actions (timeout, kick,
ban, warn...) go to the same channel.

## Server builder (Manage Channels / Manage Roles)
Say what you need in plain words, e.g. *"neuro, there's a tournament coming, make group channels for it, some players have joined"*.
Neuro decides what's useful and shows a **preview with Confirm / Cancel buttons**. Nothing is created until you press Confirm.
- **One new category, 1 to 6 channels** (`BUILD_MAX_CHANNELS`, default 6; fewer is fine) and up to 4 new roles (`BUILD_MAX_ROLES`).
  Channel types: text, voice, announcement, forum (announcement/forum need the Community feature). Channels can be private
  (visible only to chosen roles), read-only, have a topic, slowmode, and a first message that can be pinned.
- New roles have **no permissions**; they only unlock channels. Names are cleaned, `@everyone`/`@here` can't be used.
- **Change the plan** before building: *"rename that channel to game-room"*, *"that role was wrong, call it Finalist and make it blue"*,
  *"drop the voice channel"*, *"add a forum"*. Each change posts a new preview. *"never mind"* cancels. Plans expire after `BUILD_DRAFT_MINUTES` (20).
- **Undo:** *"undo build"* removes exactly what Neuro made (a category only if it is empty); asks for a Confirm button.
  `/neuro builds` lists recent builds. Every build is recorded in the database and the log channel.

### Editing existing things (only when you ask)
*"rename #general to lounge"*, *"change the topic of #rules"*, *"move #clips to the Media category"*, *"make the Staff role red"*,
*"hide #staff-chat from everyone"*, *"give bob the Finalist role"*, *"post this announcement in #news"*, *"delete the Old Stuff category"*.
Edits can be undone with *"undo that"*; deleting can't, so it needs a Confirm button. Neuro never edits, deletes or posts on its own.
Roles above Neuro's (or above yours) can't be touched; announcements never ping @everyone/@here.

## Server recap ("what happened across channels?")
Anyone can ask: *"neuro, what happened across channels?"*, *"catch me up on the Games category"*, *"summarize #general"*.
Neuro reads the last 5-15 messages (default 12) of each channel from its own message log,
grouped by category, and writes a short summary: one or two lines per active channel. Token use is capped
(about 9,000 characters of chat per recap, 30 busiest channels, messages clipped to 140 characters).
- Only channels the person asking can see; `/noshare` channels and the log channel are skipped.
- Looks back 48 hours by default (up to 30 days: "what happened this week / this month").
- **Bot added recently?** Where its log is thin (a new bot, or quiet channels) Neuro fetches the recent history from
  Discord itself (one request per channel, max 25 per recap, never twice within 30 minutes) and keeps it. It needs
  View Channel + Read Message History in those channels; `/noshare` and unreadable channels are never fetched.
- Limit: 20 recaps per person per day.

## Games (anyone)
Say it in plain words: *"neuro, me and hardik want to play riddles"*. Neuro pings only the people named (you are always
in), posts the rules and starts. Games run in the current channel, one per channel (max `GAME_MAX_PER_SERVER` per server).

| Game | How it plays |
|---|---|
| **Riddles** | type the answer; `hint` (-1 point, up to 2), `skip`. "I think it's a clock" and one-typo answers count. easy/medium/hard |
| **Trivia** | tap the answer button; one try per question; categories: general, science, geography, history, entertainment, tech |
| **Word scramble** | unscramble the word; `hint` reveals letters |
| **20 Questions** | Neuro thinks of something; ask yes/no questions, guess with `Is it ...?`; `hint`, `give up` |
| **Tic-Tac-Toe** | button board; two players, or against Neuro (no players named) |
| **Connect Four** | tap a column number; get four in a row; two players or against Neuro |
| **Rock-Paper-Scissors** | best of 3 (or `rounds`); picks are secret until both players chose; two players or against Neuro |
| **Hangman** | type a letter or the whole word; 7 lives; letters +1, solving +2; `hint` costs a life |
| **Would You Rather** | tap A or B (you can change it); results show who picked what; no points |

Say `stop` (or "neuro stop game") to end; `score` shows the standings. 3 points for riddles, 2 for trivia/scramble,
first correct answer wins the round. `/neuro leaderboard` or "show the leaderboard" shows the server board.

**Token and call savings:** 60 riddles, 64 trivia questions, 94 words (scramble and hangman) and 40 dilemmas are built in, so rounds need no model call.
Players' messages are checked in code and never reach the model; wrong guesses get no reaction; the start/stop/leaderboard
tools answer directly (no second model call). The only model use is 20 Questions: one tiny call per question (the answer is
forced to Yes/No/Sometimes/Irrelevant, and guesses are checked in code). Games end on their own after
`GAME_IDLE_MINUTES` of silence or two unanswered rounds. Games live in memory (a restart ends them); scores are saved.
Settings: see `.env.example` (`GAMES_ENABLED`, `GAME_*`).

## Web search, pictures and GIFs (anyone, only when asked)
- **Search:** *"neuro, search the latest on the new iPhone"*, *"google who won the match"*. Default (`SEARCH_PROVIDER=auto`):
  free web results from the **ddgs** package (no key, no quota; run `pip install -r requirements.txt` after updating), summarised
  by your normal `ANSWER_MODEL` in one cheap call, with source links. If that fails it falls back to Gemini's Google Search grounding
  (`SEARCH_MODEL`; free up to ~500/day on Gemini 2.5, **not** available on the free tier of newer models). Set `SEARCH_PROVIDER=ddgs`
  to never use Gemini grounding. If everything fails, the error tells you why. Limit 12 per person per day.
- **Pictures:** *"send a picture of a red panda"*, *"show me 3 photos of Mount Fuji"*. Sources in order: Pexels (if `PEXELS_API_KEY`
  is set, free), ddgs image search, DuckDuckGo, Wikimedia Commons (the bot sends a proper User-Agent; Wikimedia refuses anonymous ones). Images are downloaded by the bot (public hosts only, size-capped, checked to be
  real images) and uploaded with a credit line, so they show even if the source blocks hotlinking. Needs **Attach Files** + **Embed Links**.
  Searches with blocked words (`WEB_BLOCKED_WORDS`) are refused. ddgs/DuckDuckGo are unofficial and can be rate-limited; Pexels is the most reliable.
- **GIFs:** *"send a gif for good morning"*, or reply to someone's message with *"@Neuro react to this with a gif"* and the GIF is sent as a
  reply to that message. Needs a free **`GIPHY_API_KEY`** (developers.giphy.com; Tenor shut its API down in 2026). `GIF_RATING` defaults to `pg`.
- Nothing here runs on its own: no automatic GIF reactions, so there are no extra calls. `/neuro status` shows whether the GIF key is set.
