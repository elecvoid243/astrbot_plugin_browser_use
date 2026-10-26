"""Load the plugin against a minimal AstrBot API for lifecycle tests."""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def plugin_class(monkeypatch, tmp_path):
    """Import the entry point with an isolated host API and persistent data path.

    Args:
        monkeypatch: Pytest patch cleanup helper.
        tmp_path: Per-test filesystem workspace.

    Returns:
        Plugin class using the test host API.
    """

    class Star:
        def __init__(self, context):
            self.context = context
            self.logger = logging.getLogger("browser-use-tests")

    for name in ("astrbot", "astrbot.api", "astrbot.api.star"):
        module = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
    api = sys.modules["astrbot.api.star"]
    api.Context = object
    api.Star = Star
    api.StarTools = types.SimpleNamespace(
        get_data_dir=lambda name: tmp_path / "data" / name
    )
    package_name = "browser_use_plugin_under_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    monkeypatch.setitem(sys.modules, package_name, package)
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", Path(package.__path__[0]) / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module.BrowserUsePlugin
