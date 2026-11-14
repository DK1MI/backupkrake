"""Tiny SQLite layer. One shared connection guarded by a lock is plenty for a home server."""
import json
import sqlite3
import threading
from datetime import datetime
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    address         TEXT NOT NULL,
    port            INTEGER NOT NULL DEFAULT 22,
    user            TEXT NOT NULL DEFAULT 'root',
    scope           TEXT NOT NULL DEFAULT 'system', -- system (root, absolute paths) | home (paths relative to the user's home)
    paths           TEXT NOT NULL,
    excludes        TEXT NOT NULL DEFAULT '',
    schedule        TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    one_file_system INTEGER NOT NULL DEFAULT 0,
    acls_xattrs     INTEGER NOT NULL DEFAULT 1,
    pre_cmd         TEXT NOT NULL DEFAULT '',
    post_cmd        TEXT NOT NULL DEFAULT '',
    extra_args      TEXT NOT NULL DEFAULT '',
    keep_last       INTEGER NOT NULL DEFAULT 3,
    keep_daily      INTEGER NOT NULL DEFAULT 7,
    keep_weekly     INTEGER NOT NULL DEFAULT 4,
    keep_monthly    INTEGER NOT NULL DEFAULT 12,
    max_age_days    INTEGER,                 -- NULL = global default, 0 = never alert
    bwlimit         TEXT NOT NULL DEFAULT '', -- rsync --bwlimit value, e.g. 20M; '' = unlimited
    backup_window   TEXT NOT NULL DEFAULT '', -- e.g. 22:00-06:00; '' = any time
    created         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL,             -- backup | restore | offsite | offsite-init | offsite-check | offsite-verify | offsite-restore
    host_id   INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
    host_name TEXT,
    status    TEXT NOT NULL,             -- queued|running|success|warning|failed|cancelled|interrupted|skipped
    trigger   TEXT NOT NULL DEFAULT 'manual',
    started   TEXT NOT NULL,
    finished  TEXT,
    snapshot  TEXT,
    summary   TEXT NOT NULL DEFAULT '',
    stats     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS runs_host ON runs(host_id, id DESC);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

HOST_FIELDS = [
    "name", "address", "port", "user", "scope", "paths", "excludes", "schedule", "enabled",
    "one_file_system", "acls_xattrs", "pre_cmd", "post_cmd", "extra_args",
    "keep_last", "keep_daily", "keep_weekly", "keep_monthly", "max_age_days",
    "bwlimit", "backup_window",
]

# Columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("hosts", "max_age_days", "INTEGER"),
    ("hosts", "bwlimit", "TEXT NOT NULL DEFAULT ''"),
    ("hosts", "backup_window", "TEXT NOT NULL DEFAULT ''"),
    ("hosts", "scope", "TEXT NOT NULL DEFAULT 'system'"),
]

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def init() -> None:
    global _conn
    config.ensure_dirs()
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)
    for table, column, definition in MIGRATIONS:
        cols = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    # Runs that were in flight when the server stopped can never finish.
    _conn.execute(
        "UPDATE runs SET status='failed', finished=?, summary='interrupted (server restart)' "
        "WHERE status IN ('queued','running')",
        (now(),),
    )


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def query(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> int:
    with _lock:
        return _conn.execute(sql, args).lastrowid


# --- hosts -----------------------------------------------------------------

def get_host(host_id: int) -> dict | None:
    return one("SELECT * FROM hosts WHERE id=?", (host_id,))


def list_hosts() -> list[dict]:
    return query("SELECT * FROM hosts ORDER BY name")


def create_host(data: dict) -> int:
    cols = ", ".join(HOST_FIELDS + ["created"])
    marks = ", ".join("?" for _ in HOST_FIELDS + ["created"])
    return execute(
        f"INSERT INTO hosts ({cols}) VALUES ({marks})",
        tuple(data[f] for f in HOST_FIELDS) + (now(),),
    )


def update_host(host_id: int, data: dict) -> None:
    sets = ", ".join(f"{f}=?" for f in HOST_FIELDS)
    execute(f"UPDATE hosts SET {sets} WHERE id=?", tuple(data[f] for f in HOST_FIELDS) + (host_id,))


def delete_host(host_id: int) -> None:
    execute("DELETE FROM hosts WHERE id=?", (host_id,))


# --- runs ------------------------------------------------------------------

def create_run(kind: str, host: dict | None, trigger: str, status: str = "queued") -> int:
    return execute(
        "INSERT INTO runs (kind, host_id, host_name, status, trigger, started) VALUES (?,?,?,?,?,?)",
        (kind, host["id"] if host else None, host["name"] if host else None, status, trigger, now()),
    )


def update_run(run_id: int, **fields: Any) -> None:
    if "stats" in fields and not isinstance(fields["stats"], str):
        fields["stats"] = json.dumps(fields["stats"])
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE runs SET {sets} WHERE id=?", tuple(fields.values()) + (run_id,))


def get_run(run_id: int) -> dict | None:
    r = one("SELECT * FROM runs WHERE id=?", (run_id,))
    if r:
        r["stats"] = json.loads(r["stats"] or "{}")
    return r


def list_runs(host_id: int | None = None, limit: int = 50, kind: str | None = None) -> list[dict]:
    where, args = [], []
    if host_id is not None:
        where.append("host_id=?")
        args.append(host_id)
    if kind is not None:
        where.append("kind=?")
        args.append(kind)
    sql = "SELECT * FROM runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = query(sql, tuple(args) + (limit,))
    for r in rows:
        r["stats"] = json.loads(r["stats"] or "{}")
    return rows


def last_run(host_id: int, kind: str = "backup", status: tuple[str, ...] | None = None) -> dict | None:
    sql = "SELECT * FROM runs WHERE host_id=? AND kind=?"
    args: list = [host_id, kind]
    if status:
        sql += f" AND status IN ({','.join('?' for _ in status)})"
        args += list(status)
    r = one(sql + " ORDER BY id DESC LIMIT 1", tuple(args))
    if r:
        r["stats"] = json.loads(r["stats"] or "{}")
    return r


# --- settings --------------------------------------------------------------

def get_setting(key: str, default: Any = None) -> Any:
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
