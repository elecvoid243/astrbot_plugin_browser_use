"""Exercise the offline installer against a disposable AstrBot data directory."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
def test_installer_copies_bundle_and_refuses_overwriting(tmp_path):
    distribution = tmp_path / "离线 安装包"
    source = distribution / "astrbot_plugin_browser_use"
    executable = source / "runtime" / "python" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test-only-placeholder")
    script = distribution / "install.ps1"
    shutil.copy2(Path(__file__).resolve().parents[1] / "install.ps1", script)
    data = tmp_path / "AstrBot data"
    (data / "plugins").mkdir(parents=True)
    persistent = data / "plugin_data" / "astrbot_plugin_browser_use"
    persistent.mkdir(parents=True)
    cookie = persistent / "cookie.txt"
    cookie.write_text("keep", encoding="utf-8")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-AstrBotDataDir",
        str(data),
    ]
    first = subprocess.run(
        command, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
    )
    assert first.returncode == 0, first.stderr.decode(errors="replace")
    installed = (
        data
        / "plugins"
        / "astrbot_plugin_browser_use"
        / "runtime"
        / "python"
        / "python.exe"
    )
    assert installed.read_bytes() == executable.read_bytes()
    second = subprocess.run(
        command, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
    )
    assert second.returncode != 0
    assert cookie.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
def test_installer_rejects_native_paths_that_windows_cannot_load(tmp_path):
    """Reject unloadable native paths before writing an installed plugin.

    Args:
        tmp_path: Disposable package and data destination.
    """
    distribution = tmp_path / "package"
    source = distribution / "astrbot_plugin_browser_use"
    executable = source / "runtime/python/python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test-only-placeholder")
    native = (
        source
        / "runtime/python/Lib/site-packages/av.libs"
        / ("codec-" + "a" * 90 + ".dll")
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"test-only-placeholder")
    script = distribution / "install.ps1"
    shutil.copy2(Path(__file__).resolve().parents[1] / "install.ps1", script)
    data = tmp_path / ("long-data-" + "x" * 70)
    (data / "plugins").mkdir(parents=True)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-AstrBotDataDir",
            str(data),
        ],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode != 0
    assert b"shorter AstrBot data directory" in result.stderr
    assert not (data / "plugins/astrbot_plugin_browser_use").exists()
