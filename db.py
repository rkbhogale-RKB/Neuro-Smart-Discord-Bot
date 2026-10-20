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

-- Where the bot talked to someone recently (used for cross-channel context).
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inter ON interactions(server_id, user_id, ts);

-- Moderator warnings (strikes). Never deleted by retention; removed only by a moderator.
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warn ON warnings(server_id, user_id, ts);

-- Reminders. repeat = '' | 'daily' | 'weekly'. One-shot reminders get sent=1 after delivery.
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    creator_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    due_ts REAL NOT NULL,
    repeat TEXT NOT NULL DEFAULT '',
    sent INTEGER NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(sent, due_ts);

-- Server builder history. kind: build | edit | delete. Every thing Neuro creates or changes is
-- recorded item by item (with the old values for edits) so "undo" is exact.
CREATE TABLE IF NOT EXISTS builds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_ts REAL NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_builds ON builds(server_id, user_id, created_ts);
CREATE TABLE IF NOT EXISTS build_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    discord_id INTEGER,
    name TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_build_items ON build_items(build_id);

-- Per-server settings changed from Discord (e.g. /neuro setlog).
CREATE TABLE IF NOT EXISTS guild_settings (
    server_id INTEGER PRIMARY KEY,
    log_channel_id INTEGER
);
"""


async def init() -> None:
    global _db
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(SCHEMA)
    # Retention: don't keep raw chat forever.
    cutoff = time.time() - config.MESSAGE_RETENTION_DAYS * 86400
    await _db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
    await _db.execute("DELETE FROM interactions WHERE ts < ?", (cutoff,))
    await _db.execute("DELETE FROM tool_usage WHERE ts < ?", (cutoff,))
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


async def recent_messages_by_user(server_id: int, user_id: int, n: int):
    """Latest logged messages from one person in a server (oldest first). Used by mod tools."""
    cur = await _db.execute(
        "SELECT author_name, is_bot, content, ts FROM messages"
        " WHERE server_id=? AND user_id=? ORDER BY ts DESC LIMIT ?",
        (server_id, user_id, n),
    )
    return list(reversed(await cur.fetchall()))


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


# ---------- facts: counting / deleting ----------
async def count_current_facts(server_id: int, user_id: int) -> int:
    cur = await _db.execute(
        "SELECT COUNT(*) AS c FROM facts WHERE server_id=? AND user_id=? AND superseded_ts IS NULL",
        (server_id, user_id),
    )
    return (await cur.fetchone())["c"]


async def delete_facts(server_id: int, user_id: int, key: str | None = None) -> int:
    """Hard-delete (history included). key=None deletes everything for that user."""
    if key:
        cur = await _db.execute(
            "DELETE FROM facts WHERE server_id=? AND user_id=? AND key=?",
            (server_id, user_id, key))
    else:
        cur = await _db.execute(
            "DELETE FROM facts WHERE server_id=? AND user_id=?", (server_id, user_id))
    await _db.commit()
    return cur.rowcount


# ---------- cross-channel recall ----------
async def record_interaction(server_id: int, channel_id: int, user_id: int) -> None:
    await _db.execute(
        "INSERT INTO interactions(server_id, channel_id, user_id, ts) VALUES (?,?,?,?)",
        (server_id, channel_id, user_id, time.time()))
    await _db.commit()


async def other_channels_recent(server_id: int, user_id: int, current_channel_id: int,
                                since_ts: float, limit: int) -> list[int]:
    """Channels (other than the current one) where the bot recently talked to this user."""
    cur = await _db.execute(
        "SELECT channel_id, MAX(ts) AS t FROM interactions"
        " WHERE server_id=? AND user_id=? AND channel_id != ? AND ts > ?"
        " GROUP BY channel_id ORDER BY t DESC LIMIT ?",
        (server_id, user_id, current_channel_id, since_ts, limit))
    return [r["channel_id"] for r in await cur.fetchall()]


async def is_no_share(channel_id: int) -> bool:
    cur = await _db.execute("SELECT no_share FROM channel_config WHERE channel_id=?", (channel_id,))
    row = await cur.fetchone()
    return bool(row and row["no_share"])


async def set_no_share(channel_id: int, flag: bool) -> None:
    await _db.execute(
        "INSERT INTO channel_config(channel_id, no_share) VALUES (?,?)"
        " ON CONFLICT(channel_id) DO UPDATE SET no_share=excluded.no_share",
        (channel_id, int(flag)))
    await _db.commit()

# ---------- warnings ----------
def _warn_cutoff() -> float:
    return time.time() - config.WARN_EXPIRE_DAYS * 86400 if config.WARN_EXPIRE_DAYS > 0 else 0.0


async def add_warning(server_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    cur = await _db.execute(
        "INSERT INTO warnings(server_id, user_id, moderator_id, reason, ts) VALUES (?,?,?,?,?)",
        (server_id, user_id, moderator_id, reason, time.time()))
    await _db.commit()
    return cur.lastrowid


async def active_warnings(server_id: int, user_id: int):
    """Warnings that still count (newest first). Old ones expire after WARN_EXPIRE_DAYS."""
    cur = await _db.execute(
        "SELECT id, moderator_id, reason, ts FROM warnings"
        " WHERE server_id=? AND user_id=? AND ts>? ORDER BY ts DESC",
        (server_id, user_id, _warn_cutoff()))
    return await cur.fetchall()


async def delete_warning(server_id: int, user_id: int, warning_id: int | None = None) -> int:
    """Remove one warning by id, or the newest one if warning_id is None. Returns rows removed."""
    if warning_id is None:
        cur = await _db.execute(
            "DELETE FROM warnings WHERE id=(SELECT id FROM warnings WHERE server_id=? AND user_id=?"
            " ORDER BY ts DESC LIMIT 1)", (server_id, user_id))
    else:
        cur = await _db.execute(
            "DELETE FROM warnings WHERE id=? AND server_id=? AND user_id=?",
            (warning_id, server_id, user_id))
    await _db.commit()
    return cur.rowcount


async def clear_warnings(server_id: int, user_id: int) -> int:
    cur = await _db.execute("DELETE FROM warnings WHERE server_id=? AND user_id=?", (server_id, user_id))
    await _db.commit()
    return cur.rowcount


# ---------- reminders ----------
async def add_reminder(server_id, channel_id, creator_id, target_id, text, due_ts, repeat) -> int:
    cur = await _db.execute(
        "INSERT INTO reminders(server_id, channel_id, creator_id, target_id, text, due_ts, repeat,"
        " created_ts) VALUES (?,?,?,?,?,?,?,?)",
        (server_id, channel_id, creator_id, target_id, text, due_ts, repeat, time.time()))
    await _db.commit()
    return cur.lastrowid


async def pending_reminders(server_id: int, creator_id: int | None = None, limit: int = 25):
    if creator_id is None:
        cur = await _db.execute(
            "SELECT * FROM reminders WHERE server_id=? AND sent=0 ORDER BY due_ts LIMIT ?",
            (server_id, limit))
    else:
        cur = await _db.execute(
            "SELECT * FROM reminders WHERE server_id=? AND creator_id=? AND sent=0"
            " ORDER BY due_ts LIMIT ?", (server_id, creator_id, limit))
    return await cur.fetchall()


async def count_pending_reminders(server_id: int, creator_id: int) -> int:
    cur = await _db.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE server_id=? AND creator_id=? AND sent=0",
        (server_id, creator_id))
    return (await cur.fetchone())["c"]


async def get_reminder(reminder_id: int):
    cur = await _db.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,))
    return await cur.fetchone()


async def cancel_reminder(reminder_id: int) -> int:
    cur = await _db.execute("UPDATE reminders SET sent=1 WHERE id=? AND sent=0", (reminder_id,))
    await _db.commit()
    return cur.rowcount


async def due_reminders(now: float, limit: int = 20):
    cur = await _db.execute(
        "SELECT * FROM reminders WHERE sent=0 AND due_ts<=? ORDER BY due_ts LIMIT ?", (now, limit))
    return await cur.fetchall()


async def claim_reminder(reminder_id: int, old_due: float, next_due: float | None) -> bool:
    """Take a due reminder so it is delivered exactly once. One-shot: mark sent. Repeating:
    move it to its next time. Returns False if someone else already took it."""
    if next_due is None:
        cur = await _db.execute("UPDATE reminders SET sent=1 WHERE id=? AND sent=0", (reminder_id,))
    else:
        cur = await _db.execute(
            "UPDATE reminders SET due_ts=? WHERE id=? AND sent=0 AND due_ts=?",
            (next_due, reminder_id, old_due))
    await _db.commit()
    return cur.rowcount == 1


# ---------- logged messages (used by the event log) ----------
async def get_logged_message(discord_msg_id: int):
    cur = await _db.execute(
        "SELECT user_id, author_name, is_bot, content, channel_id FROM messages WHERE discord_msg_id=?",
        (discord_msg_id,))
    return await cur.fetchone()


# ---------- per-server settings ----------
async def get_log_channel(server_id: int) -> int | None:
    cur = await _db.execute("SELECT log_channel_id FROM guild_settings WHERE server_id=?", (server_id,))
    row = await cur.fetchone()
    return row["log_channel_id"] if row and row["log_channel_id"] else None


async def set_log_channel(server_id: int, channel_id: int | None) -> None:
    await _db.execute(
        "INSERT INTO guild_settings(server_id, log_channel_id) VALUES (?,?)"
        " ON CONFLICT(server_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
        (server_id, channel_id))
    await _db.commit()


# ---------- server builder history ----------
async def create_build(server_id: int, user_id: int, title: str, kind: str) -> int:
    cur = await _db.execute(
        "INSERT INTO builds(server_id, user_id, title, kind, created_ts) VALUES (?,?,?,?,?)",
        (server_id, user_id, title[:80], kind, time.time()))
    await _db.commit()
    return cur.lastrowid


async def add_build_item(build_id: int, kind: str, discord_id: int | None, name: str, data: str = "") -> None:
    await _db.execute("INSERT INTO build_items(build_id, kind, discord_id, name, data) VALUES (?,?,?,?,?)",
                      (build_id, kind, discord_id, name[:100], data))
    await _db.commit()


async def get_build(build_id: int):
    cur = await _db.execute("SELECT * FROM builds WHERE id=?", (build_id,))
    return await cur.fetchone()


async def build_items(build_id: int):
    cur = await _db.execute("SELECT * FROM build_items WHERE build_id=? ORDER BY id", (build_id,))
    return await cur.fetchall()


async def last_build(server_id: int, user_id: int | None = None, kinds=("build", "edit")):
    """Newest build that is not undone yet (by this user if given)."""
    marks = ",".join("?" * len(kinds))
    sql = f"SELECT * FROM builds WHERE server_id=? AND undone=0 AND kind IN ({marks})"
    args: list = [server_id, *kinds]
    if user_id is not None:
        sql += " AND user_id=?"
        args.append(user_id)
    cur = await _db.execute(sql + " ORDER BY id DESC LIMIT 1", args)
    return await cur.fetchone()


async def recent_builds(server_id: int, limit: int = 10):
    cur = await _db.execute("SELECT * FROM builds WHERE server_id=? ORDER BY id DESC LIMIT ?",
                            (server_id, limit))
    return await cur.fetchall()


async def mark_build_undone(build_id: int) -> None:
    await _db.execute("UPDATE builds SET undone=1 WHERE id=?", (build_id,))
    await _db.commit()


async def count_builds_since(server_id: int, since_ts: float, kind: str = "build") -> int:
    cur = await _db.execute(
        "SELECT COUNT(*) AS c FROM builds WHERE server_id=? AND kind=? AND created_ts>?",
        (server_id, kind, since_ts))
    return (await cur.fetchone())["c"]
