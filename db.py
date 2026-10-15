"""SQLite storage. Every row carries server_id / channel_id / user_id so that
cross-channel recall and permission checks (later steps) are possible."""
import time
from pathlib import Path

import aiosqlite

import config

_db: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_msg_id INTEGER UNIQUE,
    server_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    author_name TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_channel_ts ON messages(channel_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_user_ts ON messages(user_id, ts);

-- Durable per-user facts. Current fact = superseded_ts IS NULL; old values stay as history.
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source_channel_id INTEGER,
    created_ts REAL NOT NULL,
    superseded_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(server_id, user_id, key);

CREATE TABLE IF NOT EXISTS channel_config (
    channel_id INTEGER PRIMARY KEY,
    no_share INTEGER NOT NULL DEFAULT 0,
    persona TEXT,
    allowed_tools TEXT,       -- comma separated, NULL = all
    memory_scope TEXT         -- this_channel | server
);

CREATE TABLE IF NOT EXISTS user_settings (
    server_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    activity_optout INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (server_id, user_id)
);

CREATE TABLE IF NOT EXISTS tool_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    tool TEXT NOT NULL,
    cost INTEGER NOT NULL DEFAULT 1,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage ON tool_usage(user_id, tool, ts);
"""


async def init() -> None:
    global _db
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(SCHEMA)
    await _db.commit()


async def close() -> None:
    if _db:
        await _db.close()


# ---------- short-term memory / message log ----------
async def log_message(discord_msg_id, server_id, channel_id, user_id,
                      author_name, is_bot, content, ts) -> None:
    await _db.execute(
        "INSERT OR IGNORE INTO messages(discord_msg_id, server_id, channel_id, user_id,"
        " author_name, is_bot, content, ts) VALUES (?,?,?,?,?,?,?,?)",
        (discord_msg_id, server_id, channel_id, user_id, author_name, int(is_bot), content, ts),
    )
    await _db.commit()


async def recent_messages(channel_id: int, n: int, exclude_msg_id: int | None = None):
    cur = await _db.execute(
        "SELECT author_name, is_bot, content, ts FROM messages"
        " WHERE channel_id=? AND (? IS NULL OR discord_msg_id != ?)"
        " ORDER BY ts DESC LIMIT ?",
        (channel_id, exclude_msg_id, exclude_msg_id, n),
    )
    rows = await cur.fetchall()
    return list(reversed(rows))


# ---------- per-user facts ----------
async def save_fact(server_id: int, user_id: int, key: str, value: str,
                    channel_id: int | None) -> str:
    """Newest wins; the replaced value is kept as history. Returns saved|updated|unchanged."""
    cur = await _db.execute(
        "SELECT id, value FROM facts WHERE server_id=? AND user_id=? AND key=?"
        " AND superseded_ts IS NULL",
        (server_id, user_id, key),
    )
    row = await cur.fetchone()
    if row and row["value"].strip().lower() == value.strip().lower():
        return "unchanged"
    now = time.time()
    if row:
        await _db.execute("UPDATE facts SET superseded_ts=? WHERE id=?", (now, row["id"]))
    await _db.execute(
        "INSERT INTO facts(server_id, user_id, key, value, source_channel_id, created_ts)"
        " VALUES (?,?,?,?,?,?)",
        (server_id, user_id, key, value, channel_id, now),
    )
    await _db.commit()
    return "updated" if row else "saved"


async def current_facts(server_id: int, user_id: int, limit: int = 25):
    cur = await _db.execute(
        "SELECT key, value FROM facts WHERE server_id=? AND user_id=?"
        " AND superseded_ts IS NULL ORDER BY created_ts DESC LIMIT ?",
        (server_id, user_id, limit),
    )
    return await cur.fetchall()


# ---------- budgets ----------
async def record_tool_call(user_id: int, tool: str, cost: int) -> None:
    await _db.execute(
        "INSERT INTO tool_usage(user_id, tool, cost, ts) VALUES (?,?,?,?)",
        (user_id, tool, cost, time.time()),
    )
    await _db.commit()


async def count_tool_calls(user_id: int, tool: str, since_ts: float) -> int:
    cur = await _db.execute(
        "SELECT COUNT(*) AS c FROM tool_usage WHERE user_id=? AND tool=? AND ts>?",
        (user_id, tool, since_ts),
    )
    return (await cur.fetchone())["c"]
