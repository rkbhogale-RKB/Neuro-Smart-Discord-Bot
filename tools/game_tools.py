"""Game tools: start a game with named players, stop it, show the leaderboard.
They answer the user directly (`direct_reply`), so the agent needs no second model call."""
import config
import db
import games
from tools import discord_utils as du
from tools.registry import registry


@registry.register(
    name="start_game",
    description=("Start a game in this channel for the people named: " + games.describe() + ". Players are pinged. "
                 "Use when the user says they want to play something ('me and hardik want to play riddles'). "
                 "Pick the game yourself if they only say 'a game'. No other tool is needed afterwards."),
    parameters={"type": "object", "properties": {
        "game": {"type": "string", "description": "riddle | trivia | scramble | twenty_questions | tictactoe"},
        "players": {"type": "array", "items": {"type": "string"},
                    "description": "other people playing (names, mentions or 'me'); the asker is always included"},
        "rounds": {"type": "integer", "description": "1-10, default 5"},
        "category": {"type": "string", "description": "trivia only: general, science, geography, history, entertainment, tech"},
        "difficulty": {"type": "string", "enum": ["easy", "medium", "hard", "any"], "description": "riddle / scramble"},
        "open_to_all": {"type": "boolean", "description": "anyone in the channel may join in"}},
        "required": ["game"]},
    permission=config.GAME_TOOL_PERMISSION, daily_cap=25,
)
async def start_game(ctx, game: str, players: list = None, rounds: int = None, category: str = "",
                     difficulty: str = "any", open_to_all: bool = False):
    guild, author = ctx.message.guild, ctx.message.author
    others, errors = [], []
    for name in players or []:
        m, err = du.resolve_member(guild, str(name), fuzzy=True, author=author)
        if err:
            errors.append(err)
        elif m.id != author.id and m.id != ctx.bot.user.id:
            others.append(m)
    if errors:
        return {"error": "couldn't find: " + "; ".join(errors)}
    res = await games.start(guild, ctx.message.channel, author, others, game, rounds, category, difficulty,
                            open_to_all, bot_id=ctx.bot.user.id)
    if "error" not in res:
        res["direct_reply"] = ""          # the game's own opening message is the reply
    return res


@registry.register(
    name="stop_game",
    description="Stop the game running in this channel (only its starter, its players or a moderator).",
    parameters={"type": "object", "properties": {}},
    permission=config.GAME_TOOL_PERMISSION,
)
async def stop_game(ctx):
    g = games.GAMES.get(ctx.channel_id)
    if g is None:
        return {"error": "no game is running in this channel"}
    perms = ctx.message.author.guild_permissions
    if not (ctx.user_id == g.starter_id or (not g.open and ctx.user_id in g.players)
            or perms.manage_messages or perms.administrator or ctx.user_id == config.OWNER_ID):
        return {"error": "only the person who started it, its players or a moderator can stop it"}
    await games.finish(g, "🛑 Game stopped.")
    return {"status": "stopped", "direct_reply": ""}


@registry.register(
    name="game_leaderboard",
    description="Show the server's game leaderboard (top players by points) and, if asked, one person's record.",
    parameters={"type": "object", "properties": {"limit": {"type": "integer", "description": "1-15, default 10"}}},
    permission=config.GAME_TOOL_PERMISSION,
)
async def game_leaderboard(ctx, limit: int = 10):
    return {"status": "ok", "direct_reply": await leaderboard_text(ctx.message.guild, max(1, min(int(limit or 10), 15)))}


async def leaderboard_text(guild, limit: int = 10) -> str:
    rows = await db.top_scores(guild.id, limit)
    if not rows:
        return "🏆 No games played yet. Say \"neuro, let's play riddles\" to get on the board!"
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        m = guild.get_member(r["user_id"])
        lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {m.display_name if m else 'a former member'}: "
                     f"{r['points']} pts · {r['wins']} win(s) · {r['games']} game(s)")
    return "🏆 **Game leaderboard**\n" + "\n".join(lines)
