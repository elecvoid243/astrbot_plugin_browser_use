"""Enforce browser admission at the executable tool boundary."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import aiohttp
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.agent.tool import current_tool_invocation


class OwnedBrowserTool(MCPTool):
    """Keep ownership outside model arguments and serialize browser operations."""

    def __init__(self, tool, plugin):
        """Preserve the discovered tool schema and attach its owning plugin.

        Args:
            tool: Original MCP tool discovered by AstrBot.
            plugin: Active BrowserUsePlugin enforcing admission.
        """
        super().__init__(tool.mcp_tool, tool.mcp_client, tool.mcp_server_name)
        self.active = tool.active
        self.plugin = plugin

    async def call(self, context, **kwargs):
        """Admit only a trusted event owner before invoking the private MCP.

        Args:
            context: AstrBot execution context containing the trusted event.
            **kwargs: Model-supplied browser action parameters only.

        Returns:
            The original MCP result without observation frames.

        Raises:
            ValueError: A trusted event identity is missing or malformed.
            RuntimeError: The plugin is disabled or another chat owns the browser.
        """
        event = getattr(context.context, "event", None)
        owner = getattr(event, "unified_msg_origin", None)
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("Browser tools require a trusted chat identity.")
        webchat = None
        if event.get_platform_name() == "webchat":
            parts = event.get_session_id().split("!", 2)
            if len(parts) != 3 or parts[0] != "webchat" or not all(parts[1:]):
                raise ValueError("Browser tools require a valid WebChat identity.")
            webchat = (parts[1], parts[2])
        plugin = self.plugin
        if not plugin._enabled:
            raise RuntimeError("Browser Use is disabled.")
        if plugin._owner is None:
            plugin._owner = owner
            plugin._webchat_owner = webchat
        elif plugin._owner != owner:
            raise RuntimeError("Browser is busy in another chat. Close it there first.")
        async with plugin._operation_lock:
            if not plugin._enabled:
                raise RuntimeError("Browser Use is disabled.")
            if plugin._owner != owner:
                raise RuntimeError("Browser ownership expired; retry from this chat.")
            action_id = uuid4().hex
            activation_token = plugin._bridge_token
            bridge = None
            if plugin._bridge_file is not None:
                bridge = json.loads(plugin._bridge_file.read_text(encoding="utf-8"))
            try:
                # Reserved metadata is overwritten after model argument expansion.
                result = await super().call(
                    context,
                    **{
                        **kwargs,
                        "_astrbot_action": {
                            "action_id": action_id,
                            "invocation_id": current_tool_invocation.get(),
                            "timeout": max(0.01, context.tool_call_timeout * 0.95),
                        },
                    },
                )
            except BaseException as exc:
                # MCP cancellation can arrive after its execution response is lost.
                # Reconcile using the captured activation, never a restarted bridge.
                if bridge is not None:
                    try:
                        async with aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=3)
                        ) as client:
                            async with client.post(
                                f"http://127.0.0.1:{int(bridge['port'])}/action/cancel",
                                headers={"Authorization": f"Bearer {activation_token}"},
                                json={
                                    "generation": bridge["generation"],
                                    "action_id": action_id,
                                    "status": "cancelled"
                                    if isinstance(exc, asyncio.CancelledError)
                                    else "timed_out"
                                    if isinstance(exc, asyncio.TimeoutError)
                                    or getattr(
                                        getattr(exc, "error", None), "code", None
                                    )
                                    == 408
                                    else "failed",
                                },
                            ) as response:
                                response.raise_for_status()
                    except Exception:
                        plugin.logger.warning(
                            "Browser action reconciliation unavailable."
                        )
                raise
            # Closing the entire browser is the explicit ownership handoff.
            if self.mcp_tool.name == "browser_close_all" and not result.isError:
                plugin._owner = None
                plugin._webchat_owner = None
            return result
