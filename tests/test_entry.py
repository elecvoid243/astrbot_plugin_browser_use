"""Ensure the process entry point closes browsers after transport termination."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("failure", [None, RuntimeError("transport disconnected")])
async def test_entry_closes_browser_on_exit(monkeypatch, failure):
    server = types.SimpleNamespace(
        run=AsyncMock(side_effect=failure), _close_all_sessions=AsyncMock()
    )
    for name in ("browser_use", "browser_use.mcp", "browser_use.mcp.server"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["browser_use.mcp.server"].BrowserUseServer = type(
        "BrowserUseServer", (), {}
    )
    spec = importlib.util.spec_from_file_location(
        "browser_use_entry_under_test",
        Path(__file__).resolve().parents[1] / "mcp_entry.py",
    )
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setattr(entry, "LocalBrowserServer", lambda: server)
    if failure:
        with pytest.raises(RuntimeError, match="disconnected"):
            await entry.main()
    else:
        await entry.main()
    server._close_all_sessions.assert_awaited_once()
