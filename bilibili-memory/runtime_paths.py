from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.environ.get("BILIMEMORY_ROOT", APP_ROOT.parent)).resolve()
MEDIA_PYTHON = Path(
    os.environ.get("BILIMEMORY_MEDIA_PYTHON", os.environ.get("PYTHON", "") or "python")
).expanduser()


def _discover_mai_bot_root() -> Path:
    explicit = os.environ.get("MAIBOT_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()

    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.home() / "MaiBot"
    launcher_root = Path(appdata) / "MaiBotOneKeyDesktop"
    candidates = []
    if launcher_root.is_dir():
        candidates = [
            item / "modules" / "MaiBot"
            for item in launcher_root.iterdir()
            if (item / "modules" / "MaiBot").is_dir()
        ]
    if candidates:
        return max(candidates, key=lambda item: item.stat().st_mtime).resolve()
    return Path.home() / "MaiBot"


def _discover_python(root: Path) -> Path:
    explicit = os.environ.get("MAIBOT_PYTHON")
    if explicit:
        return Path(explicit).expanduser().resolve()
    environments = root / ".onekey" / "environments"
    candidates = sorted(environments.glob("*/python.exe")) if environments.is_dir() else []
    if candidates:
        return candidates[0].resolve()
    return Path("python")


MAIBOT_ROOT = _discover_mai_bot_root()
MAIBOT_USER_DATA = MAIBOT_ROOT.parent.parent if MAIBOT_ROOT.parent.name == "modules" else MAIBOT_ROOT
MAIBOT_PYTHON = _discover_python(MAIBOT_ROOT)
