from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin, Tool

PROJECT_ROOT = Path(os.environ.get("BILIMEMORY_APP_DIR", Path(__file__).resolve().parents[2])).resolve()
DEFAULT_DB = PROJECT_ROOT / "data" / "bili_dm.db"
ADMIN_STATUS_URL = "http://127.0.0.1:8765/api/status"
STATUS_MARKER = "\u3010\u5b9e\u65f6\u81ea\u6211\u72b6\u6001\uff08\u540e\u53f0\u4efb\u52a1\u5e93\u76f4\u8bfb\uff09\u3011"


def _status_label(status: str, memory_imported: Any) -> str:
    status = str(status or "").lower()
    if status == "completed" and int(memory_imported or 0) > 0:
        return "\u5df2\u770b\u5b8c\u5e76\u5199\u5165\u8bb0\u5fc6"
    if status == "completed":
        return "\u5904\u7406\u5b8c\u6210\uff0c\u4f46\u8bb0\u5fc6\u672a\u786e\u8ba4"
    if status in {"queued", "running"}:
        return "\u5904\u7406\u4e2d"
    if status == "interrupted":
        return "\u4e0a\u6b21\u5904\u7406\u4e2d\u65ad"
    if status == "failed":
        return "\u5904\u7406\u5931\u8d25"
    return status or "\u672a\u77e5"


def _fetch_admin_status() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(ADMIN_STATUS_URL, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _effective_label(task: dict[str, Any], active_id: str, admin_statuses: dict[str, str]) -> str:
    db_status = str(task.get("status") or "").lower()
    if db_status not in {"queued", "running"}:
        return _status_label(db_status, task.get("memory_imported"))
    admin_task_id = str(task.get("admin_task_id") or "")
    admin_status = admin_statuses.get(admin_task_id, "")
    if admin_task_id and admin_task_id == active_id:
        return "\u5904\u7406\u4e2d"
    if admin_status == "running":
        return "\u5904\u7406\u4e2d"
    if admin_status == "failed":
        return "\u5904\u7406\u5931\u8d25"
    if admin_status == "completed":
        return _status_label("completed", task.get("memory_imported"))
    return "\u4e0a\u6b21\u672a\u6b63\u5e38\u6536\u5c3e\uff08\u5f53\u524d\u672a\u5728\u5904\u7406\uff09"


def _shorten(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^(?:\u4e00\u53e5\u8bdd\u6982\u62ec|\u6458\u8981)\s*[:\uff1a]\s*", "", text)
    return text[:limit]


def build_status_data(db_path: Path | str = DEFAULT_DB, limit: int = 4, include_admin: bool = True) -> dict[str, Any]:
    path = Path(db_path)
    admin = _fetch_admin_status() if include_admin else {}
    active = admin.get("activeTask") if isinstance(admin, dict) else {}
    active_id = str((active or {}).get("id") or "") if isinstance(active, dict) else ""
    admin_statuses = {
        str(item.get("id") or ""): str(item.get("status") or "")
        for item in (admin.get("tasks") or [])
        if isinstance(item, dict) and item.get("id")
    } if isinstance(admin, dict) else {}
    result: dict[str, Any] = {
        "available": False,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": [],
        "counts": {},
        "error": "",
    }
    if not path.exists():
        result["error"] = "task database not found"
        return result
    try:
        uri = "file:" + path.resolve().as_posix() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT bvid,title,summary,one_line,status,memory_imported,source,delivery_target,admin_task_id,created_at,updated_at "
                "FROM video_tasks ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 10)),),
            ).fetchall()
            counts_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM video_tasks GROUP BY status"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM video_tasks").fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return result

    tasks = []
    for row in rows:
        task = dict(row)
        task["state_label"] = _effective_label(task, active_id, admin_statuses)
        summary = _shorten(task.get("one_line") or task.get("summary"), 180)
        task["brief"] = summary
        task["updated_text"] = datetime.fromtimestamp(float(task.get("updated_at") or 0)).strftime("%Y-%m-%d %H:%M")
        tasks.append(task)
    counts = {str(row["status"]): int(row["count"]) for row in counts_rows}
    counts["total"] = int(total)
    counts["active"] = sum(1 for task in tasks if task.get("state_label") == "\u5904\u7406\u4e2d")
    result.update(available=True, tasks=tasks, counts=counts, admin_available=bool(admin))
    return result


def format_status_text(data: dict[str, Any]) -> str:
    lines = [STATUS_MARKER]
    if not data.get("available"):
        lines.append("\u4efb\u52a1\u5e93\u6682\u65f6\u8bfb\u53d6\u4e0d\u5230\u3002\u4e0d\u80fd\u56e0\u6b64\u5426\u8ba4\u81ea\u5df1\u5df2\u7ecf\u770b\u8fc7\u6216\u8bb0\u4f4f\u8fc7\u89c6\u9891\uff0c\u53ea\u80fd\u8bf4\u6b63\u5728\u786e\u8ba4\u72b6\u6001\u3002")
        return "\n".join(lines)
    counts = data.get("counts") or {}
    active = int(counts.get("active") or 0)
    remembered = sum(1 for task in data.get("tasks") or [] if task.get("state_label") == "\u5df2\u770b\u5b8c\u5e76\u5199\u5165\u8bb0\u5fc6")
    lines.append(
        f"\u4efb\u52a1\u603b\u6570 {int(counts.get('total') or 0)}\uff0c\u5904\u7406\u4e2d {active}\uff0c\u6700\u8fd1\u8bb0\u5f55\u4e2d\u5df2\u786e\u8ba4\u8bb0\u4f4f {remembered} \u4e2a\u3002"
    )
    tasks = data.get("tasks") or []
    if not tasks:
        lines.append("\u4efb\u52a1\u5e93\u6682\u65e0\u8bb0\u5f55\u3002")
    else:
        lines.append("\u6700\u8fd1\u89c6\u9891\u4efb\u52a1\uff1a")
        for index, task in enumerate(tasks, 1):
            title = str(task.get("title") or "\u672a\u77e5\u89c6\u9891").strip()
            bvid = str(task.get("bvid") or "").strip()
            label = str(task.get("state_label") or "\u672a\u77e5")
            source = str(task.get("source") or "")
            target = str(task.get("delivery_target") or "")
            place = f"\uff0cQQ {target}" if source == "qq" and target else ""
            latest = "\u3010\u6700\u8fd1\u4e00\u6761\u3011" if index == 1 else ""
            updated = str(task.get("updated_text") or "").strip()
            updated_note = f"\uff0c\u66f4\u65b0 {updated}" if updated else ""
            line = f"{index}. {latest}\u300a{title}\u300b {bvid or ''}\uff1a{label}{place}{updated_note}"
            brief = str(task.get("brief") or "").strip()
            if brief:
                line += f"\uff1b\u5176\u4e2d\u53ef\u7528\u5185\u5bb9\uff1a{brief}"
            lines.append(line)
    lines.append(
        "\u56de\u7b54\u89c4\u5219\uff1a\u7528\u6237\u95ee\u4f60\u81ea\u5df1\u7684\u72b6\u6001\u3001\u5728\u5fd9\u4ec0\u4e48\u3001\u521a\u624d\u90a3\u4e2a\u89c6\u9891\u770b\u6ca1\u770b\u3001\u8bb0\u6ca1\u8bb0\u4f4f\u65f6\uff0c\u5fc5\u987b\u4f18\u5148\u4f7f\u7528\u672c\u6bb5\u7684\u4efb\u52a1\u8bb0\u5f55\u3002\u72b6\u6001\u662f\u201c\u5df2\u770b\u5b8c\u5e76\u5199\u5165\u8bb0\u5fc6\u201d\u65f6\uff0c\u4e0d\u5f97\u8bf4\u81ea\u5df1\u6ca1\u770b\u8fc7\u3001\u4e0d\u77e5\u9053\u72b6\u6001\u6216\u53ea\u80fd\u770b\u5230\u6807\u9898\u3002\u53ea\u6709\u672c\u6bb5\u663e\u793a\u5904\u7406\u4e2d\u3001\u5931\u8d25\uff0c\u6216\u7684\u786e\u6ca1\u6709\u5bf9\u5e94\u4efb\u52a1\u65f6\uff0c\u624d\u53ef\u4ee5\u5982\u5b9e\u8bf4\u660e\u3002"
    )
    return "\n".join(lines)


def _contains_status_block(items: list[dict[str, Any]]) -> bool:
    for item in items:
        if not isinstance(item, dict):
            continue
        for part in item.get("parts") or []:
            text = str(part.get("text") or "") if isinstance(part, dict) else ""
            if STATUS_MARKER in text and ("\n\u4efb\u52a1\u603b\u6570" in text or "\n\u4efb\u52a1\u5e93\u6682\u65f6" in text):
                return True
    return False


def inject_status_into_items(items: list[dict[str, Any]], status_text: str) -> list[dict[str, Any]]:
    if not items or not status_text or _contains_status_block(items):
        return items
    updated = copy.deepcopy(items)
    for item in updated:
        if not isinstance(item, dict) or item.get("item_type") != "SystemMessageItem":
            continue
        for part in item.get("parts") or []:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = str(part.get("text") or "").rstrip()
            part["text"] = (text + "\n\n" + status_text).strip()
            return updated
    return items


class BiliSelfStatePlugin(MaiBotPlugin):
    def __init__(self) -> None:
        super().__init__()
        self._cache_at = 0.0
        self._cache_text = ""

    async def on_load(self) -> None:
        self.ctx.logger.info("Bili self-state plugin loaded")

    async def on_unload(self) -> None:
        self._cache_text = ""
        self._cache_at = 0.0

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        self._cache_text = ""
        self._cache_at = 0.0

    def _status_text(self, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._cache_text and now - self._cache_at < 2.0:
            return self._cache_text
        self._cache_text = format_status_text(build_status_data(limit=4))
        self._cache_at = now
        return self._cache_text

    @Tool(
        "get_self_status",
        description="\u8bfb\u53d6\u81ea\u5df1\u7684\u5b9e\u65f6 B \u7ad9/\u89c6\u9891\u4efb\u52a1\u72b6\u6001\u3002\u7528\u6237\u95ee\u4f60\u5728\u5e72\u4ec0\u4e48\u3001\u770b\u6ca1\u770b\u8fc7\u67d0\u4e2a\u89c6\u9891\u3001\u662f\u5426\u8bb0\u4f4f\u65f6\u8c03\u7528\u3002",
        detailed_description="\u53ea\u8bfb\u8bbf\u95ee\u672c\u5730\u89c6\u9891\u4efb\u52a1\u5e93\uff0c\u8fd4\u56de\u6700\u8fd1\u4efb\u52a1\u3001\u5904\u7406\u72b6\u6001\u3001\u8bb0\u5fc6\u5bfc\u5165\u7ed3\u679c\u548c\u53ef\u7528\u6458\u8981\u3002\u4e0d\u4f1a\u4fee\u6539\u4efb\u4f55\u6570\u636e\u3002",
    )
    async def get_self_status(self, **kwargs: Any) -> dict[str, Any]:
        data = build_status_data(limit=6)
        return {
            "success": bool(data.get("available")),
            "status_text": format_status_text(data),
            "tasks": data.get("tasks") or [],
            "counts": data.get("counts") or {},
        }

    @HookHandler(
        "maisaka.planner.before_request",
        name="inject_live_self_state",
        description="\u5728\u6bcf\u8f6e\u5bf9\u8bdd\u524d\u6ce8\u5165\u53ea\u8bfb\u7684\u5b9e\u65f6\u89c6\u9891\u4efb\u52a1\u72b6\u6001\u3002",
        timeout_ms=5000,
    )
    async def inject_live_self_state(self, items: Any = None, **kwargs: Any) -> dict[str, Any]:
        modified = dict(kwargs)
        if isinstance(items, list):
            modified["items"] = inject_status_into_items(items, self._status_text())
        else:
            modified["items"] = items
        return {"action": "continue", "modified_kwargs": modified}


def create_plugin() -> BiliSelfStatePlugin:
    return BiliSelfStatePlugin()
