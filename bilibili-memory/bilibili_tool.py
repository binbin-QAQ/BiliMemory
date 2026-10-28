from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import random
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
PROFILE_DIR = ROOT / "browser-profile"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
from runtime_paths import MAIBOT_ROOT
MODEL_CONFIG = MAIBOT_ROOT / "config" / "model_config.toml"
WEBUI_CONFIG = MAIBOT_ROOT / "data" / "webui.json"
WEBUI_BASE = "http://127.0.0.1:8001"
EMBEDDING_HOST = "127.0.0.1"
EMBEDDING_PORT = 11435


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    return {}


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def bvid_from_url(url: str) -> str | None:
    match = re.search(r"(BV[0-9A-Za-z]{10,})", url)
    return match.group(1) if match else None


def extract_bgm_name(data: dict[str, Any], description: str) -> str:
    for key in ("bgm", "music", "music_info", "background_music"):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("title") or value.get("name") or value.get("song")
        if isinstance(value, list) and value:
            value = value[0]
            if isinstance(value, dict):
                value = value.get("title") or value.get("name") or value.get("song")
        text = str(value or "").strip()
        if text:
            return text[:180]
    patterns = [
        r"(?im)^\s*BGM\s*[:\uff1a]\s*(.+)$",
        r"(?im)^\s*\u80cc\u666f\u97f3\u4e50\s*[:\uff1a]\s*(.+)$",
        r"(?im)^\s*\u914d\u4e50\s*[:\uff1a]\s*(.+)$",
        r"(?im)^\s*\u97f3\u4e50\s*[:\uff1a]\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, description)
        if match:
            return match.group(1).strip()[:180]
    return ""


def assess_description(description: str, title: str = "") -> dict[str, Any]:
    raw = html.unescape(str(description or "")).strip()
    if not raw:
        return {"meaningful": False, "reason": "empty", "text": "", "raw_length": 0}
    text = re.sub(r"https?://\S+", " ", raw, flags=re.I)
    text = re.sub(r"(?<!\w)@[A-Za-z0-9_\-\u4e00-\u9fff]{1,40}", " ", text)
    text = re.sub(r"#[^#\s]{1,60}#?", " ", text)
    generic_patterns = (
        r"^(?:bgm|background music|\u80cc\u666f\u97f3\u4e50|\u914d\u4e50)\s*[:\uff1a]",
        r"^(?:\u611f\u8c22|\u8c22\u8c22)(?:\u89c2\u770b|\u5173\u6ce8|\u652f\u6301|\u5927\u5bb6)",
        r"^(?:\u70b9\u8d5e|\u6295\u5e01|\u6536\u85cf|\u5173\u6ce8|\u4e00\u952e\u4e09\u8fde|\u4e09\u8fde)",
        r"(?:\u672a\u7ecf\u6388\u6743|\u7981\u6b62\u8f6c\u8f7d|\u4fb5\u6743|\u5220\u9664|\u4fb5\u5220)",
        r"(?:\u5546\u52a1\u5408\u4f5c|\u7c89\u4e1d\u7fa4|\u4ea4\u6d41\u7fa4|qq|vx|\u5fae\u4fe1)",
        r"(?:\u672c\u89c6\u9891|\u672c\u4f5c\u54c1).{0,20}(?:\u4ec5\u4f9b|\u7981\u6b62|\u58f0\u660e)",
        r"(?:copyright|all rights reserved)",
    )
    lines = []
    for line in re.split(r"[\r\n]+", text):
        clean = re.sub(r"\s+", " ", line).strip(" \t-\u2014_|:\uff1a,\uff0c.\u3002")
        if not clean:
            continue
        if any(re.search(pattern, clean, re.I) for pattern in generic_patterns):
            continue
        lines.append(clean)
    effective = " ".join(lines).strip()
    compact = "".join(ch for ch in effective.casefold() if ch.isalnum())
    title_compact = "".join(ch for ch in str(title or "").casefold() if ch.isalnum())
    if not compact:
        return {"meaningful": False, "reason": "boilerplate_only", "text": "", "raw_length": len(raw)}
    if title_compact and compact == title_compact:
        return {"meaningful": False, "reason": "repeats_title", "text": "", "raw_length": len(raw)}
    if len(compact) < 8:
        return {"meaningful": False, "reason": "too_short", "text": effective, "raw_length": len(raw)}
    if len(compact) >= 24 and len(set(compact)) <= 3:
        return {"meaningful": False, "reason": "repetitive", "text": effective, "raw_length": len(raw)}
    return {"meaningful": True, "reason": "meaningful", "text": effective[:1200], "raw_length": len(raw)}


def page_from_url(url: str) -> int:
    try:
        value = parse_qs(urlparse(url).query).get("p", ["1"])[0]
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def duration_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return 0
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return 0


def launch_context(playwright: Any, headless: bool):
    channel = load_config().get("browser_channel", "msedge")
    return playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        channel=channel,
        headless=headless,
        viewport={"width": 1440, "height": 960},
        locale="zh-CN",
        args=["--disable-blink-features=AutomationControlled"],
    )


def cookie_map(context: Any) -> dict[str, str]:
    return {cookie["name"]: cookie["value"] for cookie in context.cookies("https://www.bilibili.com")}


def ensure_login(context: Any, *, wait_seconds: int = 600) -> bool:
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    deadline = time.time() + wait_seconds
    last_notice = 0.0
    while time.time() < deadline:
        cookies = cookie_map(context)
        if cookies.get("SESSDATA") and cookies.get("DedeUserID"):
            try:
                response = context.request.get(
                    "https://api.bilibili.com/x/web-interface/nav",
                    headers={"Referer": "https://www.bilibili.com/"},
                    timeout=20000,
                )
                data = response.json()
                payload = data.get("data") or {}
                if data.get("code") in (0, None) and payload.get("isLogin"):
                    state_path = ROOT / "storage_state.json"
                    context.storage_state(path=str(state_path))
                    log(f"B\u7ad9\u767b\u5f55\u6210\u529f\uff0c\u5df2\u9a8c\u8bc1 UID {payload.get('mid')}\uff0cCookie \u5df2\u4fdd\u5b58\u3002")
                    return True
            except Exception:
                pass
        if time.time() - last_notice >= 15:
            log("B\u7ad9\u5c1a\u672a\u767b\u5f55\uff1a\u8bf7\u5728 Edge \u4e2d\u626b\u7801\u5e76\u5b8c\u6210\u786e\u8ba4\u3002")
            last_notice = time.time()
        page.wait_for_timeout(2000)
    log("B\u7ad9\u767b\u5f55\u7b49\u5f85\u8d85\u65f6\uff0c\u672a\u83b7\u5f97\u6709\u6548\u767b\u5f55\u72b6\u6001\u3002")
    return False


def api_get(context: Any, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = context.request.get(
        url,
        params=params,
        headers={"Referer": "https://www.bilibili.com/"},
        timeout=45000,
    )
    if not response.ok:
        raise RuntimeError(f"B站接口 HTTP {response.status}: {response.status_text}")
    payload = response.json()
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"B站接口错误 {payload.get('code')}: {payload.get('message')}")
    return payload



def wbi_signed_params(context: Any, params: dict[str, Any]) -> dict[str, str]:
    nav_response = context.request.get(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"Referer": "https://www.bilibili.com/"},
        timeout=60000,
    )
    nav_payload = nav_response.json()
    nav_data = nav_payload.get("data") or {}
    img_url = str((nav_data.get("wbi_img") or {}).get("img_url") or "")
    sub_url = str((nav_data.get("wbi_img") or {}).get("sub_url") or "")
    source = Path(img_url).stem + Path(sub_url).stem
    tab = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]
    mixin_key = "".join(source[index] for index in tab)[:32]
    clean = {str(key): "".join(ch for ch in str(value) if ch not in "!'()*") for key, value in params.items()}
    clean["wts"] = str(int(time.time()))
    query = urlencode(sorted(clean.items()))
    clean["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return clean

def subtitle_is_ai(item: dict[str, Any]) -> bool:
    kind = str(item.get("_subtitle_kind") or "").lower()
    lan = str(item.get("lan") or "").lower()
    doc = str(item.get("lan_doc") or "").lower()
    return kind == "ai" or lan.startswith("ai") or "ai" in lan or "\u4eba\u5de5\u667a\u80fd" in doc or "\u81ea\u52a8\u5b57\u5e55" in doc


def ordered_subtitle_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    embedded = [item for item in candidates if not subtitle_is_ai(item)]
    ai = [item for item in candidates if subtitle_is_ai(item)]
    def language_key(item: dict[str, Any]) -> tuple[int, str]:
        lan = str(item.get("lan") or "").lower()
        return (0 if lan.startswith("zh") else 1, lan)
    return sorted(embedded, key=language_key) + sorted(ai, key=language_key)


def merge_subtitle_candidates(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            url = str(item.get("subtitle_url") or item.get("url") or "")
            if not url:
                continue
            existing = merged.get(url)
            if existing is None or (subtitle_is_ai(existing) and not subtitle_is_ai(item)):
                merged[url] = item
    return list(merged.values())


def fetch_subtitle_result(context: Any, bvid: str, aid: int, cid: int) -> tuple[str, str]:
    def candidates_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
        root = ((payload.get("data") or {}).get("subtitle") or {})
        values = []
        for key in ("subtitles", "ai_subtitles", "ai_subtitle"):
            value = root.get(key) or []
            if isinstance(value, dict):
                value = [value]
            kind = "ai" if "ai" in key else "embedded"
            for item in value:
                if isinstance(item, dict):
                    copy = dict(item)
                    copy["_subtitle_kind"] = kind
                    values.append(copy)
        deduped = []
        seen_urls = set()
        for item in values:
            url = str(item.get("subtitle_url") or item.get("url") or "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                deduped.append(item)
        return deduped

    player = api_get(
        context,
        "https://api.bilibili.com/x/player/v2",
        {"bvid": bvid, "cid": cid, "aid": aid},
    )
    subtitles = candidates_from(player)
    method = "player/v2"
    if not any(not subtitle_is_ai(item) for item in subtitles):
        try:
            nav_response = context.request.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers={"Referer": "https://www.bilibili.com/"},
                timeout=60000,
            )
            nav_payload = nav_response.json()
            nav_data = nav_payload.get("data") or {}
            img_url = str((nav_data.get("wbi_img") or {}).get("img_url") or "")
            sub_url = str((nav_data.get("wbi_img") or {}).get("sub_url") or "")
            source = Path(img_url).stem + Path(sub_url).stem
            tab = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]
            mixin_key = ''.join(source[index] for index in tab)[:32]
            params = {"bvid": bvid, "aid": str(aid), "cid": str(cid), "wts": str(int(time.time()))}
            clean = {key: ''.join(ch for ch in str(value) if ch not in "!'()*") for key, value in params.items()}
            query = urlencode(sorted(clean.items()))
            signed = dict(clean)
            signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
            wbi_response = context.request.get(
                "https://api.bilibili.com/x/player/wbi/v2",
                params=signed,
                headers={"Referer": "https://www.bilibili.com/"},
                timeout=60000,
            )
            wbi_payload = wbi_response.json()
            if wbi_response.ok and wbi_payload.get("code") == 0:
                subtitles = merge_subtitle_candidates(subtitles, candidates_from(wbi_payload))
                method = "player/v2+wbi/v2"
        except Exception as error:
            log(f"Bilibili WBI subtitle fallback failed: {error}")
    embedded_count = sum(1 for item in subtitles if not subtitle_is_ai(item))
    ai_count = sum(1 for item in subtitles if subtitle_is_ai(item))
    log(f"Bilibili subtitle candidates: {len(subtitles)} embedded={embedded_count} ai={ai_count} via {method}")
    if not subtitles:
        return "", "none"
    for preferred in ordered_subtitle_candidates(subtitles):
        subtitle_url = str(preferred.get("subtitle_url") or preferred.get("url") or "")
        kind = "ai" if subtitle_is_ai(preferred) else "embedded"
        kind_label = "AI\u5b57\u5e55" if kind == "ai" else "\u5185\u5d4c\u5b57\u5e55"
        log(f"Trying Bilibili subtitle: kind={kind_label} lan={preferred.get('lan')} doc={preferred.get('lan_doc')}")
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        if not subtitle_url:
            continue
        try:
            payload = api_get(context, subtitle_url)
            body = (payload.get("data") or payload).get("body") or []
            text = "".join(str(part.get("content", "")) for part in body if isinstance(part, dict)).strip()
            if text:
                log(f"Selected Bilibili subtitle: kind={kind_label} lan={preferred.get('lan')} doc={preferred.get('lan_doc')} chars={len(text)}")
                return text, kind
            log(f"Bilibili subtitle was empty: kind={kind_label} lan={preferred.get('lan')}")
        except Exception as error:
            log(f"Bilibili subtitle fetch failed: kind={kind_label} lan={preferred.get('lan')} error={error}")
    return "", "none"


def fetch_subtitle(context: Any, bvid: str, aid: int, cid: int) -> str:
    return fetch_subtitle_result(context, bvid, aid, cid)[0]


def fetch_top_comments(context: Any, aid: int, limit: int = 12) -> list[str]:
    try:
        payload = api_get(
            context,
            "https://api.bilibili.com/x/v2/reply",
            {"type": 1, "oid": aid, "sort": 2, "ps": limit, "pn": 1},
        )
        replies = (payload.get("data") or {}).get("replies") or []
        return [str(item.get("content", {}).get("message", "")).strip() for item in replies if item.get("content")]
    except Exception:
        return []


def fetch_music_tag(context: Any, bvid: str) -> dict[str, str]:
    try:
        payload = api_get(context, "https://api.bilibili.com/x/tag/archive/tags", {"bvid": bvid})
        data = payload.get("data") or []
        if isinstance(data, dict):
            data = data.get("tags") or []
        for item in data:
            if not isinstance(item, dict):
                continue
            music_id = str(item.get("music_id") or "").strip()
            tag_name = str(item.get("tag_name") or "").strip()
            if music_id:
                match = re.search(r"\u300a(.+?)\u300b", tag_name)
                name = match.group(1).strip() if match else tag_name
                return {"id": music_id, "name": name}
    except Exception:
        return {}
    return {}


def fetch_page_music_tag(context: Any, bvid: str) -> dict[str, str]:
    page = None
    try:
        page = context.new_page()
        page.goto(f"https://www.bilibili.com/video/{bvid}/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
        result = page.evaluate("""
            () => {
              const tags = (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.tags) || [];
              const tag = tags.find(item => item && item.music_id);
              return tag ? {id: String(tag.music_id || ''), tag_name: String(tag.tag_name || '')} : {};
            }
        """)
        music_id = str((result or {}).get("id") or "").strip()
        tag_name = str((result or {}).get("tag_name") or "").strip()
        if not music_id:
            return {}
        match = re.search(r"\u300a(.+?)\u300b", tag_name)
        name = match.group(1).strip() if match else tag_name
        log(f"Bilibili music tag detected: id={music_id} name={name}")
        return {"id": music_id, "name": name}
    except Exception as error:
        log(f"Bilibili music tag detection failed: {error}")
        return {}
    finally:
        if page:
            page.close()


def fetch_music_detail(context: Any, music_id: str, aid: int = 0, cid: int = 0, bvid: str = "") -> dict[str, str]:
    music_id = str(music_id or "").strip()
    if not music_id:
        return {}
    try:
        response = context.request.get(
            "https://api.bilibili.com/x/copyright-music-publicity/bgm/detail",
            params={
                "music_id": music_id,
                "from_aid": int(aid or 0),
                "from_cid": int(cid or 0),
                "position_id": 1,
                "source": 1,
            },
            headers={"Referer": f"https://www.bilibili.com/video/{bvid}/" if bvid else "https://www.bilibili.com/"},
            timeout=30000,
        )
        payload = response.json()
        data = payload.get("data") or {}
        if payload.get("code") not in (0, None) or not isinstance(data, dict):
            log(f"Bilibili music detail unavailable: code={payload.get('code')} message={payload.get('message')}")
            return {}
        title = str(data.get("music_title") or "").strip()[:180]
        artist = str(data.get("origin_artist") or data.get("origin_artist_list") or "").strip()
        artist = re.sub(r"\s*_\s*(?:Crypton|VOCALOID)\s*$", "", artist, flags=re.I)
        artist = re.sub(r"\s*,\s*", "\u3001", artist)[:240]
        result = {}
        if title:
            result["title"] = title
        if artist:
            result["artist"] = artist
        if result:
            log(f"Bilibili music detail: title={title or '-'} original_artist={artist or '-'}")
        return result
    except Exception as error:
        log(f"Bilibili music detail failed: {error}")
        return {}


def fetch_video_tags(context: Any, bvid: str) -> list[str]:
    try:
        payload = api_get(context, "https://api.bilibili.com/x/tag/archive/tags", {"bvid": bvid})
        data = payload.get("data") or []
        if isinstance(data, dict):
            data = data.get("tags") or []
        return [str(item.get("tag_name") or "").strip() for item in data if isinstance(item, dict) and item.get("tag_name")]
    except Exception:
        return []


def keyword_hit(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if re.fullmatch(r"[A-Za-z0-9+#.]+", keyword):
        return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])", text, re.IGNORECASE))
    return keyword.casefold() in text.casefold()


def matches_tag_filter(video: dict[str, Any], keywords: list[str]) -> bool:
    if not keywords:
        return True
    text = " ".join([
        str(video.get("title") or ""),
        str(video.get("description") or ""),
        " ".join(str(item) for item in (video.get("tags") or [])),
    ])
    return any(keyword_hit(text, keyword) for keyword in keywords)


def animation_content_mismatch(video: dict[str, Any], query_term: str) -> str:
    query = str(query_term or "").casefold()
    animation_markers = ("\u52a8\u753b", "\u624b\u4e66", "\u52a8\u6f2b", "amv", "mad", "mmd")
    if not any(marker.casefold() in query for marker in animation_markers):
        return ""
    title = str(video.get("title") or "")
    title_gameplay_markers = ("\u5b9e\u673a", "\u6218\u6597\u6f14\u793a", "\u653b\u7565", "\u6ee1\u661f", "\u6df1\u6e0a", "\u901f\u901a")
    title_hit = next((marker for marker in title_gameplay_markers if marker in title), "")
    visual = str(video.get("visual_analysis") or "")
    hud_markers = ("\u5c0f\u5730\u56fe", "\u6280\u80fd\u680f", "\u4efb\u52a1\u63d0\u793a", "Lv.", "\u72b6\u6001\u680f", "UID", "\u7279\u6218\u7801", "\u8840\u6761", "\u83b7\u5f97")
    visual_hits = [marker for marker in hud_markers if marker.casefold() in visual.casefold()]
    if title_hit:
        return f"gameplay title marker: {title_hit}"
    if len(visual_hits) >= 3:
        return "gameplay HUD markers: " + ",".join(visual_hits)
    return ""


def should_deliver_public_comment(manual_link: bool, publish_comment: bool, suppress_comment: bool) -> bool:
    return not suppress_comment and (manual_link or publish_comment)


def excluded_tag_hit(video: dict[str, Any], excluded_tags: list[str]) -> str:
    wanted = [str(tag).strip().casefold() for tag in excluded_tags if str(tag).strip()]
    if not wanted:
        return ""
    for tag in video.get("tags") or []:
        name = str(tag).strip().casefold()
        if name and any(excluded == name or excluded in name for excluded in wanted):
            return str(tag).strip()
    return ""


def collection_candidate_budget(target_count: int, config: dict[str, Any]) -> int:
    target = max(1, int(target_count))
    multiplier = max(1, int(config.get("candidate_pool_multiplier", 8)))
    maximum = max(target, int(config.get("max_candidate_pool", 60)))
    return min(maximum, max(target, target * multiplier))


def collection_success_count(accepted: int, imported: int, do_import: bool) -> int:
    return int(imported if do_import else accepted)


def collect_video(context: Any, bvid: str, config: dict[str, Any], include_content: bool = True, page_number: int = 1) -> dict[str, Any]:
    page_number = max(1, int(page_number or 1))
    url = f"https://www.bilibili.com/video/{bvid}/" + (f"?p={page_number}" if page_number > 1 else "")
    log(f"读取视频信息：{bvid}" + (f" P{page_number}" if page_number > 1 else ""))
    payload = api_get(context, "https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
    data = payload.get("data") or {}
    aid = int(data.get("aid") or 0)
    pages = data.get("pages") or []
    selected_page = pages[page_number - 1] if page_number <= len(pages) else (pages[0] if pages else {})
    cid = int(selected_page.get("cid") or data.get("cid") or 0)
    title = str(data.get("title") or bvid)
    part = str(selected_page.get("part") or "").strip()
    if page_number > 1 and part and part not in title:
        title = f"{title} · P{page_number} {part}"
    desc = str(data.get("desc") or "").strip()
    description_assessment = assess_description(desc, title)
    owner = str((data.get("owner") or {}).get("name") or "")
    duration = int(selected_page.get("duration") or data.get("duration") or 0)
    pubdate = int(data.get("pubdate") or 0)
    stat = data.get("stat") or {}
    tags = fetch_video_tags(context, bvid)
    music_tag = fetch_music_tag(context, bvid)
    if not music_tag:
        music_tag = fetch_page_music_tag(context, bvid)
    bgm_id = str(music_tag.get("id") or "").strip()
    music_detail = fetch_music_detail(context, bgm_id, aid, cid, bvid) if bgm_id else {}
    bgm = str(music_detail.get("title") or music_tag.get("name") or "").strip() or extract_bgm_name(data, desc)
    bgm_original_artist = str(music_detail.get("artist") or "").strip()
    subtitle = ""
    subtitle_kind = "none"
    comments: list[str] = []
    if include_content:
        subtitle, subtitle_kind = fetch_subtitle_result(context, bvid, aid, cid) if cid else ("", "none")
        if len(subtitle) > int(config.get("max_subtitle_chars_per_video", 14000)):
            subtitle = subtitle[: int(config.get("max_subtitle_chars_per_video", 14000))]
        comments = fetch_top_comments(context, aid) if aid else []
    return {
        "bvid": bvid,
        "cid": cid,
        "url": url,
        "title": title,
        "owner": owner,
        "description": desc,
        "description_meaningful": description_assessment["meaningful"],
        "description_reason": description_assessment["reason"],
        "description_effective": description_assessment["text"],
        "bgm": bgm,
        "bgm_id": bgm_id,
        "bgm_original_artist": bgm_original_artist,
        "subtitle": subtitle,
        "subtitle_kind": subtitle_kind,
        "comments": comments,
        "tags": tags,
        "duration_seconds": duration,
        "pubdate": pubdate,
        "stats": {
            "view": stat.get("view"),
            "like": stat.get("like"),
            "coin": stat.get("coin"),
            "favorite": stat.get("favorite"),
            "reply": stat.get("reply"),
        },
        "collected_at": int(time.time()),
    }


def discover_video_candidates(context: Any, config: dict[str, Any], start_url: str | None = None, query: str = "") -> list[dict[str, Any]]:
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(start_url or "https://www.bilibili.com/", wait_until="domcontentloaded", timeout=60000)
    for _ in range(int(config.get("home_scrolls", 8))):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(random.randint(1200, 2400))
    anchors = page.locator("a[href]").evaluate_all(
        "els => els.map(e => ({href: e.href, text: (e.innerText || e.textContent || e.getAttribute('title') || '').trim()}))"
        ".filter(x => x.href && x.href.includes('/video/BV'))"
    )
    found = []
    seen: set[str] = set()
    for anchor in anchors:
        bvid = bvid_from_url(str(anchor.get("href") or ""))
        if bvid and bvid not in seen:
            seen.add(bvid)
            found.append({"bvid": bvid, "title": str(anchor.get("text") or ""), "score": 0.0})
    if query:
        def normalized(value: str) -> str:
            return re.sub(r"[\s#\uff0c\u3002\uff01\uff1f\u3010\u3011\u300a\u300b\-_|:\uff1a]+", "", value.casefold())
        query_norm = normalized(query)
        for item in found:
            title_norm = normalized(str(item.get("title") or ""))
            ratio = difflib.SequenceMatcher(None, query_norm, title_norm).ratio() if query_norm and title_norm else 0.0
            contains = 1.0 if query_norm and query_norm in title_norm else 0.0
            item["score"] = round(contains + ratio, 4)
        found.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    return found


def discover_videos(context: Any, config: dict[str, Any], start_url: str | None = None, query: str = "") -> list[str]:
    return [str(item["bvid"]) for item in discover_video_candidates(context, config, start_url, query)]


def resolve_video_candidates(query: str, config: dict[str, Any], max_results: int = 1, headless: bool = False, order: str = "pubdate") -> list[dict[str, Any]]:
    log(f"Bilibili resolve query={query!r}")
    from playwright.sync_api import sync_playwright
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=bool(headless or config.get("headless", False)))
        try:
            if not ensure_login(context, wait_seconds=2):
                raise RuntimeError("Bilibili login unavailable for search")
            signed = wbi_signed_params(context, {"search_type": "video", "keyword": query, "page": 1, "order": order})
            api_response = context.request.get(
                "https://api.bilibili.com/x/web-interface/wbi/search/type",
                params=signed,
                headers={"Referer": f"https://search.bilibili.com/video?keyword={quote(query)}"},
                timeout=60000,
            )
            api_payload = api_response.json()
            search_data = api_payload.get("data") or {}
            result_rows = search_data.get("result") or []
            log(f"Bilibili WBI search API HTTP {api_response.status} code={api_payload.get('code')} message={api_payload.get('message')} data_keys={','.join(sorted(search_data.keys()))} result_count={len(result_rows)}")
            if api_response.ok and api_payload.get("code") == 0:
                rows = []
                for item in result_rows:
                    bvid = str(item.get("bvid") or "")
                    if not bvid:
                        continue
                    title = re.sub(r"<[^>]+>", "", html.unescape(str(item.get("title") or ""))).strip()
                    tags_text = re.sub(r"<[^>]+>", "", html.unescape(str(item.get("tag") or ""))).strip()
                    rows.append({
                        "bvid": bvid,
                        "title": title,
                        "author": str(item.get("author") or "").strip()[:120],
                        "description": re.sub(r"<[^>]+>", "", html.unescape(str(item.get("description") or ""))).strip()[:600],
                        "tags": [part.strip() for part in re.split(r"[,?]", tags_text) if part.strip()][:20],
                        "view_count": int(item.get("play") or 0),
                        "score": 0.0,
                        "duration_seconds": duration_seconds(item.get("duration")),
                    })
                limit = int(config.get("max_video_duration_seconds") or 1800)
                accepted = []
                for row in rows:
                    duration = int(row.get("duration_seconds") or 0)
                    if limit > 0 and duration > limit:
                        log(f"Skipping long search result: bvid={row['bvid']} duration={duration}s limit={limit}s")
                        continue
                    accepted.append(row)
                if accepted:
                    return accepted[:max(1, max_results)]
            search_url = f"https://search.bilibili.com/video?keyword={quote(query)}&order={quote(order)}"
            return discover_video_candidates(context, config, search_url, query)[:max(1, max_results)]
        finally:
            context.close()

def parse_deepseek_provider() -> tuple[str, str]:
    text = MODEL_CONFIG.read_text(encoding="utf-8-sig")
    for block in re.split(r"(?=\[\[api_providers\]\])", text):
        if re.search(r'(?m)^name\s*=\s*"DeepSeek"', block):
            base = re.search(r'(?m)^base_url\s*=\s*"([^"]*)"', block)
            key = re.search(r'(?m)^api_key\s*=\s*"([^"]*)"', block)
            if base and key:
                return base.group(1).rstrip("/"), key.group(1)
    raise RuntimeError("未找到 MaiBot 的 DeepSeek 配置")



def call_triage_model(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    base_url, api_key = parse_deepseek_provider()
    model = str(config.get("triage_model") or "deepseek-flash")
    response = requests.post(
        base_url + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a strict content relevance classifier. Return JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 220,
        },
        timeout=90,
    )
    if not response.ok:
        raise RuntimeError(f"triage HTTP {response.status}: {response.text[:300]}")
    content = str(response.json()["choices"][0]["message"].get("content") or "")
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        raise ValueError("triage response did not contain JSON")
    data = json.loads(match.group(0))
    score = max(0.0, min(100.0, float(data.get("score", 50))))
    return {
        "score": round(score, 1),
        "keep": bool(data.get("keep", score >= 55)),
        "reason": str(data.get("reason") or "").strip()[:300],
        "topics": [str(item).strip()[:40] for item in (data.get("topics") or []) if str(item).strip()][:8],
    }


def triage_candidate(video: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    threshold = float(config.get("min_relevance_score", 55))
    profile = str(config.get("interest_profile") or "AI tools, large language models, agents, programming, technology, practical tutorials")
    avoid = [str(item) for item in (config.get("avoid_topics") or []) if str(item).strip()]
    prompt = f"""
User preference:
{profile}

Topics to avoid:
{', '.join(avoid) if avoid else 'none'}

Decide whether this candidate video is worth opening and processing.
Title: {video.get('title','')}
Uploader: {video.get('owner','')}
Tags: {', '.join(video.get('tags') or [])}
Description: {str(video.get('description') or '')[:1000]}

Return strict JSON:
{{"score":0-100,"keep":true/false,"reason":"short Chinese reason","topics":["topic1","topic2"]}}
Use keep=true only when the metadata clearly relates to the user's preferences and likely has useful information.
"""
    try:
        decision = call_triage_model(prompt, config)
        if decision["score"] < threshold:
            decision["keep"] = False
            if not decision["reason"]:
                decision["reason"] = f"relevance score {decision['score']} below {threshold}"
        return decision
    except Exception as error:
        log(f"Candidate triage failed, keeping for content check: {error}")
        return {"score": 50.0, "keep": True, "reason": "triage unavailable", "topics": []}


def triage_content(video: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    subtitle = str(video.get("subtitle") or "")
    if not subtitle:
        return {"score": 50.0, "keep": True, "reason": "no subtitle for early content check", "topics": []}
    checkpoint = max(500, int(config.get("early_check_chars", 3000)))
    excerpt = subtitle[:checkpoint]
    threshold = float(config.get("early_min_score", 50))
    profile = str(config.get("interest_profile") or "")
    prompt = f"""
User preference:
{profile}

Read only this opening excerpt of a Bilibili video. Decide whether continuing is valuable.
Title: {video.get('title','')}
Tags: {', '.join(video.get('tags') or [])}
Opening transcript:
{excerpt}

Return strict JSON:
{{"score":0-100,"keep":true/false,"reason":"short Chinese reason","topics":["topic1","topic2"]}}
Use keep=false when the opening indicates low information value, off-topic entertainment, advertising, clickbait, or repetitive content.
"""
    try:
        decision = call_triage_model(prompt, config)
        if decision["score"] < threshold:
            decision["keep"] = False
        return decision
    except Exception as error:
        log(f"Content triage failed, continuing: {error}")
        return {"score": 50.0, "keep": True, "reason": "triage unavailable", "topics": []}

def visual_detail_budget(duration_seconds: Any) -> int:
    try:
        duration = max(0, int(float(duration_seconds or 0)))
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        return 3
    if duration <= 60:
        return 2
    if duration <= 300:
        return 3
    if duration <= 900:
        return 5
    return 6


def visual_summary_is_heavy(text: str) -> bool:
    value = str(text or "")
    if not value:
        return False
    timestamps = len(re.findall(r"\[\d{1,2}:\d{2}\]", value))
    visual_terms = len(re.findall(r"(?:\u753b\u9762\u4e2d|\u753b\u9762\u663e\u793a|\u5de6\u4fa7|\u53f3\u4fa7|\u4e0a\u65b9|\u4e0b\u65b9|\u80cc\u666f\u4e3a|\u53ef\u89c1|\u955c\u5934|\u5e27\u4e2d)", value))
    meaningful = len(re.sub(r"[#*\s]", "", value))
    return timestamps >= 4 or (meaningful > 0 and visual_terms >= 8 and visual_terms / max(1, meaningful) > 0.018)


def summarize(video: dict[str, Any], config: dict[str, Any]) -> str:
    base_url, api_key = parse_deepseek_provider()
    model = str(config.get("summary_model") or "deepseek-flash")
    comments = "\n".join(video.get("comments") or [])[:1200]
    audio_transcript = str(video.get("audio_transcript") or "")
    visual_analysis = str(video.get("visual_analysis") or "")
    subtitle_for_summary = str(video.get("subtitle") or "")[:3500]
    audio_for_summary = audio_transcript[:3500]
    visual_for_summary = visual_analysis[:3500]
    duration_seconds = int(video.get("duration_seconds") or 0)
    visual_detail_limit = visual_detail_budget(duration_seconds)
    description_meaningful = bool(video.get("description_meaningful"))
    description_for_summary = str(video.get("description_effective") or video.get("description") or "").strip()[:800] if description_meaningful else ""
    description_quality = "\u6709\u610f\u4e49" if description_meaningful else f"\u65e0\u6709\u6548\u4fe1\u606f\uff08{video.get('description_reason') or '\u672a\u901a\u8fc7\u8d28\u91cf\u5224\u65ad'}\uff09"
    meta_text = " ".join([str(video.get("title") or ""), str(video.get("description") or ""), " ".join(str(item) for item in (video.get("tags") or []))]).casefold()
    music_markers = ["mv", "music", "song", "vocaloid", "\u97f3\u4e50", "\u6b4c\u66f2", "\u6b4c\u8bcd", "\u7ffb\u5531", "\u539f\u521b\u66f2", "\u6f14\u5531", "\u6b4c\u59ec", "\u97f3\u4e50\u8d4f\u6790", "\u8d4f\u6790", "\u4e50\u8bc4", "\u542c\u611f", "\u6f14\u594f", "\u7eaf\u97f3\u4e50", "\u94a2\u7434", "\u4f34\u594f"]
    handdraw_markers = ["\u624b\u4e66", "\u52a8\u753b", "mad", "amv", "\u63cf\u6539", "\u6f14\u51fa"]
    mashup_markers = ["\u6df7\u526a", "\u526a\u8f91", "\u5408\u96c6", "mashup"]
    game_markers = ["\u51f9\u5206", "\u56fd\u699c", "\u51b2\u699c", "\u6df1\u6e0a", "\u914d\u961f", "\u5bf9\u5c40", "\u89d2\u8272", "\u653b\u7565"]
    is_music = any(marker in meta_text for marker in music_markers)
    lyrics_policy = "允许识别并分析歌词，但必须标注为歌词" if is_music else "不转写BGM歌词，只标记背景音乐"
    is_game = any(marker in meta_text for marker in game_markers)
    is_handdraw = any(marker in meta_text for marker in handdraw_markers)
    is_mashup = any(marker in meta_text for marker in mashup_markers)
    if is_mashup:
        video_kind = "\u6df7\u526a/\u526a\u8f91\u7c7b\uff08\u53ef\u80fd\u540c\u65f6\u542b\u6b4c\u66f2\u548c\u4eba\u58f0\uff09"
    elif is_game:
        video_kind = "\u6e38\u620f\u51f9\u5206/\u51b2\u699c\u73a9\u6cd5\u7c7b"
    elif is_music:
        video_kind = "\u6b4c\u66f2/MV \u7c7b"
    elif is_handdraw:
        video_kind = "\u624b\u4e66/\u52a8\u753b\u7c7b"
    else:
        video_kind = "\u666e\u901a\u89c6\u9891"
    source_text = f"""
\u6807\u9898\uff1a{video['title']}
UP\u4e3b\uff1a{video['owner']}
\u94fe\u63a5\uff1a{video['url']}
\u7c7b\u578b\u5224\u65ad\uff1a{video_kind}
BGM\uff1a{video.get("bgm") or "\uff08\u672a\u6807\u6ce8\uff09"}
BGM\u539f\u5531\uff1a{video.get("bgm_original_artist") or "\uff08\u672a\u6807\u6ce8\uff09"}
\u6b4c\u8bcd\u5904\u7406\uff1a{lyrics_policy}
\u6807\u7b7e\uff1a{', '.join(str(item) for item in (video.get('tags') or []))}
\u7b80\u4ecb\u8d28\u91cf\uff1a{description_quality}\n\u7b80\u4ecb\uff1a{description_for_summary or '\uff08\u65e0\u6709\u6548\u4fe1\u606f\uff09'}
B\u7ad9\u5b57\u5e55\uff1a{subtitle_for_summary or '\uff08\u65e0\u5b57\u5e55\uff09'}
\u672c\u5730\u8bed\u97f3\u8f6c\u5199\uff1a{audio_for_summary or '\uff08\u672a\u8f6c\u5199\u6216\u6ca1\u6709\u8bed\u97f3\uff09'}
\u753b\u9762\u63cf\u8ff0\uff1a{visual_for_summary or '\uff08\u672a\u505a\u89c6\u89c9\u8bc6\u522b\uff09'}
\u70ed\u95e8\u8bc4\u8bba\uff1a{comments or '\uff08\u672a\u83b7\u53d6\uff09'}
""".strip()
    prompt = f"""
\u8bf7\u628a\u4e0b\u9762\u7684 B \u7ad9\u89c6\u9891\u4fe1\u606f\u6574\u7406\u6210\u9002\u5408\u957f\u671f\u8bb0\u5fc6\u7684\u4e2d\u6587\u7b14\u8bb0\u3002
\u8981\u6c42\uff1a
1. \u4e0d\u7f16\u9020\u89c6\u9891\u4e2d\u672a\u51fa\u73b0\u7684\u4e8b\u5b9e\u3002
2. \u8f93\u51fa\u7cbe\u7b80\u7b14\u8bb0\uff0c\u6700\u591a 4 \u4e2a\u77ed\u6bb5\uff1a\u4e00\u53e5\u8bdd\u6982\u62ec\u3001\u753b\u9762\u4e0e\u6545\u4e8b\u3001\u6838\u5fc3\u4fe1\u606f\u3001\u4f9d\u636e\u4e0e\u4e0d\u786e\u5b9a\u70b9\uff1b\u603b\u957f 250 \u5230 450 \u5b57\u3002
3. \u6709\u8fde\u7eed\u753b\u9762\u63cf\u8ff0\u65f6\uff0c\u6309\u65f6\u95f4\u53d8\u5316\u6574\u7406\u6210\u8fde\u8d2f\u6545\u4e8b\uff1a\u5148\u5199\u753b\u9762\u660e\u786e\u663e\u793a\u7684\u5185\u5bb9\uff0c\u518d\u5199\u201c\u636e\u6b64\u63a8\u65ad\u201d\u7684\u5267\u60c5\uff1b\u4e0d\u8981\u628a\u63a8\u65ad\u5199\u6210\u4e8b\u5b9e\u3002
4. \u4e0d\u8981\u9010\u5e27\u7f57\u5217\uff0c\u4e0d\u8981\u91cd\u590d\u94fe\u63a5\u548c\u5143\u6570\u636e\uff1b\u53ea\u4fdd\u7559\u63a8\u52a8\u5185\u5bb9\u7406\u89e3\u7684\u753b\u9762\u53d8\u5316\u3002
5. \u5728\u7ed3\u5c3e\u7528\u4e00\u884c\u7b80\u5199\u4e3b\u8981\u4f9d\u636e\uff0c\u4f8b\u5982\u201c\u4f9d\u636e\uff1a\u8fde\u7eed\u62bd\u5e27\u3001\u5b57\u5e55\u201d\u3002
6. \u6ca1\u6709\u5b57\u5e55\u4e14\u6ca1\u6709\u8bed\u97f3\u8f6c\u5199\u65f6\uff0c\u53ea\u80fd\u4f9d\u636e\u771f\u5b9e\u6709\u6548\u7684\u7b80\u4ecb\u3001\u8bc4\u8bba\u3001\u753b\u9762\u548c\u6807\u7b7e\u6574\u7406\uff1b\u63d0\u793a\u4e3a\u201c\u7b80\u4ecb\u65e0\u6709\u6548\u4fe1\u606f\u201d\u65f6\uff0c\u4e0d\u5f97\u628a\u7b80\u4ecb\u5f53\u4f5c\u5185\u5bb9\u4f9d\u636e\u3002
6.1 \u4ec5\u542b\u94fe\u63a5\u3001\u8bdd\u9898\u6807\u7b7e\u3001\u70b9\u8d5e\u5173\u6ce8\u3001\u7248\u6743\u58f0\u660e\u3001BGM \u6807\u6ce8\u3001\u5546\u52a1\u8054\u7cfb\u3001\u91cd\u590d\u6807\u9898\u6216\u8fc7\u77ed\u5957\u8bdd\u7684\u7b80\u4ecb\uff0c\u89c6\u4e3a\u65e0\u610f\u4e49\u7b80\u4ecb\u3002
7. \u4e0d\u5f97\u5355\u51ed\u6807\u9898\u731c\u6d4b\u753b\u9762\u6216\u5267\u60c5\uff1b\u4f46\u53ef\u4ee5\u6839\u636e\u8fde\u7eed\u753b\u9762\u7684\u52a8\u4f5c\u3001\u8868\u60c5\u3001\u573a\u666f\u53d8\u5316\u505a\u5408\u7406\u63a8\u65ad\uff0c\u5e76\u660e\u786e\u6807\u6ce8\u3002
8. \u97f3\u4e50/MV/\u97f3\u4e50\u8d4f\u6790\u7c7b\uff1a\u53ef\u4ee5\u8bc6\u522b\u548c\u5206\u6790\u6b4c\u8bcd/\u5b57\u5e55\uff0c\u4f46\u5fc5\u987b\u6807\u6ce8\u4e3a\u201c\u6b4c\u8bcd\u201d\uff0c\u4e0d\u5f97\u5f53\u6210\u89c6\u9891\u5267\u60c5\u3002
10. \u6df7\u526a\u7c7b\uff1a\u8f6c\u5199\u53ef\u80fd\u540c\u65f6\u542b\u6b4c\u8bcd\u548c\u8bf4\u8bdd\uff0c\u82e5\u65e0\u6cd5\u533a\u5206\u5c31\u660e\u786e\u5199\u201c\u97f3\u9891\u5185\u5bb9\u6df7\u6742\u201d\uff0c\u4e0d\u8981\u5f3a\u884c\u5206\u7c7b\u3002
11. \u6e38\u620f\u51f9\u5206/\u51b2\u699c\u7c7b\uff1a\u91cd\u70b9\u6574\u7406\u6e38\u620f\u3001\u89d2\u8272\u3001\u914d\u961f\u3001\u5173\u5361\u3001\u64cd\u4f5c\u3001\u79ef\u5206/\u6392\u540d\u548c\u7ed3\u7b97\u4fe1\u606f\uff1b\u4e0d\u8981\u628a\u6e38\u620f\u80cc\u666f\u97f3\u4e50\u5f53\u6210\u5267\u60c5\u3002
12. \u975e\u97f3\u4e50\u7c7b\uff1a\u4e0d\u8981\u8f6c\u5199\u6216\u6458\u5f55BGM\u6b4c\u8bcd\uff0c\u53ea\u6807\u8bb0\u201c\u4f7f\u7528\u4e86\u80cc\u666f\u97f3\u4e50\u201d\u3002
13. \u53ea\u8981\u89c6\u9891\u4fe1\u606f\u91cc\u6709\u660e\u786e\u7684 BGM/\u80cc\u666f\u97f3\u4e50\u540d\uff0c\u5fc5\u987b\u628a\u6b4c\u540d\u548c\u6709\u51ed\u636e\u7684\u539f\u5531\u4fdd\u7559\u5230\u7b14\u8bb0\u4e2d\uff1b\u4e0d\u8981\u81ea\u884c\u7f16\u9020\u6b4c\u540d\u6216\u539f\u5531\uff0c\u4e5f\u4e0d\u9700\u8981\u97f3\u4e50\u8bc4\u5206\u6216\u97f3\u4e50\u8bc4\u8bba\u3002
14. \u82e5\u753b\u9762\u53ea\u662f\u5c55\u793a\u3001\u754c\u9762\u6f14\u793a\u6216\u4e92\u4e0d\u8fde\u8d2f\u7684\u7247\u6bb5\uff0c\u8981\u76f4\u8bf4\u201c\u4e0d\u662f\u53d9\u4e8b\u89c6\u9891\u201d\uff0c\u4e0d\u5f97\u786c\u7f16\u6545\u4e8b\u3002
15. \u5bf9\u753b\u9762\u91cc\u65e0\u6cd5\u786e\u8ba4\u7684\u4eba\u540d\u3001\u89d2\u8272\u540d\u6216\u5173\u7cfb\uff0c\u53ea\u7528\u5916\u89c2\u63cf\u8ff0\uff0c\u4e0d\u5f97\u731c\u5177\u4f53\u8eab\u4efd\u3002

16. \u753b\u9762\u63cf\u8ff0\u53ea\u662f\u8bc1\u636e\uff0c\u4e0d\u662f\u6458\u8981\u4e3b\u4f53\uff1b\u4e0d\u8981\u9010\u5e27\u5ff5\u51fa\u89d2\u8272\u5916\u89c2\u548c\u80cc\u666f\uff0c\u5168\u6587\u6700\u591a\u4fdd\u7559 {visual_detail_limit} \u4e2a\u771f\u6b63\u6709\u7528\u7684\u89c6\u89c9\u7ec6\u8282\u3002
17. \u6709\u5b57\u5e55\u3001\u8bed\u97f3\u8f6c\u5199\u3001\u6709\u6548\u7b80\u4ecb\u6216\u8bc4\u8bba\u65f6\uff0c\u4f18\u5148\u8bb2\u4eba\u7269\u5728\u8bf4\u4ec0\u4e48\u3001\u89c6\u9891\u5728\u8868\u8fbe\u4ec0\u4e48\uff1b\u753b\u9762\u53ea\u7528\u6765\u8f85\u52a9\u786e\u8ba4\uff0c\u753b\u9762\u7ec6\u8282\u4e0d\u5f97\u8d85\u8fc7\u5168\u6587\u4e00\u534a\u3002
18. \u53ea\u6709\u753b\u9762\u8bc1\u636e\u65f6\uff0c\u91cd\u70b9\u5199\u8fd9\u6bb5\u89c6\u9891\u5728\u5c55\u793a\u4ec0\u4e48\u3001\u53d1\u751f\u4e86\u4ec0\u4e48\u53d8\u5316\u3001\u53ef\u80fd\u7684\u7528\u9014\u6216\u60c5\u7eea\uff0c\u4e0d\u8981\u4ece\u5de6\u5230\u53f3\u63cf\u8ff0\u6bcf\u4e00\u5e27\u3002
19. \u4e0d\u8981\u8f93\u51fa\u65f6\u95f4\u6233\u6216\u201c\u7b2c\u51e0\u5e27\u201d\u7f57\u5217\uff1b\u65f6\u95f4\u4fe1\u606f\u53ea\u7528\u4e8e\u81ea\u5df1\u7406\u89e3\u753b\u9762\u987a\u5e8f\u3002
20. \u5982\u679c\u53ea\u80fd\u5f97\u51fa\u753b\u9762\u5c42\u9762\u7684\u4e8b\u5b9e\uff0c\u5c31\u660e\u786e\u5199\u201c\u672c\u6b21\u6ca1\u6709\u8db3\u591f\u7684\u53f0\u8bcd\u6216\u6587\u5b57\u8bf4\u660e\u7528\u9014\u201d\uff0c\u4e0d\u8981\u7528\u5927\u6bb5\u753b\u9762\u63cf\u8ff0\u51d1\u957f\u5ea6\u3002
21. \u82e5\u5b57\u5e55\u7c7b\u578b\u4e3a embedded\uff0c\u5fc5\u987b\u4f18\u5148\u8bfb\u53d6\u5e76\u4f7f\u7528\u8be5\u5b57\u5e55\uff1b\u4e0d\u5f97\u8df3\u8fc7\u5185\u5d4c\u5b57\u5e55\u53bb\u6539\u7528\u753b\u9762\u6216AI\u5b57\u5e55\u3002
22. \u5b57\u5e55\u7c7b\u578b\u4e3a ai \u65f6\uff0c\u8bf4\u660e\u6b63\u5728\u4f7f\u7528B\u7ad9AI\u5b57\u5e55\uff1b\u4e3a none \u65f6\uff0c\u4e0d\u8981\u5047\u88c5\u5b58\u5728\u5b57\u5e55\u3002
\u89c6\u9891\u4fe1\u606f\uff1a
{source_text}
""".strip()
    visual_retry = False
    for attempt in range(2):
        current_source = source_text if attempt == 0 else source_text[:7000]
        current_prompt = prompt if attempt == 0 else prompt.replace(source_text, current_source)
        if visual_retry:
            current_prompt += f"\n\n\u4e0a\u4e00\u6b21\u8f93\u51fa\u8fc7\u5ea6\u63cf\u8ff0\u753b\u9762\u3002\u8bf7\u91cd\u5199\uff1a\u5220\u6389\u65f6\u95f4\u6233\u548c\u5916\u89c2\u7f57\u5217\uff0c\u7528\u4e0d\u8d85\u8fc7 {visual_detail_limit} \u4e2a\u753b\u9762\u7ec6\u8282\u8bf4\u6e05\u6545\u4e8b\u3001\u5185\u5bb9\u6216\u7528\u9014\u3002"
        response = requests.post(
            base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "\u4f60\u662f\u4e25\u8c28\u7684\u89c6\u9891\u5185\u5bb9\u6574\u7406\u52a9\u624b\uff0c\u53ea\u6574\u7406\u8f93\u5165\u4e2d\u786e\u5b9e\u5b58\u5728\u7684\u4fe1\u606f\u3002"},
                    {"role": "user", "content": current_prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 2800 if attempt == 0 else 2200,
            },
            timeout=180,
        )
        if not response.ok:
            raise RuntimeError(f"DeepSeek HTTP {response.status}: {response.text[:500]}")
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        summary_text = str(message.get("content") or "").strip()
        reasoning_text = str(message.get("reasoning_content") or "").strip()
        finish_reason = choice.get("finish_reason") or ""
        meaningful_length = len(re.sub(r"[#*\s]", "", summary_text))
        visual_heavy = bool(visual_analysis) and visual_summary_is_heavy(summary_text)
        if summary_text and meaningful_length >= 180 and not visual_heavy:
            return summary_text
        if summary_text and visual_heavy:
            visual_retry = True
            log(f"Summary model output is too visual-heavy; attempt={attempt + 1} length={meaningful_length} timestamps={len(re.findall(r'\[\d{1,2}:\d{2}\]', summary_text))}")
        else:
            visual_retry = False
            log(f"Summary model returned insufficient content; attempt={attempt + 1} length={meaningful_length} reasoning_length={len(reasoning_text)} finish_reason={finish_reason}")

    visual = re.sub(r"\s+", " ", visual_analysis).strip()
    audio = re.sub(r"\s+", " ", audio_transcript).strip()
    subtitle = re.sub(r"\s+", " ", str(video.get("subtitle") or "")).strip()
    description = re.sub(r"\s+", " ", str(video.get("description_effective") or "")).strip() if video.get("description_meaningful") else ""
    comment_text = re.sub(r"\s+", " ", comments).strip()
    sections = []
    if visual:
        sections.append("\u753b\u9762\u4f9d\u636e\uff08\u76f4\u63a5\u6765\u81ea\u89c6\u9891\u62bd\u5e27\uff09\uff1a" + visual[:1200])
    if audio:
        sections.append("\u672c\u5730\u8bed\u97f3\u8f6c\u5199\uff1a" + audio[:900])
    if subtitle:
        sections.append("B\u7ad9\u5b57\u5e55\uff08\u53ef\u80fd\u4e0e\u753b\u9762\u6216\u6807\u9898\u9519\u914d\uff0c\u4ec5\u5355\u72ec\u4fdd\u7559\uff09\uff1a" + subtitle[:900])
    if description:
        sections.append("\u89c6\u9891\u7b80\u4ecb\uff1a" + description[:600])
    if comment_text:
        sections.append("\u70ed\u95e8\u8bc4\u8bba\uff1a" + comment_text[:600])
    if not sections:
        return ""
    header = "### \u5df2\u83b7\u53d6\u8bc1\u636e\u6574\u7406\uff08\u6458\u8981\u6a21\u578b\u672a\u8fd4\u56de\u6b63\u6587\uff09"
    if visual and subtitle:
        header += "\n\n\u6ce8\u610f\uff1a\u753b\u9762\u4e0eB\u7ad9\u5b57\u5e55\u53ef\u80fd\u4e0d\u662f\u540c\u4e00\u5185\u5bb9\uff0c\u4e0b\u9762\u5206\u5f00\u8bb0\u5f55\uff0c\u4e0d\u5408\u5e76\u4e3a\u540c\u4e00\u6bb5\u53d9\u4e8b\u3002"
    return header + "\n\n" + "\n\n".join(sections)

def webui_session() -> requests.Session:
    token = json.loads(WEBUI_CONFIG.read_text(encoding="utf-8-sig")).get("access_token")
    if not token:
        raise RuntimeError("MaiBot WebUI token missing")
    session = requests.Session()
    last_error: Exception | None = None
    for attempt in range(6):
        response = session.post(WEBUI_BASE + "/api/webui/auth/verify", json={"token": token}, timeout=20)
        if response.status_code == 429:
            last_error = RuntimeError("MaiBot WebUI auth rate limited")
            time.sleep(5 * (attempt + 1))
            continue
        response.raise_for_status()
        if not response.json().get("valid"):
            raise RuntimeError("MaiBot WebUI authentication failed")
        return session
    raise RuntimeError(f"MaiBot WebUI auth unavailable after retries: {last_error}")


def import_to_memory(content: str, name: str, scope: str, chat_id: str, config: dict[str, Any]) -> dict[str, Any]:
    session = webui_session()
    payload: dict[str, Any] = {
        "content": content,
        "name": name,
        "input_mode": "text",
        "llm_enabled": True,
        "strategy_override": "auto",
        "dedupe_policy": "content_hash",
        "scope_type": scope,
    }
    if scope == "chat":
        if not chat_id:
            raise RuntimeError("scope=chat 时必须提供 chat_id")
        payload["chat_id"] = chat_id
    created = session.post(WEBUI_BASE + "/api/webui/memory/import/paste", json=payload, timeout=60)
    created.raise_for_status()
    result = created.json()
    task = result.get("task") or result
    task_id = str(task.get("task_id") or "")
    if not task_id:
        raise RuntimeError(f"未返回导入任务 ID: {result}")
    log(f"已创建记忆导入任务：{task_id}")
    deadline = time.time() + int(config.get("import_wait_seconds", 600))
    last_status = ""
    while time.time() < deadline:
        response = session.get(WEBUI_BASE + f"/api/webui/memory/import/tasks/{task_id}", timeout=30)
        response.raise_for_status()
        info = response.json()
        task_info = info.get("task") or info
        status = str(task_info.get("status") or "")
        progress = task_info.get("progress")
        if status != last_status or progress is not None:
            log(f"记忆导入状态：{status or 'unknown'} progress={progress}")
            last_status = status
        if status == "completed":
            return info
        if status in {"failed", "cancelled"}:
            raise RuntimeError(f"Memory import {status}: {info}")
        time.sleep(3)
    raise RuntimeError("记忆导入等待超时")


def list_chat_targets() -> None:
    session = webui_session()
    response = session.get(WEBUI_BASE + "/api/webui/memory/import/chat-targets", timeout=30)
    response.raise_for_status()
    targets = response.json().get("data") or []
    if not targets:
        print("没有找到可关联的聊天流。")
        return
    for target in targets:
        print(
            f"{target.get('chat_name')}\t{target.get('chat_id')}\t"
            f"platform={target.get('platform')}\tgroup_id={target.get('group_id') or ''}"
        )


def login_command() -> int:
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=False)
        try:
            return 0 if ensure_login(context) else 1
        finally:
            context.close()


def collect_command(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    config = load_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "videos").mkdir(exist_ok=True)
    (DATA_DIR / "summaries").mkdir(exist_ok=True)
    seen_path = DATA_DIR / "seen.json"
    seen = set(json.loads(seen_path.read_text(encoding="utf-8-sig"))) if seen_path.exists() else set()
    target_count = max(1, int(args.max or config.get("max_videos_per_run", 5)))
    candidate_budget = collection_candidate_budget(target_count, config)
    bvids: list[str] = []
    page_by_bvid: dict[str, int] = {}
    short_links: list[str] = []
    for url in args.url or []:
        bvid = bvid_from_url(url)
        if bvid:
            bvids.append(bvid)
            page_by_bvid[bvid] = page_from_url(url)
        elif re.search(r"https?://(?:b23\.tv|bili2233\.cn)/", url, re.IGNORECASE):
            short_links.append(url)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=bool(args.headless or config.get("headless", False)))
        try:
            if not ensure_login(context, wait_seconds=2):
                if not config.get("allow_anonymous_public_videos", True):
                    raise RuntimeError("Bilibili login unavailable and anonymous public access is disabled")
                log("Bilibili login unavailable; continuing with anonymous public-video mode")
            for short_url in short_links:
                try:
                    page = context.new_page()
                    page.goto(short_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2500)
                    resolved = page.url
                    bvid = bvid_from_url(resolved)
                    if bvid and bvid not in bvids:
                        bvids.append(bvid)
                        page_by_bvid[bvid] = page_from_url(resolved)
                        log(f"Resolved short link {short_url} -> {bvid}")
                    else:
                        log(f"Short link resolved without BV id: {short_url} -> {resolved}")
                    page.close()
                except Exception as error:
                    log(f"Short link navigation failed: {short_url} {error}")
            if args.url and not bvids:
                raise RuntimeError("unable to resolve a BV id from the supplied video URL")
            query_term = str(args.tag or args.search_keyword or "").strip()
            if not bvids:
                sources: list[str] = []
                if args.tag:
                    sources.append(f"https://www.bilibili.com/tag/{quote(args.tag)}")
                    sources.append(f"https://search.bilibili.com/video?keyword={quote(args.tag)}&order=pubdate")
                if args.search_keyword:
                    sources.append(f"https://search.bilibili.com/video?keyword={quote(args.search_keyword)}&order=pubdate")
                if not sources:
                    sources.append(args.section_url or config.get("default_section_url") or "https://www.bilibili.com/")
                for source_url in sources:
                    log(f"Discovering videos from: {source_url}")
                    for found_bvid in discover_videos(context, config, source_url, query_term):
                        if found_bvid not in bvids and (args.force or args.url or found_bvid not in seen):
                            bvids.append(found_bvid)
                    if len(bvids) >= candidate_budget:
                        break
            unique = []
            for bvid in bvids:
                if bvid not in unique and (args.force or args.url or bvid not in seen):
                    unique.append(bvid)
            unique = unique[:candidate_budget]
            if not unique:
                log(f"Collection incomplete: target={target_count} watchable videos, found=0 new candidates")
                return 2
            imported = 0
            accepted = 0
            manual_link = bool((args.ignore_preference or args.url) and (args.url or short_links))
            explicit_topic = bool(query_term) and not manual_link
            excluded_tags = [str(tag) for tag in (config.get("excluded_tags") or []) if str(tag).strip()]
            for index, bvid in enumerate(unique, start=1):
                log(f"?? {index}/{len(unique)}?{bvid}")
                page_number = page_by_bvid.get(bvid, 1)
                candidate = collect_video(context, bvid, config, include_content=False, page_number=page_number)
                candidate_path = DATA_DIR / "videos" / f"{bvid}.json"
                duration_limit = int(config.get("max_video_duration_seconds") or 1800)
                candidate_duration = int(candidate.get("duration_seconds") or 0)
                if duration_limit > 0 and candidate_duration > duration_limit:
                    candidate["decision"] = {"keep": False, "stage": "duration", "reason": "video too long"}
                    candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
                    log(f"Skipping long video: {bvid} duration={candidate_duration}s limit={duration_limit}s")
                    continue
                if not manual_link:
                    excluded_hit = excluded_tag_hit(candidate, excluded_tags)
                    if excluded_hit:
                        candidate["decision"] = {"keep": False, "stage": "excluded_tag", "reason": f"excluded tag: {excluded_hit}"}
                        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
                        log(f"Skipping excluded tag: {bvid} tag={excluded_hit}")
                        seen.add(bvid)
                        seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")
                        continue
                tag_keywords = [str(item) for item in (config.get("tag_filter_keywords") or []) if str(item).strip()]
                if query_term and query_term.casefold() not in {"ai", "人工智能"}:
                    tag_keywords = [query_term]
                if not manual_link and not explicit_topic and query_term and not matches_tag_filter(candidate, tag_keywords):
                    candidate["decision"] = {"keep": False, "stage": "keyword_filter", "reason": "tag filter miss"}
                    candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
                    log(f"Skipping unrelated video: {candidate.get('title')}")
                    seen.add(bvid)
                    seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")
                    continue
                if manual_link:
                    candidate_decision = {"keep": True, "score": 100.0, "reason": "manually shared link; preference filter bypassed", "topics": []}
                elif explicit_topic:
                    candidate_decision = {"keep": True, "score": 100.0, "reason": "explicit search topic matched; global preference bypassed", "topics": [query_term]}
                else:
                    candidate_decision = triage_candidate(candidate, config)
                candidate["decision"] = {"keep": candidate_decision["keep"], "stage": "candidate", **candidate_decision}
                candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
                if not candidate_decision["keep"]:
                    log(f"Skipping low-value candidate: score={candidate_decision['score']} reason={candidate_decision['reason']}")
                    seen.add(bvid)
                    seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")
                    continue
                log(f"Candidate accepted: score={candidate_decision['score']} reason={candidate_decision['reason']}")
                video = collect_video(context, bvid, config, include_content=True, page_number=page_number)
                video["candidate_decision"] = candidate_decision
                if not manual_link and not video.get("subtitle") and not config.get("description_only_if_no_subtitle", True):
                    video["decision"] = {"keep": False, "stage": "subtitle", "reason": "no subtitle"}
                    candidate_path.write_text(json.dumps(video, ensure_ascii=False, indent=2), encoding="utf-8")
                    log("Skipping video: no subtitle and description-only is disabled.")
                    seen.add(bvid)
                    seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")
                    continue
                if manual_link:
                    content_decision = {"keep": True, "score": 100.0, "reason": "manually shared link; full processing requested", "topics": []}
                elif explicit_topic:
                    content_decision = {"keep": True, "score": 90.0, "reason": "explicit search topic; summarize available evidence", "topics": [query_term]}
                else:
                    content_decision = triage_content(video, config)
                video["content_decision"] = content_decision
                video["decision"] = {"keep": content_decision["keep"], "stage": "content", **content_decision}
                candidate_path.write_text(json.dumps(video, ensure_ascii=False, indent=2), encoding="utf-8")
                if not content_decision["keep"]:
                    log(f"Stopping after opening excerpt: score={content_decision['score']} reason={content_decision['reason']}")
                    seen.add(bvid)
                    seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")
                    continue
                log(f"Content accepted: score={content_decision['score']} reason={content_decision['reason']}")
                media_cfg = dict(config.get("media_understanding") or {})
                if (manual_link or explicit_topic) and media_cfg.get("enabled"):
                    try:
                        from media_understanding import enrich_video_media
                        media_result = enrich_video_media(context, video, config, int(video.get("cid") or 0), review_level=args.review_level)
                        video.update(media_result)
                        candidate_path.write_text(json.dumps(video, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception as media_error:
                        video["media_understanding"] = {"attempted": True, "audio_ok": False, "visual_ok": False, "errors": [str(media_error)]}
                        log(f"Media enrichment failed: {media_error}")
                if explicit_topic:
                    mismatch_reason = animation_content_mismatch(video, query_term)
                    if mismatch_reason:
                        video["decision"] = {"keep": False, "stage": "content_mismatch", "reason": mismatch_reason}
                        candidate_path.write_text(json.dumps(video, ensure_ascii=False, indent=2), encoding="utf-8")
                        log(f"Skipping content mismatch: {bvid} reason={mismatch_reason}")
                        seen.add(bvid)
                        seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")
                        continue
                summary = summarize(video, config)
                if not str(summary or "").strip():
                    raise RuntimeError("DeepSeek returned an empty summary")
                note = (
                    f"B站视频长期记忆\n"
                    f"标题：{video['title']}\n"
                    f"UP主：{video['owner']}\n"
                    f"链接：{video['url']}\n"
                    f"采集时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(video['collected_at']))}\n\n"
                    f"{summary}\n"
                )
                summary_path = DATA_DIR / "summaries" / f"{bvid}.md"
                summary_path.write_text(note, encoding="utf-8")
                accepted += 1
                log(f"摘要已保存：{summary_path}")
                if args.do_import:
                    scope = args.scope or str(config.get("memory_scope", "global"))
                    chat_id = args.chat_id or str(config.get("chat_id", ""))
                    import_to_memory(note, f"bilibili_{bvid}.txt", scope, chat_id, config)
                    imported += 1
                    if should_deliver_public_comment(manual_link, bool(getattr(args, "publish_comment", False)), bool(args.suppress_comment)):
                        try:
                            from bili_dm_service import Store, deliver_comment_once, one_line_summary
                            store = Store()
                            try:
                                existing = store.get_task(bvid) or {}
                                store.upsert_task(
                                    bvid,
                                    video["url"],
                                    str(existing.get("msg_key") or ""),
                                    "completed",
                                    source=str(existing.get("source") or "bilibili_dm"),
                                    delivery_target=existing.get("delivery_target"),
                                    source_message_key=existing.get("source_message_key"),
                                    summary=note,
                                    title=video["title"],
                                    one_line=one_line_summary(note, video["title"]),
                                    memory_imported=1,
                                )
                                comment_result = deliver_comment_once(store, bvid, note, video["title"])
                                log(f"Comment delivery: status={comment_result.get('status')} rpid={comment_result.get('rpid') or 0} verified={bool(comment_result.get('verified'))}")
                            finally:
                                store.close()
                        except Exception as comment_error:
                            log(f"Comment delivery skipped without blocking video memory: {comment_error}")
                seen.add(bvid)
                seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")
                if accepted >= target_count:
                    break
                if index < len(unique):
                    time.sleep(random.uniform(float(config.get("delay_min_seconds", 4)), float(config.get("delay_max_seconds", 10))))
            success_count = collection_success_count(accepted, imported, bool(args.do_import))
            checked = min(index, len(unique))
            if success_count < target_count:
                log(f"任务未完成：要求 {target_count} 个可看视频，实际完成 {success_count} 个；已检查 {checked} 个候选，候选池已用尽。")
                return 2
            log(f"完成：目标 {target_count} 个，接受 {accepted} 个，导入 {imported} 个，检查候选 {checked} 个。")
            return 0
        finally:
            context.close()


def status_command() -> int:
    print(f"项目目录：{ROOT}")
    print(f"浏览器配置：{'已存在' if PROFILE_DIR.exists() else '尚未登录'}")
    print(f"登录状态文件：{'已存在' if (ROOT / 'storage_state.json').exists() else '未生成'}")
    try:
        with socket.create_connection((EMBEDDING_HOST, EMBEDDING_PORT), timeout=2):
            print("本地记忆向量服务：在线")
    except OSError:
        print("本地记忆向量服务：离线")
    try:
        session = webui_session()
        response = session.get(WEBUI_BASE + "/api/webui/memory/runtime/self-check", timeout=20)
        data = response.json()
        report = data.get("report") or {}
        print(
            "A-Memorix："
            f"ok={report.get('ok')} ready={data.get('runtime_ready')} "
            f"degraded={data.get('degraded')} vector_dim={report.get('vector_store_dimension')}"
        )
    except Exception as error:
        print(f"A-Memorix：检查失败 {error}")
    return 0


def resolve_command(args: argparse.Namespace) -> int:
    config = load_config()
    query = str(args.query or "").strip()
    candidates = resolve_video_candidates(query, config, int(args.max or 1), bool(args.headless))
    if not candidates:
        print("SEARCH_RESULT_EMPTY=true")
        return 0
    for item in candidates:
        title = re.sub(r"[\r\n|]+", " ", str(item.get("title") or "")).strip()
        print(f"SEARCH_RESULT|{item['bvid']}|{item['score']}|{title}")
    return 0


def music_meta_command(args: argparse.Namespace) -> int:
    requested_url = str(args.url or "").strip()
    from playwright.sync_api import sync_playwright
    config = load_config()
    bvid = bvid_from_url(str(args.url or "")) or str(args.url or "").strip()
    if not bvid:
        raise RuntimeError("invalid bvid")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        network_urls = []
        page.on("response", lambda response: network_urls.append(response.url) if re.search(r"music|bgm|song", response.url, re.I) else None)
        try:
            if not ensure_login(context, wait_seconds=2):
                raise RuntimeError("Bilibili login unavailable")
            page.goto(f"https://www.bilibili.com/video/{bvid}/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(10000)
            for _ in range(3):
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(1200)
            info = page.evaluate("""
                () => {
                  const out = { lines: [], elements: [], state: [] };
                  const marker = /(?:BGM|music|song)/i;
                  const text = document.body ? document.body.innerText || '' : '';
                  out.lines = text.split(/\\n+/).map(s => s.trim()).filter(s => s && marker.test(s)).slice(0, 80);
                  for (const el of document.querySelectorAll('*')) {
                    const sig = ((el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : '') + ' ' + (el.getAttribute('data-title') || '')).trim();
                    if (marker.test(sig)) {
                      const value = (el.innerText || el.textContent || el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
                      const link = (el.closest('a') && el.closest('a').href) || el.getAttribute('href') || '';
                      if (value) out.elements.push({tag: el.tagName, cls: sig.slice(0, 200), text: value.slice(0, 500), href: link.slice(0, 500)});
                    }
                  }
                  const seen = new WeakSet();
                  const walk = (obj, path, depth) => {
                    if (!obj || depth > 5 || out.state.length >= 100 || typeof obj !== 'object') return;
                    if (seen.has(obj)) return; seen.add(obj);
                    for (const [key, value] of Object.entries(obj)) {
                      const next = path ? path + '.' + key : key;
                      if (marker.test(key)) out.state.push({path: next, value: (() => { try { return JSON.stringify(value).slice(0, 800); } catch { return String(value).slice(0, 800); } })()});
                      if (value && typeof value === 'object') walk(value, next, depth + 1);
                    }
                  };
                  if (window.__INITIAL_STATE__) walk(window.__INITIAL_STATE__, '__INITIAL_STATE__', 0);
                  return out;
                }
            """)
            try:
                locator = page.locator(".tag-link.bgm-link, .bpx-player-music-marquee-content").first
                if locator.count() > 0:
                    locator.click(force=True, timeout=5000)
                    page.wait_for_timeout(5000)
                    info["after_click_text"] = page.evaluate("() => (document.body.innerText || '').split(/\\n+/).map(s => s.trim()).filter(Boolean).slice(0, 240)")
                    info["after_click_pages"] = [{"url": item.url, "title": item.title()} for item in context.pages]
            except Exception as click_error:
                info["click_error"] = str(click_error)
            info["network_urls"] = list(dict.fromkeys(network_urls))[:80]
            info["bvid"] = bvid
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        finally:
            context.close()
def main() -> int:
    parser = argparse.ArgumentParser(description="B站视频摘要导入 MaiBot 长期记忆")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="打开专用 Edge 配置供扫码登录")
    collect = sub.add_parser("collect", help="从推荐流或指定链接采集视频并摘要")
    collect.add_argument("--url", action="append", help="指定视频 URL，可重复")
    collect.add_argument("--section-url", default="", help="B站分区页面 URL，例如 https://www.bilibili.com/v/knowledge/")
    collect.add_argument("--tag", default="", help="Bilibili tag, e.g. AI")
    collect.add_argument("--search-keyword", default="", help="Bilibili search keyword, e.g. AI")
    collect.add_argument("--max", type=int, default=0, help="本次最多处理多少个")
    collect.add_argument("--import", dest="do_import", action="store_true", help="将摘要导入 A-Memorix")
    collect.add_argument("--scope", choices=["global", "chat"], help="记忆作用域")
    collect.add_argument("--chat-id", default="", help="scope=chat 时使用的聊天流 ID")
    collect.add_argument("--force", action="store_true", help="忽略已处理记录")
    collect.add_argument("--ignore-preference", action="store_true", help="Bypass preference filters for manually shared links")
    collect.add_argument("--publish-comment", action="store_true", help="Publish high-confidence summaries as Bilibili comments")
    collect.add_argument("--suppress-comment", action="store_true", help="Do not publish a public comment for this run")
    collect.add_argument("--review-level", choices=["rough", "detailed"], default="rough", help="Visual review density")
    collect.add_argument("--headless", action="store_true", help="无界面运行")
    resolve = sub.add_parser("resolve", help="Resolve a search query to candidate BV ids")
    resolve.add_argument("--query", required=True)
    resolve.add_argument("--max", type=int, default=1)
    resolve.add_argument("--headless", action="store_true")
    music_meta = sub.add_parser("music-meta", help="Inspect Bilibili page music metadata")
    music_meta.add_argument("--url", required=True)
    sub.add_parser("chats", help="列出 A-Memorix 可选聊天流")
    sub.add_parser("status", help="检查登录和本地记忆状态")
    args = parser.parse_args()
    if args.command == "login":
        return login_command()
    if args.command == "collect":
        return collect_command(args)
    if args.command == "resolve":
        return resolve_command(args)
    if args.command == "music-meta":
        return music_meta_command(args)
    if args.command == "chats":
        list_chat_targets()
        return 0
    if args.command == "status":
        return status_command()
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已取消。")
        raise SystemExit(130)
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)
