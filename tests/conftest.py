"""Load the plugin against a minimal AstrBot API for lifecycle tests."""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path

import pytest
from starlette.responses import JSONResponse


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

    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.agent",
        "astrbot.core.agent.mcp_client",
        "astrbot.core.agent.tool",
    ):
        module = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
    api = sys.modules["astrbot.api.star"]
    api.Context = object
    api.Star = Star
    api.StarTools = types.SimpleNamespace(
        get_data_dir=lambda name: tmp_path / "data" / name
    )

    class MCPTool:
        def __init__(self, tool, client, server_name):
            self.mcp_tool = tool
            self.mcp_client = client
            self.mcp_server_name = server_name

        async def call(self, context, **kwargs):
            return await self.mcp_client.call_tool_with_reconnect(
                tool_name=self.mcp_tool.name,
                arguments=kwargs,
                read_timeout_seconds=timedelta(seconds=context.tool_call_timeout),
            )

    sys.modules["astrbot.core.agent.mcp_client"].MCPTool = MCPTool
    sys.modules["astrbot.core.agent.tool"].current_tool_invocation = ContextVar(
        "invocation", default=None
    )
    web = types.ModuleType("astrbot.api.web")
    web.request = types.SimpleNamespace()
    web.json_response = lambda data, **kwargs: JSONResponse(data, **kwargs)
    web.error_response = lambda message, data=None, **kwargs: JSONResponse(
        {"status": "error", "message": message, "data": data}, **kwargs
    )
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web)
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
