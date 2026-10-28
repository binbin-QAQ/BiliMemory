from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
POLICY_PATH = ROOT / "reply_policy.json"
STATE_PATH = ROOT / "storage_state.json"
DB_PATH = ROOT / "data" / "bili_dm.db"
STATUS_PATH = ROOT / "data" / "bili_dm_status.json"
PID_PATH = ROOT / "data" / "bili_dm.pid"
EMOTES_PATH = ROOT / "emotes.json"
PUBLIC_COMMENT_DELIVERY_ENABLED = True
ADMIN_BASE = "http://127.0.0.1:8765"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
BVID_RE = re.compile(r"BV[0-9A-Za-z]{10,}")
SHORT_BILI_RE = re.compile(r"https?://(?:b23\.tv|bili2233\.cn)/[A-Za-z0-9]+", re.IGNORECASE)
BILI_HOST_RE = re.compile(r"https?://(?:www\.)?(?:bilibili\.com|b23\.tv|bili2233\.cn)/", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>'\"\u3002\uff0c\uff1b\uff09\uff3d]+", re.IGNORECASE)
REVIEW_RE = re.compile(r"(?:\u770b\u9519|\u6ca1\u770b\u61c2|\u770b\u4e0d\u61c2|\u91cd\u65b0\u770b|\u518d\u4ed4\u7ec6\u770b|\u4ed4\u7ec6\u770b|\u8ba4\u771f\u770b|\u7ec6\u770b|\u91cd\u770b|\u518d\u770b\u4e00\u904d|\u91cd\u65b0\u5206\u6790|\u6839\u672c\u6ca1\u770b|\u770b\u6f0f|\u6f0f\u770b|\u4e0d\u662f\u8fd9\u4e2a|\u4e0d\u7b26|\u5f20\u51a0\u674e\u6234)", re.IGNORECASE)
work_queue: queue.Queue[dict[str, Any]] = queue.Queue()
instance_socket: socket.socket | None = None
stop_event = threading.Event()


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return fallback


def config() -> dict[str, Any]:
    return load_json(CONFIG_PATH, {})


def dm_config() -> dict[str, Any]:
    return dict(config().get("bili_dm") or {})


def owner_uid() -> int:
    direct = int(dm_config().get("owner_uid") or 0)
    if direct > 0:
        return direct
    return int(load_json(POLICY_PATH, {}).get("owner_bilibili_uid") or 0)


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        log_dir = ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "bili_dm.log").open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    except Exception:
        pass


def cookies_from_state() -> dict[str, str]:
    state = load_json(STATE_PATH, {})
    result: dict[str, str] = {}
    for item in state.get("cookies") or []:
        name = str(item.get("name") or "")
        domain = str(item.get("domain") or "")
        value = str(item.get("value") or "")
        normalized_domain = domain.lstrip(".").lower()
        if name and value and (normalized_domain == "bilibili.com" or normalized_domain.endswith(".bilibili.com")):
            result[name] = value
    return result


def cookie_header(cookies: dict[str, str]) -> str:
    order = ["SESSDATA", "bili_jct", "buvid3", "DedeUserID", "DedeUserID__ckMd5", "buvid4", "b_nut", "bili_ticket", "_uuid"]
    names = [name for name in order if cookies.get(name)]
    names.extend(sorted(name for name in cookies if name not in names))
    return "; ".join(f"{name}={cookies[name]}" for name in names)


def headers(cookies: dict[str, str], referer: str = "https://message.bilibili.com/") -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Referer": referer,
        "Origin": "https://message.bilibili.com",
        "Cookie": cookie_header(cookies),
    }


def api_get(url: str, cookies: dict[str, str], params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(url, params=params, headers=headers(cookies), timeout=30)
    data = response.json()
    if response.status_code != 200 or data.get("code") not in (0, None):
        raise RuntimeError(f"Bilibili API HTTP {response.status_code} code={data.get('code')} message={data.get('message') or data.get('msg') or ''}")
    return data.get("data") or {}


def check_login(cookies: dict[str, str] | None = None) -> dict[str, Any]:
    cookies = cookies or cookies_from_state()
    missing = [name for name in ("SESSDATA", "bili_jct", "buvid3", "DedeUserID") if not cookies.get(name)]
    if missing:
        return {"ok": False, "error": "missing cookies", "missing": missing}
    variants = [
        {
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Cookie": cookie_header(cookies),
            "Accept": "application/json, text/plain, */*",
        },
        {
            "User-Agent": UA,
            "Cookie": cookie_header(cookies),
        },
        headers(cookies),
    ]
    last = {}
    for request_headers in variants:
        response = requests.get("https://api.bilibili.com/x/web-interface/nav", headers=request_headers, timeout=30)
        try:
            data = response.json()
        except Exception:
            data = {}
        payload = data.get("data") or {}
        last = {
            "ok": response.ok and bool(payload.get("isLogin")),
            "code": data.get("code"),
            "uid": int(payload.get("mid") or cookies.get("DedeUserID") or 0),
            "uname": str(payload.get("uname") or ""),
            "message": data.get("message") or "",
        }
        if last["ok"]:
            return last
    return last


def list_sessions(cookies: dict[str, str]) -> list[dict[str, Any]]:
    data = api_get(
        "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions",
        cookies,
        {"session_type": 1, "group_id": 0, "Build": 0, "mobi_app": "web"},
    )
    result = []
    for item in data.get("session_list") or []:
        uid = int(item.get("talker_id") or 0)
        if uid <= 0:
            continue
        result.append({
            "uid": uid,
            "unread": int(item.get("unread_count") or 0),
            "last_ts": int((item.get("last_msg") or {}).get("timestamp") or 0),
        })
    return result


def fetch_messages(cookies: dict[str, str], talker_uid: int, size: int = 20) -> list[dict[str, Any]]:
    data = api_get(
        "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs",
        cookies,
        {"talker_id": int(talker_uid), "session_type": 1, "size": max(1, min(50, size)), "build": 0, "mobi_app": "web"},
    )
    rows = list(data.get("messages") or [])
    rows.reverse()
    my_uid = int(cookies.get("DedeUserID") or 0)
    messages = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        msg_type = int(item.get("msg_type") or 1)
        content = item.get("content") or "{}"
        text = ""
        bvid = ""
        kind = "text"
        try:
            payload = json.loads(content)
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        bili_url = ""
        if msg_type in (7, 8):
            kind = "video_card"
            bvid = str(payload.get("bvid") or "")
            bili_url = str(payload.get("jump_url") or "")
            if not bvid:
                match = BVID_RE.search(bili_url)
                bvid = match.group(0) if match else ""
            title = str(payload.get("title") or payload.get("headline") or payload.get("desc") or "").strip()
            author = str(payload.get("author") or "").strip()
            text = "[shared video] " + " / ".join(part for part in (title, author, bvid) if part)
        else:
            text = str(payload.get("content") or payload.get("text") or "").strip()
            match = BVID_RE.search(text)
            bvid = match.group(0) if match else ""
            for raw in URL_RE.findall(text):
                if BILI_HOST_RE.match(raw):
                    bili_url = bili_url or raw
                if not bvid:
                    match = BVID_RE.search(raw)
                    if match:
                        bvid = match.group(0)
        if not text:
            text = "[non-text message]"
        messages.append({
            "key": str(item.get("msg_key") or item.get("msg_seqno") or item.get("timestamp") or uuid.uuid4()),
            "timestamp": int(item.get("timestamp") or 0),
            "sender_uid": int(item.get("sender_uid") or 0),
            "from_me": int(item.get("sender_uid") or 0) == my_uid,
            "kind": kind,
            "text": text[:1500],
            "bvid": bvid,
            "bili_url": bili_url,
            "raw_content": str(content)[:8000],
        })
    return messages


def resolve_bili_short(url: str) -> str:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
            allow_redirects=True,
            timeout=25,
            stream=True,
        )
        resolved = str(response.url or "")
        response.close()
        match = BVID_RE.search(resolved)
        return match.group(0) if match else ""
    except Exception as error:
        log(f"Bilibili short link resolve failed: {url} {error}")
        return ""


def send_dm_text(cookies: dict[str, str], receiver_uid: int, text: str) -> dict[str, Any]:
    if receiver_uid <= 0:
        return {"ok": False, "error": "invalid receiver"}
    device_id = str(dm_config().get("device_id") or "").strip()
    if not device_id:
        device_id = uuid.uuid4().hex.upper()
        try:
            cfg = config()
            section = dict(cfg.get("bili_dm") or {})
            section["device_id"] = device_id
            cfg["bili_dm"] = section
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    payload = {
        "msg[sender_uid]": str(cookies.get("DedeUserID") or ""),
        "msg[receiver_id]": str(receiver_uid),
        "msg[receiver_type]": "1",
        "msg[msg_type]": "1",
        "msg[msg_status]": "0",
        "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
        "msg[timestamp]": str(int(time.time())),
        "msg[dev_id]": device_id,
        "csrf": str(cookies.get("bili_jct") or ""),
    }
    try:
        response = requests.post(
            "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
            data=payload,
            headers=headers(cookies),
            timeout=30,
        )
        data = response.json()
    except Exception as error:
        return {"ok": False, "error": str(error), "msg_key": "", "code": None}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    msg_key = data.get("msg_key") or inner.get("msg_key")
    ok = response.ok and data.get("code") in (0, None) and bool(msg_key)
    return {
        "ok": ok,
        "error": "" if ok else str(data.get("message") or data.get("msg") or f"HTTP {response.status_code}"),
        "msg_key": str(msg_key or ""),
        "code": data.get("code"),
    }


class Store:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=20, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                msg_key TEXT PRIMARY KEY,
                uid INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                bvid TEXT,
                msg_ts INTEGER,
                handled_at REAL,
                reply_status TEXT,
                reply_error TEXT
            );
            CREATE TABLE IF NOT EXISTS video_tasks (
                bvid TEXT PRIMARY KEY,
                msg_key TEXT,
                url TEXT NOT NULL,
                title TEXT,
                summary TEXT,
                one_line TEXT,
                status TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'picked',
                memory_imported INTEGER NOT NULL DEFAULT 0,
                delivered INTEGER NOT NULL DEFAULT 0,
                admin_task_id TEXT,
                source TEXT NOT NULL DEFAULT 'bilibili_dm',
                delivery_target TEXT,
                source_message_key TEXT,
                review_level TEXT NOT NULL DEFAULT 'rough',
                private_mode INTEGER NOT NULL DEFAULT 0,
                requester_uid TEXT,
                comment_status TEXT,
                comment_rpid INTEGER NOT NULL DEFAULT 0,
                comment_verified INTEGER NOT NULL DEFAULT 0,
                comment_message TEXT,
                comment_checked_at REAL,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_video_tasks_status ON video_tasks(status, updated_at);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS emote_catalog (
                code TEXT PRIMARY KEY,
                label TEXT,
                source_uid INTEGER,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                count INTEGER NOT NULL DEFAULT 1
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)").fetchall()}
        if "raw_content" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN raw_content TEXT")
        task_columns = {row[1] for row in self.db.execute("PRAGMA table_info(video_tasks)").fetchall()}
        if "source" not in task_columns:
            self.db.execute("ALTER TABLE video_tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'bilibili_dm'")
        if "delivery_target" not in task_columns:
            self.db.execute("ALTER TABLE video_tasks ADD COLUMN delivery_target TEXT")
        if "source_message_key" not in task_columns:
            self.db.execute("ALTER TABLE video_tasks ADD COLUMN source_message_key TEXT")
        for name, definition in (
            ("review_level", "TEXT NOT NULL DEFAULT 'rough'"),
            ("private_mode", "INTEGER NOT NULL DEFAULT 0"),
            ("requester_uid", "TEXT"),
            ("comment_status", "TEXT"),
            ("comment_rpid", "INTEGER NOT NULL DEFAULT 0"),
            ("comment_verified", "INTEGER NOT NULL DEFAULT 0"),
            ("comment_message", "TEXT"),
            ("comment_checked_at", "REAL"),
        ):
            if name not in task_columns:
                self.db.execute(f"ALTER TABLE video_tasks ADD COLUMN {name} {definition}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def has_message(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM messages WHERE msg_key = ?", (key,)).fetchone() is not None

    def add_message(self, msg: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO messages(msg_key,uid,from_me,kind,text,bvid,msg_ts,raw_content) VALUES(?,?,?,?,?,?,?,?)",
            (msg["key"], msg["sender_uid"], int(bool(msg["from_me"])), msg["kind"], msg["text"], msg["bvid"], msg["timestamp"], msg.get("raw_content") or ""),
        )
        self.db.commit()

    def mark_message(self, key: str, status: str, error: str = "") -> None:
        self.db.execute("UPDATE messages SET handled_at=?, reply_status=?, reply_error=? WHERE msg_key=?", (time.time(), status, error[:1000], key))
        self.db.commit()

    def upsert_task(self, bvid: str, url: str, msg_key: str, status: str, **fields: Any) -> None:
        now = time.time()
        existing = self.db.execute("SELECT created_at FROM video_tasks WHERE bvid=?", (bvid,)).fetchone()
        if existing:
            assignments = ["status=?", "updated_at=?"]
            values: list[Any] = [status, now]
            for key, value in fields.items():
                assignments.append(f"{key}=?")
                values.append(value)
            values.append(bvid)
            self.db.execute(f"UPDATE video_tasks SET {', '.join(assignments)} WHERE bvid=?", values)
        else:
            self.db.execute(
                "INSERT INTO video_tasks(bvid,msg_key,url,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (bvid, msg_key, url, status, now, now),
            )
            if fields:
                assignments = [f"{key}=?" for key in fields]
                values = list(fields.values()) + [bvid]
                self.db.execute(f"UPDATE video_tasks SET {', '.join(assignments)} WHERE bvid=?", values)
        self.db.commit()

    def record_emote(self, code: str, source_uid: int) -> None:
        now = time.time()
        label = code.strip("[]")
        self.db.execute(
            """INSERT INTO emote_catalog(code,label,source_uid,first_seen,last_seen,count)
               VALUES(?,?,?,?,?,1)
               ON CONFLICT(code) DO UPDATE SET last_seen=excluded.last_seen, count=count+1""",
            (code, label, int(source_uid), now, now),
        )
        self.db.commit()

    def emote_catalog(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM emote_catalog ORDER BY last_seen DESC").fetchall()
        return [dict(row) for row in rows]

    def kv_get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value))
        self.db.commit()

    def get_task(self, bvid: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM video_tasks WHERE bvid=?", (str(bvid or ""),)).fetchone()
        return dict(row) if row else None

    def tasks(self, filter_name: str = "all", limit: int = 100) -> list[dict[str, Any]]:
        where = ""
        if filter_name == "picked":
            where = "WHERE category='picked'"
        elif filter_name == "skipped":
            where = "WHERE category='skipped' OR status='failed'"
        elif filter_name == "delivered":
            where = "WHERE delivered=1"
        elif filter_name in {"queued", "running", "completed", "failed", "interrupted"}:
            where = f"WHERE status='{filter_name}'"
        rows = self.db.execute(f"SELECT * FROM video_tasks {where} ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) count FROM video_tasks GROUP BY status").fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        counts["total"] = int(self.db.execute("SELECT COUNT(*) FROM video_tasks").fetchone()[0])
        counts["delivered"] = int(self.db.execute("SELECT COUNT(*) FROM video_tasks WHERE delivered=1").fetchone()[0])
        return counts


def persona_text() -> str:
    section = dm_config()
    candidates = []
    for key in ("persona_file", "agents_file"):
        raw = str(section.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        try:
            if path.exists():
                candidates.append(path.read_text(encoding="utf-8-sig").strip())
        except Exception:
            pass
    inline = str(section.get("persona_prompt") or "").strip()
    if inline:
        candidates.append(inline)
    return "\n\n".join(item for item in candidates if item)[:12000]


def recall_memory(query: str, limit: int = 8) -> str:
    try:
        from bilibili_tool import WEBUI_BASE, webui_session
        session = webui_session()
        response = session.get(
            WEBUI_BASE + "/api/webui/memory/records/search",
            params={"query": query[:500], "types": "episode,paragraph", "limit": max(1, min(20, limit)), "include_inactive": "false"},
            timeout=30,
        )
        response.raise_for_status()
        items = response.json().get("items") or []
        rows = []
        seen = set()
        for item in items:
            title = str(item.get("title") or "").strip()
            summary = str(item.get("summary") or "").strip()
            dedupe_key = (title, summary[:200])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            if title or summary:
                rows.append(f"- {title}: {summary[:500]}")
        return "\n".join(rows[:limit])[:6000]
    except Exception as error:
        log(f"memory recall failed: {error}")
        return ""


def emote_codes(text: str) -> list[str]:
    value = str(text or "")
    codes = []
    for inner in re.findall(r"\[([^\[\]]{1,160})\]", value):
        inner = inner.strip()
        if inner and "\n" not in inner:
            code = f"[{inner}]"
            if code not in codes:
                codes.append(code)
    return codes


def emote_code(text: str) -> str:
    codes = emote_codes(text)
    return codes[0] if len(codes) == 1 and str(text or "").strip() == codes[0] else ""


def emote_catalog() -> list[dict[str, Any]]:
    data = load_json(EMOTES_PATH, {})
    return [dict(item) for item in (data.get("items") or []) if item.get("code")]

def requested_test_emotes(user_text: str) -> list[dict[str, Any]]:
    text = str(user_text or "").casefold()
    markers = ["\u6d4b\u8bd5\u8868\u60c5", "\u53d1\u4e2a\u8868\u60c5", "\u6765\u4e2a\u8868\u60c5", "\u53d1\u4e00\u4e2a\u8868\u60c5", "\u8868\u60c5\u6d4b\u8bd5"]
    if not any(marker in text for marker in markers):
        return []
    owned = [item for item in emote_catalog() if item.get("owned")]
    data = load_json(EMOTES_PATH, {})
    test_key = str(data.get("test_key") or "")
    preferred = [item for item in owned if item.get("key") == test_key]
    return (preferred or owned)[:1]


def deterministic_action_reply(user_text: str) -> str:
    text = str(user_text or "").casefold()
    rules = [
        (["\u5173\u6ce8\u6211", "\u56de\u5173", "\u4e92\u5173", "\u5173\u6ce8\u4e00\u4e0b", "follow me"], "\u4e0d\u884c\uff0c\u5f53\u524d\u7248\u672c\u6ca1\u6709\u5173\u6ce8/\u56de\u5173\u529f\u80fd\u3002\u521a\u624d\u5982\u679c\u8bf4\u4e86\u201c\u5173\u6ce8\u6210\u529f\u201d\uff0c\u90a3\u662f\u9519\u8bef\u56de\u590d\uff0c\u5e76\u6ca1\u6709\u771f\u7684\u5173\u6ce8\u3002"),
        (["\u70b9\u8d5e", "\u6295\u5e01", "\u6536\u85cf", "\u4e09\u8fde"], "\u8fd9\u4e2a\u52a8\u4f5c\u6211\u73b0\u5728\u505a\u4e0d\u5230\uff0c\u5f53\u524d\u7248\u672c\u6ca1\u6709\u5f00\u653e\u70b9\u8d5e\u3001\u6295\u5e01\u6216\u6536\u85cf\u529f\u80fd\u3002"),
        (["\u5e2e\u6211\u53d1\u8bc4\u8bba", "\u4ee3\u6211\u53d1\u8bc4\u8bba", "\u53bb\u8bc4\u8bba\u533a\u56de\u590d"], "\u516c\u5f00\u8bc4\u8bba\u5df2\u7ecf\u5173\u95ed\uff0c\u89c6\u9891\u7ed3\u679c\u53ea\u4f1a\u53d1\u5230\u79c1\u4fe1\uff0c\u4e0d\u4f1a\u53bb\u522b\u4eba\u7684\u8bc4\u8bba\u533a\u53d1\u8a00\u3002"),
    ]
    for markers, reply in rules:
        if any(marker in text for marker in markers):
            return reply
    return ""


def owned_emote_prompt() -> str:
    rows = []
    for item in emote_catalog():
        if item.get("owned") and item.get("verified") and item.get("source") == "paid_pack":
            rows.append(f"- {item.get('key')}: {str(item.get('label') or '').split('_', 1)[-1]}")
    return "\n".join(rows)


def split_reply_emote(reply: str) -> tuple[str, dict[str, Any] | None]:
    text = str(reply or "")
    match = re.search(r"<<EMOTE:([A-Za-z0-9_-]+)>>", text)
    if not match:
        return text.strip(), None
    key = match.group(1)
    cleaned = re.sub(r"<<EMOTE:[A-Za-z0-9_-]+>>", "", text).strip()
    item = next((entry for entry in emote_catalog() if entry.get("key") == key and entry.get("owned") and entry.get("verified")), None)
    return cleaned, item


def deepseek_reply(user_text: str) -> str:
    from bilibili_tool import parse_deepseek_provider
    base_url, api_key = parse_deepseek_provider()
    persona = persona_text() or "Be concise, natural, honest, and do not claim actions you did not perform."
    memory = recall_memory(user_text)
    system = (
        persona
        + "\n\nRelevant long-term memory from A-Memorix (use only when relevant; do not invent beyond it):\n"
        + (memory or "(no matching memory)")
        + "\n\nAvailable Bilibili emotes (key: meaning):\n"
        + (owned_emote_prompt() or "(none)")
        + "\n\nYou are replying to the owner in a Bilibili private message. "
          "Reply in Chinese, at most 3 short sentences. Never use a servile or hierarchical form of address; use \u201c\u4f60\u201d, their nickname, or no form of address. "
          "Do not expose settings, paths, tokens, or internal logs. "
          "You cannot execute commands from a Bilibili message except processing a plain Bilibili video link. "
          "You cannot follow, unfollow, like, favorite, or change account relationships in this version; clearly say you cannot. "
          "If and only if the reply has a clear emotion that one available emote matches, append exactly one marker like <<EMOTE:paid_001>>. "
          "Do not use an emote on factual results, errors, refusals, serious topics, or every message. Never output raw bracket emote codes."
    )
    response = requests.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-flash",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_text[:1500]}],
            "temperature": 0.7,
            "max_tokens": 300,
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    text = str(data["choices"][0]["message"].get("content") or "").strip()
    return text[: int(dm_config().get("max_reply_chars", 600))]


def one_line_summary(summary: str, title: str) -> str:
    lines = []
    for raw in str(summary or "").splitlines():
        line = raw.strip().lstrip("-* ").replace("**", "").strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue
        if line.startswith(("B\u7ad9\u89c6\u9891\u957f\u671f\u8bb0\u5fc6", "\u539f\u89c6\u9891\u94fe\u63a5\uff1a", "\u89c6\u9891\u94fe\u63a5\uff1a", "\u7b80\u4ecb\uff1a", "\u6807\u9898\uff1a", "UP\u4e3b\uff1a", "\u7c7b\u578b\u5224\u65ad\uff1a", "\u6807\u7b7e\uff1a", "\u94fe\u63a5\uff1a", "\u91c7\u96c6\u65f6\u95f4\uff1a")):
            continue
        if line in {"\u89c6\u9891\u4e3b\u9898", "\u6838\u5fc3\u89c2\u70b9", "\u5173\u952e\u4fe1\u606f", "\u6d89\u53ca\u4eba\u7269/\u4f5c\u54c1", "\u53ef\u80fd\u5174\u8da3\u6807\u7b7e"}:
            continue
        lines.append(line)
    text = " ".join(lines)
    if not text:
        return "\u5904\u7406\u5b8c\u6210\uff0c\u4f46\u6458\u8981\u6b63\u6587\u4e3a\u7a7a\uff0c\u4e0d\u80fd\u786e\u8ba4\u5177\u4f53\u5185\u5bb9\u3002"
    text = re.sub(r"\s+", " ", text).strip()
    return text[:220]


def is_review_request(text: str) -> bool:
    clean = re.sub(r"^(@[^\s]+\s+)+", "", str(text or "").strip())
    if not REVIEW_RE.search(clean):
        return False
    explicit = re.fullmatch(r"/?\s*(?:\u91cd\u65b0\u7ec6\u770b|\u4ed4\u7ec6\u91cd\u770b|\u91cd\u770b|\u91cd\u65b0\u5206\u6790)(?:\u4e00\u4e0b|\u4e00\u904d)?\s*", clean, re.I)
    if explicit:
        return True
    if BVID_RE.search(clean) or BILI_HOST_RE.search(clean):
        return True
    context = re.search(r"(?:\u89c6\u9891|B\u7ad9|BV[0-9A-Za-z]{10,}|\u4f60\u53d1\u7684|\u4e0a\u4e00\u6761|\u521a\u624d\u90a3\u6761|\u521a\u624d\u90a3\u4e2a|\u90a3\u6761\u94fe\u63a5|\u8fd9\u4e2a\u94fe\u63a5|\u8fd9\u6761|\u8fd9\u4e2a\u89c6\u9891)", clean, re.I)
    return bool(context)


def summary_is_comment_confident(summary: str) -> bool:
    text = str(summary or "").strip()
    meaningful = len(re.sub(r"[#*\s]", "", text))
    if meaningful < 300:
        return False
    low_confidence_markers = (
        "\u6458\u8981\u6a21\u578b\u672a\u8fd4\u56de\u6b63\u6587",
        "\u5df2\u83b7\u53d6\u8bc1\u636e\u6574\u7406",
        "\u6a21\u578b\u6458\u8981\u5931\u8d25",
        "\u65e0\u6cd5\u786e\u8ba4\u5177\u4f53\u5185\u5bb9",
        "\u5b57\u5e55\u53ef\u80fd\u4e0e\u753b\u9762",
        "\u4e0e\u89c6\u9891\u4e3b\u9898\u660e\u663e\u4e0d\u4e00\u81f4",
    )
    return not any(marker in text for marker in low_confidence_markers)


def public_comment_body(summary: str) -> str:
    candidates = []
    for raw in str(summary or "").splitlines():
        line = raw.strip().lstrip("-* ").replace("**", "").strip()
        if not line or line.startswith(("#", ">", "---", "|")):
            continue
        lowered = line.casefold()
        skip_markers = (
            "\u94fe\u63a5", "UP\u4e3b", "\u6807\u9898\uff1a", "\u6807\u7b7e\uff1a", "\u7c7b\u578b\u5224\u65ad", "\u91c7\u96c6\u65f6\u95f4",
            "\u539f\u89c6\u9891\u94fe\u63a5", "\u8f93\u5165\u7ed9\u51fa\u7684\u7c7b\u578b\u5224\u65ad", "\u672c\u5730\u8bed\u97f3", "B\u7ad9\u5b57\u5e55",
            "\u753b\u9762\u4f9d\u636e", "\u70ed\u95e8\u8bc4\u8bba", "\u8bc4\u8bba\u53cd\u9988", "\u4f9d\u636e\u6765\u6e90", "\u8bc1\u636e\u6574\u7406",
            "\u6458\u8981\u6a21\u578b", "\u672c\u5730\u8bed\u97f3\u8f6c\u5199", "rpid", "api",
        )
        if any(marker.casefold() in lowered for marker in skip_markers):
            continue
        line = re.sub(r"^\u4f9d\u636e[^\uff0c\u3002]{0,80}[\uff0c,]\s*", "", line)
        line = re.sub(r"^\u4f9d\u636e[^\uff1a:]{0,80}[\uff1a:]\s*", "", line)
        line = re.sub(r"^(\u89c6\u9891\u4e3b\u9898|\u6838\u5fc3\u89c2\u70b9|\u5173\u952e\u4fe1\u606f|\u89c6\u9891\u6458\u8981|\u4e00\u53e5\u8bdd\u6982\u62ec|\u753b\u9762\u4e0e\u6545\u4e8b|\u753b\u9762\u5185\u5bb9|\u5185\u5bb9\u6982\u62ec)[\uff1a:]\s*", "", line)
        if len(re.sub(r"\s", "", line)) >= 12:
            candidates.append(line)
    if not candidates:
        return ""
    body = candidates[0]
    body = re.split(r"[\uff1b;]", body, maxsplit=1)[0].strip()
    sentence = re.search(r"^(.{12,140}?[\u3002\uff01\uff1f.!?])", body)
    if sentence:
        body = sentence.group(1)
    body = body.replace("\u53ef\u786e\u8ba4\u672c\u89c6\u9891\u4e0e", "\u8fd9\u6761\u89c6\u9891\u8bb2\u7684\u662f")
    body = body.replace("\u672c\u89c6\u9891\u4e0e", "\u8fd9\u6761\u89c6\u9891\u662f\u5173\u4e8e")
    body = body.replace("\u4e3b\u9898\u76f8\u5173", "")
    body = body.replace("\u672c\u89c6\u9891", "\u8fd9\u6761\u89c6\u9891").strip().rstrip("\uff0c,\uff1b;")
    if body and body[-1] not in "\u3002\uff01\uff1f.!?":
        body += "\u3002"
    return body


def humanize_public_comment(summary: str, title: str) -> str:
    fallback = public_comment_body(summary)
    try:
        from bilibili_tool import parse_deepseek_provider
        base_url, api_key = parse_deepseek_provider()
        source = re.sub(r"\s+", " ", str(summary or "")).strip()[:5000]
        prompt = (
            "\u4f60\u662f\u666e\u901a B \u7ad9\u7528\u6237\u3002\u53ea\u6839\u636e\u4e0b\u9762\u7684\u89c6\u9891\u603b\u7ed3\uff0c\u5199\u4e00\u6761\u81ea\u7136\u3001\u53e3\u8bed\u3001\u5177\u4f53\u7684\u4e2d\u6587\u77ed\u8bc4\uff0c40 \u5230 80 \u5b57\u3002"
            "\u53ea\u5199\u89c6\u9891\u5185\u5bb9\u672c\u8eab\u3002\u4e0d\u8981\u628a\u8bc4\u8bba\u533a\u731c\u6d4b\u3001\u89c2\u4f17\u8bf4\u6cd5\u6216\u6807\u9898\u91cc\u65e0\u6cd5\u786e\u8ba4\u7684\u5185\u5bb9\u5199\u6210\u89c6\u9891\u4e8b\u5b9e\u3002\u7981\u6b62\u5199\u5904\u7406\u8fc7\u7a0b\u3001\u6458\u8981\u3001\u6a21\u578b\u3001\u8bc1\u636e\u3001\u5b57\u5e55\u3001\u62bd\u5e27\u3001\u753b\u9762\u63cf\u8ff0\u3001\u6807\u7b7e\u3001\u94fe\u63a5\u3001\u5931\u8d25\u3001\u65e0\u6cd5\u786e\u8ba4\u6216\u4efb\u4f55\u4e0d\u786e\u5b9a\u8bf4\u660e\u3002"
            "\u4e0d\u8981\u5ba2\u670d\u8154\uff0c\u4e0d\u8981\u6807\u9898\u590d\u8bfb\uff0c\u4e0d\u8981\u5206\u70b9\uff0c\u53ea\u8f93\u51fa\u8bc4\u8bba\u6b63\u6587\u3002\n\n\u89c6\u9891\u603b\u7ed3\uff1a\n" + source
        )
        response = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": "\u53ea\u8f93\u51fa\u4e00\u53e5\u81ea\u7136\u7684\u4e2d\u6587 B \u7ad9\u77ed\u8bc4\uff0c\u4e0d\u89e3\u91ca\u4efb\u52a1\uff0c\u4e0d\u8f93\u51fa\u601d\u8003\u8fc7\u7a0b\u3002"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.6,
                "max_tokens": 900,
            },
            timeout=90,
        )
        response.raise_for_status()
        data = response.json()
        text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        text = text.strip("\"' \n")
        text = re.sub(r"\s+", " ", text)
        banned = (
            "\u5904\u7406", "\u6458\u8981", "\u6a21\u578b", "\u8bc1\u636e", "\u5b57\u5e55", "\u62bd\u5e27", "\u753b\u9762\u63cf\u8ff0",
            "\u6807\u7b7e", "\u94fe\u63a5", "\u5931\u8d25", "\u65e0\u6cd5\u786e\u8ba4", "\u4e0d\u786e\u5b9a", "\u53ef\u80fd", "API", "rpid", "\u79c1\u4fe1",
        )
        matched_banned = [marker for marker in banned if marker.casefold() in text.casefold()]
        if 12 <= len(text) <= 120 and not matched_banned:
            return text
        log(f"humanized public comment rejected length={len(text)} banned={','.join(matched_banned)} text={text[:180]}")
    except Exception as error:
        log(f"public comment humanization fallback: {str(error)[:300]}")
    return fallback

def comment_mention_accounts() -> list[dict[str, str]]:
    try:
        path = Path(__file__).resolve().parent / "mention_allowlist.json"
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not data.get("enabled", True):
            return []
        accounts = []
        for item in data.get("accounts") or []:
            uid = str(item.get("uid") or "").strip()
            label = str(item.get("label") or "").strip()
            if not uid:
                continue
            accounts.append({"name": label or uid, "uid": uid})
        return accounts[:5]
    except Exception:
        return []


def deliver_comment_once(store: Store, bvid: str, summary: str, title: str) -> dict[str, Any]:
    result: dict[str, Any] = {"attempted": False, "accepted": False, "verified": False, "status": "disabled"}
    if not PUBLIC_COMMENT_DELIVERY_ENABLED:
        result.update(status="public_comments_disabled")
        return result
    section = dict(config().get("comment_delivery") or {})
    if not section.get("enabled") or not section.get("execute"):
        return result
    task = store.get_task(bvid)
    if not task:
        result.update(status="task_missing")
        return result
    if int(task.get("comment_rpid") or 0) > 0:
        result.update(status="already_sent", accepted=True, verified=bool(task.get("comment_verified")), rpid=int(task.get("comment_rpid") or 0))
        return result
    if task.get("status") != "completed" or not int(task.get("memory_imported") or 0):
        result.update(status="memory_not_confirmed")
        return result
    if not summary_is_comment_confident(summary):
        result.update(status="low_confidence_private_only")
        return result
    body = humanize_public_comment(summary, title)
    if not body:
        result.update(status="summary_missing")
        return result
    body = body[: int(section.get("max_chars", 100))]
    result["attempted"] = True
    try:
        from bili_comment_delivery import BilibiliCommentClient
        client = BilibiliCommentClient()
        accounts = comment_mention_accounts()
        if accounts:
            sent = client.send_comment(bvid, body, mention_accounts=accounts, execute=True)
        else:
            sent = client.send_comment(
                bvid,
                body,
                mention_name=str(section.get("mention_name") or "\u6ee8\u6ee8QAQ"),
                mention_uid=str(section.get("mention_uid") or "1000000003"),
                execute=True,
            )
    except Exception as error:
        result.update(status="exception", message=str(error)[:800])
        store.upsert_task(bvid, str(task.get("url") or ""), str(task.get("msg_key") or ""), "completed", comment_status="exception", comment_message=str(error)[:800], comment_checked_at=time.time())
        log(f"comment delivery exception bvid={bvid}: {str(error)[:300]}")
        return result
    rpid = int(sent.get("rpid") or 0)
    accepted = bool(sent.get("accepted") and rpid > 0)
    result.update(accepted=accepted, status=str(sent.get("status") or "unknown"), rpid=rpid, code=sent.get("code"), message=str(sent.get("message") or "")[:800])
    store.upsert_task(bvid, str(task.get("url") or ""), str(task.get("msg_key") or ""), "completed", comment_status=result["status"], comment_rpid=rpid, comment_message=result["message"], comment_checked_at=time.time())
    if not accepted:
        log(f"comment not accepted bvid={bvid} status={result['status']} code={result.get('code')}")
        return result
    verified = False
    checked: dict[str, Any] = {}
    if section.get("verify_readback", True):
        for delay in (2, 6, 12):
            time.sleep(delay)
            try:
                checked = client.verify_comment_visible(bvid, rpid)
            except Exception as error:
                checked = {"status": "verify_exception", "message": str(error)[:500], "verified": False}
            verified = bool(checked.get("verified"))
            if verified:
                break
    result.update(verified=verified, check_status=str(checked.get("status") or ""), checked=checked)
    store.upsert_task(
        bvid,
        str(task.get("url") or ""),
        str(task.get("msg_key") or ""),
        "completed",
        comment_status=("visible" if verified else result["status"]),
        comment_rpid=rpid,
        comment_verified=1 if verified else 0,
        comment_message=(str(checked.get("message") or "") if checked else result["message"])[:800],
        comment_checked_at=time.time(),
    )
    log(f"comment accepted bvid={bvid} rpid={rpid} verified={verified}")
    return result


def comment_delivery_note(result: dict[str, Any]) -> str:
    if not result.get("accepted"):
        return ""
    if result.get("verified"):
        return "\u89c6\u9891\u4e0b\u9762\u4e5f@\u4f60\u4e86\uff0c\u8bc4\u8bba\u56de\u8bfb\u53ef\u89c1\u3002"
    return "\u89c6\u9891\u4e0b\u9762\u4e5f@\u4f60\u4e86\uff0c\u63a5\u53e3\u5df2\u63a5\u53d7\uff0c\u662f\u5426\u516c\u5f00\u53ef\u89c1\u8fd8\u6ca1\u56de\u8bfb\u5230\u3002"


def admin_collect(url: str, review_level: str = "rough") -> tuple[str, dict[str, Any]]:
    payload = {"mode": "direct", "urls": [url], "max": 1, "import": True, "force": True, "ignore_preference": True, "review_level": review_level}
    task_id = ""
    for _ in range(20):
        response = requests.post(ADMIN_BASE + "/api/actions/collect", json=payload, timeout=30)
        if response.status_code == 202:
            task_id = str((response.json().get("task") or {}).get("id") or "")
            break
        if response.status_code == 409:
            time.sleep(10)
            continue
        raise RuntimeError(f"collect rejected HTTP {response.status_code}: {response.text[:300]}")
    if not task_id:
        raise RuntimeError("collect task did not start")
    deadline = time.time() + int(dm_config().get("video_timeout_seconds", 1800))
    while time.time() < deadline:
        response = requests.get(ADMIN_BASE + f"/api/tasks/{task_id}", timeout=30)
        if response.ok:
            task = response.json()
            if task.get("status") in {"completed", "failed"}:
                return task_id, task
        time.sleep(4)
    raise TimeoutError("video task timed out")


def process_video(item: dict[str, Any], store: Store) -> None:
    bvid = item["bvid"]
    url = item.get("url") or f"https://www.bilibili.com/video/{bvid}/"
    msg_key = item["msg_key"]
    review_level = str(item.get("review_level") or "rough")
    store.upsert_task(bvid, url, msg_key, "running", source="bilibili_dm", delivery_target="bili_dm", source_message_key=msg_key)
    try:
        task_id, task = admin_collect(url, review_level=review_level)
        task_log = str(task.get("log") or "")
        imported_match = re.search(r"\u5bfc\u5165\s*([0-9]+)\s*\u4e2a", task_log)
        imported = int(imported_match.group(1)) if imported_match else 0
        title_match = re.search(r"\u6807\u9898\uff1a(.+)", task_log)
        summary_path = ROOT / "data" / "summaries" / f"{bvid}.md"
        summary = summary_path.read_text(encoding="utf-8-sig") if summary_path.exists() else ""
        title = title_match.group(1).strip() if title_match else bvid
        if not title or title == bvid:
            title_match = re.search(r"\u6807\u9898\uff1a(.+)", summary)
            if title_match:
                title = title_match.group(1).strip()
        if task.get("status") == "completed" and imported > 0:
            one_line = one_line_summary(summary, title)
            low_confidence = "\u6458\u8981\u6a21\u578b\u672a\u8fd4\u56de\u6b63\u6587" in summary or "\u5df2\u83b7\u53d6\u8bc1\u636e\u6574\u7406" in summary
            if low_confidence:
                result_text = f"\u8fd9\u6761\u6211\u6ca1\u80fd\u53ef\u9760\u6574\u7406\u51fa\u7ed3\u8bba\uff0c\u5148\u628a\u80fd\u786e\u8ba4\u7684\u5185\u5bb9\u79c1\u4fe1\u7ed9\u4f60\uff1a{one_line}\n{url}"
            else:
                action = "\u6309\u4f60\u7684\u53cd\u9988\u91cd\u65b0\u7ec6\u770b\u4e86" if review_level == "detailed" else "\u770b\u4e86"
                result_text = f"{action}\u300a{title}\u300b\u3002{one_line}\n{url}"
            store.upsert_task(bvid, url, msg_key, "completed", summary=summary, title=title, one_line=one_line, memory_imported=1, admin_task_id=task_id)
            note = comment_delivery_note(deliver_comment_once(store, bvid, summary, title))
            if note:
                result_text += "\n" + note
        else:
            result_text = "\u8fd9\u4e2a\u89c6\u9891\u6211\u6ca1\u5904\u7406\u6210\u529f\uff0c\u6ca1\u6709\u751f\u6210\u53ef\u8bb0\u4f4f\u7684\u6458\u8981\u3002"
            store.upsert_task(bvid, url, msg_key, "failed", summary=summary, title=title, memory_imported=0, admin_task_id=task_id, error=(task_log[-1200:] or "no summary"))
        cookies = cookies_from_state()
        delivery = send_dm_text(cookies, owner_uid(), result_text)
        store.upsert_task(bvid, url, msg_key, "completed" if imported > 0 else "failed", delivered=1 if delivery.get("ok") else 0)
        store.mark_message(msg_key, "video_replied" if delivery.get("ok") else "video_reply_failed", str(delivery.get("error") or ""))
    except Exception as error:
        store.upsert_task(bvid, url, msg_key, "failed", category="skipped", error=str(error)[:1500])
        try:
            send_dm_text(cookies_from_state(), owner_uid(), "\u8fd9\u4e2a\u89c6\u9891\u6211\u6ca1\u5904\u7406\u6210\u529f\uff0c\u540e\u9762\u53ef\u4ee5\u518d\u8bd5\u4e00\u6b21\u3002")
        except Exception:
            pass
        store.mark_message(msg_key, "failed", str(error))
        log(f"video task failed bvid={bvid}: {error}")

def video_worker(store: Store) -> None:
    while not stop_event.is_set():
        try:
            item = work_queue.get(timeout=2)
        except queue.Empty:
            continue
        try:
            process_video(item, store)
        finally:
            work_queue.task_done()


def write_status(**fields: Any) -> None:
    payload = {
        "pid": os.getpid(),
        "updated_at": time.time(),
        **fields,
    }
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def process_admin_restart_request() -> None:
    request_path = ROOT / "data" / "restart_admin_request.json"
    if not request_path.exists():
        return
    try:
        request_path.unlink()
        netstat = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        admin_pid = 0
        for line in (netstat.stdout or "").splitlines():
            if ":8765" in line and "LISTENING" in line.upper():
                match = re.search(r"\s(\d+)\s*$", line)
                if match:
                    admin_pid = int(match.group(1))
                    break
        if admin_pid > 0:
            os.kill(admin_pid, signal.SIGTERM)
            time.sleep(2)
        node_exe = Path(shutil.which("node") or shutil.which("node.exe") or "node")
        server_script = ROOT / "admin" / "server.mjs"
        if not node_exe.exists() or not server_script.exists():
            raise RuntimeError("admin restart prerequisites missing")
        env = dict(os.environ)
        env.update({"ADMIN_NO_OPEN": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [str(node_exe), str(server_script)],
            cwd=str(ROOT / "admin"),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
        log(f"admin restart requested; old_pid={admin_pid}")
    except Exception as error:
        log(f"admin restart request failed: {error}")


def process_comment_verify_request(store: Store) -> None:
    request_path = ROOT / "data" / "comment_verify_request.json"
    if not request_path.exists():
        return
    result_path = ROOT / "data" / "comment_verify_result.json"
    try:
        request = load_json(request_path, {})
        bvid = str(request.get("bvid") or "").strip()
        rpid = int(request.get("rpid") or 0)
        if not bvid or rpid <= 0:
            raise ValueError("bvid and positive rpid are required")
        from bili_comment_delivery import BilibiliCommentClient
        result = BilibiliCommentClient().verify_comment_visible(bvid, rpid)
        reply = result.get("reply") or {}
        safe = {key: result.get(key) for key in ("verified", "found", "visible", "status", "bvid", "aid", "rpid", "http_status", "code", "message", "page", "page_size")}
        safe.update({
            "reply_content": reply.get("content"),
            "mentioned_users": reply.get("mentioned_users") or [],
            "at_name_to_mid": reply.get("at_name_to_mid"),
            "member_mid": reply.get("member_mid"),
            "member_name": reply.get("member_name"),
        })
        result_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        task = store.get_task(bvid)
        if task:
            store.upsert_task(
                bvid,
                str(task.get("url") or ""),
                str(task.get("msg_key") or ""),
                str(task.get("status") or "completed"),
                comment_status=str(result.get("status") or ""),
                comment_rpid=rpid,
                comment_verified=1 if result.get("verified") else 0,
                comment_message=str(result.get("message") or "")[:800],
                comment_checked_at=time.time(),
            )
        log(f"comment verify bvid={bvid} rpid={rpid} status={result.get('status')} verified={bool(result.get('verified'))}")
    except Exception as error:
        result_path.write_text(json.dumps({"verified": False, "status": "exception", "error": str(error)}, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"comment verify failed: {error}")
    finally:
        try:
            request_path.unlink()
        except Exception:
            pass


def process_subtitle_check_request() -> None:
    request_path = ROOT / "data" / "subtitle_check_request.json"
    if not request_path.exists():
        return
    result_path = ROOT / "data" / "subtitle_check_result.json"
    try:
        request = load_json(request_path, {})
        bvid = str(request.get("bvid") or "").strip()
        if not bvid:
            raise ValueError("bvid is required")
        cookies = cookies_from_state()
        view_response = requests.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            headers=headers(cookies, "https://www.bilibili.com/"),
            timeout=30,
        )
        view_payload = view_response.json()
        if not view_response.ok or view_payload.get("code") != 0:
            raise RuntimeError(f"view lookup failed: HTTP {view_response.status_code} code={view_payload.get('code')} message={view_payload.get('message')}")
        view_data = view_payload.get("data") or {}
        aid = int(view_data.get("aid") or 0)
        pages = view_data.get("pages") or []
        requested_cid = int(request.get("cid") or 0)
        cid = requested_cid or int((pages[0] if pages else {}).get("cid") or view_data.get("cid") or 0)
        player_response = requests.get(
            "https://api.bilibili.com/x/player/v2",
            params={"bvid": bvid, "aid": aid, "cid": cid},
            headers=headers(cookies, "https://www.bilibili.com/"),
            timeout=30,
        )
        player_payload = player_response.json()
        if not player_response.ok or player_payload.get("code") != 0:
            raise RuntimeError(f"player lookup failed: HTTP {player_response.status_code} code={player_payload.get('code')} message={player_payload.get('message')}")
        subtitle_root = ((player_payload.get("data") or {}).get("subtitle") or {})
        player_method = "player/v2"
        if not (subtitle_root.get("subtitles") or subtitle_root.get("ai_subtitles") or subtitle_root.get("ai_subtitle")):
            try:
                nav_response = requests.get("https://api.bilibili.com/x/web-interface/nav", headers=headers(cookies, "https://www.bilibili.com/"), timeout=30)
                nav_data = (nav_response.json().get("data") or {})
                img_url = str((nav_data.get("wbi_img") or {}).get("img_url") or "")
                sub_url = str((nav_data.get("wbi_img") or {}).get("sub_url") or "")
                img_key = Path(img_url).stem
                sub_key = Path(sub_url).stem
                tab = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]
                source = (img_key + sub_key)
                mixin_key = ''.join(source[index] for index in tab)[:32]
                params = {"bvid": bvid, "aid": str(aid), "cid": str(cid), "wts": str(int(time.time()))}
                clean = {key: ''.join(ch for ch in str(value) if ch not in "!'()*") for key, value in params.items()}
                query = urlencode(sorted(clean.items()))
                signed = dict(clean)
                signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
                wbi_response = requests.get(
                    "https://api.bilibili.com/x/player/wbi/v2",
                    params=signed,
                    headers=headers(cookies, "https://www.bilibili.com/"),
                    timeout=30,
                )
                wbi_payload = wbi_response.json()
                if wbi_response.ok and wbi_payload.get("code") == 0:
                    subtitle_root = ((wbi_payload.get("data") or {}).get("subtitle") or {})
                    player_method = "player/wbi/v2"
            except Exception as error:
                log(f"subtitle WBI fallback failed: {error}")
        candidates = []
        for key in ("subtitles", "ai_subtitles", "ai_subtitle"):
            value = subtitle_root.get(key) or []
            if isinstance(value, dict):
                value = [value]
            for item in value:
                if not isinstance(item, dict):
                    continue
                candidates.append({
                    "container": key,
                    "lan": item.get("lan"),
                    "lan_doc": item.get("lan_doc"),
                    "subtitle_url": item.get("subtitle_url") or item.get("url"),
                    "ai_type": item.get("ai_type"),
                    "type": item.get("type"),
                })
        result = {
            "bvid": bvid,
            "aid": aid,
            "cid": cid,
            "subtitle_root_keys": sorted(subtitle_root.keys()),
            "player_method": player_method,
            "candidate_count": len(candidates),
            "candidates": candidates,
        }
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"subtitle check bvid={bvid} candidates={len(candidates)} keys={','.join(result['subtitle_root_keys'])}")
    except Exception as error:
        result_path.write_text(json.dumps({"error": str(error)}, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"subtitle check failed: {error}")
    finally:
        try:
            request_path.unlink()
        except Exception:
            pass


def process_comment_draft_request() -> None:
    request_path = ROOT / "data" / "comment_draft_request.json"
    if not request_path.exists():
        return
    result_path = ROOT / "data" / "comment_draft_result.json"
    try:
        request = load_json(request_path, {})
        bvid = str(request.get("bvid") or "").strip()
        summary_path = ROOT / "data" / "summaries" / f"{bvid}.md"
        summary = summary_path.read_text(encoding="utf-8-sig") if summary_path.exists() else ""
        if not bvid or not summary:
            raise ValueError("bvid and saved summary are required")
        title_match = re.search(r"\u6807\u9898\uff1a(.+)", summary)
        title = title_match.group(1).strip() if title_match else bvid
        draft = humanize_public_comment(summary, title)
        result_path.write_text(json.dumps({
            "bvid": bvid,
            "confident": summary_is_comment_confident(summary),
            "draft": draft,
            "would_comment": bool(summary_is_comment_confident(summary) and draft),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"comment draft check bvid={bvid} length={len(draft)}")
    except Exception as error:
        result_path.write_text(json.dumps({"error": str(error)}, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"comment draft check failed: {error}")
    finally:
        try:
            request_path.unlink()
        except Exception:
            pass


def process_popular_discovery_request() -> None:
    request_path = ROOT / "data" / "popular_discovery_request.json"
    if not request_path.exists():
        return
    result_path = ROOT / "data" / "popular_discovery_result.json"
    try:
        cookies = cookies_from_state()
        response = requests.get(
            "https://api.bilibili.com/x/web-interface/popular",
            params={"ps": 30, "pn": 1},
            headers=headers(cookies, "https://www.bilibili.com/"),
            timeout=30,
        )
        payload = response.json()
        if not response.ok or payload.get("code") != 0:
            raise RuntimeError(f"popular lookup failed: HTTP {response.status_code} code={payload.get('code')} message={payload.get('message')}")
        rows = []
        for item in ((payload.get("data") or {}).get("list") or []):
            bvid = str(item.get("bvid") or "")
            if not bvid:
                continue
            rows.append({
                "bvid": bvid,
                "title": item.get("title"),
                "owner": (item.get("owner") or {}).get("name"),
                "duration": item.get("duration"),
                "desc": str(item.get("desc") or "")[:300],
            })
        result_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"popular discovery rows={len(rows)}")
    except Exception as error:
        result_path.write_text(json.dumps({"error": str(error)}, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"popular discovery failed: {error}")
    finally:
        try:
            request_path.unlink()
        except Exception:
            pass


def poll_once(store: Store) -> dict[str, Any]:
    process_admin_restart_request()
    process_comment_verify_request(store)
    process_subtitle_check_request()
    process_comment_draft_request()
    process_popular_discovery_request()
    cookies = cookies_from_state()
    try:
        login = check_login(cookies)
    except Exception as error:
        login = {"ok": False, "error": str(error)}
    if not login.get("ok"):
        result = {"ok": False, "error": "Bilibili login unavailable", "login": login, "new_messages": 0}
        write_status(running=True, last_poll=time.time(), last_error=result["error"], login=login, counts=store.counts())
        return result
    owner = owner_uid()
    if owner <= 0:
        raise RuntimeError("owner Bilibili UID missing")
    sessions = list_sessions(cookies)
    owner_sessions = [item for item in sessions if int(item.get("uid") or 0) == owner]
    if not owner_sessions:
        result = {"ok": True, "login": login, "new_messages": 0, "owner_session": False, "ignored_sessions": len(sessions)}
        write_status(running=True, last_poll=time.time(), last_error="", login=login, owner_uid=owner, counts=store.counts())
        return result
    messages = fetch_messages(cookies, owner, int(dm_config().get("fetch_size", 20)))
    if store.kv_get("dm_initialized") != "1":
        for msg in messages:
            store.add_message(msg)
            store.mark_message(msg["key"], "initialized")
        store.kv_set("dm_initialized", "1")
        result = {"ok": True, "login": login, "owner_session": True, "new_messages": 0, "queued_videos": 0, "initialized": True}
        write_status(running=True, last_poll=time.time(), last_error="", login=login, owner_uid=owner, owner_session=True, counts=store.counts(), last_result=result)
        return result
    new_messages = 0
    queued = 0
    for msg in messages:
        if store.has_message(msg["key"]):
            continue
        store.add_message(msg)
        new_messages += 1
        if msg["from_me"]:
            store.mark_message(msg["key"], "own_message")
            continue
        emotes = emote_codes(msg["text"])
        if emotes:
            for emote in emotes:
                store.record_emote(emote, int(msg["sender_uid"]))
            store.mark_message(msg["key"], "emote_recorded")
            continue
        if not bvid and msg.get("bili_url") and SHORT_BILI_RE.match(str(msg.get("bili_url"))):
            resolved_bvid = resolve_bili_short(str(msg.get("bili_url")))
            if resolved_bvid:
                bvid = resolved_bvid
                msg["bvid"] = resolved_bvid
        if not bvid and msg.get("bili_url"):
            send_dm_text(cookies, owner, "\u8fd9\u4e2aB\u7ad9\u94fe\u63a5\u6211\u6ca1\u89e3\u6790\u6210\u529f\u3002\u8bf7\u53d1\u5e26BV\u53f7\u7684\u5b8c\u6574\u94fe\u63a5\uff0c\u6211\u518d\u5904\u7406\u3002")
            store.mark_message(msg["key"], "link_resolve_failed", "Bilibili URL could not be resolved")
            continue
        test_emotes = requested_test_emotes(msg["text"])
        if test_emotes:
            sent_all = True
            errors = []
            for item in test_emotes:
                sent = send_dm_text(cookies, owner, str(item["code"]))
                sent_all = sent_all and bool(sent.get("ok"))
                if not sent.get("ok"):
                    errors.append(str(sent.get("error") or "send failed"))
                else:
                    log(f"emote sent via Bilibili DM: {item.get('label') or item.get('key')}")
            store.mark_message(msg["key"], "emote_sent" if sent_all else "emote_send_failed", "; ".join(errors))
            continue
        bvid = msg.get("bvid") or ""
        review_level = "detailed" if is_review_request(msg["text"]) else "rough"
        if bvid:
            url = f"https://www.bilibili.com/video/{bvid}/"
            store.kv_set("last_video:bilibili_dm", bvid)
            store.upsert_task(bvid, url, msg["key"], "queued", category="picked")
            action = "\u6536\u5230\uff0c\u6211\u6309\u4f60\u7684\u53cd\u9988\u91cd\u65b0\u7ec6\u770b" if review_level == "detailed" else "\u6536\u5230\uff0c\u6211\u53bb\u770b\u770b"
            ack = send_dm_text(cookies, owner, f"{action} BV\u53f7 {bvid}\u3002")
            store.mark_message(msg["key"], "queued", str(ack.get("error") or ""))
            work_queue.put({"bvid": bvid, "url": url, "msg_key": msg["key"], "review_level": review_level})
            queued += 1
        elif review_level == "detailed":
            last_bvid = store.kv_get("last_video:bilibili_dm")
            match = BVID_RE.search(last_bvid)
            if match:
                bvid = match.group(0)
                url = f"https://www.bilibili.com/video/{bvid}/"
                store.upsert_task(bvid, url, msg["key"], "queued", category="picked")
                ack = send_dm_text(cookies, owner, f"\u6536\u5230\uff0c\u6211\u91cd\u65b0\u7ec6\u770b BV\u53f7 {bvid}\u3002")
                store.mark_message(msg["key"], "queued_detail", str(ack.get("error") or ""))
                work_queue.put({"bvid": bvid, "url": url, "msg_key": msg["key"], "review_level": "detailed"})
                queued += 1
            else:
                ack = send_dm_text(cookies, owner, "\u73b0\u5728\u8fd8\u6ca1\u6709\u53ef\u91cd\u770b\u7684\u89c6\u9891\uff0c\u4f60\u5148\u53d1\u4e00\u4e2a B \u7ad9\u94fe\u63a5\u3002")
                store.mark_message(msg["key"], "review_no_video", str(ack.get("error") or ""))
        else:
            try:
                reply = deterministic_action_reply(msg["text"]) or deepseek_reply(msg["text"])
            except Exception as error:
                reply = "\u6211\u5728\uff0c\u4f46\u521a\u624d\u6ca1\u60f3\u597d\u600e\u4e48\u56de\u3002\u4f60\u518d\u8bf4\u4e00\u904d\uff1f"
                log(f"DeepSeek reply failed: {error}")
            reply_text, emote_item = split_reply_emote(reply)
            if emote_item:
                min_interval = int(dm_config().get("emote_min_interval_seconds", 120))
                last_emote = float(store.kv_get("last_emote_at", "0") or 0)
                if time.time() - last_emote < min_interval:
                    emote_item = None
            text_result = {"ok": True, "error": ""}
            emote_result = {"ok": True, "error": ""}
            if reply_text and emote_item:
                combined = reply_text + " " + str(emote_item["code"])
                text_result = send_dm_text(cookies, owner, combined)
                if text_result.get("ok"):
                    store.kv_set("last_emote_at", str(time.time()))
                    log(f"combined text and emote sent: {emote_item.get('label')}")
            elif reply_text:
                text_result = send_dm_text(cookies, owner, reply_text)
            elif emote_item:
                emote_result = send_dm_text(cookies, owner, str(emote_item["code"]))
                if emote_result.get("ok"):
                    store.kv_set("last_emote_at", str(time.time()))
                    log(f"emote-only message sent: {emote_item.get('label')}")
            ok = bool(text_result.get("ok") and emote_result.get("ok") and (reply_text or emote_item))
            error = "; ".join(part for part in (text_result.get("error"), emote_result.get("error")) if part)
            status = "replied_emote" if ok and emote_item else "replied" if ok else "reply_failed"
            store.mark_message(msg["key"], status, error)
    result = {"ok": True, "login": login, "owner_session": True, "new_messages": new_messages, "queued_videos": queued}
    write_status(running=True, last_poll=time.time(), last_error="", login=login, owner_uid=owner, owner_session=True, counts=store.counts(), last_result=result)
    return result


def acquire_single_instance() -> bool:
    global instance_socket
    port = int(dm_config().get("instance_port", 8768))
    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        candidate.bind(("127.0.0.1", port))
        candidate.listen(1)
    except OSError:
        candidate.close()
        return False
    instance_socket = candidate
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="ascii")
    return True


def run_service() -> None:
    if not acquire_single_instance():
        print("Bili DM service already running")
        return
    store = Store()
    worker = threading.Thread(target=video_worker, args=(store,), daemon=True, name="bili-dm-video-worker")
    worker.start()
    interval = max(20, int(dm_config().get("poll_seconds", 60)))
    log("Bili DM service started")
    while not stop_event.is_set():
        try:
            result = poll_once(store)
            log(f"poll: ok={result.get('ok')} new={result.get('new_messages', 0)} queued={result.get('queued_videos', 0)}")
        except Exception as error:
            log(f"poll failed: {error}\n{traceback.format_exc()}")
            write_status(running=True, last_poll=time.time(), last_error=str(error), counts=store.counts())
        stop_event.wait(interval)
    store.close()


def probe_browser_login() -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
        from bilibili_tool import launch_context
        with sync_playwright() as playwright:
            context = launch_context(playwright, headless=True)
            try:
                response = context.request.get(
                    "https://api.bilibili.com/x/web-interface/nav",
                    headers={"Referer": "https://www.bilibili.com/", "User-Agent": UA},
                    timeout=30000,
                )
                data = response.json()
                payload = data.get("data") or {}
                return {"ok": bool(payload.get("isLogin")), "code": data.get("code"), "uid": int(payload.get("mid") or 0), "uname": str(payload.get("uname") or ""), "message": data.get("message") or ""}
            finally:
                context.close()
    except Exception as error:
        return {"ok": False, "error": str(error)}


def self_check() -> int:
    store = Store()
    cookies = cookies_from_state()
    errors: list[str] = []
    try:
        login = check_login(cookies)
    except Exception as error:
        login = {"ok": False, "error": str(error)}
        errors.append("login: " + str(error))
    media_cfg = dict(config().get("media_understanding") or {})
    browser_login = probe_browser_login()
    report = {
        "credentials": login,
        "browser_credentials": browser_login,
        "owner_uid": owner_uid(),
        "persona_present": bool(persona_text()),
        "db_writable": True,
        "admin_online": False,
        "memory": None,
        "sessions": None,
        "media": {
            "enabled": bool(media_cfg.get("enabled")),
            "audio_transcription": bool(media_cfg.get("audio_transcription")),
            "visual_analysis": bool(media_cfg.get("visual_analysis")),
            "whisper_model": media_cfg.get("whisper_model") or "openai/whisper-small",
            "transcribe_script_present": (ROOT / "transcribe_audio.py").exists(),
        },
        "errors": errors,
    }
    try:
        response = requests.get(ADMIN_BASE + "/api/status", timeout=20)
        report["admin_online"] = response.ok
        if response.ok:
            data = response.json()
            report["memory"] = {
                "runtime_ready": (data.get("maibot") or {}).get("runtimeReady"),
                "degraded": (data.get("maibot") or {}).get("degraded"),
                "active_count": (data.get("maibot") or {}).get("activeCount"),
            }
    except Exception as error:
        report["errors"].append(f"admin: {error}")
    request_path = ROOT / "data" / "send_emote_request.json"
    if request_path.exists() and login.get("ok"):
        try:
            request = load_json(request_path, {})
            key = str(request.get("key") or "")
            item = next((entry for entry in emote_catalog() if entry.get("key") == key), None)
            if not item or not item.get("owned") or not item.get("verified"):
                raise RuntimeError("emote is not owned and verified")
            prefix_text = str(request.get("text") or "").strip()
            message = (prefix_text + " " + str(item["code"])) if prefix_text else str(item["code"])
            sent = send_dm_text(cookies, owner_uid(), message)
            report["test_emote"] = {"ok": bool(sent.get("ok")), "key": key, "label": item.get("label"), "code": item.get("code"), "message": message, "error": sent.get("error"), "msg_key": sent.get("msg_key")}
            (ROOT / "data" / "send_emote_result.json").write_text(json.dumps(report["test_emote"], ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as error:
            report["test_emote"] = {"ok": False, "error": str(error)}
        finally:
            try:
                request_path.unlink()
            except Exception:
                pass
    if login.get("ok"):
        try:
            owner_response = requests.get(
                "https://api.bilibili.com/x/web-interface/card",
                params={"mid": owner_uid()},
                headers=headers(cookies, "https://www.bilibili.com/"),
                timeout=30,
            )
            owner_data = owner_response.json()
            card = ((owner_data.get("data") or {}).get("card") or {})
            report["owner_profile"] = {"code": owner_data.get("code"), "name": card.get("name"), "mid": card.get("mid"), "face": card.get("face")}
        except Exception as error:
            report["owner_profile"] = {"error": str(error)}
        try:
            report["sessions"] = len(list_sessions(cookies))
            recent = fetch_messages(cookies, owner_uid(), 50)
            seen_emotes = set()
            recent_emotes = []
            for item in recent:
                for code in emote_codes(item.get("text") or ""):
                    if code not in seen_emotes:
                        seen_emotes.add(code)
                        recent_emotes.append({"code": code, "timestamp": item.get("timestamp"), "from_me": bool(item.get("from_me"))})
            report["recent_emotes"] = recent_emotes
            report["recent_messages"] = [
                {
                    "key": item.get("key"),
                    "from_me": item.get("from_me"),
                    "kind": item.get("kind"),
                    "text": item.get("text"),
                    "raw_content": item.get("raw_content"),
                }
                for item in recent[-12:]
            ]
        except Exception as error:
            report["errors"].append(f"messages: {error}")
    store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bilibili private-message bridge")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--tasks-json", action="store_true")
    parser.add_argument("--emotes-json", action="store_true")
    parser.add_argument("--filter", default="all")
    args = parser.parse_args()
    if args.check:
        return self_check()
    if args.tasks_json:
        store = Store()
        print(json.dumps(store.tasks(args.filter), ensure_ascii=False, indent=2))
        store.close()
        return 0
    if args.emotes_json:
        store = Store()
        print(json.dumps(store.emote_catalog(), ensure_ascii=False, indent=2))
        store.close()
        return 0
    if args.once:
        store = Store()
        result = poll_once(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        store.close()
        return 0 if result.get("ok") else 1
    run_service()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
