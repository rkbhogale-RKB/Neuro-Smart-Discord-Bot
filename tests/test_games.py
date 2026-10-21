"""Tests for the game engine: matching, hints, full simulated games (riddle, trivia, scramble, 20 questions,
tic-tac-toe), leaderboard, stopping, and "no model call" behaviour. Fake Discord objects + real SQLite.
Run with:  python -m pytest tests   (or:  python tests/test_games.py)"""
import asyncio
import os
import sys
import tempfile
import types as pytypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("OWNER_ID", "999")

import agent  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import game_data as data  # noqa: E402
import games  # noqa: E402
from tools import game_tools  # noqa: E402,F401
from tools.registry import registry  # noqa: E402

N = pytypes.SimpleNamespace
games.INTRO_PAUSE = games.ROUND_PAUSE = 0
config.GAME_RIDDLE_SECONDS = config.GAME_TRIVIA_SECONDS = config.GAME_SCRAMBLE_SECONDS = 5


# ------------------------------------------------------------------ pure logic
def check_logic():
    assert games.is_correct("clock", ["clock", "watch"])
    assert games.is_correct("I think it's a Clock!", ["clock"])
    assert games.is_correct("envelop", ["envelope"]), "one typo is fine"
    assert not games.is_correct("clocks and more words in a very long sentence about nothing at all", ["cat"])
    assert not games.is_correct("woman", ["man"]), "whole words only"
    assert games.is_correct("the future", ["future"]) and not games.is_correct("", ["x"])
    assert games.mask("clock", 1) == "c _ _ _ _"
    assert games.mask("clock", 1, first_last=True) == "c _ _ _ k"
    assert games.command("Stop the game!") == "stop" and games.command("hint") == "hint"
    assert games.command("i give up") == "skip" and games.command("clock") is None
    for _ in range(30):
        w = "dragon"
        assert games.scramble(w) != w and sorted(games.scramble(w)) == sorted(w)
    b = ["X", "X", "", "O", "O", "", "", "", ""]
    assert games.ttt_ai(b, "X", "O") == 2, "takes the win"
    assert games.ttt_ai(["O", "O", "", "X", "", "", "", "", ""], "X", "O") == 2, "blocks"
    assert games.ttt_result(["X"] * 3 + [""] * 6) == "X" and games.ttt_result(list("XOXXOOOXX")) == "draw"
    for _ in range(20):                       # no repeats within 80% of the bank
        games._recent.clear()
        seen = [games.pick(1, "t", list(range(10))) for _ in range(8)]
        assert len(set(seen)) == 8


def check_data():
    assert len(data.RIDDLES) >= 50 and len(data.TRIVIA) >= 50
    assert all(a for _, _, answers, _ in data.RIDDLES for a in answers)
    for cat, q, right, wrong in data.TRIVIA:
        assert cat in data.TRIVIA_CATEGORIES and len(set(wrong + [right])) == 4, q
    words = [w for ws in data.SCRAMBLE_WORDS.values() for w in ws]
    assert len({"".join(sorted(w)) for w in words}) == len(words), "two words are anagrams of each other"
    reserved = {games.norm(x) for x in games.STOP | games.HINT | games.SKIP | games.SCORE}
    assert not (set(words) & reserved)
    assert all(c in ("animal", "object", "place", "food", "character") for c, _ in data.TWENTY_Q)


# ------------------------------------------------------------------ fakes
class Perms:
    def __init__(self, mod=False):
        self.manage_messages = self.administrator = mod


def member(id, name, bot=False, mod=False):
    return N(id=id, display_name=name, name=name.lower(), global_name=None, bot=bot,
             guild_permissions=Perms(mod), mention=f"<@{id}>")


class Msg:
    def __init__(self, ch, text, **kw):
        self.ch, self.content, self.view = ch, text, kw.get("view")

    async def edit(self, content=None, view="keep", **kw):
        if content is not None:
            self.content = content
        if view != "keep":
            self.view = view


class Chan:
    def __init__(self, id):
        self.id, self.sent = id, []

    async def send(self, text, **kw):
        m = Msg(self, text, **kw)
        self.sent.append(m)
        return m

    def texts(self):
        return [m.content for m in self.sent]


me, ann, hardik, bob, mod = (member(1, "neuro", bot=True), member(10, "Ann"), member(11, "Hardik"),
                             member(12, "Bob"), member(13, "Mod", mod=True))
people = [me, ann, hardik, bob, mod]
guild = N(id=1, get_member=lambda i: next((p for p in people if p.id == i), None), members=people)


def say(ch, who, text):
    return games.handle_message(N(channel=ch, author=who, content=text), me.id)


async def wait_for(cond, secs=3.0):
    for _ in range(int(secs / 0.01)):
        if cond():
            return True
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


def click(user, button):
    log = []

    async def defer(): log.append("defer")

    async def send_message(t, ephemeral=False): log.append(("msg", t))

    async def edit_message(content=None, view=None): log.append(("edit", content, view))
    it = N(user=user, response=N(defer=defer, send_message=send_message, edit_message=edit_message))
    return asyncio.ensure_future(button.callback(it)), log


# ------------------------------------------------------------------ games
async def check_riddle():
    ch = Chan(100)
    res = await games.start(guild, ch, ann, [hardik], "riddles", rounds=2, bot_id=me.id)
    assert res["status"] == "started"
    g = games.GAMES[100]
    await wait_for(lambda: ch.sent)
    assert "<@10>" in ch.texts()[0] and "<@11>" in ch.texts()[0]
    assert (await games.start(guild, ch, ann, [], "riddle"))["error"].startswith("a game is already")
    await wait_for(lambda: len(ch.sent) >= 2)                    # intro + first riddle
    assert await say(ch, bob, "clock") is False, "a non-player is never consumed"
    assert await say(ch, hardik, "hint") is True
    answers = g.item["answers"]
    assert await say(ch, hardik, "no idea lol") is True and g.winner is None
    assert await say(ch, hardik, answers[0]) is True
    await wait_for(lambda: any("got it" in t or "nailed" in t or "fire" in t or "Big brain" in t for t in ch.texts()))
    assert g.scores[11] in (1, 2), "a hint costs a point"
    await wait_for(lambda: g.round_no == 2 and g.item is not None and g.msg.content.startswith("🧩 **2/2**"))
    assert await say(ch, ann, "skip") is True
    await wait_for(lambda: 100 not in games.GAMES)
    last = ch.texts()[-1]
    assert "Game over" in last and "Hardik" in last
    rows = {r["user_id"]: r for r in await db.top_scores(1)}
    assert rows[11]["points"] >= 1 and rows[11]["wins"] == 1 and rows[10]["games"] == 1 and rows[10]["wins"] == 0


async def check_idle_and_stop():
    ch = Chan(101)
    await games.start(guild, ch, ann, [], "riddle", rounds=5, bot_id=me.id)
    g = games.GAMES[101]
    config.GAME_RIDDLE_SECONDS = 0.05
    await wait_for(lambda: 101 not in games.GAMES, 5)
    assert "ending the game" in ch.texts()[-1] and g.idle_rounds == 2
    config.GAME_RIDDLE_SECONDS = 5

    ch = Chan(102)
    await games.start(guild, ch, ann, [hardik], "scramble", rounds=3, bot_id=me.id)
    await wait_for(lambda: len(ch.sent) >= 2)
    assert await say(ch, bob, "stop") is False
    assert await say(ch, hardik, "neuro what is the weather") is False, "talking to Neuro falls through"
    word = games.GAMES[102].item["word"]
    assert await say(ch, hardik, word) is True
    await wait_for(lambda: games.GAMES[102].round_no == 2)
    assert await say(ch, hardik, "<@1> stop") is True and 102 not in games.GAMES
    assert "stopped" in ch.texts()[-1] and "Hardik: 2 pts" in ch.texts()[-1]


async def check_trivia():
    ch = Chan(103)
    await games.start(guild, ch, ann, [hardik], "trivia", rounds=2, category="science", bot_id=me.id)
    g = games.GAMES[103]
    await wait_for(lambda: len(ch.sent) >= 2)
    q = ch.sent[1]
    assert q.view is not None and len(q.view.children) == 4 and "Science" in q.content
    right = g.item["options"].index(g.item["right"])
    wrong = next(i for i in range(4) if i != right)
    t, log = click(bob, q.view.children[right]); await t
    assert ("msg", "This game isn't open to you.") in log
    t, log = click(hardik, q.view.children[wrong]); await t
    assert any(isinstance(x, tuple) and "out for this question" in x[1] for x in log)
    t, log = click(hardik, q.view.children[right]); await t
    assert any(isinstance(x, tuple) and "already answered" in x[1] for x in log)
    t, log = click(ann, q.view.children[right]); await t
    await wait_for(lambda: g.scores.get(10) == 2)
    await wait_for(lambda: "got it" in q.content or "nailed" in q.content or "fire" in q.content or "Big brain" in q.content)
    assert q.view is None and g.item["right"] in q.content, "result is edited into the question message"
    await wait_for(lambda: g.round_no == 2 and g.msg is not q)
    q2 = g.msg
    for u in (ann, hardik):                                      # everybody wrong: round ends without waiting
        w = next(i for i in range(4) if g.item["options"][i] != g.item["right"])
        t, _ = click(u, q2.view.children[w]); await t
    await wait_for(lambda: 103 not in games.GAMES)
    assert g.scores == {10: 2}


async def check_twenty():
    ch = Chan(104)
    calls = []

    async def oracle(secret, cat, q):
        calls.append(q)
        return "Yes"
    games.ask_oracle = oracle
    await games.start(guild, ch, ann, [hardik], "20 questions", bot_id=me.id)
    g = games.GAMES[104]
    assert await say(ch, hardik, "is it big?") is True and ch.texts()[-1] == "**1/20** Yes"
    assert await say(ch, ann, "lol that was funny") is False, "chat that isn't a question is left alone"
    assert await say(ch, ann, "does it fly") is True and ch.texts()[-1] == "**2/20** Yes"
    assert len(calls) == 2
    assert await say(ch, ann, "hint") is True and "starts with" in ch.texts()[-1]
    assert await say(ch, hardik, f"Is it a {g.secret}?") is True and 104 not in games.GAMES
    assert "got it in 2 question" in ch.texts()[-1] and len(calls) == 2, "guesses are checked in code"
    ch = Chan(105)
    await games.start(guild, ch, ann, [], "twenty_questions", bot_id=me.id)
    g = games.GAMES[105]
    for i in range(20):
        await say(ch, ann, f"is it number {i}?")
    assert 105 not in games.GAMES and "20 questions used up" in ch.texts()[-1]
    ch = Chan(106)
    await games.start(guild, ch, ann, [], "twenty_questions", bot_id=me.id)
    secret = games.GAMES[106].secret
    assert await say(ch, ann, "give up") is True and secret in ch.texts()[-1]

    async def broken(secret, cat, q): return None
    games.ask_oracle = broken
    ch = Chan(107)
    await games.start(guild, ch, ann, [], "twenty_questions", bot_id=me.id)
    await say(ch, ann, "is it alive?")
    assert games.GAMES[107].asked == 0 and "didn't count" in ch.texts()[-1]
    await games.finish(games.GAMES[107], "bye")


async def check_tictactoe():
    ch = Chan(108)
    await games.start(guild, ch, ann, [hardik], "tictactoe", bot_id=me.id)
    g = games.GAMES[108]
    v = g.view
    assert len(v.children) == 9 and "Ann" in ch.texts()[0]
    t, log = click(hardik, v.children[0]); await t
    assert ("msg", "Wait for your turn!") in log
    t, log = click(bob, v.children[0]); await t
    assert ("msg", "You're not in this game.") in log
    for who, cell in ((ann, 0), (hardik, 3), (ann, 1), (hardik, 4), (ann, 2)):
        t, log = click(who, v.children[cell]); await t
    assert 108 not in games.GAMES and g.winners == {10}
    assert log[-1][0] == "edit" and "Ann" in log[-1][1] and "wins" in log[-1][1] and all(c.disabled for c in v.children)

    ch = Chan(109)                                               # against Neuro
    await games.start(guild, ch, ann, [], "tictactoe", bot_id=me.id)
    g = games.GAMES[109]
    assert me.id in g.sym
    for _ in range(5):
        if g.finished:
            break
        free = next(i for i, c in enumerate(g.board) if not c)
        t, log = click(ann, g.view.children[free]); await t
    assert g.finished and games.ttt_result(g.board) is not None
    assert await say(Chan(110), ann, "hi") is False

    ch = Chan(111)
    await games.start(guild, ch, ann, [], "tictactoe", bot_id=me.id)
    g = games.GAMES[111]
    assert await say(ch, ann, "stop") is True and 111 not in games.GAMES
    assert all(c.disabled for c in g.view.children), "board is locked when the game is stopped"


async def check_tools_and_board():
    ch = Chan(120)
    ctx = N(user_id=10, channel_id=120, server_id=1, bot=N(user=me),
            message=N(guild=guild, author=ann, channel=ch))
    r = await registry.execute("start_game", {"game": "riddle", "players": ["me", "hardik"], "rounds": 3}, ctx)
    assert r["status"] == "started" and r["direct_reply"] == "" and games.GAMES[120].players == {10, 11}
    r = await registry.execute("start_game", {"game": "riddle"}, ctx)
    assert "error" in r and "direct_reply" not in r
    ctx_bob = N(**{**ctx.__dict__, "user_id": 12, "message": N(guild=guild, author=bob, channel=ch)})
    assert "error" in await registry.execute("stop_game", {}, ctx_bob)
    r = await registry.execute("stop_game", {}, ctx)
    assert r["status"] == "stopped" and 120 not in games.GAMES
    assert "error" in await registry.execute("start_game", {"game": "chess"}, ctx)
    assert "error" in await registry.execute("start_game", {"game": "riddle", "players": ["zzzz"]}, ctx)
    r = await registry.execute("game_leaderboard", {}, ctx)
    assert "Hardik" in r["direct_reply"] and "Ann" in r["direct_reply"]
    assert agent.action_succeeded(r) and r["direct_reply"]
    for s in ("neuro let's play riddles", "start a trivia game", "me and hardik want to play 20 questions", "show the leaderboard"):
        assert agent.ACTION_RE.search(s), s
    assert agent.unbacked_claims("I started a riddle game for you!", set())
    assert not agent.unbacked_claims("I started a riddle game for you!", {"start_game"})
    assert agent.SILENT


async def _all():
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "g.db")
    await db.init()
    try:
        await check_riddle()
        await check_idle_and_stop()
        await check_trivia()
        await check_twenty()
        await check_tictactoe()
        await check_tools_and_board()
    finally:
        for g in list(games.GAMES.values()):
            await games.finish(g, None, announce=False)
        await db.close()
    check_logic()
    check_data()


def test_games():
    asyncio.run(_all())


if __name__ == "__main__":
    asyncio.run(_all())
    print("ALL GAME CHECKS PASSED")
