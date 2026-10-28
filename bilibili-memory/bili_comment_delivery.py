from __future__ import annotations

"""Isolated Bilibili video-comment delivery.

This module never sends a comment unless ``execute=True`` is explicitly passed.
Even an accepted API response is not treated as proof that a comment is publicly
visible; callers must use ``verify_comment_visible`` for a read-only check.
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = ROOT / "storage_state.json"
VIEW_ENDPOINT = "https://api.bilibili.com/x/web-interface/view"
REPLY_ADD_ENDPOINT = "https://api.bilibili.com/x/v2/reply/add"
REPLY_LIST_ENDPOINT = "https://api.bilibili.com/x/v2/reply"
REPLY_DELETE_ENDPOINT = "https://api.bilibili.com/x/v2/reply/del"
DEFAULT_MENTION_NAME = "\u6ee8\u6ee8QAQ"
DEFAULT_MENTION_UID = "1000000003"
BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10,}$")
REQUIRED_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class CookieStateError(ValueError):
    """Raised when the stored Bilibili login is missing required cookies."""


class BilibiliApiError(RuntimeError):
    """Raised when a Bilibili API response cannot be used safely."""

    def __init__(
        self,
        message: str,
        *,
        code: Any = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _is_bilibili_domain(domain: str) -> bool:
    normalized = domain.strip().lower().lstrip(".")
    return normalized == "bilibili.com" or normalized.endswith(".bilibili.com")


def load_main_site_cookies(
    state_path: str | Path = DEFAULT_STATE_PATH,
    *,
    require_login: bool = True,
    now: float | None = None,
) -> dict[str, str]:
    """Read only Bilibili main-site cookies from Playwright storage state.

    Cookies for unrelated Bilibili subdomains (for example biligame.com or
    huasheng.cn) are deliberately ignored. The returned mapping and errors never
    include cookie values.
    """

    path = Path(state_path)
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise CookieStateError(f"storage state does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise CookieStateError(f"cannot read storage state: {path}") from error

    if not isinstance(state, dict):
        raise CookieStateError("storage state root must be an object")

    current_time = time.time() if now is None else float(now)
    cookies: dict[str, str] = {}
    for item in state.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        value = item.get("value")
        domain = str(item.get("domain") or "")
        if not name or value is None or not _is_bilibili_domain(domain):
            continue
        try:
            expires = float(item.get("expires") or -1)
        except (TypeError, ValueError):
            expires = -1
        if expires > 0 and expires <= current_time:
            continue
        cookies[name] = str(value)

    if require_login:
        missing = [name for name in REQUIRED_COOKIES if not cookies.get(name)]
        if missing:
            raise CookieStateError(
                "main-site login cookies are incomplete: " + ", ".join(missing)
            )
    return cookies


def cookie_header(cookies: Mapping[str, str]) -> str:
    """Build a deterministic Cookie header without logging its contents."""

    preferred = [
        "SESSDATA",
        "bili_jct",
        "buvid3",
        "DedeUserID",
        "DedeUserID__ckMd5",
        "buvid4",
        "b_nut",
        "bili_ticket",
        "_uuid",
    ]
    names = [name for name in preferred if cookies.get(name)]
    names.extend(sorted(name for name in cookies if name not in names and cookies.get(name)))
    return "; ".join(f"{name}={cookies[name]}" for name in names)


def _request_headers(cookies: Mapping[str, str], referer: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Cookie": cookie_header(cookies),
    }


def _response_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as error:
        raise BilibiliApiError(
            "Bilibili returned a non-JSON response",
            http_status=int(getattr(response, "status_code", 0) or 0),
        ) from error
    if not isinstance(payload, dict):
        raise BilibiliApiError(
            "Bilibili returned an unexpected JSON root",
            http_status=int(getattr(response, "status_code", 0) or 0),
        )
    return payload


def _http_ok(response: Any) -> bool:
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return False
    return 200 <= status < 300


def _api_message(payload: Mapping[str, Any]) -> str:
    return str(payload.get("message") or payload.get("msg") or "")


def _normalize_rpid(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_bvid(bvid: str) -> str:
    value = str(bvid or "").strip()
    if value.lower().startswith("bv"):
        value = "BV" + value[2:]
    if not BVID_RE.fullmatch(value):
        raise ValueError("bvid must be a bare BV identifier such as BV1xx411c7mD")
    return value


def build_comment_text(
    body: str,
    *,
    mention_name: str = DEFAULT_MENTION_NAME,
    mention_names: list[str] | None = None,
) -> str:
    """Return a comment body with a leading set of @mentions."""

    text = str(body or "").strip()
    if not text:
        raise ValueError("comment body must not be empty")
    names = mention_names if mention_names is not None else [mention_name]
    cleaned = []
    for value in names:
        name = str(value or "").strip().lstrip("@")
        if name and name not in cleaned:
            cleaned.append(name)
    cleaned = cleaned[:5]
    if not cleaned:
        raise ValueError("mention name must not be empty")
    mention = " ".join(f"@{name}" for name in cleaned)
    if text.startswith("@"):
        return text
    return f"{mention} {text}"


def resolve_video_aid(
    session: Any,
    cookies: Mapping[str, str],
    bvid: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Resolve BVID to aid using the read-only video-view endpoint."""

    normalized_bvid = normalize_bvid(bvid)
    try:
        response = session.get(
            VIEW_ENDPOINT,
            params={"bvid": normalized_bvid},
            headers=_request_headers(
                cookies, f"https://www.bilibili.com/video/{normalized_bvid}/"
            ),
            timeout=timeout,
        )
    except Exception as error:
        raise BilibiliApiError(f"video lookup transport error: {error}") from error

    payload = _response_payload(response)
    code = payload.get("code")
    http_status = int(getattr(response, "status_code", 0) or 0)
    if not _http_ok(response) or code != 0:
        raise BilibiliApiError(
            f"video lookup failed: code={code} message={_api_message(payload)}",
            code=code,
            http_status=http_status,
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise BilibiliApiError(
            "video lookup returned no data", code=code, http_status=http_status
        )
    aid = _normalize_rpid(data.get("aid"))
    if aid <= 0:
        raise BilibiliApiError(
            "video lookup returned no valid aid", code=code, http_status=http_status
        )
    return {
        "bvid": normalized_bvid,
        "aid": aid,
        "cid": _normalize_rpid(data.get("cid")),
        "title": str(data.get("title") or ""),
        "owner": str((data.get("owner") or {}).get("name") or ""),
        "http_status": http_status,
        "code": code,
    }


def _reply_summary(reply: Mapping[str, Any]) -> dict[str, Any]:
    content = reply.get("content")
    message = str(content.get("message") or "") if isinstance(content, dict) else ""
    member = reply.get("member")
    member_data = member if isinstance(member, dict) else {}
    content_data = content if isinstance(content, dict) else {}
    mentioned_users = []
    for item in content_data.get("members") or []:
        if isinstance(item, dict) and item.get("mid"):
            mentioned_users.append({"mid": str(item.get("mid")), "name": str(item.get("uname") or item.get("name") or "")})
    state = reply.get("state")
    try:
        state_value = int(state) if state is not None else 0
    except (TypeError, ValueError):
        state_value = -1
    return {
        "rpid": _normalize_rpid(reply.get("rpid")),
        "rpid_str": str(reply.get("rpid_str") or reply.get("rpid") or ""),
        "content": message,
        "mentioned_users": mentioned_users,
        "at_name_to_mid": content_data.get("at_name_to_mid"),
        "member_mid": str(member_data.get("mid") or ""),
        "member_name": str(member_data.get("uname") or ""),
        "state": state_value,
        "visible": bool(message) and state_value in (0, None),
        "ctime": _normalize_rpid(reply.get("ctime")),
    }


def _find_reply(replies: Any, rpid: int) -> dict[str, Any] | None:
    if not isinstance(replies, list):
        return None
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        if _normalize_rpid(reply.get("rpid")) == rpid:
            return _reply_summary(reply)
        nested = _find_reply(reply.get("replies"), rpid)
        if nested is not None:
            return nested
    return None


class BilibiliCommentClient:
    """Safety-gated Bilibili comment delivery and read-only visibility check."""

    def __init__(
        self,
        *,
        storage_state_path: str | Path = DEFAULT_STATE_PATH,
        cookies: Mapping[str, str] | None = None,
        session: Any | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.cookies = dict(
            cookies if cookies is not None else load_main_site_cookies(storage_state_path)
        )
        self.session = session if session is not None else requests.Session()
        self.timeout = max(1.0, float(timeout))

    def resolve_aid(self, bvid: str) -> dict[str, Any]:
        return resolve_video_aid(
            self.session,
            self.cookies,
            bvid,
            timeout=self.timeout,
        )

    def send_comment(
        self,
        bvid: str,
        body: str,
        *,
        mention_name: str = DEFAULT_MENTION_NAME,
        mention_uid: str = DEFAULT_MENTION_UID,
        mention_accounts: list[Mapping[str, Any]] | None = None,
        execute: bool = False,
    ) -> dict[str, Any]:
        """Prepare or send one top-level video comment.

        ``execute`` defaults to False. A dry-run result is never marked accepted.
        A real post is accepted only when HTTP succeeds, API code is exactly 0,
        and a non-zero rpid is returned.
        """

        result: dict[str, Any] = {
            "accepted": False,
            "post_attempted": False,
            "status": "not_started",
            "bvid": str(bvid or ""),
            "aid": 0,
            "rpid": 0,
            "rpid_str": "",
            "msg_key": "",
            "http_status": None,
            "code": None,
            "message": "",
            "comment_text": "",
            "request_preview": None,
        }
        mention_pairs: list[tuple[str, str]] = []
        if mention_accounts:
            for account in mention_accounts:
                name = str(account.get("name") or account.get("label") or "").strip().lstrip("@")
                uid = str(account.get("uid") or account.get("mid") or "").strip()
                if name and uid and (name, uid) not in mention_pairs:
                    mention_pairs.append((name, uid))
        if not mention_pairs:
            clean_name = str(mention_name or "").strip().lstrip("@")
            clean_uid = str(mention_uid or "").strip()
            if clean_name:
                mention_pairs.append((clean_name, clean_uid))
        mention_pairs = mention_pairs[:5]
        try:
            comment_text = build_comment_text(body, mention_names=[name for name, _ in mention_pairs])
        except ValueError as error:
            result.update(status="validation_error", message=str(error))
            return result

        try:
            video = self.resolve_aid(bvid)
        except (BilibiliApiError, ValueError) as error:
            result.update(
                status="aid_lookup_failed",
                code=getattr(error, "code", None),
                http_status=getattr(error, "http_status", None),
                message=str(error),
            )
            return result

        aid = int(video["aid"])
        normalized_bvid = str(video["bvid"])
        form: dict[str, str] = {
            "type": "1",
            "oid": str(aid),
            "message": comment_text,
            "plat": "1",
            "csrf": str(self.cookies.get("bili_jct") or ""),
        }
        mention_mapping = {name: uid for name, uid in mention_pairs if uid}
        if mention_mapping:
            form["at_name_to_mid"] = json.dumps(mention_mapping, ensure_ascii=False)

        result.update(
            bvid=normalized_bvid,
            aid=aid,
            comment_text=comment_text,
            request_preview={
                "method": "POST",
                "endpoint": REPLY_ADD_ENDPOINT,
                "form": {
                    "type": form["type"],
                    "oid": form["oid"],
                    "message": form["message"],
                    "plat": form["plat"],
                    "at_name_to_mid": form.get("at_name_to_mid", ""),
                },
            },
        )
        if not execute:
            result.update(status="dry_run")
            return result

        result["post_attempted"] = True
        try:
            response = self.session.post(
                REPLY_ADD_ENDPOINT,
                data=form,
                headers=_request_headers(
                    self.cookies,
                    f"https://www.bilibili.com/video/{normalized_bvid}/",
                ),
                timeout=self.timeout,
            )
        except Exception as error:
            result.update(status="transport_error", message=str(error))
            return result

        try:
            payload = _response_payload(response)
        except BilibiliApiError as error:
            result.update(
                status="invalid_response",
                http_status=error.http_status,
                message=str(error),
            )
            return result

        http_status = int(getattr(response, "status_code", 0) or 0)
        code = payload.get("code")
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        rpid = _normalize_rpid(data.get("rpid"))
        rpid_str = str(data.get("rpid_str") or (rpid if rpid else ""))
        msg_key = str(payload.get("msg_key") or data.get("msg_key") or "")
        http_ok = _http_ok(response)
        accepted = bool(http_ok and code == 0 and rpid > 0)

        if accepted:
            status = "api_accepted"
        elif http_ok and code == 0:
            status = "missing_rpid"
        elif not http_ok:
            status = "http_error"
        else:
            status = "api_rejected"

        result.update(
            accepted=accepted,
            status=status,
            rpid=rpid,
            rpid_str=rpid_str,
            msg_key=msg_key,
            http_status=http_status,
            code=code,
            message=_api_message(payload),
            api_data={
                "dialog": data.get("dialog"),
                "root": data.get("root"),
                "parent": data.get("parent"),
                "ctime": data.get("ctime"),
                "action": data.get("action"),
                "rpid": data.get("rpid"),
                "rpid_str": data.get("rpid_str"),
                "msg_key": data.get("msg_key"),
            },
        )
        return result

    def delete_comment(self, bvid: str, rpid: int | str, *, execute: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "deleted": False,
            "delete_attempted": False,
            "status": "not_started",
            "bvid": str(bvid or ""),
            "aid": 0,
            "rpid": _normalize_rpid(rpid),
            "http_status": None,
            "code": None,
            "message": "",
        }
        target_rpid = _normalize_rpid(rpid)
        if target_rpid <= 0:
            result.update(status="validation_error", message="rpid must be positive")
            return result
        try:
            video = self.resolve_aid(bvid)
        except (BilibiliApiError, ValueError) as error:
            result.update(status="aid_lookup_failed", message=str(error))
            return result
        aid = int(video["aid"])
        normalized_bvid = str(video["bvid"])
        form = {
            "type": "1",
            "oid": str(aid),
            "rpid": str(target_rpid),
            "csrf": str(self.cookies.get("bili_jct") or ""),
        }
        result.update(bvid=normalized_bvid, aid=aid)
        if not execute:
            result.update(status="dry_run")
            return result
        result["delete_attempted"] = True
        try:
            response = self.session.post(
                REPLY_DELETE_ENDPOINT,
                data=form,
                headers=_request_headers(self.cookies, f"https://www.bilibili.com/video/{normalized_bvid}/"),
                timeout=self.timeout,
            )
        except Exception as error:
            result.update(status="transport_error", message=str(error))
            return result
        try:
            payload = _response_payload(response)
        except BilibiliApiError as error:
            result.update(status="invalid_response", http_status=error.http_status, message=str(error))
            return result
        http_status = int(getattr(response, "status_code", 0) or 0)
        code = payload.get("code")
        deleted = bool(_http_ok(response) and code == 0)
        result.update(
            deleted=deleted,
            status="deleted" if deleted else ("http_error" if not _http_ok(response) else "api_rejected"),
            http_status=http_status,
            code=code,
            message=_api_message(payload),
        )
        return result

    def verify_comment_visible(
        self,
        bvid: str,
        rpid: int | str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: int = 0,
    ) -> dict[str, Any]:
        """Read a comment list and look for rpid without modifying anything.

        Absence from the requested page is reported as ``not_found_on_page``;
        it is not evidence that the comment was not posted.
        """

        target_rpid = _normalize_rpid(rpid)
        result: dict[str, Any] = {
            "read_only": True,
            "verified": False,
            "found": False,
            "visible": False,
            "status": "not_started",
            "bvid": str(bvid or ""),
            "aid": 0,
            "rpid": target_rpid,
            "http_status": None,
            "code": None,
            "message": "",
            "reply": None,
            "page": max(1, int(page)),
            "page_size": max(1, min(20, int(page_size))),
        }
        if target_rpid <= 0:
            result.update(status="validation_error", message="rpid must be positive")
            return result

        try:
            video = self.resolve_aid(bvid)
        except (BilibiliApiError, ValueError) as error:
            result.update(
                status="aid_lookup_failed",
                code=getattr(error, "code", None),
                http_status=getattr(error, "http_status", None),
                message=str(error),
            )
            return result

        aid = int(video["aid"])
        normalized_bvid = str(video["bvid"])
        result.update(bvid=normalized_bvid, aid=aid)
        try:
            response = self.session.get(
                REPLY_LIST_ENDPOINT,
                params={
                    "type": 1,
                    "oid": aid,
                    "sort": int(sort),
                    "pn": result["page"],
                    "ps": result["page_size"],
                },
                headers=_request_headers(
                    self.cookies,
                    f"https://www.bilibili.com/video/{normalized_bvid}/",
                ),
                timeout=self.timeout,
            )
        except Exception as error:
            result.update(status="transport_error", message=str(error))
            return result

        try:
            payload = _response_payload(response)
        except BilibiliApiError as error:
            result.update(
                status="invalid_response",
                http_status=error.http_status,
                message=str(error),
            )
            return result

        http_status = int(getattr(response, "status_code", 0) or 0)
        code = payload.get("code")
        result.update(
            http_status=http_status,
            code=code,
            message=_api_message(payload),
        )
        if not _http_ok(response) or code != 0:
            result["status"] = "api_error"
            return result

        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        found = _find_reply(data.get("replies"), target_rpid)
        if found is None:
            found = _find_reply(data.get("top_replies"), target_rpid)

        if found is None:
            result["status"] = "not_found_on_page"
            return result

        result.update(
            found=True,
            visible=bool(found["visible"]),
            verified=bool(found["visible"]),
            status="visible" if found["visible"] else "present_but_not_confirmed",
            reply=found,
        )
        return result
