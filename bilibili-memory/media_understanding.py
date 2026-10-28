from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"
TRANSCRIBE_SCRIPT = ROOT / "transcribe_audio.py"
VISUAL_SCRIPT = ROOT / "visual_analysis.py"
VISUAL_SAMPLER = ROOT / "sample_visual_frames.py"
from runtime_paths import MAIBOT_ROOT, MEDIA_PYTHON
MAIBOT_MODEL_CONFIG = MAIBOT_ROOT / "config" / "model_config.toml"
FRAME_PYTHON = MEDIA_PYTHON


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def options(config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("media_understanding") or {})


def api_get(context: Any, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = context.request.get(
        url,
        params=params,
        headers={"Referer": "https://www.bilibili.com/"},
        timeout=60000,
    )
    if not response.ok:
        raise RuntimeError(f"Bilibili API HTTP {response.status}")
    payload = response.json()
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"Bilibili API error {payload.get('code')}: {payload.get('message')}")
    return payload


def fetch_playurl(context: Any, bvid: str, cid: int, *, fnval: int, qn: int = 32) -> dict[str, Any]:
    payload = api_get(
        context,
        "https://api.bilibili.com/x/player/playurl",
        {"bvid": bvid, "cid": cid, "fnval": fnval, "fnver": 0, "qn": qn, "fourk": 1},
    )
    return payload.get("data") or {}


def download_bytes(context: Any, url: str, max_bytes: int) -> bytes:
    if url.startswith("//"):
        url = "https:" + url
    response = context.request.get(
        url,
        headers={"Referer": "https://www.bilibili.com/", "User-Agent": "Mozilla/5.0"},
        timeout=180000,
    )
    if not response.ok:
        raise RuntimeError(f"media download HTTP {response.status}")
    data = response.body()
    if len(data) > max_bytes:
        raise RuntimeError(f"media too large: {len(data)} bytes")
    return data


AUDIO_DECODE_JS = r"""
async (b64) => {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const decoder = new AudioContext();
  let audio;
  try {
    audio = await decoder.decodeAudioData(bytes.buffer.slice(0));
  } finally {
    await decoder.close();
  }
  const sampleRate = 16000;
  const frames = Math.ceil(audio.duration * sampleRate);
  const offline = new OfflineAudioContext(1, frames, sampleRate);
  const source = offline.createBufferSource();
  source.buffer = audio;
  source.connect(offline.destination);
  source.start();
  const rendered = await offline.startRendering();
  const pcm = rendered.getChannelData(0);
  const wav = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(wav);
  const text = (offset, value) => { for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i)); };
  text(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); text(8, 'WAVE');
  text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, 'data'); view.setUint32(40, pcm.length * 2, true);
  let offset = 44;
  for (let i = 0; i < pcm.length; i++, offset += 2) {
    const value = Math.max(-1, Math.min(1, pcm[i]));
    view.setInt16(offset, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  const raw = new Uint8Array(wav);
  let result = '';
  const chunk = 0x8000;
  for (let i = 0; i < raw.length; i += chunk) {
    result += String.fromCharCode.apply(null, raw.subarray(i, i + chunk));
  }
  return btoa(result);
}
"""


def wav_duration(path: Path) -> float:
    try:
        import wave
        with wave.open(str(path), "rb") as stream:
            return stream.getnframes() / float(stream.getframerate() or 1)
    except Exception:
        return 0.0


def extract_audio(context: Any, bvid: str, cid: int, opts: dict[str, Any]) -> tuple[Path | None, str]:
    try:
        play = fetch_playurl(context, bvid, cid, fnval=16, qn=32)
        dash = play.get("dash") or {}
        audio_items = [item for item in (dash.get("audio") or []) if item.get("base_url")]
        if not audio_items:
            return None, "no DASH audio"
        audio_items.sort(key=lambda item: int(item.get("bandwidth") or 0))
        item = audio_items[0]
        max_bytes = int(float(opts.get("max_media_mb", 250)) * 1024 * 1024)
        raw = download_bytes(context, str(item["base_url"]), max_bytes)
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        raw_path = MEDIA_DIR / f"{bvid}.m4a"
        wav_path = MEDIA_DIR / f"{bvid}.wav"
        raw_path.write_bytes(raw)
        log(f"Audio downloaded: {len(raw) / 1024 / 1024:.1f} MB")
        page = context.pages[0] if context.pages else context.new_page()
        result = page.evaluate(AUDIO_DECODE_JS, base64.b64encode(raw).decode("ascii"))
        wav_path.write_bytes(base64.b64decode(result))
        duration = wav_duration(wav_path)
        log(f"Audio decoded to WAV: {duration:.1f}s")
        return wav_path, ""
    except Exception as error:
        log(f"Audio extraction failed: {error}")
        return None, str(error)


def transcribe_audio(audio_path: Path, bvid: str, opts: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    if not MEDIA_PYTHON.exists() or not TRANSCRIBE_SCRIPT.exists():
        return "", "Whisper environment missing"
    output = MEDIA_DIR / f"{bvid}.transcript.json"
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "HF_ENDPOINT": "https://hf-mirror.com", "HF_HUB_DISABLE_XET": "1", "HF_HOME": str(DATA_DIR / "hf-cache")})
    model = str(opts.get("whisper_model") or "openai/whisper-small")
    log(f"Transcribing audio with Whisper: {model}")
    try:
        result = subprocess.run(
            [str(MEDIA_PYTHON), str(TRANSCRIBE_SCRIPT), "--audio", str(audio_path), "--out", str(output), "--model", model],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(opts.get("transcribe_timeout_seconds", 3600)),
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(details or f"Whisper exited {result.returncode}")
        data = json.loads(output.read_text(encoding="utf-8"))
        text = str(data.get("formatted") or data.get("text") or "").strip()
        max_chars = int(opts.get("max_transcript_chars", 50000))
        text = text[:max_chars]
        log(f"Whisper transcript ready: {len(text)} chars")
        return text, ""
    except Exception as error:
        log(f"Whisper transcription failed: {error}")
        return "", str(error)


def extract_visual_frames(context: Any, bvid: str, cid: int, duration: int, opts: dict[str, Any]) -> tuple[Path | None, str]:
    if not FRAME_PYTHON.exists() or not VISUAL_SAMPLER.exists():
        return None, "frame extraction environment missing"
    staging_root: Path | None = None
    try:
        play = fetch_playurl(context, bvid, cid, fnval=1, qn=32)
        durl = [item for item in (play.get("durl") or []) if item.get("url")]
        if not durl:
            return None, "no combined video stream"
        max_bytes = int(float(opts.get("max_media_mb", 250)) * 1024 * 1024)
        max_frames = max(2, int(opts.get("max_frames", 10)))
        scan_fps = max(1.0, float(opts.get("scan_fps", 15)))
        frames_root = DATA_DIR / "visual_frames"
        signature = json.dumps({"bvid": bvid, "cid": cid, "duration": duration, "frames": max_frames, "scan_fps": scan_fps, "urls": [item.get("url") for item in durl]}, ensure_ascii=False, sort_keys=True)
        digest = hashlib.md5(signature.encode("utf-8")).hexdigest()[:8]
        frame_dir = frames_root / f"v_{digest}"
        existing = sorted(frame_dir.glob("frame_*.jpg")) if frame_dir.exists() else []
        if existing:
            log(f"Reusing {len(existing)} visual frames: {frame_dir}")
            return frame_dir, ""
        frame_dir.mkdir(parents=True, exist_ok=True)
        staging_root = frames_root / f".local_{uuid.uuid4().hex}"
        staging_root.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        offset_seconds = 0.0
        global_index = 0
        for segment_index, item in enumerate(durl, 1):
            remaining = max_bytes - total_bytes
            if remaining <= 0:
                raise RuntimeError(f"media too large: exceeds {opts.get('max_media_mb', 250)} MB")
            raw = download_bytes(context, str(item["url"]), remaining)
            total_bytes += len(raw)
            ext = ".mp4" if b"ftyp" in raw[:64] else ".flv"
            video_path = MEDIA_DIR / f"{bvid}_seg{segment_index:03d}{ext}"
            MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            video_path.write_bytes(raw)
            log(f"Video segment {segment_index}/{len(durl)} downloaded: {len(raw) / 1024 / 1024:.1f} MB")
            segment_output = staging_root / f"segment_{segment_index:03d}"
            result = subprocess.run(
                [str(FRAME_PYTHON), str(VISUAL_SAMPLER), "--video", str(video_path), "--out", str(segment_output), "--scan-fps", f"{scan_fps:.3f}", "--max-frames", str(max_frames)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=int(opts.get("frame_timeout_seconds", 600)),
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "").strip()[-2000:])
            produced = sorted(segment_output.glob("frame_*.jpg"))
            for local_path in produced:
                match = re.search(r"_(\d+(?:\.\d+)?)s\.jpg$", local_path.name)
                local_seconds = float(match.group(1)) if match else 0.0
                target = frame_dir / f"frame_{global_index:05d}_{offset_seconds + local_seconds:05.1f}s.jpg"
                shutil.copy2(local_path, target)
                global_index += 1
            try:
                length_seconds = float(item.get("length") or 0) / 1000.0
            except (TypeError, ValueError):
                length_seconds = 0.0
            if length_seconds <= 0:
                length_seconds = float(duration or 0) / max(1, len(durl))
            offset_seconds += length_seconds
            for folder in staging_root.glob("segment_*"):
                shutil.rmtree(folder, ignore_errors=True)
        frames = sorted(frame_dir.glob("frame_*.jpg"))
        if not frames:
            return None, "frame directory missing"
        log(f"Visual frames ready: {len(frames)} from {len(durl)} segment(s)")
        return frame_dir, ""
    except Exception as error:
        log(f"Visual frame extraction failed: {error}")
        return None, str(error)
    finally:
        if staging_root and staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)

def qwen_vision_credentials(opts: dict[str, Any]) -> tuple[str, str, str]:
    data = tomllib.loads(MAIBOT_MODEL_CONFIG.read_text(encoding="utf-8"))
    model_name = str(opts.get("vision_api_model") or "qwen3-vl-plus")
    provider_name = str(opts.get("vision_api_provider") or "Alibaba")
    model = next((item for item in data.get("models") or [] if item.get("name") == model_name), None)
    provider = next((item for item in data.get("api_providers") or [] if item.get("name") == provider_name), None)
    if not model or not provider:
        raise RuntimeError("Qwen vision model or provider is not configured")
    base_url = str(provider.get("base_url") or "").rstrip("/")
    api_key = str(provider.get("api_key") or "")
    identifier = str(model.get("model_identifier") or model_name)
    if not base_url or not api_key:
        raise RuntimeError("Qwen vision provider credentials are incomplete")
    return base_url, api_key, identifier


def qwen_describe_one(frame_path: Path, base_url: str, api_key: str, model: str, opts: dict[str, Any]) -> str:
    match = re.search(r"_(\d+(?:\.\d+)?)s\.jpg$", frame_path.name)
    stamp = f"{int(float(match.group(1))) // 60:02d}:{int(float(match.group(1))) % 60:02d}" if match else frame_path.name
    encoded = base64.b64encode(frame_path.read_bytes()).decode("ascii")
    allow_lyrics = bool(opts.get("allow_lyrics"))
    if allow_lyrics:
        system_prompt = "\u4f60\u53ea\u63cf\u8ff0\u5355\u5e27\u753b\u9762\u4e2d\u660e\u786e\u53ef\u89c1\u7684\u8bc1\u636e\u3002\u53ef\u4ee5\u8bc6\u522b\u5e76\u8f6c\u5199\u753b\u9762\u4e0a\u7684\u6b4c\u8bcd\u6216\u5b57\u5e55\uff0c\u4f46\u5fc5\u987b\u6807\u660e\u662f\u6b4c\u8bcd\uff0c\u4e0d\u5f97\u5f53\u6210\u5267\u60c5\u3001\u53d9\u8ff0\u6216\u89c6\u9891\u4eba\u7269\u7684\u5bf9\u767d\u3002\u5fc5\u987b\u4f7f\u7528\u7b80\u4f53\u4e2d\u6587\u3002"
        text_prompt = "\u8bf7\u7528 2 \u5230 4 \u53e5\u7b80\u4f53\u4e2d\u6587\u63cf\u8ff0\u8fd9\u4e00\u5e27\u91cc\u660e\u786e\u53ef\u89c1\u7684\u4eba\u7269\u3001\u7269\u4f53\u3001\u52a8\u4f5c\u3001\u573a\u666f\u548c\u5c4f\u5e55\u6587\u5b57\u3002\u89c1\u5230\u6b4c\u8bcd\u6216\u5b57\u5e55\u65f6\u53ef\u4ee5\u5355\u72ec\u5f15\u7528\uff0c\u4f46\u6807\u6ce8\u4e3a\u6b4c\u8bcd\uff0c\u4e0d\u8981\u63a8\u65ad\u5267\u60c5\u3002\u5fc5\u987b\u4f7f\u7528\u4e2d\u6587\u3002"
    else:
        system_prompt = "\u4f60\u53ea\u63cf\u8ff0\u5355\u5e27\u753b\u9762\u4e2d\u660e\u786e\u53ef\u89c1\u7684\u8bc1\u636e\u3002\u4e0d\u5f97\u63a8\u65ad\u5267\u60c5\u3001\u8eab\u4efd\u3001\u60c5\u7eea\u6216\u753b\u9762\u5916\u4e8b\u4ef6\u3002\u5ffd\u7565\u6b4c\u8bcd\u3001BGM\u5b57\u5e55\u3001\u6b4c\u8bcd\u7ffb\u8bd1\u548c\u6eda\u52a8\u5b57\u5e55\uff0c\u4e0d\u8f6c\u5199\u8fd9\u4e9b\u6587\u5b57\u3002\u53ea\u53ef\u8bb0\u5f55\u975e\u6b4c\u8bcd\u7684\u6807\u9898\u3001\u6309\u94ae\u3001\u72b6\u6001\u548c\u754c\u9762\u63d0\u793a\u3002\u5fc5\u987b\u4f7f\u7528\u7b80\u4f53\u4e2d\u6587\u56de\u7b54\u3002"
        text_prompt = "\u8bf7\u7528 2 \u5230 4 \u53e5\u7b80\u4f53\u4e2d\u6587\u63cf\u8ff0\u8fd9\u4e00\u5e27\u91cc\u660e\u786e\u53ef\u89c1\u7684\u4eba\u7269\u3001\u7269\u4f53\u3001\u52a8\u4f5c\u3001\u573a\u666f\u548c\u975e\u6b4c\u8bcd\u6587\u5b57\u3002\u4e0d\u8981\u8bc6\u522b\u6216\u8f6c\u5199 BGM \u6b4c\u8bcd\u3001\u5b57\u5e55\u6b4c\u8bcd\u3001\u6b4c\u8bcd\u7ffb\u8bd1\u548c\u6eda\u52a8\u5b57\u5e55\u3002\u5fc5\u987b\u4f7f\u7528\u4e2d\u6587\u3002"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}},
                {"type": "text", "text": text_prompt},
            ]},
        ],
        "temperature": 0.1,
        "max_tokens": 220,
        "enable_thinking": False,
    }
    last_error = ""
    for attempt in range(3):
        try:
            response = requests.post(
                base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=int(opts.get("vision_api_timeout_seconds", 120)),
            )
            response.raise_for_status()
            data = response.json()
            message = ((data.get("choices") or [{}])[0].get("message") or {})
            text = str(message.get("content") or message.get("reasoning_content") or "").strip()
            has_chinese = bool(re.search(r"[\u4e00-\u9fff]", text))
            has_english_sentences = bool(re.search(r"[A-Za-z]{4,}(?:\s+[A-Za-z]{2,}){3,}", text))
            if text and (has_chinese or not has_english_sentences):
                return f"[{stamp}] {text}"
            last_error = "empty or non-Chinese vision response"
        except Exception as error:
            last_error = str(error)
        time.sleep(1 + attempt)
    raise RuntimeError(f"frame {stamp} failed: {last_error}")


def describe_frames_qwen_api(frame_dir: Path, bvid: str, opts: dict[str, Any]) -> tuple[str, str]:
    try:
        base_url, api_key, model = qwen_vision_credentials(opts)
        images = sorted(frame_dir.glob("frame_*.jpg"))
        max_frames = max(1, int(opts.get("max_frames", 18)))
        stride = max(1, (len(images) + max_frames - 1) // max_frames)
        images = images[::stride][:max_frames]
        if not images:
            return "", "no frames for Qwen API"
        workers = max(1, min(4, int(opts.get("vision_api_concurrency", 3))))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(lambda path: qwen_describe_one(path, base_url, api_key, model, opts), images))
        text = "\n".join(results)
        (frame_dir / "vision_answers.txt").write_text(text, encoding="utf-8")
        log(f"Qwen vision API ready: {len(images)} frames, {len(text)} chars")
        return text, ""
    except Exception as error:
        log(f"Qwen vision API failed: {error}")
        return "", str(error)


def describe_frames(frame_dir: Path, bvid: str, opts: dict[str, Any]) -> tuple[str, str]:
    if str(opts.get("vision_provider") or "local") == "qwen_api":
        text, error = describe_frames_qwen_api(frame_dir, bvid, opts)
        if text:
            return text, ""
        log(f"Qwen vision failed; local vision is disabled: {error}")
        return "", error
    if not MEDIA_PYTHON.exists() or not VISUAL_SCRIPT.exists():
        return "", "local vision environment missing"
    output = frame_dir / "vision_answers.txt"
    model = str(opts.get("vision_model") or "HuggingFaceTB/SmolVLM-500M-Instruct")
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "HF_ENDPOINT": "https://hf-mirror.com",
        "HF_HUB_DISABLE_XET": "1",
        "HF_HOME": str(DATA_DIR / "hf-cache"),
    })
    try:
        result = subprocess.run(
            [
                str(MEDIA_PYTHON), str(VISUAL_SCRIPT), "--images", str(frame_dir),
                "--out", str(output), "--model", model,
                "--max-frames", str(max(1, int(opts.get("max_frames", 10)))),
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(opts.get("vision_timeout_seconds", 1800)),
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()[-2500:]
            raise RuntimeError(details or f"visual model exited {result.returncode}")
        text = output.read_text(encoding="utf-8").strip()
        max_chars = int(opts.get("max_visual_chars", 16000))
        text = text[:max_chars]
        log(f"Local visual analysis ready: {len(text)} chars")
        return text, ""
    except Exception as error:
        log(f"Local visual analysis failed: {error}")
        return "", str(error)


def music_lyrics_analysis(video: dict[str, Any]) -> bool:
    meta = " ".join([
        str(video.get("title") or ""),
        str(video.get("description") or ""),
        " ".join(str(item) for item in (video.get("tags") or [])),
    ]).casefold()
    markers = [
        "mv", "music", "song", "vocaloid", "rolling girl", "\u97f3\u4e50", "\u6b4c\u66f2", "\u6b4c\u8bcd",
        "\u7ffb\u5531", "\u539f\u521b\u66f2", "\u6f14\u5531", "\u6b4c\u59ec", "\u97f3\u4e50\u8d4f\u6790", "\u8d4f\u6790",
        "\u4e50\u8bc4", "\u542c\u611f", "\u7eaf\u97f3\u4e50", "\u6f14\u594f", "\u94a2\u7434", "\u4f34\u594f",
    ]
    return any(marker in meta for marker in markers)


def visual_heavy(video: dict[str, Any]) -> bool:
    meta = " ".join([
        str(video.get("title") or ""),
        str(video.get("description") or ""),
        " ".join(str(item) for item in (video.get("tags") or [])),
    ]).casefold()
    markers = [
        "mv", "music", "song", "rolling girl", "\u97f3\u4e50", "\u7ffb\u5531", "\u539f\u521b\u66f2",
        "\u6f14\u594f", "\u7eaf\u97f3\u4e50", "\u94a2\u7434", "\u624b\u4e66", "\u52a8\u753b", "mad", "amv",
        "\u63cf\u6539", "\u6df7\u526a", "\u526a\u8f91", "\u5408\u96c6", "\u821e\u8e48", "mmd", "bgm",
        "\u6e38\u620f", "\u7535\u7ade", "\u5b9e\u51b5", "\u901a\u5173", "\u653b\u7565", "\u5bf9\u5c40", "\u6392\u4f4d",
        "\u51f9\u5206", "\u51b2\u699c", "\u56fd\u699c", "\u5173\u5361", "\u64cd\u4f5c", "gameplay", "walkthrough",
        "\u756a\u5267", "\u52a8\u6f2b", "anime",
    ]
    return any(marker in meta for marker in markers)


def visual_required(video: dict[str, Any]) -> bool:
    return not video.get("subtitle") or visual_heavy(video)


def visual_duration_factor(duration_seconds: int) -> float:
    duration = max(0, int(duration_seconds or 0))
    if duration <= 0:
        return 1.0
    if duration <= 60:
        return 0.7
    if duration <= 300:
        return 1.0
    if duration <= 900:
        return 1.35
    return 1.6


def visual_frame_count(video: dict[str, Any], media: dict[str, Any], review_level: str) -> int:
    has_subtitle = bool(str(video.get("subtitle") or "").strip())
    low_frame_mode = has_subtitle and not visual_heavy(video)
    if review_level == "detailed":
        key = "detailed_subtitle_frames" if low_frame_mode else "detailed_max_frames"
        base = int(media.get(key, 30 if low_frame_mode else 60))
    else:
        key = "rough_subtitle_frames" if low_frame_mode else "rough_max_frames"
        base = int(media.get(key, 6 if low_frame_mode else 18))
    factor = visual_duration_factor(int(video.get("duration_seconds") or 0))
    return max(2, min(120, int(round(base * factor))))


def enrich_video_media(context: Any, video: dict[str, Any], config: dict[str, Any], cid: int, review_level: str = "rough") -> dict[str, Any]:
    opts = options(config)
    if not opts.get("enabled", False):
        return {}
    opts = dict(opts)
    media = dict(config.get("media_understanding") or {})
    opts["max_frames"] = visual_frame_count(video, media, review_level)
    opts["force_visual"] = True if review_level == "detailed" else bool(media.get("visual_always_rough", True))
    opts["allow_lyrics"] = music_lyrics_analysis(video)
    bvid = str(video.get("bvid") or "")
    duration = int(video.get("duration_seconds") or 0)
    transcript = ""
    visual = ""
    errors: list[str] = []
    if opts.get("audio_transcription", True) and not video.get("subtitle"):
        audio_path, error = extract_audio(context, bvid, cid, opts)
        if error:
            errors.append("audio: " + error)
        if audio_path:
            transcript, error = transcribe_audio(audio_path, bvid, opts, config)
            if error:
                errors.append("transcribe: " + error)
    if opts.get("visual_analysis", True) and (opts.get("force_visual") or visual_required(video)):
        frame_dir, error = extract_visual_frames(context, bvid, cid, duration, opts)
        if error:
            errors.append("frames: " + error)
        if frame_dir:
            visual, error = describe_frames(frame_dir, bvid, opts)
            if error:
                errors.append("vision: " + error)
    return {
        "audio_transcript": transcript,
        "visual_analysis": visual,
        "media_understanding": {
            "attempted": True,
            "review_level": review_level,
            "max_frames": opts.get("max_frames"),
            "audio_ok": bool(transcript),
            "visual_ok": bool(visual),
            "errors": errors,
        },
    }
