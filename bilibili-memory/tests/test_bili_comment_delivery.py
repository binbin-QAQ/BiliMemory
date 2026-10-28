from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from bili_comment_delivery import (
    BilibiliCommentClient,
    CookieStateError,
    load_main_site_cookies,
    normalize_bvid,
)

BVID = "BV1xx411c7mD"
AID = 987654321
RPID = 123456789012345


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *, view=None, post=None, replies=None):
        self.view = view or {"code": 0, "data": {"aid": AID, "cid": 10, "title": "t", "owner": {"name": "u"}}}
        self.post_response = post or {"code": 0, "data": {"rpid": RPID, "rpid_str": str(RPID)}, "msg_key": "mk"}
        self.replies_response = replies or {"code": 0, "data": {"replies": []}}
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if url.endswith("/x/web-interface/view"):
            return FakeResponse(200, self.view)
        return FakeResponse(200, self.replies_response)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse(200, self.post_response)


def logged_in_cookies():
    return {
        "SESSDATA": "main-session",
        "bili_jct": "csrf-token",
        "DedeUserID": "1000000000000001",
        "buvid3": "device",
    }


class CookieLoadingTests(unittest.TestCase):
    def test_loads_only_main_site_cookies_and_hides_values_in_errors(self):
        now = time.time()
        state = {
            "cookies": [
                {"name": "SESSDATA", "value": "main-session", "domain": ".bilibili.com", "expires": now + 3600},
                {"name": "bili_jct", "value": "csrf-token", "domain": "api.bilibili.com", "expires": now + 3600},
                {"name": "DedeUserID", "value": "main-uid", "domain": "www.bilibili.com", "expires": now + 3600},
                {"name": "buvid3", "value": "device", "domain": ".bilibili.com", "expires": now + 3600},
                {"name": "SESSDATA", "value": "wrong-domain", "domain": ".huasheng.cn", "expires": now + 3600},
                {"name": "DedeUserID", "value": "wrong-domain-uid", "domain": ".biligame.com", "expires": now + 3600},
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "storage_state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            cookies = load_main_site_cookies(path, now=now)
        self.assertEqual(cookies["SESSDATA"], "main-session")
        self.assertEqual(cookies["bili_jct"], "csrf-token")
        self.assertEqual(cookies["DedeUserID"], "main-uid")
        self.assertEqual(cookies["buvid3"], "device")
        self.assertNotIn("wrong-domain", cookies.values())

    def test_missing_login_cookie_error_does_not_include_cookie_value(self):
        state = {
            "cookies": [
                {"name": "SESSDATA", "value": "super-secret", "domain": ".bilibili.com", "expires": 0},
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "storage_state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(CookieStateError) as caught:
                load_main_site_cookies(path)
        self.assertIn("bili_jct", str(caught.exception))
        self.assertNotIn("super-secret", str(caught.exception))

    def test_rejects_nonstandard_bvid(self):
        with self.assertRaises(ValueError):
            normalize_bvid("https://www.bilibili.com/video/BV1xx411c7mD")
        with self.assertRaises(ValueError):
            normalize_bvid("BV!!!!!!!!!!")


class DeliveryTests(unittest.TestCase):
    def test_dry_run_never_posts(self):
        session = FakeSession()
        client = BilibiliCommentClient(cookies=logged_in_cookies(), session=session)
        result = client.send_comment(BVID, "this is worth a look")
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["accepted"])
        self.assertFalse(result["post_attempted"])
        self.assertEqual(session.post_calls, [])
        self.assertTrue(result["comment_text"].startswith("@"))
        self.assertIn("this is worth a look", result["comment_text"])
        self.assertNotIn("csrf", result["request_preview"]["form"])
        self.assertEqual(result["request_preview"]["form"]["oid"], str(AID))

    def test_accepts_only_code_zero_with_rpid_and_preserves_msg_key(self):
        session = FakeSession()
        client = BilibiliCommentClient(cookies=logged_in_cookies(), session=session)
        result = client.send_comment(BVID, "summary", execute=True)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "api_accepted")
        self.assertEqual(result["rpid"], RPID)
        self.assertEqual(result["rpid_str"], str(RPID))
        self.assertEqual(result["msg_key"], "mk")
        self.assertEqual(result["code"], 0)
        self.assertTrue(result["post_attempted"])
        form = session.post_calls[0][1]["data"]
        self.assertEqual(form["type"], "1")
        self.assertEqual(form["oid"], str(AID))
        self.assertEqual(form["csrf"], "csrf-token")
        self.assertIn("@", form["message"])
        self.assertIn("at_name_to_mid", form)

    def test_multiple_allowlisted_mentions_are_encoded(self):
        session = FakeSession()
        client = BilibiliCommentClient(cookies=logged_in_cookies(), session=session)
        result = client.send_comment(
            BVID,
            "summary",
            mention_accounts=[
                {"name": "Alice", "uid": "1001"},
                {"name": "Bob", "uid": "1002"},
            ],
        )
        self.assertTrue(result["comment_text"].startswith("@Alice @Bob "))
        mapping = json.loads(result["request_preview"]["form"]["at_name_to_mid"])
        self.assertEqual(mapping, {"Alice": "1001", "Bob": "1002"})

    def test_code_zero_without_rpid_is_not_accepted(self):
        session = FakeSession(post={"code": 0, "data": {}, "msg_key": "mk"})
        client = BilibiliCommentClient(cookies=logged_in_cookies(), session=session)
        result = client.send_comment(BVID, "summary", execute=True)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "missing_rpid")
        self.assertEqual(result["rpid"], 0)
        self.assertEqual(result["msg_key"], "mk")

    def test_nonzero_code_is_not_accepted(self):
        session = FakeSession(post={"code": -412, "message": "risk control", "data": {"rpid": RPID}})
        client = BilibiliCommentClient(cookies=logged_in_cookies(), session=session)
        result = client.send_comment(BVID, "summary", execute=True)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "api_rejected")
        self.assertEqual(result["code"], -412)
        self.assertEqual(result["message"], "risk control")

    def test_delete_comment_is_explicit_and_requires_code_zero(self):
        session = FakeSession(post={"code": 0, "data": {}})
        client = BilibiliCommentClient(cookies=logged_in_cookies(), session=session)
        dry = client.delete_comment(BVID, RPID)
        self.assertEqual(dry["status"], "dry_run")
        self.assertFalse(session.post_calls)
        result = client.delete_comment(BVID, RPID, execute=True)
        self.assertTrue(result["deleted"])
        self.assertEqual(result["status"], "deleted")
        form = session.post_calls[0][1]["data"]
        self.assertEqual(form["oid"], str(AID))
        self.assertEqual(form["rpid"], str(RPID))
        self.assertEqual(form["csrf"], "csrf-token")

    def test_http_error_is_not_accepted_even_with_code_zero_and_rpid(self):
        session = FakeSession()
        client = BilibiliCommentClient(cookies=logged_in_cookies(), session=session)

        class HttpErrorSession(FakeSession):
            def post(self, url, **kwargs):
                self.post_calls.append((url, kwargs))
                return FakeResponse(403, {"code": 0, "data": {"rpid": RPID}})

        client.session = HttpErrorSession()
        result = client.send_comment(BVID, "summary", execute=True)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "http_error")


class ReadbackTests(unittest.TestCase):
    def test_readback_finds_nested_visible_reply(self):
        replies = {
            "code": 0,
            "data": {
                "replies": [
                    {
                        "rpid": 999,
                        "content": {"message": "other"},
                        "member": {"mid": "1", "uname": "other"},
                        "state": 0,
                    },
                    {
                        "rpid": 111,
                        "content": {"message": "root"},
                        "member": {"mid": "2", "uname": "root"},
                        "state": 0,
                        "replies": [
                            {
                                "rpid": RPID,
                                "content": {"message": "hello"},
                                "member": {"mid": "1000000003", "uname": "target"},
                                "state": 0,
                            }
                        ],
                    },
                ]
            },
        }
        session = FakeSession(replies=replies)
        client = BilibiliCommentClient(cookies=logged_in_cookies(), session=session)
        result = client.verify_comment_visible(BVID, RPID)
        self.assertTrue(result["read_only"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["visible"])
        self.assertEqual(result["status"], "visible")
        self.assertEqual(result["reply"]["content"], "hello")
        self.assertFalse(session.post_calls)

    def test_readback_not_found_is_not_reported_as_absent(self):
        session = FakeSession(replies={"code": 0, "data": {"replies": []}})
        client = BilibiliCommentClient(cookies=logged_in_cookies(), session=session)
        result = client.verify_comment_visible(BVID, RPID)
        self.assertFalse(result["verified"])
        self.assertFalse(result["found"])
        self.assertEqual(result["status"], "not_found_on_page")
        self.assertFalse(session.post_calls)


if __name__ == "__main__":
    unittest.main()
