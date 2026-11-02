"""Bind the Browser Use MCP process to the AstrBot plugin lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.active_event_registry import active_event_registry
from starlette.responses import StreamingResponse

from .observation_queue import ObservationQueue
from .owned_tool import OwnedBrowserTool
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
        self._operation_lock = asyncio.Lock()
        self._owner: str | None = None
        self._webchat_owner: tuple[str, str] | None = None
        self._enabled = False
        self._bridge_file: Path | None = None
        self._bridge_token = ""
        self._lease = 0
        self._stopped = False
        self._disable_revision = 0
        self._control_lock = asyncio.Lock()
        self._closed_state = None
        self._manual_blocked = False
        self._control_epoch = 0
        self._agent_pause = None

    @filter.on_waiting_llm_request()
    async def guard_human_control(self, event: AstrMessageEvent):
        """Reject new model turns until the human explicitly hands control back.

        Args:
            event: Trusted foreground or background chat event.
        """
        if (
            self._enabled
            and self._manual_blocked
            and event.unified_msg_origin == self._owner
        ):
            event.stop_event()
            await event.send(
                event.plain_result(
                    "Browser is under human control or paused. Hand control back in the Agent browser before starting a new task."
                )
            )

    async def initialize(self) -> None:
        """Connect the private MCP runtime and expose its browser tools.

        Raises:
            FileNotFoundError: The private interpreter or browser is missing.
            RuntimeError: Another instance already owns the server name.
            Exception: Runtime configuration or MCP initialization fails.
        """
        revision = self._disable_revision
        async with self._lock:
            if revision != self._disable_revision:
                return
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
            self._bridge_file = Path(data_dir) / f"observer-{uuid4().hex}.json"
            self._bridge_token = secrets.token_urlsafe(32)
            self._owner = None
            self._webchat_owner = None
            self._stopped = False
            self._closed_state = None
            self._lease += 1
            mcp_config["env"].update(
                ASTRBOT_BROWSER_BRIDGE_TOKEN=self._bridge_token,
                ASTRBOT_BROWSER_BRIDGE_FILE=str(self._bridge_file),
            )
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
                    OwnedBrowserTool(tool, self)
                    if getattr(tool, "mcp_server_name", None) == SERVER_NAME
                    else tool
                    for tool in manager.func_list
                    if not (
                        getattr(tool, "mcp_server_name", None) == SERVER_NAME
                        and tool.mcp_tool.name
                        in {"retry_with_browser_use_agent", "browser_extract_content"}
                    )
                ]
                if revision != self._disable_revision:
                    # Retained tools are guarded even when disable overtakes startup.
                    return
                self._enabled = True
                self._manual_blocked = False
                self._control_epoch = 0
                self.context.register_web_api(
                    "/browser-use/manual",
                    self.browser_manual,
                    ["POST"],
                    "Authenticated host browser control channel.",
                )
                for control in ("stop", "close"):
                    self.context.register_web_api(
                        f"/browser-use/{control}",
                        self.browser_observation,
                        ["POST"],
                        "Stop execution or close the current chat's Agent browser.",
                    )
                self.context.register_web_api(
                    "/browser-use/status",
                    self.browser_observation,
                    ["GET"],
                    "Get the current chat's Agent browser state.",
                )
                self.context.register_web_api(
                    "/browser-use/snapshot",
                    self.browser_observation,
                    ["GET"],
                    "Capture the current chat's Agent browser viewport.",
                )
                self.context.register_web_api(
                    "/browser-use/stream",
                    self.browser_observation,
                    ["GET"],
                    "Watch the current chat's Agent browser viewport.",
                )
            except BaseException:
                runtime = manager.mcp_server_runtime_view.get(SERVER_NAME)
                if (
                    runtime is not None
                    and runtime.shutdown_event is self._shutdown_event
                ):
                    await manager.disable_mcp_server(SERVER_NAME, timeout=15)
                self._enabled = False
                self._shutdown_event = None
                raise
            self.logger.info("Browser Use MCP connected using its private runtime.")

    async def terminate(self) -> None:
        """Close owned browsers, stop MCP, and let AstrBot unregister its tools.

        Raises:
            Exception: AstrBot cannot finish shutting down the owned MCP runtime.
        """
        self._disable_revision += 1
        self._enabled = False
        self._stopped = True
        self._lease += 1
        if self._owner is not None and self._agent_pause is not None:
            active_event_registry.request_agent_stop_all(self._owner)
            self._agent_pause.resume()
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

                # Stop the execution child before closing the browser or MCP transport.
                if self._bridge_file is not None and self._bridge_file.exists():
                    try:
                        bridge = json.loads(
                            self._bridge_file.read_text(encoding="utf-8")
                        )
                        async with aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=15)
                        ) as client:
                            async with client.post(
                                f"http://127.0.0.1:{int(bridge['port'])}/control",
                                headers={
                                    "Authorization": f"Bearer {self._bridge_token}"
                                },
                                json={
                                    "generation": bridge["generation"],
                                    "operation": "disable",
                                },
                            ) as response:
                                response.raise_for_status()
                    except (
                        OSError,
                        ValueError,
                        aiohttp.ClientError,
                        asyncio.TimeoutError,
                    ):
                        self.logger.warning(
                            "Browser bridge cleanup unavailable; stopping owned MCP."
                        )

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
            self._owner = None
            self._webchat_owner = None
            if self._bridge_file is not None:
                self._bridge_file.unlink(missing_ok=True)
            self.logger.info("Browser Use MCP stopped and its tools were removed.")

    async def browser_observation(self):
        """Authorize a Dashboard chat and proxy a private runtime observation.

        Returns:
            State or snapshot in an AstrBot envelope, with explicit HTTP errors.
        """
        if not request.username:
            return error_response("Authentication required.", status_code=401)
        if not self._enabled:
            return error_response(
                "Browser Use is disabled.", status_code=503, data={"reason": "disabled"}
            )
        cid = request.query.get("chat_id", "")
        if not cid:
            return error_response("A chat identity is required.", status_code=400)
        session = await self.context.get_db().get_platform_session_by_id(cid)
        if (
            session is None
            or session.creator != request.username
            or session.platform_id != "webchat"
        ):
            return error_response("Chat access denied.", status_code=403)
        if self._owner is not None and self._webchat_owner != (request.username, cid):
            return error_response(
                "Browser is busy in another chat.",
                status_code=409,
                data={"reason": "busy"},
            )
        if self._owner is None:
            if request.path.rsplit("/", 1)[-1] in {
                "stop",
                "close",
                "snapshot",
                "stream",
            }:
                return error_response("Browser session expired.", status_code=409)
            return json_response(
                {
                    "status": "ok",
                    "data": self._closed_state[1]
                    if self._closed_state
                    and self._closed_state[0] == (request.username, cid)
                    else {"available": False},
                },
                headers={"Cache-Control": "no-store"},
            )
        owner = self._owner
        lease = self._lease
        token = self._bridge_token
        path = request.path.rsplit("/", 1)[-1]
        if path not in {"snapshot", "stream", "stop", "close", "manual"}:
            path = "status"
        try:
            if self._bridge_file is None:
                raise FileNotFoundError
            bridge = json.loads(self._bridge_file.read_text(encoding="utf-8"))
            generation = request.query.get("generation")
            browser_session_id = request.query.get("browser_session_id")
            if path in {"snapshot", "stream", "stop", "close", "manual"} and (
                not generation or not browser_session_id
            ):
                return error_response("Browser identity is required.", status_code=400)
            if generation is not None and generation != bridge["generation"]:
                return error_response("Browser generation expired.", status_code=409)
            private_path = (
                "identity"
                if path in {"stop", "close", "manual"}
                else "snapshot"
                if path == "snapshot"
                else "status"
            )
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as client:
                async with client.get(
                    f"http://127.0.0.1:{int(bridge['port'])}/{private_path}",
                    params={"generation": bridge["generation"]},
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.status != 200:
                        return error_response(
                            "Browser observation unavailable; refresh state.",
                            status_code=409,
                        )
                    state = await response.json()
            if (
                not self._enabled
                or self._owner != owner
                or self._lease != lease
                or self._bridge_token != token
            ):
                return error_response("Browser ownership expired.", status_code=409)
            if (
                browser_session_id is not None
                and browser_session_id != state["browser_session_id"]
            ):
                return error_response("Browser session expired.", status_code=409)
            if path in {"stop", "close"}:
                return await self.browser_control(
                    path, bridge, state, owner, lease, token
                )
            if path == "manual":
                command = getattr(request, "browser_control", None)
                if command is None:
                    return error_response(
                        "Use the authenticated host control channel.", status_code=403
                    )
                if command["operation"] == "inspect":
                    return json_response({"status": "ok", "data": state})
                async with self._control_lock:
                    if (
                        not self._enabled
                        or self._bridge_token != token
                        or self._owner != owner
                    ):
                        return error_response(
                            "Browser ownership expired.", status_code=409
                        )
                    if command["operation"] == "acquire":
                        if self._stopped or state.get("control_mode") in {
                            "human",
                            "acquiring",
                        }:
                            return error_response(
                                "Browser is stopped or already controlled by another client.",
                                status_code=409,
                            )
                        self._manual_blocked = True
                        self._lease += 1
                        self._agent_pause = (
                            active_event_registry.get_agent_manual_pause(owner)
                        )
                        self._agent_pause.pause()
                        self.logger.info(
                            "Browser control acquisition requested; Agent admission paused."
                        )
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=20)
                    ) as client:
                        async with client.post(
                            f"http://127.0.0.1:{int(bridge['port'])}/manual",
                            headers={"Authorization": f"Bearer {token}"},
                            json={
                                **command,
                                "owner": owner,
                                "generation": generation,
                                "browser_session_id": browser_session_id,
                            },
                        ) as response:
                            if response.status != 200:
                                return error_response(
                                    await response.text(), status_code=response.status
                                )
                            result = await response.json()
                    self._control_epoch = result["control_epoch"]
                    observation = result.pop("agent_observation", "")
                    if command["operation"] == "release":
                        self._manual_blocked = False
                        self._lease += 1
                        if self._agent_pause is not None:
                            self._agent_pause.resume(observation)
                    return json_response(
                        {"status": "ok", "data": result},
                        headers={"Cache-Control": "no-store"},
                    )
            if path == "stream":
                try:
                    action_cursor = int(request.query.get("action_cursor", "0"))
                except ValueError:
                    return error_response("Invalid action cursor.", status_code=400)
                if action_cursor < 0 or action_cursor > state["sequence"]:
                    return error_response("Action cursor expired.", status_code=409)
                # Request-bound values must be captured before the generator runs.
                return self.browser_stream(
                    bridge,
                    owner,
                    self._bridge_token,
                    browser_session_id,
                    str(action_cursor),
                )
            return json_response(
                {"status": "ok", "data": state}, headers={"Cache-Control": "no-store"}
            )
        except (OSError, ValueError, aiohttp.ClientError, asyncio.TimeoutError):
            return error_response(
                "Browser runtime unavailable.",
                status_code=503,
                data={"reason": "runtime_unavailable"},
            )

    async def browser_manual(self):
        """Enter the observer only through the host's trusted control context.

        Returns:
            A manual-control acknowledgement for the authenticated socket.
        """
        if getattr(request, "browser_control", None) is None:
            return error_response(
                "Authenticated control channel required.", status_code=403
            )
        return await self.browser_observation()

    browser_manual.__astrbot_chat_guard__ = "browser-owned-chat-control-v1"

    # The host admits only the actual ownership-aware observer, including old
    # installations where an unguarded handler may still occupy the same route.
    browser_observation.__astrbot_chat_guard__ = "browser-owned-chat-v1"

    async def browser_control(self, operation, bridge, state, owner, lease, token):
        """Revoke queued calls and wait for real execution/browser cleanup.

        Args:
            operation: Authorized stop or close operation.
            bridge: Captured private bridge discovery.
            state: Validated browser lifetime snapshot.
            owner: Trusted chat owner.
            lease: Admission epoch before this control request.
            token: Captured activation credential.

        Returns:
            Confirmed runtime state, or an explicit stale request error.

        Raises:
            ClientError: The private runtime cannot confirm cleanup.
            TimeoutError: The private operation exceeds the control deadline.
        """
        async with self._control_lock:
            if (
                not self._enabled
                or self._lease != lease
                or self._bridge_token != token
                or self._owner != owner
            ):
                return error_response("Browser ownership expired.", status_code=409)
            self._stopped = True
            self._lease += 1
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            ) as client:
                async with client.post(
                    f"http://127.0.0.1:{int(bridge['port'])}/control",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "generation": bridge["generation"],
                        "browser_session_id": state["browser_session_id"],
                        "operation": operation,
                        "lease": lease,
                    },
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
            async with self._operation_lock:
                if not self._enabled or self._bridge_token != token:
                    return error_response(
                        "Browser Use is disabled.",
                        status_code=503,
                        data={"reason": "disabled"},
                    )
                self._control_epoch = result.get("control_epoch", self._control_epoch)
                if operation == "close":
                    self._manual_blocked = False
                    self._closed_state = (self._webchat_owner, result)
                    self._owner = None
                    self._webchat_owner = None
                    self._stopped = False
                # The browser request has drained. Cancelling a runner before this
                # point could cancel the MCP SDK handler instead of its child.
                active_event_registry.request_agent_stop_all(owner)
                if operation == "close" and self._agent_pause is not None:
                    self._agent_pause.resume()
            return json_response(
                {"status": "ok", "data": result}, headers={"Cache-Control": "no-store"}
            )

    def browser_stream(
        self, bridge, owner, token, browser_session_id, action_cursor="0"
    ):
        """Proxy bounded SSE while continuously checking the admitted owner.

        Args:
            bridge: Validated loopback endpoint and runtime generation.
            owner: Trusted owner admitted by the HTTP request.
            token: Credential for this plugin activation only.
            browser_session_id: Validated browser lifetime identity.
            action_cursor: Last applied action sequence for bounded recovery.

        Returns:
            A streaming response whose cancellation closes its private subscription.
        """

        async def events():
            """Yield admitted observations and cancel the private reader on exit.

            Yields:
                SSE blocks or idle heartbeats for this browser lifetime only.
            """
            queue = ObservationQueue()

            async def receive():
                """Read independently of downstream backpressure into bounded frame and state slots."""
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(
                            total=None, sock_connect=5, sock_read=5
                        ),
                        read_bufsize=768 * 1024,
                    ) as client:
                        async with client.get(
                            f"http://127.0.0.1:{int(bridge['port'])}/stream",
                            params={
                                "generation": bridge["generation"],
                                "browser_session_id": browser_session_id,
                                "action_cursor": action_cursor,
                            },
                            headers={"Authorization": f"Bearer {token}"},
                        ) as response:
                            response.raise_for_status()
                            buffer = b""
                            async for chunk in response.content.iter_any():
                                buffer += chunk
                                while b"\n\n" in buffer:
                                    block, buffer = buffer.split(b"\n\n", 1)
                                    if len(block) > 768 * 1024:
                                        raise ValueError(
                                            "Browser stream event is too large."
                                        )
                                    if block.startswith(b":"):
                                        continue
                                    payload = next(
                                        line[6:]
                                        for line in block.split(b"\n")
                                        if line.startswith(b"data: ")
                                    )
                                    event = json.loads(payload)
                                    if (
                                        event.get("generation") != bridge["generation"]
                                        or event.get("browser_session_id")
                                        != browser_session_id
                                    ):
                                        raise ValueError(
                                            "Browser stream identity expired."
                                        )
                                    queue.put_nowait(event)
                                if len(buffer) > 768 * 1024:
                                    raise ValueError(
                                        "Browser stream event is too large."
                                    )
                finally:
                    queue.put_nowait(None)

            reader = asyncio.create_task(receive())
            try:
                while (
                    self._enabled
                    and self._owner == owner
                    and self._bridge_token == token
                ):
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=1)
                    except asyncio.TimeoutError:
                        yield b": hb\n\n"
                        continue
                    if event is None:
                        break
                    # Ownership can change while awaiting the next private event.
                    if (
                        not self._enabled
                        or self._owner != owner
                        or self._bridge_token != token
                    ):
                        break
                    yield f"id: {event['sequence']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
            finally:
                reader.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    ValueError,
                ):
                    await reader

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
