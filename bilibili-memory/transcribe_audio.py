from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Whisper transcription")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default="openai/whisper-small")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        raise SystemExit("audio file not found")

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    import numpy as np
    import soundfile as sf

    data, sample_rate = sf.read(str(audio_path), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sample_rate != 16000:
        import librosa
        data = librosa.resample(np.asarray(data), orig_sr=sample_rate, target_sr=16000).astype(np.float32)
        sample_rate = 16000

    import torch
    from transformers import pipeline

    kwargs = {
        "chunk_length_s": 30,
        "stride_length_s": 5,
        "return_timestamps": True,
        "generate_kwargs": {"language": None if args.language == "auto" else args.language, "task": "transcribe"},
    }
    started = time.time()
    try:
        pipe = pipeline("automatic-speech-recognition", model=args.model, device=0, torch_dtype=torch.float16)
        result = pipe({"raw": np.asarray(data), "sampling_rate": sample_rate}, **kwargs)
    except Exception as gpu_error:
        print(f"GPU transcription failed, retrying CPU: {gpu_error}", file=sys.stderr)
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        pipe = pipeline("automatic-speech-recognition", model=args.model, device=-1, torch_dtype=torch.float32)
        result = pipe({"raw": np.asarray(data), "sampling_rate": sample_rate}, **kwargs)

    text = str(result.get("text") or "").strip()
    chunks = []
    for item in result.get("chunks") or []:
        timestamp = item.get("timestamp") or (None, None)
        start = float(timestamp[0] or 0) if isinstance(timestamp, (list, tuple)) else 0.0
        chunk_text = str(item.get("text") or "").strip()
        if chunk_text:
            chunks.append({"start": start, "time": format_time(start), "text": chunk_text})
    formatted = "\n".join(f"[{item['time']}] {item['text']}" for item in chunks) or text
    output = {
        "model": args.model,
        "language": args.language,
        "duration_seconds": round(len(data) / max(1, sample_rate), 2),
        "elapsed_seconds": round(time.time() - started, 2),
        "text": text,
        "formatted": formatted,
        "chunks": chunks,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(formatted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
