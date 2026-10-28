from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "plugins" / "bili-self-state-awareness" / "plugin.py"
spec = importlib.util.spec_from_file_location("bili_self_state_plugin", PLUGIN_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


class SelfStatePluginTests(unittest.TestCase):
    def _db(self, root: Path) -> Path:
        path = root / "tasks.db"
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE video_tasks (bvid TEXT, title TEXT, summary TEXT, one_line TEXT, status TEXT, "
            "memory_imported INTEGER, source TEXT, delivery_target TEXT, admin_task_id TEXT, created_at REAL, updated_at REAL)"
        )
        db.execute(
            "INSERT INTO video_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "BV1xx411c7mD",
                "\u6d4b\u8bd5\u89c6\u9891",
                "",
                "\u4e00\u53e5\u8bdd\u6982\u62ec\uff1a\u753b\u9762\u91cc\u6709\u9ec4\u8272\u89d2\u8272\u548c\u900f\u660e\u8f6f\u7ba1\u3002",
                "completed",
                1,
                "qq",
                "900000001",
                "task-1",
                time.time() - 3,
                time.time(),
            ),
        )
        db.commit()
        db.close()
        return path

    def test_interrupted_status_is_not_reported_as_active(self):
        self.assertEqual(module._status_label("interrupted", 1), "上次处理中断")

    def test_status_data_reports_confirmed_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = module.build_status_data(self._db(Path(tmp)), include_admin=False)
            self.assertTrue(data["available"])
            self.assertEqual(data["tasks"][0]["state_label"], "\u5df2\u770b\u5b8c\u5e76\u5199\u5165\u8bb0\u5fc6")
            text = module.format_status_text(data)
            self.assertIn("\u6d4b\u8bd5\u89c6\u9891", text)
            self.assertIn("\u9ec4\u8272\u89d2\u8272", text)
            self.assertIn("\u4e0d\u5f97\u8bf4\u81ea\u5df1\u6ca1\u770b\u8fc7", text)

    def test_marker_mention_in_persona_does_not_block_injection(self):
        items = [{"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": module.STATUS_MARKER + "\u3002\u4f60\u5e94\u8be5\u67e5\u770b\u72b6\u6001"}]}]
        result = module.inject_status_into_items(items, module.STATUS_MARKER + "\n\u4efb\u52a1\u603b\u6570 1")
        self.assertEqual(result[0]["parts"][0]["text"].count(module.STATUS_MARKER), 2)
        second = module.inject_status_into_items(result, module.STATUS_MARKER + "\n\u4efb\u52a1\u603b\u6570 1")
        self.assertEqual(second[0]["parts"][0]["text"].count(module.STATUS_MARKER), 2)

    def test_stale_running_task_is_not_reported_as_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db(Path(tmp))
            db = sqlite3.connect(path)
            db.execute("UPDATE video_tasks SET status='running', memory_imported=0")
            db.commit()
            db.close()
            data = module.build_status_data(path, include_admin=False)
            self.assertEqual(data["counts"]["active"], 0)
            self.assertIn("\u4e0a\u6b21\u672a\u6b63\u5e38\u6536\u5c3e", data["tasks"][0]["state_label"])

    def test_injects_only_into_system_message(self):
        items = [
            {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "base prompt"}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "hello"}]},
        ]
        result = module.inject_status_into_items(items, module.STATUS_MARKER + "\nstate")
        self.assertIn(module.STATUS_MARKER, result[0]["parts"][0]["text"])
        self.assertEqual(result[1]["parts"][0]["text"], "hello")
        self.assertNotIn(module.STATUS_MARKER, items[0]["parts"][0]["text"])

    def test_missing_database_is_not_treated_as_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = module.build_status_data(Path(tmp) / "missing.db")
            text = module.format_status_text(data)
            self.assertFalse(data["available"])
            self.assertIn("\u4e0d\u80fd\u56e0\u6b64\u5426\u8ba4", text)
