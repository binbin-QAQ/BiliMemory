from __future__ import annotations
import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
DB = ROOT / "data" / "comment_memory.db"


def config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def settings() -> dict[str, Any]:
    return dict(config().get("comment_memory") or {})


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS own_comments (
            rpid TEXT PRIMARY KEY,
            oid INTEGER NOT NULL,
            bvid TEXT,
            root_rpid TEXT,
            parent_rpid TEXT,
            video_title TEXT,
            content TEXT NOT NULL,
            thread_key TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            raw_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_own_comments_expiry ON own_comments(expires_at);
        CREATE INDEX IF NOT EXISTS idx_own_comments_thread ON own_comments(thread_key);
        CREATE TABLE IF NOT EXISTS thread_snapshots (
            thread_key TEXT PRIMARY KEY,
            oid INTEGER NOT NULL,
            root_rpid TEXT NOT NULL,
            context_json TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_thread_expiry ON thread_snapshots(expires_at);
    """)
    return db


def retention_hours(key: str, default: float) -> float:
    value = settings().get(key, default)
    try:
        return max(1.0, float(value))
    except Exception:
        return default


def prune(db: sqlite3.Connection | None = None) -> int:
    own = db or connect()
    now = time.time()
    try:
        cursor = own.execute("DELETE FROM own_comments WHERE expires_at <= ?", (now,))
        removed_comments = cursor.rowcount
        cursor = own.execute("DELETE FROM thread_snapshots WHERE expires_at <= ?", (now,))
        removed_threads = cursor.rowcount
        own.commit()
        return max(0, removed_comments) + max(0, removed_threads)
    finally:
        if db is None:
            own.close()


def remember_own_comment(payload: dict[str, Any]) -> None:
    cfg = settings()
    if not cfg.get("remember_own_comments", True):
        return
    now = time.time()
    expires = now + retention_hours("own_comments_retention_hours", 24) * 3600
    oid = int(payload.get("oid") or 0)
    root = str(payload.get("root_rpid") or payload.get("root") or payload.get("rpid") or "")
    thread_key = f"{oid}:{root}"
    with connect() as db:
        db.execute(
            """INSERT OR REPLACE INTO own_comments
            (rpid, oid, bvid, root_rpid, parent_rpid, video_title, content, thread_key, created_at, expires_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(payload["rpid"]), oid, str(payload.get("bvid") or ""), root,
                str(payload.get("parent_rpid") or payload.get("parent") or ""),
                str(payload.get("video_title") or ""), str(payload.get("content") or ""),
                thread_key, now, expires, json.dumps(payload, ensure_ascii=False),
            ),
        )
        db.commit()


def remember_thread_snapshot(oid: int, root_rpid: str, comments: list[dict[str, Any]]) -> str:
    limit = int(settings().get("thread_context_limit", 50) or 50)
    thread_key = f"{int(oid)}:{str(root_rpid)}"
    now = time.time()
    expires = now + retention_hours("thread_context_retention_hours", 24) * 3600
    context = comments[: max(1, min(limit, 200))]
    with connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO thread_snapshots(thread_key, oid, root_rpid, context_json, fetched_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (thread_key, int(oid), str(root_rpid), json.dumps(context, ensure_ascii=False), now, expires),
        )
        db.commit()
    return thread_key


def thread_context(oid: int, root_rpid: str) -> list[dict[str, Any]]:
    thread_key = f"{int(oid)}:{str(root_rpid)}"
    with connect() as db:
        row = db.execute("SELECT context_json, expires_at FROM thread_snapshots WHERE thread_key = ?", (thread_key,)).fetchone()
        if not row or float(row["expires_at"]) <= time.time():
            return []
        try:
            return list(json.loads(row["context_json"]))
        except Exception:
            return []


def today_comments() -> list[dict[str, Any]]:
    now = time.time()
    local_start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM own_comments WHERE created_at >= ? AND expires_at > ? ORDER BY created_at DESC",
            (local_start, now),
        ).fetchall()
    return [dict(row) for row in rows]


def status() -> dict[str, Any]:
    with connect() as db:
        own_count = db.execute("SELECT COUNT(*) FROM own_comments").fetchone()[0]
        thread_count = db.execute("SELECT COUNT(*) FROM thread_snapshots").fetchone()[0]
        oldest = db.execute("SELECT MIN(created_at) FROM own_comments").fetchone()[0]
    return {
        "db": str(DB), "own_comments": own_count, "thread_snapshots": thread_count,
        "oldest_comment_at": oldest, "settings": settings(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bilibili comment short-term memory")
    parser.add_argument("command", choices=["status", "prune", "today"])
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.command == "prune":
        print(json.dumps({"removed": prune()}, ensure_ascii=False))
    else:
        print(json.dumps(today_comments(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
