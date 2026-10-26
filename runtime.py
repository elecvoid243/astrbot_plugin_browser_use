"""Prepare local paths and offline configuration without importing Browser Use."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SERVER_NAME = "astrbot_plugin_browser_use"


def build_mcp_config(
    plugin_dir: Path, data_dir: Path, settings: dict[str, Any]
) -> dict[str, Any]:
    """Write an offline browser profile and construct a stdio launch configuration.

    Args:
        plugin_dir: Installed plugin directory containing the bundled runtime.
        data_dir: AstrBot-managed persistent data directory for this plugin.
        settings: Optional interpreter, browser, headless, and domain settings.

    Returns:
        Configuration accepted by AstrBot's dynamic MCP manager.

    Raises:
        FileNotFoundError: A required executable is unavailable.
        ValueError: Paths or browser settings are invalid.
        OSError: The plugin data directory or configuration cannot be written.
    """
    plugin_dir = Path(plugin_dir).resolve()
    data_dir = Path(data_dir).resolve()
    executables: dict[str, Path] = {}
    for key, bundled in (
        ("python_path", "runtime/python/python.exe"),
        ("browser_path", "runtime/chromium/chrome.exe"),
    ):
        value = settings.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a path string.")
        path = Path(value).expanduser() if value.strip() else plugin_dir / bundled
        if not path.is_absolute():
            path = plugin_dir / path
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(
                f"Missing {key}: {path}. Install the offline plugin ZIP or configure "
                "a separate Browser Use runtime and browser in the plugin settings."
            )
        executables[key] = path
    if executables["python_path"] == Path(sys.executable).resolve():
        raise ValueError("Browser Use must use a separate interpreter from AstrBot.")
    headless = settings.get("headless", True)
    domains = settings.get("allowed_domains", [])
    if not isinstance(headless, bool):
        raise ValueError("headless must be a boolean.")
    if not isinstance(domains, list) or not all(
        isinstance(domain, str) and domain.strip() for domain in domains
    ):
        raise ValueError("allowed_domains must be a list of non-empty domain strings.")

    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("profile", "downloads", "workspace"):
        (data_dir / name).mkdir(exist_ok=True)
    browser_config = {
        "browser_profile": {
            "offline": {
                "id": "offline",
                "default": True,
                "executable_path": str(executables["browser_path"]),
                "headless": headless,
                "enable_default_extensions": False,
                "user_data_dir": str(data_dir / "profile"),
                "downloads_path": str(data_dir / "downloads"),
                "file_system_path": str(data_dir / "workspace"),
                "allowed_domains": [domain.strip() for domain in domains],
            }
        },
        "llm": {},
        "agent": {},
    }
    config_path = data_dir / "browser-use.json"
    temporary_path = config_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(browser_config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_path.replace(config_path)
    return {
        "command": str(executables["python_path"]),
        "args": ["-X", "utf8", str(plugin_dir / "mcp_entry.py")],
        "cwd": str(data_dir),
        "env": {
            "PYTHONNOUSERSITE": "1",
            "BROWSER_USE_CONFIG_PATH": str(config_path),
            "BROWSER_USE_CONFIG_DIR": str(data_dir),
            "XDG_CACHE_HOME": str(data_dir / "cache"),
            "BROWSER_USE_DISABLE_EXTENSIONS": "true",
            "BROWSER_USE_HEADLESS": str(headless).lower(),
            "ANONYMIZED_TELEMETRY": "false",
            "BROWSER_USE_CLOUD_SYNC": "false",
            "BROWSER_USE_VERSION_CHECK": "false",
            "BROWSER_USE_CALCULATE_COST": "false",
            "BROWSER_USE_CLOUD_HINTS": "false",
            "BROWSER_USE_SETUP_LOGGING": "false",
            "BROWSER_USE_LOGGING_LEVEL": "critical",
            "OPENAI_API_KEY": "",
            "BROWSER_USE_API_KEY": "",
            "MODEL_PROVIDER": "",
        },
    }
