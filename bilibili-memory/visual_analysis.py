from __future__ import annotations

import argparse
import re
from pathlib import Path


def frame_time(path: Path) -> str:
    match = re.search(r"_(\d+(?:\.\d+)?)s\.jpg$", path.name)
    if not match:
        return path.name
    total = int(float(match.group(1)))
    return f"{total // 60:02d}:{total % 60:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Local frame description with a small vision model")
    parser.add_argument("--images", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default="Qwen/Qwen2-VL-2B-Instruct")
    parser.add_argument("--max-frames", type=int, default=10)
    args = parser.parse_args()

    try:
        from PIL import Image
    except ImportError as error:
        raise SystemExit("Pillow is missing from the BiliMemory media environment") from error

    import torch
    from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

    image_dir = Path(args.images)
    images = sorted(image_dir.glob("frame_*.jpg"))
    if not images:
        raise SystemExit("no extracted frames found")
    stride = max(1, (len(images) + max(1, args.max_frames) - 1) // max(1, args.max_frames))
    images = images[::stride][: max(1, args.max_frames)]

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    print(f"loading vision model {args.model} on {device}", flush=True)
    processor = AutoProcessor.from_pretrained(args.model, min_pixels=256 * 256, max_pixels=768 * 768)
    model = Qwen2VLForConditionalGeneration.from_pretrained(args.model, torch_dtype=dtype)
    model.to(device)
    model.eval()

    question = (
        "You are describing one frame from a video. Describe only what is visibly present: "
        "people, objects, actions, setting, and on-screen text. Do not infer plot, identity, "
        "emotion, or events outside this frame. Answer in 2 to 4 concise English sentences."
    )
    results = []
    with torch.inference_mode():
        for index, image_path in enumerate(images, 1):
            stamp = frame_time(image_path)
            try:
                image = Image.open(image_path).convert("RGB")
                messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": question}]}]
                prompt = processor.apply_chat_template(messages, add_generation_prompt=True)
                inputs = processor(text=[prompt], images=[image], return_tensors="pt")
                inputs = {key: value.to(device) if hasattr(value, "to") else value for key, value in inputs.items()}
                generated = model.generate(**inputs, max_new_tokens=128, do_sample=False)
                input_length = inputs["input_ids"].shape[1]
                answer = processor.batch_decode(generated[:, input_length:], skip_special_tokens=True)[0].strip()
                results.append(f"[{stamp}] {answer}")
                print(f"described {index}/{len(images)} {image_path.name}", flush=True)
            except Exception as error:
                results.append(f"[{stamp}] frame analysis failed: {error}")
                print(f"frame failed {index}/{len(images)} {image_path.name}: {error}", flush=True)
                if device == "cuda":
                    torch.cuda.empty_cache()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(results), encoding="utf-8")
    print("written", out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
