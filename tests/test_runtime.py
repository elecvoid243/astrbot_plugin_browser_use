"""Check relocatable configuration and isolation from the host environment."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from runtime import build_mcp_config


@pytest.fixture
def bundled(tmp_path):
    """Create a bundle layout in a directory with spaces and Chinese characters.

    Args:
        tmp_path: Per-test filesystem workspace.

    Returns:
        Paths to the plugin bundle and its persistent data directory.
    """
    plugin = tmp_path / "离线 插件"
    for executable in ("runtime/python/python.exe", "runtime/chromium/chrome.exe"):
        path = plugin / executable
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return plugin, tmp_path / "persistent data"


def test_offline_settings_do_not_mutate_environment(bundled, monkeypatch):
    plugin, data = bundled
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    before = os.environ.copy()
    config = build_mcp_config(plugin, data, {})
    assert os.environ == before
    assert config["env"]["OPENAI_API_KEY"] == ""
    assert config["env"]["ANONYMIZED_TELEMETRY"] == "false"
    assert config["env"]["BROWSER_USE_DISABLE_EXTENSIONS"] == "true"
    assert config["command"] == str((plugin / "runtime/python/python.exe").resolve())
    assert "uvx" not in str(config)
    profile = json.loads((data / "browser-use.json").read_text(encoding="utf-8"))
    assert profile["llm"] == {}
    assert profile["browser_profile"]["offline"]["enable_default_extensions"] is False


def test_relocation_rewrites_paths_and_preserves_browser_state(bundled, tmp_path):
    plugin, data = bundled
    build_mcp_config(plugin, data, {})
    cookie = data / "profile" / "persistent-cookie"
    cookie.write_text("keep", encoding="utf-8")
    moved = tmp_path / "new location"
    plugin.rename(moved)
    config = build_mcp_config(
        moved, data, {"headless": False, "allowed_domains": [" http://127.0.0.1 "]}
    )
    profile = json.loads((data / "browser-use.json").read_text(encoding="utf-8"))[
        "browser_profile"
    ]["offline"]
    assert Path(config["command"]).is_relative_to(moved)
    assert Path(profile["executable_path"]).is_relative_to(moved)
    assert profile["allowed_domains"] == ["http://127.0.0.1"]
    assert profile["headless"] is False
    assert cookie.read_text(encoding="utf-8") == "keep"


def test_relative_overrides_resolve_from_plugin_not_working_directory(bundled):
    plugin, data = bundled
    config = build_mcp_config(
        plugin,
        data,
        {
            "python_path": "runtime/python/python.exe",
            "browser_path": "runtime/chromium/chrome.exe",
        },
    )
    assert Path(config["command"]).is_absolute()
    assert config["cwd"] == str(data.resolve())


def test_host_interpreter_is_rejected(bundled):
    plugin, data = bundled
    with pytest.raises(ValueError, match="separate interpreter"):
        build_mcp_config(plugin, data, {"python_path": sys.executable})


@pytest.mark.parametrize(
    "settings",
    [
        {"headless": "false"},
        {"allowed_domains": "intranet"},
        {"allowed_domains": [""]},
        {"allowed_domains": [1]},
        {"python_path": None},
    ],
)
def test_invalid_settings_fail_before_start(bundled, settings):
    with pytest.raises(ValueError):
        build_mcp_config(*bundled, settings)
