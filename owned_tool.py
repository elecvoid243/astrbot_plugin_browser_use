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
        self.activation_token = plugin._bridge_token

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
        if not plugin._enabled or self.activation_token != plugin._bridge_token:
            raise RuntimeError("Browser Use is disabled.")
        if plugin._manual_blocked:
            raise RuntimeError(
                "Browser is under human control or paused; explicitly hand control back first."
            )
        if plugin._stopped or getattr(event, "get_extra", lambda *args: False)(
            "agent_stop_requested", False
        ):
            raise RuntimeError(
                "Browser execution stopped. Close the browser before restarting."
            )
        if plugin._owner is None:
            plugin._owner = owner
            plugin._webchat_owner = webchat
            plugin._closed_state = None
        elif plugin._owner != owner:
            raise RuntimeError("Browser is busy in another chat. Close it there first.")
        lease = plugin._lease
        async with plugin._operation_lock:
            if not plugin._enabled or self.activation_token != plugin._bridge_token:
                raise RuntimeError("Browser Use is disabled.")
            if (
                plugin._owner != owner
                or plugin._lease != lease
                or plugin._stopped
                or plugin._manual_blocked
            ):
                raise RuntimeError("Browser ownership expired; retry from this chat.")
            action_id = uuid4().hex
            activation_token = plugin._bridge_token
            bridge = None
            if plugin._bridge_file is not None:
                bridge = json.loads(plugin._bridge_file.read_text(encoding="utf-8"))
            execution = None
            try:
                # Reserved metadata is overwritten after model argument expansion.
                execution = asyncio.create_task(
                    super().call(
                        context,
                        **{
                            **kwargs,
                            "_astrbot_action": {
                                "action_id": action_id,
                                "invocation_id": current_tool_invocation.get(),
                                "timeout": max(0.01, context.tool_call_timeout * 0.95),
                                "lease": lease,
                                "control_epoch": plugin._control_epoch,
                                "owner": owner,
                                "generation": bridge["generation"] if bridge else None,
                            },
                        },
                    )
                )
                # Reconcile cancellation through the private child-task protocol;
                # cancelling the MCP SDK request itself can end its whole service.
                result = await asyncio.shield(execution)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError) and plugin._lease == lease:
                    plugin._stopped = True
                    plugin._lease += 1
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
                                    "lease": lease,
                                    "stop_execution": isinstance(
                                        exc, asyncio.CancelledError
                                    )
                                    and not plugin._manual_blocked,
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
                if execution is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(execution), 5)
                    except (Exception, asyncio.CancelledError):
                        execution.add_done_callback(
                            lambda future: (
                                None if future.cancelled() else future.exception()
                            )
                        )
                raise
            # Closing the entire browser is the explicit ownership handoff.
            if (
                self.mcp_tool.name
                in {"browser_close_all", "browser_close_session", "browser_close"}
                and not result.isError
            ):
                plugin._owner = None
                plugin._webchat_owner = None
                plugin._lease += 1
            return result
