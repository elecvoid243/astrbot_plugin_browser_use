"""Bind the Browser Use MCP process to the AstrBot plugin lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api.star import Context, Star, StarTools

from .runtime import SERVER_NAME, build_mcp_config


class BrowserUsePlugin(Star):
    """Start Browser Use on activation and release its tools on deactivation."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        """Initialize lifecycle state without starting a subprocess.

        Args:
            context: AstrBot context providing the MCP tool manager.
            config: Plugin settings supplied by the dashboard.
        """
        super().__init__(context)
        self._config = config if config is not None else {}
        self._plugin_dir = Path(__file__).resolve().parent
        self._lock = asyncio.Lock()
        self._shutdown_event: asyncio.Event | None = None

    async def initialize(self) -> None:
        """Connect the private MCP runtime and expose its browser tools.

        Raises:
            FileNotFoundError: The private interpreter or browser is missing.
            RuntimeError: Another instance already owns the server name.
            Exception: Runtime configuration or MCP initialization fails.
        """
        async with self._lock:
            manager = self.context.get_llm_tool_manager()
            runtime = manager.mcp_server_runtime_view.get(SERVER_NAME)
            if runtime is not None:
                if runtime.shutdown_event is self._shutdown_event:
                    return
                raise RuntimeError(
                    f"MCP server {SERVER_NAME} is already registered by another owner."
                )

            data_dir = StarTools.get_data_dir("astrbot_plugin_browser_use")
            mcp_config = build_mcp_config(self._plugin_dir, data_dir, self._config)
            self._shutdown_event = asyncio.Event()
            try:
                await manager.enable_mcp_server(
                    name=SERVER_NAME,
                    config=mcp_config,
                    shutdown_event=self._shutdown_event,
                    timeout=self._config.get("startup_timeout", 180),
                )
                # AstrBot owns reasoning; these two tools start a second model loop.
                manager.func_list = [
                    tool
                    for tool in manager.func_list
                    if not (
                        getattr(tool, "mcp_server_name", None) == SERVER_NAME
                        and tool.mcp_tool.name
                        in {"retry_with_browser_use_agent", "browser_extract_content"}
                    )
                ]
            except BaseException:
                runtime = manager.mcp_server_runtime_view.get(SERVER_NAME)
                if (
                    runtime is not None
                    and runtime.shutdown_event is self._shutdown_event
                ):
                    await manager.disable_mcp_server(SERVER_NAME, timeout=15)
                self._shutdown_event = None
                raise
            self.logger.info("Browser Use MCP connected using its private runtime.")

    async def terminate(self) -> None:
        """Close owned browsers, stop MCP, and let AstrBot unregister its tools.

        Raises:
            Exception: AstrBot cannot finish shutting down the owned MCP runtime.
        """
        async with self._lock:
            if self._shutdown_event is None:
                return
            manager = self.context.get_llm_tool_manager()
            runtime = manager.mcp_server_runtime_view.get(SERVER_NAME)
            if runtime is None or runtime.shutdown_event is not self._shutdown_event:
                self._shutdown_event = None
                return

            # Old agent runs can retain MCPTool instances after unregistration.
            # Revoke their client under its reconnect lock so they cannot restart
            # the process after the plugin has been disabled.
            async with runtime.client._reconnect_lock:
                session = runtime.client.session
                runtime.client.session = None
                runtime.client._mcp_server_config = None
                if session is not None:
                    try:
                        await asyncio.wait_for(
                            session.call_tool("browser_close_all", arguments={}),
                            timeout=10,
                        )
                    except Exception as exc:
                        self.logger.warning(
                            "Browser cleanup before MCP shutdown failed: %s", exc
                        )
                await manager.disable_mcp_server(SERVER_NAME, timeout=15)
            self._shutdown_event = None
            self.logger.info("Browser Use MCP stopped and its tools were removed.")
