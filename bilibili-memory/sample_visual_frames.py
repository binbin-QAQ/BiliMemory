from __future__ import annotations

import argparse
import math
from pathlib import Path

import cv2
import numpy as np


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan video at a fixed frame rate and keep representative frames")
    parser.add_argument("--video", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--scan-fps", type=float, default=15.0)
    parser.add_argument("--max-frames", type=int, default=18)
    args = parser.parse_args()

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise SystemExit(f"cannot open video: {args.video}")
    source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    scan_fps = min(source_fps, max(1.0, float(args.scan_fps)))
    sample_stride = max(1.0, source_fps / scan_fps)
    total_samples = max(1, int(math.ceil(total_frames / sample_stride))) if total_frames else 0
    max_frames = max(2, int(args.max_frames))
    samples_per_bin = max(1.0, total_samples / max_frames)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()

    read_index = 0
    sample_index = 0
    next_sample = 0.0
    previous_small = None
    current_bin = -1
    best_frame = None
    best_score = -1.0
    best_sample_index = 0
    saved = 0

    def save_best() -> None:
        nonlocal saved
        if best_frame is None:
            return
        timestamp = best_sample_index / scan_fps
        path = out_dir / f"frame_{saved:05d}_{timestamp:05.1f}s.jpg"
        if cv2.imwrite(str(path), best_frame):
            saved += 1

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if read_index + 1e-9 < next_sample:
            read_index += 1
            continue
        small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
        small_gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if previous_small is None:
            score = 1.0
        else:
            score = float(np.mean(cv2.absdiff(small_gray, previous_small))) / 255.0
        previous_small = small_gray
        bin_index = min(max_frames - 1, int(sample_index / samples_per_bin))
        if bin_index != current_bin:
            if current_bin >= 0:
                save_best()
            current_bin = bin_index
            best_frame = frame.copy()
            best_score = score
            best_sample_index = sample_index
        elif score > best_score:
            best_frame = frame.copy()
            best_score = score
            best_sample_index = sample_index
        sample_index += 1
        next_sample = sample_index * sample_stride
        read_index += 1
    save_best()
    cap.release()
    print(f"scan_fps={scan_fps:.2f} source_fps={source_fps:.2f} saved={saved} out={out_dir}", flush=True)
    return 0 if saved else 2


if __name__ == "__main__":
    raise SystemExit(main())
