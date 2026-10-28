from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests
from websockets.sync.client import connect as ws_connect

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
SEEN_FILE = ROOT / "data" / "qq_link_seen.json"
LOG_FILE = ROOT / "logs" / "qq_link_watcher.log"
PID_FILE = ROOT / "data" / "qq_watcher.pid"
QQ_ALLOWLIST_FILE = ROOT / "data" / "qq_user_allowlist.json"
REVIEWED_FILE = ROOT / "data" / "qq_reviewed.json"
INSTANCE_PORT = 8766
ADMIN_BASE = "http://127.0.0.1:8765"
QUEUE_RECOVERY_DEFAULT_SECONDS = 2100
LOG_TAIL_GROUP = str(os.environ.get("BILIMEMORY_LOG_TAIL_GROUP") or "").strip()
from runtime_paths import MAIBOT_ROOT, MAIBOT_USER_DATA
BOT_CONFIG = MAIBOT_ROOT / "plugins" / "snowluma-adapter" / "config.toml"
NAPCAT_LOG = MAIBOT_USER_DATA / "logs" / "napcat.log"
URL_RE = re.compile(r"https?://[^\s<>'\"\u3002\uff0c\uff1b\uff09\uff3d]+", re.IGNORECASE)
BILI_SHARE_TITLE_RE = re.compile(r"\u3010[^\u3011]{1,300}?[-\u2014\u2013\uff0d]\s*\u54d4\u54a9\u54d4\u54a9\u3011", re.IGNORECASE)
AT_MENTION_RE = re.compile(r"(?:\[CQ:at,[^\]]+\]|@[^\s@]+)", re.IGNORECASE)
BV_RE = re.compile(r"BV[0-9A-Za-z]{10,}")
SHORT_RE = re.compile(r"https?://(?:b23\.tv|bili2233\.cn)/[A-Za-z0-9]+", re.IGNORECASE)
BILI_RE = re.compile(r"https?://(?:www\.)?bilibili\.com/video/", re.IGNORECASE)

lock = threading.Lock()
work_queue: queue.Queue[dict[str, Any]] = queue.Queue()
reject_cooldown: dict[str, float] = {}
ws_connected = False
task_store = None


def load_qq_allowlist() -> dict[str, Any]:
    try:
        data = json.loads(QQ_ALLOWLIST_FILE.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {"enabled": False, "users": []}
    except Exception:
        return {"enabled": False, "users": []}


def qq_user_permission(user_id: str, permission: str) -> bool:
    data = load_qq_allowlist()
    if not bool(data.get("enabled", False)):
        return False
    uid = str(user_id or "")
    wanted = str(permission or "")
    for user in data.get("users") or []:
        if not isinstance(user, dict) or str(user.get("qq") or "") != uid:
            continue
        return wanted in {str(item) for item in (user.get("permissions") or [])}
    return False


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    except Exception:
        pass


def load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8-sig")))
    except Exception:
        return set()


seen = load_seen()
inflight: set[str] = set()


def load_reviewed() -> set[str]:
    try:
        return set(json.loads(REVIEWED_FILE.read_text(encoding="utf-8-sig")))
    except Exception:
        return set()


def mark_reviewed(bvid: str) -> None:
    if not bvid:
        return
    with lock:
        reviewed.add(bvid)
        REVIEWED_FILE.parent.mkdir(parents=True, exist_ok=True)
        REVIEWED_FILE.write_text(json.dumps(sorted(reviewed), ensure_ascii=False, indent=2), encoding="utf-8")


def review_already_used(bvid: str) -> bool:
    return bool(bvid) and bvid in reviewed


reviewed = load_reviewed()


def mark_seen(bvid: str) -> None:
    with lock:
        seen.add(bvid)
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def bot_token() -> str:
    text = BOT_CONFIG.read_text(encoding="utf-8-sig")
    match = re.search(r'(?m)^token\s*=\s*"([^"]*)"', text)
    return match.group(1) if match else ""


def allowed_groups(config: dict[str, Any]) -> set[str]:
    return {str(item) for item in ((config.get("qq_link_policy") or {}).get("allowed_groups") or [])}


def segment_text(event: dict[str, Any]) -> str:
    message = event.get("message")
    if isinstance(message, list):
        return "".join(str(seg.get("data", {}).get("text", "")) for seg in message if seg.get("type") == "text")
    return str(message or event.get("raw_message") or "")


def has_share_card(event: dict[str, Any]) -> bool:
    message = event.get("message")
    if isinstance(message, list) and any(seg.get("type") in {"json", "xml", "mface", "marketface"} for seg in message):
        return True
    raw = json.dumps(event, ensure_ascii=False).casefold()
    return any(token in raw for token in ("com.tencent.miniapp", "com.tencent.structmsg", "miniapp", "<msg ", "小程序"))


def card_is_bilibili(event: dict[str, Any]) -> bool:
    raw = json.dumps(event, ensure_ascii=False).casefold()
    return any(token in raw for token in ("bilibili", "哔哩哔哩", "b23.tv", "bili2233"))


def video_page(url: str) -> int:
    try:
        return max(1, int(parse_qs(urlparse(url).query).get("p", ["1"])[0]))
    except (TypeError, ValueError):
        return 1


def resolve_url(url: str) -> str | None:
    if BV_RE.search(url):
        return url
    if not SHORT_RE.match(url):
        return None
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=True,
            timeout=20,
            stream=True,
        )
        resolved = response.url
        response.close()
        return resolved if BV_RE.search(resolved) else None
    except Exception as error:
        log(f"Short-link resolve failed: {url} {error}")
        return None


SEARCH_COMMAND_RE = re.compile(r"^/?\s*(?:\u770b\u7c7b\u578b|\u770b\u7c7b\u578b\u89c6\u9891|\u627e\u7c7b\u578b|\u641c\u7c7b\u578b|\u641c\u89c6\u9891|\u627e\u89c6\u9891|\u770b\u89c6\u9891|\u67e5\u89c6\u9891|\u5237\u4e00\u4e2a|\u5237\u51e0\u4e2a|\u5237\u4e00\u4e0b|\u5237\u70b9|\u627e\u4e00\u4e2a|\u641c\u4e00\u4e2a|\u770b\u4e2a|\u5237\u4e2a|\u627e\u4e2a)\s*[\uff1a:]?\s*(.+)$", re.IGNORECASE)
SEARCH_COUNT_RE = re.compile(r"^(.*?)\s*(?:\u6570\u91cf\s*[=:\uff1a]?\s*)?(\d+)\s*(?:\u4e2a|\u6761|\u90e8)?\s*$")
SEARCH_HELP_RE = re.compile(r"^(?:/)?\s*(?:\u641c\u89c6\u9891\u5e2e\u52a9|\u770b\u89c6\u9891\u5e2e\u52a9|\u641c\u7d22\u5e2e\u52a9)$")


def is_explicit_search_command(text: str) -> bool:
    clean = re.sub(r"^(@[^\s]+\s+)+", "", str(text or "").strip())
    return clean.startswith("/")


def parse_configured_search_command(text: str, policy: dict[str, Any], default_max: int, max_results: int) -> dict[str, Any] | None:
    if not bool(policy.get("allow_natural_search", False)) and not is_explicit_search_command(text):
        return None
    return parse_search_command(text, default_max=default_max, max_results=max_results)


def is_link_only_message(text: str) -> bool:
    clean = str(text or "").strip()
    urls = URL_RE.findall(clean)
    if not urls:
        return False
    for url in urls:
        clean = clean.replace(url, " ")
    # QQ Bilibili shares prepend a title and may append an @mention.
    # Remove only those structural parts; ordinary chat with a link stays rejected.
    clean = BILI_SHARE_TITLE_RE.sub(" ", clean)
    clean = AT_MENTION_RE.sub(" ", clean)
    clean = re.sub(r"[\W_]+", "", clean, flags=re.UNICODE)
    if not clean:
        return True
    return bool(re.fullmatch(r"(?:\u770b\u8fd9\u4e2a|\u770b\u4e0b\u8fd9\u4e2a|\u770b\u4e00\u4e0b\u8fd9\u4e2a|\u770b\u770b\u8fd9\u4e2a|\u5e2e\u6211\u770b\u770b|\u770b\u4e0b|\u770b\u770b|\u5904\u7406\u8fd9\u4e2a|\u770b\u89c6\u9891)", clean, re.I))


def parse_search_command(text: str, default_max: int = 2, max_results: int = 5) -> dict[str, Any] | None:
    clean = re.sub(r"^(@[^\s]+\s+)+", "", str(text or "").strip())
    match = SEARCH_COMMAND_RE.match(clean)
    if not match:
        return None
    query = match.group(1).strip().strip("\"\u201c\u201d'")
    if not query or query in {"\u5417", "\u5462", "\u554a", "\u4ec0\u4e48"}:
        return None
    command_prefix = re.match(r"^/?\s*(刷一个|找一个|搜一个)", clean)
    result_max = 1 if command_prefix else max(1, min(max_results, int(default_max)))
    count_match = SEARCH_COUNT_RE.match(query)
    if count_match:
        candidate_query = count_match.group(1).strip().strip("\"\u201c\u201d'")
        if candidate_query:
            query = candidate_query
            result_max = max(1, min(max_results, int(count_match.group(2))))
    if len(query) < 2:
        return None
    return {"query": query[:80], "max_results": result_max}




















def enqueue_search(query: str, max_results: int, group_id: str, user_id: str, source: str, message_key: str = "", private: bool = False) -> None:
    work_queue.put({"query": query, "max_results": max_results, "group_id": group_id, "user_id": user_id, "source": source, "message_key": message_key, "private": private})


def enqueue(url: str, group_id: str, user_id: str, source: str, message_key: str = "", review_level: str = "rough", private: bool = False) -> None:
    work_queue.put({"url": url, "group_id": group_id, "user_id": user_id, "source": source, "message_key": message_key, "review_level": review_level, "private": private})


def get_task_store():
    global task_store
    if task_store is None:
        from bili_dm_service import Store
        task_store = Store()
    return task_store


def send_group_message(group_id: str, message: str) -> bool:
    token = bot_token()
    uri = f"ws://127.0.0.1:7998?access_token={quote(token, safe='')}"
    echo = f"qq-link-result-{uuid.uuid4().hex}"
    payload = {
        "action": "send_group_msg",
        "params": {"group_id": group_id, "message": message},
        "echo": echo,
    }
    try:
        with ws_connect(uri, open_timeout=8, close_timeout=3, ping_interval=None) as ws:
            ws.send(json.dumps(payload, ensure_ascii=False))
            deadline = time.time() + 12
            while time.time() < deadline:
                raw = ws.recv(timeout=max(1, deadline - time.time()))
                response = json.loads(raw)
                if str(response.get("echo") or "") != echo:
                    continue
                if response.get("status") == "failed" or response.get("retcode", 0) != 0:
                    log(f"OneBot result message failed: {str(response)[:500]}")
                    return False
                log(f"Sent result message to group {group_id}: {message}")
                return True
    except Exception as error:
        log(f"Unable to send result message to group {group_id}: {error}")
    return False


def send_private_message(user_id: str, message: str) -> bool:
    token = bot_token()
    uri = f"ws://127.0.0.1:7998?access_token={quote(token, safe='')}"
    echo = f"qq-private-result-{uuid.uuid4().hex}"
    payload = {"action": "send_private_msg", "params": {"user_id": user_id, "message": message}, "echo": echo}
    try:
        with ws_connect(uri, open_timeout=8, close_timeout=3, ping_interval=None) as ws:
            ws.send(json.dumps(payload, ensure_ascii=False))
            deadline = time.time() + 12
            while time.time() < deadline:
                raw = ws.recv(timeout=max(1, deadline - time.time()))
                response = json.loads(raw)
                if str(response.get("echo") or "") != echo:
                    continue
                if response.get("status") == "failed" or response.get("retcode", 0) != 0:
                    log(f"OneBot private message failed: {str(response)[:500]}")
                    return False
                log(f"Sent private message to user {user_id}: {message}")
                return True
    except Exception as error:
        log(f"Unable to send private message to user {user_id}: {error}")
    return False


def send_item_message(item: dict[str, Any], message: str) -> bool:
    if item.get("private"):
        return send_private_message(str(item.get("user_id") or ""), message)
    return send_group_message(str(item.get("group_id") or ""), message)


def item_delivery_target(item: dict[str, Any]) -> str:
    return str(item.get("user_id") or "") if item.get("private") else str(item.get("group_id") or "")


def result_message(task: dict[str, Any], status: str, review_level: str = "rough") -> str:
    task_log = str(task.get("log") or "")
    if status != "completed":
        return "这个我没处理成功：B站这次没取到完整内容。"

    imported = re.search(r"导入\s*([0-9]+)\s*个", task_log)
    if imported and int(imported.group(1)) > 0:
        if review_level == "detailed":
            return "按你的反馈重新细看完了，摘要又更新进记忆。"
        return "处理完了，摘要已经记住。"

    if any(token in task_log for token in (
        "Skipping low-value candidate",
        "Stopping after opening excerpt",
        "Skipping unrelated video",
        "Skipping video: no subtitle",
    )):
        return "这个我没继续看：按现在的兴趣筛选后觉得价值不高。"

    if "没有发现新的视频" in task_log:
        return "这个之前处理过，没重新生成摘要。"

    if re.search(r"完成：处理\s*[0-9]+\s*个，导入\s*0\s*个", task_log):
        return "这个我没处理成功：没生成可记住的摘要。"

    return "这个我没处理成功：这次没有产出视频摘要。"


def visual_evidence_from_summary(summary: str, max_chars: int = 320) -> str:
    candidates = []
    for line in str(summary or "").splitlines():
        clean = re.sub(r"^[\s>*#\-]+", "", line).strip()
        if not clean or "\u753b\u9762" not in clean:
            continue
        if re.search(r"(?:\u672a\u63d0\u4f9b|\u672a\u83b7\u53d6|\u65e0\u753b\u9762|\u672a\u505a\u89c6\u89c9|\u65e0\u6cd5\u786e\u8ba4|\u4e0d\u786e\u5b9a|\u89c6\u89c9\u5206\u6790\u5931\u8d25|\u672a\u8fd4\u56de\u753b\u9762)", clean):
            continue
        if any(marker in clean for marker in ("\u753b\u9762\u4f9d\u636e", "\u753b\u9762\u63cf\u8ff0", "\u753b\u9762\u786e\u8ba4", "\u753b\u9762\u4e0e\u6545\u4e8b", "\u753b\u9762\u6545\u4e8b", "\u955c\u5934", "\u89c6\u89c9")):
            candidates.append(clean)
    if not candidates:
        return ""
    best = max(candidates, key=len)
    best = re.sub(r"^\s*(?:\u753b\u9762\u4f9d\u636e|\u753b\u9762\u63cf\u8ff0|\u753b\u9762\u786e\u8ba4|\u753b\u9762\u4e0e\u6545\u4e8b|\u753b\u9762\u6545\u4e8b|\u955c\u5934|\u89c6\u89c9)[^\uff1a:]*[\uff1a:]\s*(?:\*\*|__)?\s*", "", best)
    return best[:max_chars].strip()


def summary_delivery_text(summary: str, one_line: str, bvid: str, requester: str = "") -> str:
    title_match = re.search(r"\u6807\u9898\uff1a(.+)", summary)
    title = title_match.group(1).strip() if title_match else bvid
    source_note = f"\uff08QQ {requester} \u63d0\u4f9b\u94fe\u63a5\uff09" if requester else ""
    parts = [f"\u3010\u89c6\u9891\u72b6\u6001\uff1a\u5df2\u770b\u5b8c\u5e76\u5199\u5165\u8bb0\u5fc6\u3011\u300a{title}\u300b {bvid}{source_note}"]
    if one_line:
        parts.append(one_line.strip()[:420])
    visual = visual_evidence_from_summary(summary)
    if visual:
        parts.append("\u753b\u9762\u786e\u8ba4\uff1a" + visual)
    else:
        parts.append("\u8fd9\u6b21\u6ca1\u6709\u53d6\u5230\u53ef\u7528\u7684\u753b\u9762\u8bc1\u636e\u3002")
    return "\n".join(parts)[:1100]






VIDEO_STATUS_QUERY_RE = re.compile(r"^(?:/)?\s*(?:\u72b6\u6001|\u89c6\u9891\u72b6\u6001|\u5f53\u524d\u72b6\u6001|\u81ea\u5df1\u7684\u72b6\u6001|\u4f60\u5728\u5e72\u5417|\u4f60\u73b0\u5728\u5728\u5e72\u5417|\u4f60\u73b0\u5728\u5728\u5e72\u4ec0\u4e48|\u4f60\u5728\u5fd9\u5417|\u5fd9\u4e0d\u5fd9|\u662f\u4e0d\u662f\u5728\u5904\u7406|\u5904\u7406\u5230\u54ea\u4e86|\u521a\u624d\u90a3\u4e2a\u89c6\u9891|\u524d\u9762\u90a3\u4e2a\u89c6\u9891|\u770b\u4e86\u5417|\u770b\u5b8c\u4e86\u5417|\u770b\u5b8c\u6ca1|\u770b\u6ca1\u770b|\u5904\u7406\u5b8c\u4e86\u5417|\u5904\u7406\u5b8c\u6ca1|\u6709\u6ca1\u6709\u770b\u5b8c)", re.I)


def is_video_status_query(text: str) -> bool:
    clean = re.sub(r"^(@[^\s]+\s+)+", "", str(text or "").strip())
    return bool(VIDEO_STATUS_QUERY_RE.match(clean))


def task_status_label(task: dict[str, Any]) -> str:
    status = str(task.get("status") or "")
    if status == "completed" and int(task.get("memory_imported") or 0) > 0:
        return "\u5df2\u770b\u5b8c\u5e76\u5199\u5165\u8bb0\u5fc6"
    if status == "completed":
        return "\u5904\u7406\u5b8c\u6210\uff0c\u4f46\u8bb0\u5fc6\u672a\u786e\u8ba4"
    if status == "interrupted":
        return "\u4e0a\u6b21\u5904\u7406\u4e2d\u65ad"
    if status in {"queued", "running"}:
        return "\u5904\u7406\u4e2d"
    if status == "failed":
        return "\u5904\u7406\u5931\u8d25"
    return "\u6ca1\u5904\u7406\u6210\u529f"


def recent_video_status_message(group_id: str, limit: int = 3) -> str:
    store = get_task_store()
    rows = []
    for task in store.tasks(limit=40):
        if str(task.get("delivery_target") or "") != str(group_id):
            continue
        if not task.get("bvid") and not task.get("title"):
            continue
        rows.append(task)
        if len(rows) >= limit:
            break
    if not rows:
        return "\u6211\u73b0\u5728\u5728\u7ebf\uff0c\u6ca1\u6709\u6b63\u5728\u5904\u7406\u7684\u89c6\u9891\uff0c\u4e5f\u8fd8\u6ca1\u6709\u53ef\u67e5\u7684\u89c6\u9891\u4efb\u52a1\u3002"
    active = next((task for task in rows if str(task.get("status") or "") in {"queued", "running"}), None)
    completed = next((task for task in rows if str(task.get("status") or "") == "completed" and int(task.get("memory_imported") or 0) > 0), None)
    lines = ["\u6211\u73b0\u5728\u5728\u7ebf\u3002"]
    if active:
        active_title = str(active.get("title") or active.get("bvid") or "\u672a\u77e5\u89c6\u9891")
        lines.append(f"\u6b63\u5728\u5904\u7406\uff1a\u300a{active_title}\u300b")
    else:
        lines.append("\u76ee\u524d\u6ca1\u6709\u6b63\u5728\u5904\u7406\u7684\u89c6\u9891\u3002")
    if completed:
        completed_title = str(completed.get("title") or completed.get("bvid") or "\u672a\u77e5\u89c6\u9891")
        lines.append(f"\u6700\u8fd1\u5df2\u786e\u8ba4\u770b\u5b8c\u5e76\u8bb0\u4f4f\uff1a\u300a{completed_title}\u300b")
    lines.append("\u6700\u8fd1\u89c6\u9891\u8bb0\u5f55\uff1a")
    for task in rows:
        title = str(task.get("title") or task.get("bvid") or "\u672a\u77e5\u89c6\u9891").strip()
        bvid = str(task.get("bvid") or "").strip()
        suffix = f" {bvid}" if bvid and bvid not in title else ""
        lines.append(f"- {task_status_label(task)} | \u300a{title}\u300b{suffix}")
    return "\n".join(lines)


def notify_result(item: dict[str, Any], task: dict[str, Any], status: str, note: str = "", summary: str = "", one_line: str = "") -> bool:
    target = item_delivery_target(item)
    if not target:
        return False
    if status == "completed" and summary:
        bvid_match = BV_RE.search(str(item.get("url") or ""))
        message = summary_delivery_text(summary, one_line, bvid_match.group(0) if bvid_match else str(item.get("url") or ""), str(item.get("user_id") or ""))
    elif status == "memory_failed":
        message = "视频处理过了，但没有确认写入长期记忆，所以不能说已经记住。"
    else:
        message = result_message(task, status, str(item.get("review_level") or "rough"))
    if note:
        message += "\n" + note
    return send_item_message(item, message)


def handle_text(text: str, group_id: str, user_id: str, source: str, event: dict[str, Any] | None = None, message_key: str = "") -> None:
    config = load_config()
    if group_id not in allowed_groups(config):
        return
    link_policy = dict(config.get("qq_link_policy") or {})
    owner_qq = str(link_policy.get("owner_qq") or os.environ.get("BILIMEMORY_OWNER_QQ") or "")
    if not bool(link_policy.get("allow_all_group_members", True)) and str(user_id) != owner_qq and not qq_user_permission(user_id, "links"):
        log(f"QQ user {user_id} is not in the link allowlist")
        return
    if is_video_status_query(text):
        send_group_message(group_id, recent_video_status_message(group_id))
        return
    if event and has_share_card(event):
        if card_is_bilibili(event):
            log(f"Ignored Bilibili mini-program/share card from {user_id} in group {group_id}")
        return
    search_policy = dict(config.get("qq_link_policy") or {})
    search_request = None
    if not URL_RE.search(text):
        search_request = parse_configured_search_command(
            text,
            search_policy,
            int(search_policy.get("search_default_max") or 2),
            int(search_policy.get("search_max_results") or 5),
        )
    if search_request:
        if not bool(search_policy.get("search_enabled", True)):
            return
        owner_qq = str(search_policy.get("owner_qq") or os.environ.get("BILIMEMORY_OWNER_QQ") or "")
        allow_group_members = bool(search_policy.get("search_allow_all_group_members", False))
        owner_only = bool(search_policy.get("control_commands_owner_only", True))
        if owner_only and not allow_group_members and str(user_id) != owner_qq and not qq_user_permission(user_id, "search"):
            send_group_message(group_id, "这个搜索指令暂时不对外开放。")
            return
        query = str(search_request["query"])
        max_results = int(search_request["max_results"])
        send_group_message(group_id, f"收到，我去 B 站搜“{query}”，最多看 {max_results} 个。")
        enqueue_search(query, max_results, group_id, user_id, source, message_key)
        return
    from bili_dm_service import is_review_request
    if URL_RE.search(text) and bool(search_policy.get("require_link_only_message", True)) and not is_link_only_message(text):
        log(f"Ignored non-standalone Bilibili link from user {user_id} in group {group_id}")
        return
    urls = []
    for raw in URL_RE.findall(text):
        url = raw.rstrip(".,);]}")
        if BILI_RE.match(url) or SHORT_RE.match(url):
            if url not in urls:
                urls.append(url)
    review_level = "detailed" if is_review_request(text) else "rough"
    queued_bvids = []
    for url in urls:
        if BV_RE.search(url) or SHORT_RE.match(url):
            resolved = resolve_url(url) if SHORT_RE.match(url) and not BV_RE.search(url) else url
            if resolved:
                match = BV_RE.search(resolved)
                bvid = match.group(0) if match else ""
                if review_level == "detailed" and review_already_used(bvid):
                    send_group_message(group_id, f"\u8fd9\u4e2a\u89c6\u9891\u5df2\u7ecf\u91cd\u770b\u8fc7\u4e00\u6b21\u4e86\uff0c\u4e0d\u518d\u91cd\u590d\u89e6\u53d1\uff1a{bvid}")
                    continue
                if review_level == "detailed" and bvid:
                    mark_reviewed(bvid)
                enqueue(resolved, group_id, user_id, source, message_key, review_level=review_level)
                if bvid:
                    queued_bvids.append(bvid)
    if urls or review_level != "detailed":
        return
    store = get_task_store()
    last_bvid = store.kv_get("last_video:qq")
    match = BV_RE.search(last_bvid)
    if not match:
        send_group_message(group_id, "现在还没有可重看的视频，先发一个 B 站链接。")
        return
    bvid = match.group(0)
    if review_already_used(bvid):
        send_group_message(group_id, f"\u8fd9\u4e2a\u89c6\u9891\u5df2\u7ecf\u91cd\u770b\u8fc7\u4e00\u6b21\u4e86\uff0c\u4e0d\u518d\u91cd\u590d\u89e6\u53d1\uff1a{bvid}")
        return
    mark_reviewed(bvid)
    review_url = last_bvid if BILI_RE.match(last_bvid) or "bilibili.com/video/" in last_bvid else f"https://www.bilibili.com/video/{bvid}/"
    send_group_message(group_id, f"\u6536\u5230\uff0c\u6211\u91cd\u65b0\u7ec6\u770b BV\u53f7 {bvid}\u3002")
    enqueue(review_url, group_id, user_id, source, message_key, review_level="detailed")


def handle_private_text(text: str, user_id: str, source: str, event: dict[str, Any] | None = None, message_key: str = "") -> None:
    config = load_config()
    policy = dict(config.get("qq_link_policy") or {})
    owner_qq = str(policy.get("owner_qq") or os.environ.get("BILIMEMORY_OWNER_QQ") or "")
    if str(user_id) != owner_qq:
        return
    if is_video_status_query(text):
        send_private_message(user_id, recent_video_status_message(user_id))
        return
    if event and has_share_card(event):
        if card_is_bilibili(event):
            log(f"Ignored Bilibili mini-program/share card in private chat from {user_id}")
        return
    search_request = None if URL_RE.search(text) else parse_configured_search_command(
        text,
        policy,
        int(policy.get("search_default_max") or 2),
        int(policy.get("search_max_results") or 5),
    )
    if search_request:
        query = str(search_request["query"])
        max_results = int(search_request["max_results"])
        send_private_message(user_id, f"收到，我去 B 站搜“{query}”，最多看 {max_results} 个。")
        enqueue_search(query, max_results, "", user_id, source, message_key, private=True)
        return
    from bili_dm_service import is_review_request
    if URL_RE.search(text) and bool(policy.get("require_link_only_message", True)) and not is_link_only_message(text):
        log(f"Ignored non-standalone Bilibili link in private chat from user {user_id}")
        return
    urls = []
    for raw in URL_RE.findall(text):
        url = raw.rstrip(".,);]}")
        if BILI_RE.match(url) or SHORT_RE.match(url):
            if url not in urls:
                urls.append(url)
    review_level = "detailed" if is_review_request(text) else "rough"
    resolved_urls = []
    for url in urls:
        if BV_RE.search(url) or SHORT_RE.match(url):
            resolved = resolve_url(url) if SHORT_RE.match(url) and not BV_RE.search(url) else url
            if resolved:
                match = BV_RE.search(resolved)
                bvid = match.group(0) if match else ""
                if review_level == "detailed" and review_already_used(bvid):
                    send_private_message(user_id, f"\u8fd9\u4e2a\u89c6\u9891\u5df2\u7ecf\u91cd\u770b\u8fc7\u4e00\u6b21\u4e86\uff0c\u4e0d\u518d\u91cd\u590d\u89e6\u53d1\uff1a{bvid}")
                    continue
                if review_level == "detailed" and bvid:
                    mark_reviewed(bvid)
                resolved_urls.append(resolved)
    if resolved_urls:
        bvid_match = BV_RE.search(resolved_urls[0])
        bvid = bvid_match.group(0) if bvid_match else ""
        send_private_message(user_id, f"\u6536\u5230\uff0c\u6211\u6309 BV \u53f7 {bvid} \u53bb\u770b\u3002" if bvid else "\u6536\u5230\uff0c\u6211\u53bb\u770b\u8fd9\u4e2a\u89c6\u9891\u3002")
        for resolved in resolved_urls:
            enqueue(resolved, "", user_id, source, message_key, review_level=review_level, private=True)
        return
    if review_level != "detailed":
        return
    store = get_task_store()
    last_video = store.kv_get("last_video:qq_private")
    match = BV_RE.search(last_video)
    if not match:
        send_private_message(user_id, "\u73b0\u5728\u8fd8\u6ca1\u6709\u53ef\u91cd\u770b\u7684\u89c6\u9891\uff0c\u5148\u53d1\u4e00\u4e2a B \u7ad9\u94fe\u63a5\u3002")
        return
    bvid = match.group(0)
    if review_already_used(bvid):
        send_private_message(user_id, f"\u8fd9\u4e2a\u89c6\u9891\u5df2\u7ecf\u91cd\u770b\u8fc7\u4e00\u6b21\u4e86\uff0c\u4e0d\u518d\u91cd\u590d\u89e6\u53d1\uff1a{bvid}")
        return
    mark_reviewed(bvid)
    canonical = last_video if "bilibili.com/video/" in last_video else f"https://www.bilibili.com/video/{bvid}/"
    send_private_message(user_id, f"\u6536\u5230\uff0c\u6211\u91cd\u65b0\u7ec6\u770b BV\u53f7 {bvid}\u3002")
    enqueue(canonical, "", user_id, source, message_key, review_level="detailed", private=True)


def should_skip_known_video(bvid: str, review_level: str, known: set[str] | None = None) -> bool:
    if not bvid or str(review_level) == "detailed":
        return False
    return bvid in (known if known is not None else seen)


def qq_collection_payload(canonical: str, review_level: str) -> dict[str, Any]:
    return {"mode": "direct", "urls": [canonical], "max": 1, "import": True, "force": True, "ignore_preference": True, "suppress_comment": True, "review_level": review_level}


def process_url(item: dict[str, Any]) -> None:
    config = load_config()
    url = item["url"]
    bvid_match = BV_RE.search(url)
    bvid = bvid_match.group(0) if bvid_match else ""
    dedupe_key = bvid or f"url:{url}"
    with lock:
        already_inflight = dedupe_key in inflight
        if not already_inflight:
            inflight.add(dedupe_key)
    if already_inflight:
        log(f"In-flight video skipped: {dedupe_key}")
        send_item_message(item, "\u8fd9\u4e2a\u8fd8\u5728\u5904\u7406\uff0c\u6211\u4e0d\u91cd\u590d\u8dd1\u4e86\u3002")
        return
    if should_skip_known_video(bvid, str(item.get("review_level") or "rough")):
        log(f"Previously processed video skipped: {bvid}")
        if item.get("recovered"):
            store = get_task_store()
            existing = store.get_task(dedupe_key)
            if existing and str(existing.get("status") or "") == "queued":
                store.upsert_task(
                    dedupe_key,
                    str(existing.get("url") or item.get("url") or ""),
                    str(existing.get("msg_key") or item.get("message_key") or ""),
                    "completed",
                    review_level=str(item.get("review_level") or "rough"),
                    private_mode=1 if item.get("private") else 0,
                    requester_uid=str(item.get("user_id") or ""),
                    error="recovered task was already processed",
                )
            return
        send_item_message(item, "\u8fd9\u4e2a\u4e4b\u524d\u770b\u8fc7\u4e86\uff0c\u6211\u4e0d\u91cd\u590d\u5904\u7406\u3002\u8981\u91cd\u65b0\u7ec6\u770b\u5c31\u76f4\u63a5\u8bf4\u201c\u91cd\u65b0\u7ec6\u770b\u201d\u3002")
        return
    page_number = video_page(url)
    canonical = f"https://www.bilibili.com/video/{bvid}/" + (f"?p={page_number}" if bvid and page_number > 1 else "") if bvid else url
    store = get_task_store()
    store.upsert_task(
        dedupe_key,
        canonical,
        str(item.get("message_key") or ""),
        "queued",
        source="qq",
        delivery_target=item_delivery_target(item),
        source_message_key=str(item.get("message_key") or ""),
        review_level=str(item.get("review_level") or "rough"),
        private_mode=1 if item.get("private") else 0,
        requester_uid=str(item.get("user_id") or ""),
        error="",
    )
    if bvid:
        store.kv_set("last_video:qq", canonical)
    log(f"Queueing video {dedupe_key} from group {item['group_id']} user {item['user_id']} via {item['source']}")
    payload = qq_collection_payload(canonical, str(item.get("review_level") or "rough"))
    task_id = str(item.get("admin_task_id") or "")
    reattached = False
    if task_id:
        try:
            response = requests.get(f"{ADMIN_BASE}/api/tasks/{task_id}", timeout=20)
            existing_status = str((response.json() if response.ok else {}).get("status") or "")
            if response.ok and existing_status in {"queued", "running", "completed", "failed"}:
                reattached = True
                log(f"Reattached recovered video {dedupe_key} to admin task {task_id} ({existing_status})")
            else:
                task_id = ""
        except Exception as error:
            log(f"Admin task reattach failed for {dedupe_key}: {error}")
            task_id = ""
    if not task_id:
        for attempt in range(8):
            try:
                response = requests.post(ADMIN_BASE + "/api/actions/collect", json=payload, timeout=20)
                if response.status_code == 202:
                    data = response.json()
                    task_id = str((data.get("task") or {}).get("id") or "")
                    break
                if response.status_code == 409:
                    time.sleep(15)
                    continue
                log(f"Admin collect rejected HTTP {response.status_code}: {response.text[:300]}")
                send_item_message(item, "\u8fd9\u4e2a\u6211\u6ca1\u5904\u7406\u6210\u529f\uff1aB\u7ad9\u91c7\u96c6\u670d\u52a1\u6ca1\u6709\u63a5\u4e0a\u3002")
                store.upsert_task(dedupe_key, canonical, str(item.get("message_key") or ""), "failed", source="qq", delivery_target=item_delivery_target(item), source_message_key=str(item.get("message_key") or ""), error=f"collect rejected HTTP {response.status_code}")
                with lock:
                    inflight.discard(dedupe_key)
                return
            except Exception as error:
                log(f"Admin collect request failed: {error}")
                time.sleep(5)
    if not task_id:
        with lock:
            inflight.discard(dedupe_key)
        log(f"Unable to start collection for {bvid}")
        send_item_message(item, "\u8fd9\u4e2a\u6211\u6ca1\u5904\u7406\u6210\u529f\uff1a\u91c7\u96c6\u4efb\u52a1\u6ca1\u6709\u542f\u52a8\u3002")
        store.upsert_task(dedupe_key, canonical, str(item.get("message_key") or ""), "failed", source="qq", delivery_target=item_delivery_target(item), source_message_key=str(item.get("message_key") or ""), error="collect task did not start")
        return
    store.upsert_task(
        dedupe_key,
        canonical,
        str(item.get("message_key") or ""),
        "queued",
        source="qq",
        delivery_target=item_delivery_target(item),
        source_message_key=str(item.get("message_key") or ""),
        review_level=str(item.get("review_level") or "rough"),
        private_mode=1 if item.get("private") else 0,
        requester_uid=str(item.get("user_id") or ""),
        admin_task_id=task_id,
        error="",
    )
    log(f"Collection {'reattached to' if reattached else 'started for'} {dedupe_key}, task {task_id}")
    deadline = time.time() + int(((config.get("bili_dm") or {}).get("video_timeout_seconds") or 1800)) + 300
    while time.time() < deadline:
        try:
            response = requests.get(f"{ADMIN_BASE}/api/tasks/{task_id}", timeout=20)
            task = response.json() if response.ok else {}
            status = str(task.get("status") or "")
            if status in {"completed", "failed"}:
                with lock:
                    inflight.discard(dedupe_key)
                task_log = str(task.get("log") or "")
                imported = re.search(r"\u5bfc\u5165\s*([0-9]+)\s*\u4e2a", task_log)
                imported_count = int(imported.group(1)) if imported else 0
                resolved = re.search(r"Resolved short link .* -> (BV[0-9A-Za-z]{10,})", task_log)
                final_key = resolved.group(1) if resolved else dedupe_key
                if status == "completed" and imported_count > 0:
                    mark_seen(final_key)
                summary_path = ROOT / "data" / "summaries" / f"{final_key}.md"
                summary = summary_path.read_text(encoding="utf-8-sig") if summary_path.exists() else ""
                title_match = re.search(r"\u6807\u9898\uff1a(.+)", summary)
                title = title_match.group(1).strip() if title_match else final_key
                try:
                    from bili_dm_service import one_line_summary
                    one_line = one_line_summary(summary, title) if summary else ""
                except Exception:
                    one_line = ""
                db_status = "completed" if status == "completed" and imported_count > 0 else "failed"
                fields = dict(source="qq", delivery_target=item_delivery_target(item), source_message_key=str(item.get("message_key") or ""), review_level=str(item.get("review_level") or "rough"), private_mode=1 if item.get("private") else 0, requester_uid=str(item.get("user_id") or ""), memory_imported=1 if imported_count > 0 else 0, admin_task_id=task_id, title=title, summary=summary, one_line=one_line, error="" if db_status == "completed" else task_log[-1200:])
                store.upsert_task(dedupe_key, canonical, str(item.get("message_key") or ""), db_status, **fields)
                if final_key != dedupe_key:
                    store.upsert_task(final_key, canonical, str(item.get("message_key") or ""), db_status, **fields)
                # QQ-linked videos are delivered back to QQ only. Public Bilibili comments are reserved for the Bilibili DM flow.
                delivered = notify_result(item, task, db_status if db_status == "completed" else "memory_failed", "", summary=summary, one_line=one_line)
                store.upsert_task(dedupe_key, canonical, str(item.get("message_key") or ""), db_status, delivered=1 if delivered else 0)
                if final_key != dedupe_key:
                    store.upsert_task(final_key, canonical, str(item.get("message_key") or ""), db_status, delivered=1 if delivered else 0)
                log(f"Collection {dedupe_key} finished with status={status}")
                return
        except Exception:
            pass
        time.sleep(4)
    with lock:
        inflight.discard(dedupe_key)
    log(f"Collection {dedupe_key} timed out")
    delivered = send_item_message(item, "\u8fd9\u4e2a\u5904\u7406\u8d85\u65f6\u4e86\uff0c\u8fd8\u6ca1\u8bb0\u4f4f\u3002")
    store.upsert_task(dedupe_key, canonical, str(item.get("message_key") or ""), "failed", source="qq", delivery_target=item_delivery_target(item), source_message_key=str(item.get("message_key") or ""), delivered=1 if delivered else 0, error="collection timed out")

def queued_task_item(task: dict[str, Any]) -> dict[str, Any]:
    private = bool(int(task.get("private_mode") or 0))
    target = str(task.get("delivery_target") or "")
    requester = str(task.get("requester_uid") or "")
    return {
        "url": str(task.get("url") or ""),
        "group_id": "" if private else target,
        "user_id": requester or (target if private else ""),
        "source": str(task.get("source") or "qq"),
        "message_key": str(task.get("source_message_key") or task.get("msg_key") or ""),
        "review_level": str(task.get("review_level") or "rough"),
        "admin_task_id": str(task.get("admin_task_id") or ""),
        "private": private,
        "recovered": True,
    }


def recover_queued_tasks(store=None, *, now: float | None = None, max_age_seconds: int | None = None) -> dict[str, int]:
    config = load_config()
    policy = dict(config.get("qq_link_policy") or {})
    if not bool(policy.get("enabled", True)):
        return {"recovered": 0, "interrupted": 0}
    if max_age_seconds is None:
        video_timeout = int((config.get("bili_dm") or {}).get("video_timeout_seconds") or 1800)
        max_age_seconds = int(policy.get("queue_recovery_max_age_seconds") or max(QUEUE_RECOVERY_DEFAULT_SECONDS, video_timeout + 300))
    max_age_seconds = max(60, int(max_age_seconds))
    reference = time.time() if now is None else float(now)
    store = store or get_task_store()
    result = {"recovered": 0, "interrupted": 0}
    tasks = list(reversed(store.tasks(filter_name="queued", limit=1000)))
    for task in tasks:
        key = str(task.get("bvid") or "")
        url = str(task.get("url") or "")
        msg_key = str(task.get("msg_key") or "")
        target = str(task.get("delivery_target") or "")
        age = max(0.0, reference - float(task.get("updated_at") or 0))
        item = queued_task_item(task)
        reason = ""
        if not key or not url:
            reason = "queued task has no valid video key or URL"
        elif not target:
            reason = "queued task has no delivery target"
        elif age > max_age_seconds:
            reason = f"queued task expired after {int(age)} seconds before restart recovery"
        if reason:
            store.upsert_task(
                key,
                url,
                msg_key,
                "interrupted",
                delivery_target=target,
                source_message_key=str(task.get("source_message_key") or msg_key),
                review_level=item["review_level"],
                private_mode=1 if item["private"] else 0,
                requester_uid=str(item.get("user_id") or ""),
                error=reason,
            )
            result["interrupted"] += 1
            log(f"Queued video interrupted instead of resumed: {key} ({reason})")
            continue
        work_queue.put(item)
        store.upsert_task(
            key,
            url,
            msg_key,
            "queued",
            delivery_target=target,
            source_message_key=str(task.get("source_message_key") or msg_key),
            review_level=item["review_level"],
            private_mode=1 if item["private"] else 0,
            requester_uid=str(item.get("user_id") or ""),
            error="",
        )
        result["recovered"] += 1
        log(f"Recovered queued video after restart: {key} ({item['review_level']})")
    return result


def process_search(item: dict[str, Any]) -> None:
    config = load_config()
    query = str(item.get("query") or "").strip()
    max_results = max(1, min(5, int(item.get("max_results") or 2)))
    group_id = str(item.get("group_id") or "")
    private_mode = bool(item.get("private"))
    target_id = str(item.get("user_id") or "") if private_mode else group_id
    message_key = str(item.get("message_key") or "")

    def send_target(message: str) -> bool:
        return send_private_message(target_id, message) if private_mode else send_group_message(target_id, message)
    dedupe_key = f"search:{query.casefold()}:{max_results}"
    with lock:
        if dedupe_key in inflight:
            send_target( "\u8fd9\u4e2a\u641c\u7d22\u8fd8\u5728\u5904\u7406\uff0c\u6211\u4e0d\u91cd\u590d\u8dd1\u4e86\u3002")
            return
        inflight.add(dedupe_key)
    try:
        resolve_payload = {"query": query, "max": max_results}
        resolve_task_id = ""
        for _ in range(8):
            response = requests.post(ADMIN_BASE + "/api/actions/resolve", json=resolve_payload, timeout=20)
            if response.status_code == 202:
                resolve_task_id = str((response.json().get("task") or {}).get("id") or "")
                break
            if response.status_code == 409:
                time.sleep(15)
                continue
            raise RuntimeError(f"resolve rejected HTTP {response.status_code}: {response.text[:300]}")
        if not resolve_task_id:
            raise RuntimeError("resolve task did not start")
        log(f"BV resolve started query={query!r} max={max_results} task={resolve_task_id}")
        deadline = time.time() + 300
        resolve_result: dict[str, Any] = {}
        while time.time() < deadline:
            response = requests.get(f"{ADMIN_BASE}/api/tasks/{resolve_task_id}", timeout=20)
            resolve_result = response.json() if response.ok else {}
            if resolve_result.get("status") in {"completed", "failed"}:
                break
            time.sleep(3)
        resolve_log = str(resolve_result.get("log") or "")
        candidates = []
        seen_bvids = set()
        for line in resolve_log.splitlines():
            match = re.search(r"SEARCH_RESULT\|(BV[0-9A-Za-z]{10,})\|([0-9.]+)\|(.+)", line)
            if not match:
                continue
            bvid = match.group(1)
            if bvid in seen_bvids:
                continue
            seen_bvids.add(bvid)
            candidates.append({"bvid": bvid, "score": float(match.group(2)), "title": match.group(3).strip()})
        if not candidates:
            if "SEARCH_RESULT_EMPTY=true" in resolve_log:
                send_target( f"\u6ca1\u641c\u5230\u548c\u201c{query}\u201d\u76f8\u5173\u7684\u89c6\u9891\u3002")
            else:
                send_target( "\u641c\u7d22\u9875\u6ca1\u6709\u8fd4\u56de\u53ef\u7528 BV \u53f7\uff0c\u8fd9\u6b21\u6ca1\u6709\u771f\u6b63\u5f00\u59cb\u770b\u3002")
            return
        candidates = candidates[:max_results]
        selected_lines = [f"- \u300a{item['title']}\u300b {item['bvid']}" for item in candidates]
        send_target( "\u3010\u89c6\u9891\u72b6\u6001\uff1a\u5904\u7406\u4e2d\u3011\u641c\u5230\u4e86\uff0c\u63a5\u4e0b\u6765\u6309 BV \u53f7\u53bb\u770b\uff1a\n" + "\n".join(selected_lines))
        direct_payload = {
            "mode": "direct",
            "urls": [f"https://www.bilibili.com/video/{item['bvid']}/" for item in candidates],
            "max": len(candidates),
            "import": True,
            "force": False,
            "ignore_preference": True,
            "suppress_comment": True,
        }
        task_id = ""
        for _ in range(8):
            response = requests.post(ADMIN_BASE + "/api/actions/collect", json=direct_payload, timeout=20)
            if response.status_code == 202:
                task_id = str((response.json().get("task") or {}).get("id") or "")
                break
            if response.status_code == 409:
                time.sleep(15)
                continue
            raise RuntimeError(f"BV collect rejected HTTP {response.status_code}: {response.text[:300]}")
        if not task_id:
            raise RuntimeError("BV collect task did not start")
        log(f"BV collect started query={query!r} bvids={','.join(item['bvid'] for item in candidates)} task={task_id}")
        deadline = time.time() + int(((config.get("bili_dm") or {}).get("video_timeout_seconds") or 1800)) + 300
        while time.time() < deadline:
            response = requests.get(f"{ADMIN_BASE}/api/tasks/{task_id}", timeout=20)
            task = response.json() if response.ok else {}
            status = str(task.get("status") or "")
            if status not in {"completed", "failed"}:
                time.sleep(4)
                continue
            task_log = str(task.get("log") or "")
            imported_match = re.search(r"\u5bfc\u5165\s*([0-9]+)\s*\u4e2a", task_log)
            imported_count = int(imported_match.group(1)) if imported_match else 0
            store = get_task_store()
            completed_rows = []
            for candidate in candidates:
                bvid = candidate["bvid"]
                canonical = f"https://www.bilibili.com/video/{bvid}/"
                summary_path = ROOT / "data" / "summaries" / f"{bvid}.md"
                summary = summary_path.read_text(encoding="utf-8-sig") if summary_path.exists() else ""
                title_match = re.search(r"\u6807\u9898\uff1a(.+)", summary)
                title = title_match.group(1).strip() if title_match else candidate["title"]
                try:
                    from bili_dm_service import one_line_summary
                    one_line = one_line_summary(summary, title) if summary else ""
                except Exception:
                    one_line = ""
                db_status = "completed" if status == "completed" and imported_count > 0 and summary else "failed"
                if db_status == "completed":
                    completed_rows.append((bvid, title, one_line))
                store.upsert_task(
                    bvid,
                    canonical,
                    message_key,
                    db_status,
                    source="qq",
                    delivery_target=target_id,
                    source_message_key=message_key,
                    admin_task_id=task_id,
                    title=title,
                    summary=summary,
                    one_line=one_line,
                    memory_imported=1 if summary and imported_count > 0 else 0,
                    error="" if db_status == "completed" else task_log[-1200:],
                )
                if summary:
                    store.kv_set("last_video:qq", canonical)
            if completed_rows:
                lines = [f"- \u300a{title}\u300b {bvid}" for bvid, title in completed_rows]
                message = "\u770b\u5b8c\u5e76\u8bb0\u4f4f\u4e86\uff1a\n" + "\n".join(lines)
            else:
                message = "\u5df2\u7ecf\u62ff\u5230 BV \u53f7\uff0c\u4f46\u89c6\u9891\u6ca1\u6709\u751f\u6210\u53ef\u8bb0\u4f4f\u7684\u5b8c\u6574\u6458\u8981\u3002"
            delivered = send_target( message)
            log(f"BV search finished query={query!r} status={status} imported={imported_count} completed={len(completed_rows)} delivered={delivered}")
            return
        raise TimeoutError("BV video task timed out")
    except Exception as error:
        log(f"Search failed query={query!r}: {error}")
        send_target( f"\u641c\u7d22\u6ca1\u5904\u7406\u6210\u529f\uff1a{str(error)[:180]}")
    finally:
        with lock:
            inflight.discard(dedupe_key)




def worker() -> None:
    while True:
        item = work_queue.get()
        try:
            if item.get("query"):
                process_search(item)
            else:
                process_url(item)
        except Exception as error:
            log(f"Worker error: {error}")
        finally:
            work_queue.task_done()
            time.sleep(5)


def start_log_tail() -> None:
    try:
        offset = NAPCAT_LOG.stat().st_size if NAPCAT_LOG.exists() else 0
    except OSError:
        offset = 0
    while True:
        try:
            if not NAPCAT_LOG.exists():
                time.sleep(3)
                continue
            size = NAPCAT_LOG.stat().st_size
            if size < offset:
                offset = 0
            if size == offset:
                time.sleep(2)
                continue
            with NAPCAT_LOG.open("r", encoding="utf-8", errors="ignore") as file:
                file.seek(offset)
                lines = file.readlines()
                offset = file.tell()
            for line in lines:
                if not LOG_TAIL_GROUP or LOG_TAIL_GROUP not in line:
                    continue
                if any(token in line.casefold() for token in ("miniapp", "<msg ", "小程序", "json", "xml")):
                    continue
                handle_text(line, LOG_TAIL_GROUP, "log-tail", "napcat-log", message_key=f"napcat-log:{uuid.uuid4().hex}")
        except Exception as error:
            log(f"Log-tail error: {error}")
            time.sleep(5)


def websocket_loop() -> None:
    global ws_connected
    token = bot_token()
    uri = f"ws://127.0.0.1:7998?access_token={quote(token, safe='')}"
    while True:
        try:
            with ws_connect(uri, open_timeout=10, close_timeout=5, ping_interval=20, ping_timeout=20) as ws:
                ws_connected = True
                log("Connected to OneBot WebSocket 127.0.0.1:7998")
                while True:
                    try:
                        raw = ws.recv(timeout=30)
                    except TimeoutError:
                        continue
                    try:
                        event = json.loads(raw)
                    except Exception:
                        continue
                    if event.get("post_type") != "message":
                        continue
                    user_id = str((event.get("sender") or {}).get("user_id") or event.get("user_id") or "")
                    if str(event.get("self_id") or "") == user_id:
                        continue
                    text = segment_text(event)
                    message_key = str(event.get("message_id") or "")
                    if event.get("message_type") == "private":
                        handle_private_text(text, user_id, "qq-private", event, message_key)
                        continue
                    if event.get("message_type") != "group":
                        continue
                    group_id = str(event.get("group_id") or "")
                    if has_share_card(event) and not re.search(r"https?://", text):
                        if card_is_bilibili(event):
                            log(f"Ignored mini-program card from user {user_id} in group {group_id}")
                        continue
                    handle_text(text, group_id, user_id, "onebot", event, message_key)
        except Exception as error:
            ws_connected = False
            log(f"OneBot reconnect in 5s: {error}")
            time.sleep(5)


instance_socket: socket.socket | None = None


def acquire_single_instance() -> bool:
    global instance_socket
    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        candidate.bind(("127.0.0.1", INSTANCE_PORT))
        candidate.listen(1)
    except OSError:
        candidate.close()
        return False
    instance_socket = candidate
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    return True


def main() -> None:
    if not acquire_single_instance():
        log("Another QQ link watcher is already running; exiting")
        return
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        recovery = recover_queued_tasks()
        if recovery["recovered"] or recovery["interrupted"]:
            log(f"Queue recovery: recovered={recovery['recovered']} interrupted={recovery['interrupted']}")
    except Exception as error:
        log(f"Queue recovery failed: {error}")
    threading.Thread(target=worker, daemon=True, name="video-worker").start()
    config = load_config()
    enable_log_tail = bool((config.get("qq_link_policy") or {}).get("enable_napcat_log_tail", False))
    if enable_log_tail:
        threading.Thread(target=start_log_tail, daemon=True, name="napcat-log-tail").start()
    log(f"QQ link watcher started; log-tail-group={LOG_TAIL_GROUP or 'unset'}; log-tail=" + ("on" if enable_log_tail else "off"))
    websocket_loop()


if __name__ == "__main__":
    main()
