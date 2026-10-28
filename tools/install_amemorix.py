from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import subprocess
import sys
import time


DEFAULT_PACKAGE_SPEC = "MaiBot>=1.3.15,<1.4"


def module_available() -> bool:
    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec("src.A_memorix") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def package_version() -> str | None:
    try:
        return importlib.metadata.version("MaiBot")
    except importlib.metadata.PackageNotFoundError:
        return None


def tail(value: str | bytes | None, limit: int = 12000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-limit:]


def ensure_amemorix(package_spec: str, apply: bool, timeout_seconds: int = 1800) -> dict:
    result = {
        "aMemorixInstalled": module_available(),
        "packageVersion": package_version(),
        "packageSpec": package_spec,
        "applied": False,
        "action": "none",
        "error": None,
        "output": "",
    }
    if result["aMemorixInstalled"]:
        return result

    if not apply:
        result["action"] = "would-install"
        return result

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--upgrade-strategy",
        "only-if-needed",
        "--disable-pip-version-check",
        "--no-input",
        package_spec,
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        result["action"] = "install-timeout"
        result["error"] = f"MaiBot installation timed out after {timeout_seconds} seconds"
        result["output"] = tail(error.stdout) + tail(error.stderr)
        return result
    except OSError as error:
        result["action"] = "install-failed"
        result["error"] = f"Could not start pip: {error}"
        return result

    output = (completed.stdout or "") + (completed.stderr or "")
    result["elapsedSeconds"] = round(time.monotonic() - started, 2)
    result["packageVersion"] = package_version()
    if completed.returncode != 0:
        result["action"] = "install-failed"
        result["error"] = f"pip exited with code {completed.returncode}"
        result["output"] = tail(output)
        return result

    result["aMemorixInstalled"] = module_available()
    if not result["aMemorixInstalled"] and result["packageVersion"]:
        result["action"] = "repairing"
        try:
            repair = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--force-reinstall",
                    "--no-deps",
                    "--disable-pip-version-check",
                    "--no-input",
                    f"MaiBot=={result['packageVersion']}",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            result["action"] = "repair-timeout"
            result["error"] = f"MaiBot repair timed out after {timeout_seconds} seconds"
            result["output"] = tail(output + tail(error.stdout) + tail(error.stderr))
            return result
        except OSError as error:
            result["action"] = "install-failed"
            result["error"] = f"Could not start pip repair: {error}"
            result["output"] = tail(output)
            return result
        output += (repair.stdout or "") + (repair.stderr or "")
        result["packageVersion"] = package_version()
        if repair.returncode != 0:
            result["action"] = "install-failed"
            result["error"] = f"pip repair exited with code {repair.returncode}"
            result["output"] = tail(output)
            return result
        result["aMemorixInstalled"] = module_available()
        result["action"] = "repaired" if result["aMemorixInstalled"] else "module-still-missing"
    else:
        result["action"] = "installed"

    result["output"] = tail(output)
    if not result["aMemorixInstalled"]:
        result["action"] = "module-still-missing"
        result["error"] = "MaiBot installed, but src.A_memorix is still unavailable"
        return result
    result["applied"] = True
    return result

def main() -> int:
    parser = argparse.ArgumentParser(description="Install or repair the MaiBot package that provides A-Memorix")
    parser.add_argument("--package-spec", default=DEFAULT_PACKAGE_SPEC)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = ensure_amemorix(args.package_spec.strip(), args.apply, args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("aMemorixInstalled") and not result.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())