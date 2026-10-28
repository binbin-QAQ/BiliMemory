from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import time
import tomllib
from pathlib import Path


DEFAULT_EMBEDDING_URL = "http://127.0.0.1:11435/v1"
DEFAULT_MODEL_NAME = "local-bge-small-zh"
DEFAULT_MODEL_ID = "bge-small-zh-v1.5"
DEFAULT_DIMENSION = 512


def module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def section_bounds(lines: list[str], header: str) -> tuple[int, int] | None:
    start = next((index for index, line in enumerate(lines) if line.split('#', 1)[0].strip() == header), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines):
        stripped = lines[end].split('#', 1)[0].strip()
        if stripped.startswith("[") and stripped != header:
            break
        end += 1
    return start, end


def set_key(lines: list[str], start: int, end: int, key: str, value: str) -> None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(start + 1, end):
        if pattern.match(lines[index]):
            lines[index] = f"{key} = {value}\n"
            return
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, f"{key} = {value}\n")


def set_table_key(lines: list[str], header: str, key: str, value: str) -> None:
    bounds = section_bounds(lines, header)
    if bounds is None:
        lines.extend(["\n", f"{header}\n", f"{key} = {value}\n"])
        return
    set_key(lines, bounds[0], bounds[1], key, value)


def find_array_block(lines: list[str], header: str, name: str) -> tuple[int, int] | None:
    name_pattern = re.compile(rf"^\s*name\s*=\s*[\"']{re.escape(name)}[\"']")
    index = 0
    while index < len(lines):
        if lines[index].split('#', 1)[0].strip() == header:
            end = index + 1
            while end < len(lines):
                stripped = lines[end].split('#', 1)[0].strip()
                if stripped.startswith("[") and stripped != header:
                    break
                end += 1
            if any(name_pattern.match(lines[pos]) for pos in range(index + 1, end)):
                return index, end
            index = end
        else:
            index += 1
    return None


def model_block(model_name: str, model_id: str, provider: str) -> list[str]:
    return [
        "\n",
        "[[models]]\n",
        'model_type = "embedding"\n',
        f'model_identifier = "{model_id}"\n',
        f'name = "{model_name}"\n',
        f'api_provider = "{provider}"\n',
        "price_in = 0.0\n",
        "cache = false\n",
        "cache_price_in = 0.0\n",
        "price_out = 0.0\n",
        "price_periods = []\n",
        "send_temperature = false\n",
        "force_stream_mode = false\n",
        "visual = false\n",
        "extra_params = {}\n",
    ]


def provider_block(provider: str, embedding_url: str) -> list[str]:
    return [
        "\n",
        "[[api_providers]]\n",
        f'name = "{provider}"\n',
        f'base_url = "{embedding_url}"\n',
        'api_key = ""\n',
        'client_type = "openai"\n',
        'auth_type = "none"\n',
        'auth_header_name = "Authorization"\n',
        'auth_header_prefix = "Bearer"\n',
        'auth_query_name = "api_key"\n',
        "default_headers = {}\n",
        "default_query = {}\n",
        'model_list_endpoint = "/models"\n',
        'reasoning_parse_mode = "auto"\n',
        'tool_argument_parse_mode = "auto"\n',
        "max_retry = 3\n",
        "timeout = 120\n",
        "retry_interval = 5\n",
    ]


def ensure_model(lines: list[str], model_name: str, model_id: str, provider: str) -> None:
    block = find_array_block(lines, "[[models]]", model_name)
    if block:
        start, end = block
        set_key(lines, start, end, "model_type", '"embedding"')
        set_key(lines, start, end, "model_identifier", f'"{model_id}"')
        set_key(lines, start, end, "api_provider", f'"{provider}"')
        return
    provider_index = next((i for i, line in enumerate(lines) if line.split('#', 1)[0].strip() == "[[api_providers]]"), len(lines))
    lines[provider_index:provider_index] = model_block(model_name, model_id, provider)


def ensure_provider(lines: list[str], provider: str, embedding_url: str) -> None:
    block = find_array_block(lines, "[[api_providers]]", provider)
    if block:
        start, end = block
        set_key(lines, start, end, "base_url", f'"{embedding_url}"')
        set_key(lines, start, end, "api_key", '""')
        set_key(lines, start, end, "client_type", '"openai"')
        set_key(lines, start, end, "auth_type", '"none"')
        return
    lines.extend(provider_block(provider, embedding_url))


def configure(mai_bot_root: Path, embedding_url: str, model_name: str, model_id: str, dimension: int, apply: bool) -> dict:
    bot_config_path = mai_bot_root / "config" / "bot_config.toml"
    model_config_path = mai_bot_root / "config" / "model_config.toml"
    result = {
        "aMemorixInstalled": module_available("src.A_memorix"),
        "botConfig": str(bot_config_path),
        "modelConfig": str(model_config_path),
        "applied": False,
        "backups": [],
        "settings": {
            "embeddingUrl": embedding_url,
            "modelName": model_name,
            "modelId": model_id,
            "dimension": dimension
        }
    }
    if not bot_config_path.is_file() or not model_config_path.is_file():
        result["error"] = "MaiBot configuration files were not found"
        return result

    bot_lines = bot_config_path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    set_table_key(bot_lines, "[a_memorix.plugin]", "enabled", "true")
    set_table_key(bot_lines, "[a_memorix.storage]", "data_dir", '"data/a-memorix"')
    set_table_key(bot_lines, "[a_memorix.embedding]", "model_name", f'"{model_name}"')
    set_table_key(bot_lines, "[a_memorix.embedding]", "dimension", str(dimension))
    set_table_key(bot_lines, "[a_memorix.embedding]", "dimension_request_mode", '"never"')
    bot_text = "".join(bot_lines)

    model_lines = model_config_path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    ensure_model(model_lines, model_name, model_id, "LocalEmbedding")
    ensure_provider(model_lines, "LocalEmbedding", embedding_url)
    set_table_key(model_lines, "[model_task_config.embedding]", "model_list", f'["{model_name}"]')
    model_text = "".join(model_lines)

    tomllib.loads(bot_text)
    tomllib.loads(model_text)
    result["valid"] = True
    if not apply:
        return result

    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    bot_backup = bot_config_path.with_name(f"{bot_config_path.name}.bilimemory-backup-{stamp}")
    model_backup = model_config_path.with_name(f"{model_config_path.name}.bilimemory-backup-{stamp}")
    shutil.copy2(bot_config_path, bot_backup)
    shutil.copy2(model_config_path, model_backup)
    bot_config_path.write_text(bot_text, encoding="utf-8", newline="")
    model_config_path.write_text(model_text, encoding="utf-8", newline="")
    (mai_bot_root / "data" / "a-memorix").mkdir(parents=True, exist_ok=True)
    result["applied"] = True
    result["backups"] = [str(bot_backup), str(model_backup)]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure MaiBot A-Memorix and local embedding")
    parser.add_argument("--mai-bot-root", required=True)
    parser.add_argument("--embedding-url", default=DEFAULT_EMBEDDING_URL)
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--dimension", type=int, default=DEFAULT_DIMENSION)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = configure(Path(args.mai_bot_root).resolve(), args.embedding_url, args.model_name, args.model_id, args.dimension, args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())