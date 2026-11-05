"""Observe and authorize typed control of the actual Chromium session."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import io
import json
import math
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from PIL import Image

from observation_queue import ACTION_LIMIT, ObservationQueue

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 3 * 1024 * 1024
MAX_FRAME_WIDTH = 1920
MAX_FRAME_HEIGHT = 1080
FRAME_INTERVAL = 0.25
HUMAN_FRAME_INTERVAL = 0.1
# A scroll revision also covers nested scrollers without treating animation frames
# as new targets. The listener is installed once in each actual page document.
PAGE_STATE_EXPRESSION = """JSON.stringify((() => {
const key = Symbol.for('astrbot.browser.position');
if (!window[key]) {
  const position = {revision: 0};
  Object.defineProperty(window, key, {value: position});
  addEventListener('scroll', () => position.revision++, true);
  addEventListener('resize', () => position.revision++, true);
}
return {url:location.href,title:document.title,
document_id:String(performance.timeOrigin),viewport_width:innerWidth,
viewport_height:innerHeight,scroll_x:scrollX,scroll_y:scrollY,
position_revision:window[key].revision};
})())"""


class BrowserObserver:
    """Expose a credential-protected loopback bridge in the private runtime."""

    def __init__(self, server, *, coordinate_guard=None):
        """Attach to the existing MCP server without starting another browser.

        Args:
            server: Upstream BrowserUseServer that executes Agent tools.
            coordinate_guard: Optional frame-tree contract for the isolated V02
                rollout. Existing runtimes retain their version-one behavior.
        """
        self.server = server
        self.generation = uuid4().hex
        self.sequence = 0
        self.token = os.environ["ASTRBOT_BROWSER_BRIDGE_TOKEN"]
        self.discovery = Path(os.environ["ASTRBOT_BROWSER_BRIDGE_FILE"])
        self.runner = None
        self.subscribers: set[asyncio.Queue] = set()
        self._producer = None
        self._stream_lock = asyncio.Lock()
        self._closed = False
        self._refresh_stream = False
        self.current_action = None
        self.recent_actions = deque(maxlen=32)
        self.action_events = deque(maxlen=ACTION_LIMIT)
        self.action_cursor = 0
        self.action_task = None
        self.cancelled_actions = deque(maxlen=ACTION_LIMIT)
        self.highlight = None
        self.execution_state = "active"
        self.revoked_leases = deque(maxlen=ACTION_LIMIT)
        self.action_done = asyncio.Event()
        self.action_done.set()
        self._control_lock = asyncio.Lock()
        self.control_mode = "agent"
        self.owner = None
        self.control_epoch = 0
        self.applied_input_id = 0
        self.human = None
        self._human_expiry = None
        self._held_keys = {}
        self._held_mouse = None
        self._drag_target = None
        self.pending_browser_events = []
        self._snapshot_lock = asyncio.Lock()
        self._snapshot_session = None
        self.coordinate_guard = coordinate_guard

    async def start(self) -> None:
        """Bind an ephemeral loopback port and publish its discovery record.

        Raises:
            OSError: The socket or discovery file cannot be created.
        """
        app = web.Application()
        app.router.add_get("/status", self.observe)
        app.router.add_get("/identity", self.observe)
        app.router.add_get("/snapshot", self.observe)
        app.router.add_get("/stream", self.stream)
        app.router.add_post("/action/cancel", self.cancel_action)
        app.router.add_post("/control", self.control)
        app.router.add_post("/manual", self.manual)
        app.router.add_post("/viewport", self.viewport)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        temporary = self.discovery.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"port": port, "generation": self.generation}), encoding="utf-8"
        )
        temporary.replace(self.discovery)

    async def read_state(self, *, coordinates=True, gesture=False):
        """Read the actual target metadata without changing focus or DOM indices.

        Args:
            coordinates: Include the authoritative frame-tree contract. Releases
                can read only the existing top-level state while a child is unknown.
            gesture: Revalidate held gesture structure independently of scrolling.

        Returns:
            Browser, CDP session and current identity/metadata.

        Raises:
            HTTPConflict: The browser switches targets during the read.
        """
        browser = self.server.browser_session
        state = {
            "generation": self.generation,
            "browser_session_id": browser.id if browser else None,
            "tab_id": None,
            "sequence": self.sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "available": browser is not None,
            "execution_state": self.execution_state,
            "control_mode": self.control_mode,
            "control_epoch": self.control_epoch,
            "manual_control_version": 1,
            "keyboard_input_version": 1,
            "pointer_input_version": 1,
            "drag_input_version": 1,
            "viewport_resize_version": 1,
            "applied_input_id": self.applied_input_id,
            "url": "",
            "title": "",
            "width": 0,
            "height": 0,
            **self.action_state(browser.id if browser else None),
        }
        if self.coordinate_guard is not None:
            state.update(
                coordinate_contract_version=2,
                coordinate_ready=False,
                coordinate_token=None,
                frame_tree_token=None,
            )
        if browser is None or not browser.agent_focus_target_id:
            return browser, None, state
        target = browser.agent_focus_target_id
        cdp = await browser.get_or_create_cdp_session(target_id=target, focus=False)
        info = await cdp.cdp_client.send.Runtime.evaluate(
            params={
                "expression": PAGE_STATE_EXPRESSION,
                "returnByValue": True,
            },
            session_id=cdp.session_id,
        )
        state.update(json.loads(info["result"]["value"]))
        if len(state["url"]) > 8192 or len(state["title"]) > 1024:
            raise web.HTTPRequestEntityTooLarge(
                max_size=8192, actual_size=len(state["url"]) + len(state["title"])
            )
        state["tab_id"] = target
        if self.coordinate_guard is not None:
            if coordinates and self.coordinate_guard.browser is browser:
                from frame_coordinates import CoordinateUnavailable

                try:
                    state.update(
                        await self.coordinate_guard.sample(target, gesture=gesture)
                    )
                except CoordinateUnavailable:
                    # Keep the lease and last pixels. Unknown geometry authorizes
                    # neither a new discrete input nor a new validated capture.
                    pass
        if self.highlight and any(
            self.highlight.get(key) != state.get(key)
            for key in (
                "browser_session_id",
                "tab_id",
                "document_id",
                "url",
                "viewport_width",
                "viewport_height",
                "scroll_x",
                "scroll_y",
                "position_revision",
            )
        ):
            self.highlight = None
        if (
            self.server.browser_session is not browser
            or browser.agent_focus_target_id != target
        ):
            raise web.HTTPConflict(text="Browser target changed; refresh observation.")
        state.update(self.action_state(browser.id))
        return browser, cdp, state

    async def release_keys(self):
        """Release held input on its original CDP session before changing admission.

        Raises:
            HTTPConflict: Input release remains unconfirmed after all attempts.
                Admission stays paused and failed input is retained for retry or close.
        """
        if self.coordinate_guard is not None and (self._held_keys or self._held_mouse):
            # A destroyed OOPIF has no live input widget to release. Confirm this
            # through the browser target registry, never a delayed detach event.
            targets = None
            with contextlib.suppress(Exception):
                registry = await asyncio.wait_for(
                    self.coordinate_guard.browser.cdp_client.send.Target.getTargets(),
                    1,
                )
                targets = {target["targetId"] for target in registry["targetInfos"]}
            if targets is not None:
                self._held_keys = {
                    code: held
                    for code, held in self._held_keys.items()
                    if held[0].target_id in targets
                }
                if (
                    self._held_mouse is not None
                    and self._held_mouse[0].target_id not in targets
                ):
                    self._held_mouse = None
                    self._drag_target = None
        keys = list(self._held_keys.items())
        # Releases are independent. Share one bounded deadline even if many targets died.
        releases = [
            asyncio.wait_for(
                cdp.cdp_client.send.Input.dispatchKeyEvent(
                    params={
                        **params,
                        "type": "keyUp",
                        "modifiers": 0,
                        "text": "",
                        "unmodifiedText": "",
                        "autoRepeat": False,
                    },
                    session_id=cdp.session_id,
                ),
                3,
            )
            for _, (cdp, params) in keys
        ]
        mouse = self._held_mouse
        if mouse is not None:
            cdp, _, _ = mouse
            # Release outside the page so an uncertain press cannot become a
            # second click/submit during disconnect cleanup.
            releases.append(
                asyncio.wait_for(
                    cdp.cdp_client.send.Input.dispatchMouseEvent(
                        params={
                            "type": "mouseReleased",
                            "x": -1,
                            "y": -1,
                            "button": "left",
                            "clickCount": 1,
                        },
                        session_id=cdp.session_id,
                    ),
                    3,
                )
            )
        results = await asyncio.gather(*releases, return_exceptions=True)
        failed = False
        for (code, _), result in zip(keys, results[: len(keys)]):
            if isinstance(result, BaseException):
                failed = True
            else:
                self._held_keys.pop(code, None)
        if mouse is not None:
            if isinstance(results[-1], BaseException):
                failed = True
            elif self._held_mouse is mouse:
                self._held_mouse = None
                self._drag_target = None
        if failed:
            self.control_mode = "paused"
            raise web.HTTPConflict(
                text="Input release unconfirmed; browser remains paused. Close the browser before continuing."
            )

    async def resize_viewport(self, browser, cdp, body):
        """Apply bounded display metrics to the runtime-selected target.

        Args:
            browser: Owned browser whose action admission is locked.
            cdp: Session for the current browser target.
            body: Requested width, height and device scale.

        Raises:
            HTTPBadRequest: Dimensions exceed the capture budget.
        """
        width, height, scale = (
            body.get(key) for key in ("width", "height", "device_scale_factor")
        )
        if (
            type(width) is not int
            or type(height) is not int
            or not 320 <= width <= 2560
            or not 240 <= height <= 1600
            or type(scale) not in (int, float)
            or not math.isfinite(scale)
            or not 1 <= scale <= 2
        ):
            raise web.HTTPBadRequest(text="Invalid viewport dimensions.")
        scale = min(scale, MAX_FRAME_WIDTH / width, MAX_FRAME_HEIGHT / height)
        await self.release_keys()
        await asyncio.wait_for(
            cdp.cdp_client.send.Emulation.setDeviceMetricsOverride(
                params={
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": scale,
                    "mobile": False,
                },
                session_id=cdp.session_id,
            ),
            3,
        )
        if hasattr(browser, "browser_profile"):
            browser.browser_profile.viewport = {"width": width, "height": height}
            browser.browser_profile.device_scale_factor = scale
        self.highlight = None
        self._refresh_stream = True

    async def viewport(self, request):
        """Resize an observed browser when no human or Agent action owns it.

        Args:
            request: Credential-protected request carrying a trusted chat owner.

        Returns:
            Fresh metadata after applying the requested viewport.

        Raises:
            HTTPUnauthorized: Bridge credentials are invalid.
            HTTPConflict: Ownership or the selected tab changed, or input is active.
        """
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), f"Bearer {self.token}"
        ):
            raise web.HTTPUnauthorized()
        body = await request.json()
        async with self._control_lock:
            browser = self.server.browser_session
            if (
                self._closed
                or browser is None
                or body.get("owner") != self.owner
                or body.get("generation") != self.generation
                or body.get("browser_session_id") != browser.id
                or body.get("tab_id") != browser.agent_focus_target_id
                or self.human is not None
                or self.control_mode == "acquiring"
                or self.current_action is not None
            ):
                raise web.HTTPConflict(
                    text="Viewport is currently owned by another operation."
                )
            _, cdp, _ = await self.read_state()
            if cdp is None:
                raise web.HTTPConflict(text="No current browser target.")
            await self.resize_viewport(browser, cdp, body)
            _, _, state = await self.read_state()
            return web.json_response(self.publish("state", state))

    async def manual(self, request):
        """Serialize typed human commands with Agent admission in this runtime.

        Args:
            request: Host-authenticated command with trusted owner and connection.

        Returns:
            Ordered acknowledgement and fresh page metadata, without credentials.

        Raises:
            HTTPUnauthorized: The private bridge credential is invalid.
            HTTPConflict: Ownership, epoch, ordering or displayed target expired.
            HTTPBadRequest: The operation or click coordinates are invalid.
        """
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), f"Bearer {self.token}"
        ):
            raise web.HTTPUnauthorized()
        body = await request.json()
        operation = body.get("operation")
        agent_observation = ""
        async with self._control_lock:
            browser = self.server.browser_session
            if (
                self._closed
                or body.get("generation") != self.generation
                or browser is None
                or body.get("browser_session_id") != browser.id
                or body.get("owner") != self.owner
            ):
                raise web.HTTPConflict(text="Browser identity expired.")
            if operation == "acquire":
                if self.human is not None or self.control_mode == "acquiring":
                    raise web.HTTPConflict(
                        text="Browser already has a human controller."
                    )
                if self.execution_state != "active":
                    raise web.HTTPConflict(text="A stopped Agent cannot be taken over.")
                self.control_mode = "acquiring"
                self.applied_input_id = 0
                self.control_epoch += 1
                try:
                    await self.release_keys()
                    await self.action_changed()
                    if self.current_action is not None:
                        self.cancelled_actions.append(self.current_action["action_id"])
                        if self.action_task is not None:
                            self.action_task.cancel()
                        await asyncio.wait_for(self.action_done.wait(), 5)
                        if self.current_action is not None:
                            raise web.HTTPConflict(text="Agent action has not settled.")
                    # Bubus dispatch survives cancellation of the waiting tool.
                    # Never acknowledge human admission before its handlers settle.
                    if self.pending_browser_events:
                        await asyncio.wait_for(
                            asyncio.gather(
                                *(
                                    event.event_completed_signal.wait()
                                    for event in self.pending_browser_events
                                )
                            ),
                            5,
                        )
                        self.pending_browser_events.clear()
                    _, cdp, state = await asyncio.wait_for(self.read_state(), 3)
                    if cdp is None:
                        raise web.HTTPConflict(
                            text="No current browser target is available."
                        )
                    self.human = {
                        "owner": body["owner"],
                        "client_id": body["client_id"],
                        "last_input": 0,
                        "last_ack": None,
                    }
                    self.control_mode = "human"
                except BaseException:
                    self.control_mode = "paused"
                    with contextlib.suppress(Exception):
                        await self.action_changed()
                    raise
            else:
                if (
                    self.human is None
                    or self.human["owner"] != body.get("owner")
                    or self.human["client_id"] != body.get("client_id")
                    or self.control_epoch != body.get("control_epoch")
                ):
                    raise web.HTTPConflict(text="Human control expired.")
                if self.control_mode != "human" and operation not in {
                    "release",
                    "disconnect",
                }:
                    raise web.HTTPConflict(text="Browser input is paused.")
                if operation in {"release", "disconnect"}:
                    self.control_mode = "paused"
                    self.human = None
                    self.control_epoch += 1
                    if operation == "release":
                        await self.release_keys()
                    else:
                        with contextlib.suppress(Exception):
                            await self.release_keys()
                    # Existing element indices cannot survive human page changes.
                    browser._cached_browser_state_summary = None
                    browser._cached_selector_map = {}
                    browser._cached_selector_indices = {}
                    if operation == "release":
                        await asyncio.wait_for(
                            browser.get_browser_state_summary(cached=False), 5
                        )
                        agent_observation, _ = await asyncio.wait_for(
                            self.server._get_browser_state(), 3
                        )
                        if not isinstance(
                            agent_observation, str
                        ) or agent_observation.lower().startswith("error"):
                            raise web.HTTPConflict(
                                text="Fresh browser observation failed."
                            )
                        self.control_mode = "agent"
                elif operation in {
                    "click",
                    "pointer",
                    "wheel",
                    "key",
                    "text",
                    "reset_keys",
                    "resize",
                }:
                    number = body.get("input_id")
                    if type(number) is not int or number < 1:
                        raise web.HTTPBadRequest(text="Invalid input number.")
                    if number == self.human["last_input"]:
                        return web.json_response(self.human["last_ack"])
                    if number != self.human["last_input"] + 1:
                        raise web.HTTPConflict(text="Input sequence expired.")
                    terminal = (
                        operation == "reset_keys"
                        or (
                            operation == "pointer"
                            and body.get("phase") in {"up", "cancel"}
                        )
                        or (operation == "key" and body.get("phase") == "up")
                    )
                    if self.coordinate_guard is not None and terminal:
                        self.human["last_input"] = number
                        self.human["last_ack"] = {
                            "operation": operation,
                            "input_id": number,
                            "status": "uncertain",
                            "control_epoch": self.control_epoch,
                        }
                        try:
                            if (
                                operation == "reset_keys"
                                or body.get("phase") == "cancel"
                            ):
                                await self.release_keys()
                            elif (
                                operation == "pointer" and self._held_mouse is not None
                            ):
                                held, x, y = self._held_mouse
                                ready = None
                                with contextlib.suppress(Exception):
                                    ready = await asyncio.wait_for(
                                        self.coordinate_guard.sample(
                                            browser.agent_focus_target_id, gesture=True
                                        ),
                                        0.75,
                                    )
                                if not ready or ready["frame_tree_token"] != (
                                    self._drag_target or {}
                                ).get("frame_tree_token"):
                                    x, y = -1, -1
                                elif all(
                                    type(body.get(k)) in (int, float)
                                    and math.isfinite(body[k])
                                    for k in ("x", "y")
                                ):
                                    _, x, y = await self.coordinate_guard.route(
                                        body["x"],
                                        body["y"],
                                        target=(self._drag_target or {}).get(
                                            "input_frame"
                                        ),
                                    )
                                await asyncio.wait_for(
                                    held.cdp_client.send.Input.dispatchMouseEvent(
                                        params={
                                            "type": "mouseReleased",
                                            "x": x,
                                            "y": y,
                                            "button": "left",
                                            "buttons": 0,
                                            "clickCount": 1,
                                        },
                                        session_id=held.session_id,
                                    ),
                                    3,
                                )
                                self._held_mouse = None
                                self._drag_target = None
                            elif (
                                operation == "key"
                                and body.get("code") in self._held_keys
                            ):
                                code = body["code"]
                                held, params = self._held_keys[code]
                                await asyncio.wait_for(
                                    held.cdp_client.send.Input.dispatchKeyEvent(
                                        params={
                                            **params,
                                            "type": "keyUp",
                                            "text": "",
                                            "unmodifiedText": "",
                                            "autoRepeat": False,
                                        },
                                        session_id=held.session_id,
                                    ),
                                    3,
                                )
                                self._held_keys.pop(code, None)
                        except Exception:
                            try:
                                await self.release_keys()
                            except Exception:
                                self.control_mode = "paused"
                                return web.json_response(self.human["last_ack"])
                        self.applied_input_id = number
                        self.human["last_ack"] = {
                            "operation": operation,
                            "input_id": number,
                            "status": "applied",
                            "control_epoch": self.control_epoch,
                        }
                        return web.json_response(self.human["last_ack"])
                    if (
                        self.coordinate_guard is not None
                        and operation == "pointer"
                        and body.get("phase") == "move"
                    ):
                        _, cdp, state = await asyncio.wait_for(
                            self.read_state(gesture=True), 3
                        )
                    else:
                        _, cdp, state = await asyncio.wait_for(self.read_state(), 3)
                    target = body.get("target", {})
                    phase = body.get("phase")
                    if operation == "pointer" and phase not in {
                        "down",
                        "move",
                        "up",
                        "cancel",
                    }:
                        raise web.HTTPBadRequest(text="Invalid pointer phase.")
                    continuation = operation == "pointer" and phase in {"move", "up"}
                    drag_target = self._drag_target
                    if continuation and (
                        self._held_mouse is None or drag_target is None
                    ):
                        raise web.HTTPConflict(text="No pointer gesture is active.")
                    if self._held_mouse is not None and operation not in {
                        "pointer",
                        "reset_keys",
                    }:
                        await self.release_keys()
                        raise web.HTTPConflict(text="Pointer gesture interrupted.")
                    if (
                        operation == "pointer"
                        and phase == "down"
                        and self._held_mouse is not None
                    ):
                        await self.release_keys()
                        raise web.HTTPConflict(text="Pointer gesture already active.")
                    # A held drag can scroll the page. Its coordinates remain bound
                    # to the original document and viewport, never a replacement tab.
                    target_fields = (
                        "tab_id",
                        "document_id",
                        "viewport_width",
                        "viewport_height",
                    )
                    coordinate_stale = False
                    if self.coordinate_guard is not None and not terminal:
                        coordinate_stale = not (
                            state.get("gesture_ready")
                            if continuation
                            else state.get("coordinate_ready")
                        )
                        if continuation:
                            coordinate_stale = (
                                coordinate_stale
                                or not drag_target
                                or state.get("frame_tree_token")
                                != drag_target.get("frame_tree_token")
                                or target.get("coordinate_token")
                                != drag_target.get("coordinate_token")
                            )
                        else:
                            coordinate_stale = (
                                coordinate_stale
                                or not target.get("coordinate_token")
                                or target.get("coordinate_token")
                                != state.get("coordinate_token")
                            )
                    if (
                        operation != "reset_keys"
                        and not (operation == "pointer" and phase == "cancel")
                        and (
                            coordinate_stale
                            or cdp is None
                            or (
                                continuation
                                and (
                                    any(
                                        target.get(key) != drag_target.get(key)
                                        for key in target_fields
                                    )
                                    or (
                                        self.coordinate_guard is None
                                        and self._held_mouse[0].session_id
                                        != cdp.session_id
                                    )
                                )
                            )
                            or (
                                not continuation
                                and "position_revision" in target
                                and target["position_revision"]
                                != state.get("position_revision")
                            )
                            or any(
                                target.get(key) != state.get(key)
                                for key in target_fields
                                + (() if continuation else ("scroll_x", "scroll_y"))
                            )
                        )
                    ):
                        await self.release_keys()
                        # The request has not executed. Consume its ID and keep
                        # ownership, allowing a new explicit input after fresh pixels.
                        self.human["last_input"] = number
                        self.human["last_ack"] = {
                            "operation": operation,
                            "input_id": number,
                            "status": "stale",
                            "control_epoch": self.control_epoch,
                            "state": state,
                        }
                        self.publish("state", state)
                        return web.json_response(self.human["last_ack"])
                    params = None
                    routed_target = state.get("tab_id")
                    if operation in {"click", "wheel", "pointer"} and not (
                        operation == "pointer" and phase == "cancel"
                    ):
                        x, y = body.get("x"), body.get("y")
                        if (
                            any(
                                type(v) not in (int, float) or not math.isfinite(v)
                                for v in (x, y)
                            )
                            or not 0 <= x < state["viewport_width"]
                            or not 0 <= y < state["viewport_height"]
                        ):
                            raise web.HTTPBadRequest(
                                text="Invalid pointer coordinates."
                            )
                        if operation in {"click", "pointer"} and (
                            type(body.get("click_count", 1)) is not int
                            or body.get("click_count", 1) not in (1, 2)
                        ):
                            raise web.HTTPBadRequest(text="Invalid click count.")
                        if operation == "wheel" and any(
                            type(body.get(key)) not in (int, float)
                            or not math.isfinite(body[key])
                            or abs(body[key]) > 2048
                            for key in ("delta_x", "delta_y")
                        ):
                            raise web.HTTPBadRequest(text="Invalid wheel delta.")
                        if self.coordinate_guard is not None:
                            from frame_coordinates import CoordinateUnavailable

                            try:
                                routed_target, x, y = await self.coordinate_guard.route(
                                    x,
                                    y,
                                    target=(drag_target or {}).get("input_frame")
                                    if continuation
                                    else None,
                                )
                                cdp = await browser.get_or_create_cdp_session(
                                    target_id=routed_target, focus=False
                                )
                            except CoordinateUnavailable:
                                await self.release_keys()
                                self.human["last_input"] = number
                                self.human["last_ack"] = {
                                    "operation": operation,
                                    "input_id": number,
                                    "status": "stale",
                                    "control_epoch": self.control_epoch,
                                    "state": state,
                                }
                                return web.json_response(self.human["last_ack"])
                    elif operation == "text":
                        text = body.get("text")
                        if (
                            not isinstance(text, str)
                            or not text
                            or len(text) > 2048
                            or any(0xD800 <= ord(char) <= 0xDFFF for char in text)
                            or len(text.encode("utf-8")) > 4096
                            or "\x00" in text
                            or body.get("source")
                            not in {"typing", "composition", "paste"}
                        ):
                            raise web.HTTPBadRequest(text="Invalid text input.")
                    elif operation == "key":
                        code, key = body.get("code"), body.get("key")
                        special = {
                            "Backspace": 8,
                            "Tab": 9,
                            "Enter": 13,
                            "Escape": 27,
                            "Space": 32,
                            "PageUp": 33,
                            "PageDown": 34,
                            "End": 35,
                            "Home": 36,
                            "ArrowLeft": 37,
                            "ArrowUp": 38,
                            "ArrowRight": 39,
                            "ArrowDown": 40,
                            "Delete": 46,
                            "ShiftLeft": 16,
                            "ShiftRight": 16,
                            "ControlLeft": 17,
                            "ControlRight": 17,
                            "AltLeft": 18,
                            "AltRight": 18,
                            "MetaLeft": 91,
                            "MetaRight": 92,
                        }
                        virtual = special.get(code)
                        if (
                            isinstance(code, str)
                            and len(code) == 4
                            and code.startswith("Key")
                            and "A" <= code[-1] <= "Z"
                        ):
                            virtual = ord(code[-1])
                        modifiers = body.get("modifiers", 0)
                        if (
                            virtual is None
                            or not isinstance(key, str)
                            or len(key) > 32
                            or body.get("phase") not in {"down", "up"}
                            or type(modifiers) is not int
                            or not 0 <= modifiers <= 15
                            or type(body.get("repeat", False)) is not bool
                        ):
                            raise web.HTTPBadRequest(text="Invalid key input.")
                        params = {
                            "type": "rawKeyDown"
                            if body["phase"] == "down"
                            else "keyUp",
                            "key": key,
                            "code": code,
                            "windowsVirtualKeyCode": virtual,
                            "modifiers": modifiers,
                            "autoRepeat": body.get("repeat", False),
                        }
                        if code == "Enter" and body["phase"] == "down":
                            params.update(
                                type="keyDown", text="\r", unmodifiedText="\r"
                            )
                    if self.coordinate_guard is not None and operation in {
                        "key",
                        "text",
                    }:
                        routed_target, _, _ = await self.coordinate_guard.route()
                        cdp = await browser.get_or_create_cdp_session(
                            target_id=routed_target, focus=False
                        )
                    # Consume before dispatch: an uncertain CDP response must never
                    # cause a retry to click twice.
                    self.human["last_input"] = number
                    self.human["last_ack"] = {
                        "operation": operation,
                        "input_id": number,
                        "status": "uncertain",
                        "control_epoch": self.control_epoch,
                    }
                    try:
                        if operation == "pointer":
                            if phase == "cancel":
                                await self.release_keys()
                            else:
                                self._held_mouse = (cdp, x, y)
                                if phase == "down":
                                    self._drag_target = {
                                        key: state.get(key) for key in target_fields
                                    }
                                    if self.coordinate_guard is not None:
                                        self._drag_target.update(
                                            coordinate_token=state["coordinate_token"],
                                            frame_tree_token=state["frame_tree_token"],
                                            input_frame=routed_target,
                                        )
                                await asyncio.wait_for(
                                    cdp.cdp_client.send.Input.dispatchMouseEvent(
                                        params={
                                            "type": {
                                                "down": "mousePressed",
                                                "move": "mouseMoved",
                                                "up": "mouseReleased",
                                            }[phase],
                                            "x": x,
                                            "y": y,
                                            "button": "left",
                                            "buttons": 0 if phase == "up" else 1,
                                            "clickCount": body.get("click_count", 1),
                                        },
                                        session_id=cdp.session_id,
                                    ),
                                    3,
                                )
                                if phase == "up":
                                    self._held_mouse = None
                                    self._drag_target = None
                        elif operation == "click":
                            for kind in ("mousePressed", "mouseReleased"):
                                self._held_mouse = (cdp, x, y)
                                await asyncio.wait_for(
                                    cdp.cdp_client.send.Input.dispatchMouseEvent(
                                        params={
                                            "type": kind,
                                            "x": x,
                                            "y": y,
                                            "button": "left",
                                            "clickCount": body.get("click_count", 1),
                                        },
                                        session_id=cdp.session_id,
                                    ),
                                    3,
                                )
                            self._held_mouse = None
                        elif operation == "wheel":
                            await asyncio.wait_for(
                                cdp.cdp_client.send.Input.dispatchMouseEvent(
                                    params={
                                        "type": "mouseWheel",
                                        "x": x,
                                        "y": y,
                                        "deltaX": body["delta_x"],
                                        "deltaY": body["delta_y"],
                                    },
                                    session_id=cdp.session_id,
                                ),
                                3,
                            )
                        elif operation == "resize":
                            await self.resize_viewport(browser, cdp, body)
                        elif operation == "reset_keys":
                            await self.release_keys()
                        elif operation == "text":
                            await asyncio.wait_for(
                                cdp.cdp_client.send.Input.insertText(
                                    params={"text": text}, session_id=cdp.session_id
                                ),
                                3,
                            )
                        else:
                            # Track before sending so uncertain dispatches are cleaned up too.
                            if body["phase"] == "down":
                                self._held_keys[code] = (cdp, params)
                            await asyncio.wait_for(
                                cdp.cdp_client.send.Input.dispatchKeyEvent(
                                    params=params, session_id=cdp.session_id
                                ),
                                3,
                            )
                            if body["phase"] == "up":
                                self._held_keys.pop(code, None)
                    except Exception:
                        self.control_mode = "paused"
                        with contextlib.suppress(Exception):
                            await self.release_keys()
                        return web.json_response(self.human["last_ack"])
                    self.applied_input_id = number
                    if operation in {"text", "key", "click", "wheel"} or (
                        operation == "pointer" and phase == "up"
                    ):
                        # The timeline contains categories only, including for passwords.
                        self.recent_actions.append(
                            {
                                "action_id": uuid4().hex,
                                "kind": "scroll"
                                if operation == "wheel"
                                else "click"
                                if operation in {"click", "pointer"}
                                else "type",
                                "actor": "human",
                                "status": "succeeded",
                                "input_category": body.get("source", "keyboard"),
                                "browser_session_id": browser.id,
                            }
                        )
                        await self.action_changed()
                    self.human["last_ack"] = {
                        "operation": operation,
                        "input_id": number,
                        "status": "applied",
                        "control_epoch": self.control_epoch,
                    }
                elif operation != "heartbeat":
                    raise web.HTTPBadRequest(text="Unsupported manual operation.")
            if self._human_expiry is not None:
                self._human_expiry.cancel()
                self._human_expiry = None
            if self.human is not None:
                epoch = self.control_epoch

                async def expire():
                    """Revoke a silent connection without restarting any Agent."""
                    await asyncio.sleep(30)
                    async with self._control_lock:
                        if self.control_epoch == epoch:
                            self.control_mode = "paused"
                            self.human = None
                            self.control_epoch += 1
                            with contextlib.suppress(Exception):
                                await self.release_keys()
                            await self.action_changed()

                self._human_expiry = asyncio.create_task(expire())
            if operation == "heartbeat":
                # A lease renewal must not depend on a document execution context
                # surviving navigation or a slow page answering Runtime.evaluate.
                return web.json_response(
                    {
                        "operation": operation,
                        "status": "applied",
                        "control_epoch": self.control_epoch,
                    }
                )
            _, _, state = await asyncio.wait_for(self.read_state(), 3)
            state.update(
                control_mode=self.control_mode, control_epoch=self.control_epoch
            )
            self.publish("state", state)
            return web.json_response(
                {
                    "operation": operation,
                    "status": "applied",
                    "input_id": body.get("input_id"),
                    "control_epoch": self.control_epoch,
                    "state": state,
                    "agent_observation": agent_observation,
                }
            )

    def action_state(self, session_id):
        """Read a bounded action snapshot scoped to one browser lifetime.

        Args:
            session_id: Actual BrowserSession ID, never a model parameter.

        Returns:
            Current action, recent terminal actions, cursor and optional marker.
        """
        if self.current_action and self.current_action["browser_session_id"] is None:
            self.current_action["browser_session_id"] = session_id
        current = self.current_action
        return {
            "current_action": dict(current)
            if current and current["browser_session_id"] == session_id
            else None,
            "recent_actions": [
                dict(item)
                for item in self.recent_actions
                if item["browser_session_id"] == session_id
            ],
            "action_cursor": self.action_cursor,
            "highlight": self.highlight
            if self.highlight
            and self.highlight["browser_session_id"] == session_id
            and self.highlight["expires_at"] > time.time()
            else None,
        }

    async def action_changed(self, closed_session_id=None):
        """Publish an authoritative action snapshot even during CDP failure.

        Args:
            closed_session_id: Previous lifetime for a confirmed browser close.
        """
        try:
            _, _, state = await asyncio.wait_for(self.read_state(), 2)
        except Exception:
            browser = self.server.browser_session
            state = {
                "generation": self.generation,
                "browser_session_id": browser.id if browser else None,
                "tab_id": None,
                "available": browser is not None,
                "url": "",
                "title": "",
                "width": 0,
                "height": 0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        if closed_session_id and not state.get("available"):
            self.execution_state = "closed"
            state.update(browser_session_id=closed_session_id, execution_state="closed")
        state["execution_state"] = self.execution_state
        self.action_cursor = self.sequence + 1
        state.update(self.action_state(state["browser_session_id"]))
        event = self.publish("action", state)
        self.action_events.append(event)

    async def cancel_action(self, request):
        """Reconcile a host timeout/cancellation without trusting public clients.

        Args:
            request: Credential-protected, generation-bound internal request.

        Returns:
            Acknowledgement after the matching action reaches a terminal state.
        """
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), f"Bearer {self.token}"
        ):
            raise web.HTTPUnauthorized()
        body = await request.json()
        if body.get("generation") != self.generation:
            raise web.HTTPConflict()
        action_id = body.get("action_id")
        if body.get("stop_execution"):
            async with self._control_lock:
                self.execution_state = "stopped"
                self.control_mode = "paused"
                self.human = None
                self.control_epoch += 1
                self.revoked_leases.append(body.get("lease"))
                with contextlib.suppress(Exception):
                    await self.release_keys()
        self.cancelled_actions.append(action_id)
        if self.current_action and self.current_action["action_id"] == action_id:
            action = self.current_action
            self.current_action = None
            self.highlight = None
            self.recent_actions.append(
                {
                    **action,
                    "status": body.get("status")
                    if body.get("status") in {"failed", "timed_out", "cancelled"}
                    else "cancelled",
                    "finished_at": time.time(),
                }
            )
            if self.action_task:
                self.action_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.action_task
            await asyncio.wait_for(self.action_done.wait(), 10)
            await self.action_changed()
        return web.json_response({"ok": True})

    async def control(self, request):
        """Stop only the execution child, then optionally close owned browsers.

        Args:
            request: Authenticated internal request bound to this runtime/session.

        Returns:
            Confirmed terminal state after execution and cleanup have completed.

        Raises:
            HTTPUnauthorized: The private credential is missing or invalid.
            HTTPConflict: The runtime or browser lifetime has expired.
            HTTPBadRequest: The requested operation is unsupported.
            Exception: Execution or browser cleanup cannot finish.
        """
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), f"Bearer {self.token}"
        ):
            raise web.HTTPUnauthorized()
        body = await request.json()
        if body.get("generation") != self.generation or self._closed:
            raise web.HTTPConflict(text="Browser generation expired.")
        operation = body.get("operation")
        if operation not in {"stop", "close", "disable"}:
            raise web.HTTPBadRequest()
        async with self._control_lock:
            browser = self.server.browser_session
            if operation != "disable" and (
                browser is None or body.get("browser_session_id") != browser.id
            ):
                raise web.HTTPConflict(text="Browser session expired.")
            self.execution_state = "stopped"
            self.control_mode = "paused"
            self.human = None
            self.control_epoch += 1
            self.revoked_leases.append(body.get("lease"))
            with contextlib.suppress(Exception):
                await self.release_keys()
            self.highlight = None
            if self.current_action:
                self.cancelled_actions.append(self.current_action["action_id"])
                if self.action_task:
                    self.action_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.action_task
                await asyncio.wait_for(self.action_done.wait(), 10)
            try:
                _, _, state = await asyncio.wait_for(self.read_state(), 2)
            except Exception:
                state = {
                    "generation": self.generation,
                    "browser_session_id": browser.id if browser else None,
                    "available": browser is not None,
                    "url": "",
                    "title": "",
                    "width": 0,
                    "height": 0,
                }
            if operation in {"close", "disable"}:
                await self.server._close_all_sessions()
                # Only confirmed browser destruction resolves unacknowledged keys.
                self._held_keys.clear()
                self._held_mouse = None
                self._drag_target = None
                self.owner = None
                self.execution_state = (
                    "disabled" if operation == "disable" else "closed"
                )
                self.control_mode = "agent" if operation == "close" else "paused"
                state.update(available=False, url="", title="", width=0, height=0)
            state.update(self.action_state(browser.id if browser else None))
            state["execution_state"] = self.execution_state
            self._refresh_stream = True
            event = self.publish("state", state)
            return web.json_response(event, headers={"Cache-Control": "no-store"})

    def publish(self, kind: str, state: dict) -> dict:
        """Order observations and replace each subscriber's pending event.

        Args:
            kind: Event discriminator: state, frame, action, or error.
            state: Self-contained browser identity and observation payload.

        Returns:
            The ordered event, shared by bounded subscriber queues.
        """
        self.sequence += 1
        event = {
            **state,
            "execution_state": self.execution_state,
            "control_mode": self.control_mode,
            "control_epoch": state.get("control_epoch", self.control_epoch),
            "manual_control_version": 1,
            "keyboard_input_version": 1,
            "pointer_input_version": 1,
            "drag_input_version": 1,
            "viewport_resize_version": 1,
            "applied_input_id": state.get("applied_input_id", self.applied_input_id),
            **self.action_state(state.get("browser_session_id")),
            "type": kind,
            "sequence": self.sequence,
        }
        for queue in self.subscribers:
            queue.put_nowait(event)
        return event

    async def _close_snapshot_session(self):
        """Release capture resources while the snapshot lock is held."""
        cached, self._snapshot_session = self._snapshot_session, None
        if cached is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(cached[2].stop(), 1)

    async def capture_snapshot(self, browser, cdp, state):
        """Capture pixels with the same document and viewport consistency checks.

        Args:
            browser: Actual owned BrowserSession, never a client-selected browser.
            cdp: Runtime-resolved current target's CDP session.
            state: Metadata sampled immediately before capturing the viewport.

        Returns:
            Bounded JPEG pixels tagged with their validated document identity.

        Raises:
            HTTPConflict: Navigation, target or viewport changed during capture.
            HTTPRequestEntityTooLarge: The encoded image exceeds the bridge budget.
        """
        state = dict(state)
        if self.coordinate_guard is not None and not state.get("coordinate_ready"):
            raise web.HTTPConflict(text="Frame coordinates are not ready.")
        captured_at = time.time()
        async with self._snapshot_lock:
            if (
                self._closed
                or self.server.browser_session is not browser
                or browser.agent_focus_target_id != state["tab_id"]
            ):
                raise web.HTTPConflict(text="Browser capture lifetime expired.")
            cached = self._snapshot_session
            if cached is not None and (
                cached[0] is not browser or cached[1] != state["tab_id"]
            ):
                await self._close_snapshot_session()
                cached = None
            try:
                if cached is None:
                    from cdp_use import CDPClient

                    # Keep capture outside the Agent's CDP session pool: upstream
                    # may select any pooled session for held-key input. Replacing
                    # this read-only connection must never detach an input session.
                    client = CDPClient(browser.cdp_url)
                    self._snapshot_session = (browser, state["tab_id"], client, None)
                    await asyncio.wait_for(client.start(), 2)
                    attached = await asyncio.wait_for(
                        client.send.Target.attachToTarget(
                            params={"targetId": state["tab_id"], "flatten": True}
                        ),
                        2,
                    )
                    cached = (browser, state["tab_id"], client, attached["sessionId"])
                    self._snapshot_session = cached
                shot = await cached[2].send.Page.captureScreenshot(
                    params={
                        "format": "jpeg",
                        "quality": 90,
                        "captureBeyondViewport": False,
                        "clip": {
                            "x": state["scroll_x"],
                            "y": state["scroll_y"],
                            "width": state["viewport_width"],
                            "height": state["viewport_height"],
                            "scale": min(
                                2,
                                MAX_FRAME_WIDTH / state["viewport_width"],
                                MAX_FRAME_HEIGHT / state["viewport_height"],
                            ),
                        },
                    },
                    session_id=cached[3],
                )
                after = await cached[2].send.Runtime.evaluate(
                    params={"expression": PAGE_STATE_EXPRESSION, "returnByValue": True},
                    session_id=cached[3],
                )
            except BaseException:
                await self._close_snapshot_session()
                raise
        after_state = json.loads(after["result"]["value"])
        expected = {
            key: state[key]
            for key in (
                "url",
                "title",
                "document_id",
                "viewport_width",
                "viewport_height",
                "scroll_x",
                "scroll_y",
                "position_revision",
            )
        }
        if after_state != expected:
            raise web.HTTPConflict(text="Page changed; refresh observation.")
        if self.coordinate_guard is not None:
            from frame_coordinates import CoordinateUnavailable

            try:
                checked = await self.coordinate_guard.sample(state["tab_id"])
            except CoordinateUnavailable as exc:
                raise web.HTTPConflict(
                    text="Frame coordinates changed during capture."
                ) from exc
            if any(
                checked[key] != state.get(key)
                for key in ("coordinate_token", "coordinate_event_revision")
            ):
                raise web.HTTPConflict(text="Frame coordinates changed during capture.")
        raw = base64.b64decode(shot["data"])
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.width > MAX_FRAME_WIDTH or picture.height > MAX_FRAME_HEIGHT:
                picture.thumbnail((MAX_FRAME_WIDTH, MAX_FRAME_HEIGHT))
                encoded = io.BytesIO()
                picture.convert("RGB").save(encoded, format="JPEG", quality=90)
                raw = encoded.getvalue()
                shot["data"] = base64.b64encode(raw).decode("ascii")
        if len(raw) > MAX_IMAGE_BYTES:
            with Image.open(io.BytesIO(raw)) as picture:
                for quality in (80, 65, 45):
                    encoded = io.BytesIO()
                    picture.convert("RGB").save(encoded, format="JPEG", quality=quality)
                    raw = encoded.getvalue()
                    if len(raw) <= MAX_IMAGE_BYTES:
                        break
            shot["data"] = base64.b64encode(raw).decode("ascii")
        if len(raw) > MAX_IMAGE_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_IMAGE_BYTES, actual_size=len(raw)
            )
        with Image.open(io.BytesIO(raw)) as picture:
            state["width"], state["height"] = picture.size
        state.update(
            image=shot["data"], mime_type="image/jpeg", capture_timestamp=captured_at
        )
        if (
            self._closed
            or self.server.browser_session is not browser
            or browser.agent_focus_target_id != state["tab_id"]
        ):
            raise web.HTTPConflict(text="Browser target changed; refresh observation.")
        return state

    async def observe(self, request: web.Request) -> web.Response:
        """Read metadata or capture the focused target without rebuilding the DOM.

        Args:
            request: Authenticated internal status or snapshot request.

        Returns:
            Bounded image and identity metadata, or an explicit HTTP error.
        """
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), f"Bearer {self.token}"
        ):
            raise web.HTTPUnauthorized()
        if request.query.get("generation") != self.generation:
            raise web.HTTPConflict(text="Browser generation expired.")
        if request.path == "/identity":
            browser = self.server.browser_session
            return web.json_response(
                {
                    "generation": self.generation,
                    "browser_session_id": browser.id if browser else None,
                    "available": browser is not None,
                    "execution_state": self.execution_state,
                    "control_mode": self.control_mode,
                    "control_epoch": self.control_epoch,
                    "manual_control_version": 1,
                    "keyboard_input_version": 1,
                    "pointer_input_version": 1,
                    "drag_input_version": 1,
                    "viewport_resize_version": 1,
                    "applied_input_id": self.applied_input_id,
                },
                headers={"Cache-Control": "no-store"},
            )
        browser, cdp, state = await self.read_state()
        if cdp is None:
            return web.json_response(state, headers={"Cache-Control": "no-store"})
        target = state["tab_id"]
        if request.path == "/snapshot":
            state = await self.capture_snapshot(browser, cdp, state)
            self.sequence += 1
            state["sequence"] = self.sequence
        # A concurrent tab switch must never label an old image as the new page.
        if (
            self.server.browser_session is not browser
            or browser.agent_focus_target_id != target
        ):
            raise web.HTTPConflict(text="Browser target changed; refresh observation.")
        state.update(self.action_state(state["browser_session_id"]))
        return web.json_response(state, headers={"Cache-Control": "no-store"})

    async def stream(self, request: web.Request) -> web.StreamResponse:
        """Subscribe to the existing browser using bounded frame and state slots.

        Args:
            request: Credential-protected request with generation/session identity.

        Returns:
            SSE with self-contained state/frame events and idle heartbeats.
        """
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), f"Bearer {self.token}"
        ):
            raise web.HTTPUnauthorized()
        if request.query.get("generation") != self.generation:
            raise web.HTTPConflict(text="Browser generation expired.")
        browser, _, state = await self.read_state()
        if browser is None or request.query.get("browser_session_id") != browser.id:
            raise web.HTTPConflict(text="Browser session expired.")
        queue = ObservationQueue()
        try:
            cursor = int(request.query.get("action_cursor", "0"))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid action cursor.") from None
        if cursor < 0 or cursor > self.sequence:
            raise web.HTTPConflict(text="Action cursor expired.")
        async with self._stream_lock:
            if len(self.subscribers) >= 4:
                raise web.HTTPTooManyRequests()
            self.subscribers.add(queue)
            retained = list(self.action_events)
            if cursor and retained and cursor >= retained[0]["sequence"] - 1:
                for event in retained:
                    if (
                        event["sequence"] > cursor
                        and event["browser_session_id"] == browser.id
                    ):
                        queue.put_nowait(event)
            else:
                state["action_reset"] = True
            if self._producer is None:
                self._producer = asyncio.create_task(self.capture())
            self.publish("state", state)
            # Restart the shared cast so even a static page supplies a fresh frame.
            self._refresh_stream = True
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            }
        )
        try:
            await response.prepare(request)
            while not self._closed:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1)
                    if event is None:
                        break
                    if event.get("browser_session_id") != browser.id:
                        continue
                    data = f"id: {event['sequence']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
                    if len(data) > MAX_EVENT_BYTES:
                        break
                except asyncio.TimeoutError:
                    data = b": hb\n\n"
                await asyncio.wait_for(response.write(data), timeout=2)
                if data != b": hb\n\n" and event.get("execution_state") in {
                    "closed",
                    "disabled",
                }:
                    break
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            async with self._stream_lock:
                self.subscribers.discard(queue)
                if not self.subscribers and self._producer is not None:
                    self._producer.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._producer
                    self._producer = None
        return response

    async def acknowledge(self, queue):
        """Acknowledge every CDP frame independently of image delivery.

        Args:
            queue: Bounded CDP client/session/frame acknowledgement tuples.
        """
        while True:
            client, session_id, frame_id = await queue.get()
            try:
                await asyncio.wait_for(
                    client.send.Page.screencastFrameAck(
                        params={"sessionId": frame_id}, session_id=session_id
                    ),
                    2,
                )
            except Exception:
                # A detached old target still needs an attempted acknowledgement.
                continue

    async def capture(self):
        """Follow Agent focus with bounded human and read-only capture rates.

        Frame callbacks never await CDP: its receive loop also resolves commands.
        The acknowledgement queue bounds work even if the CDP connection stalls.
        """
        active = None
        pending = None
        metadata = None
        last_screencast = None
        not_before = 0.0
        acknowledgements = asyncio.Queue(maxsize=8)
        ack_task = asyncio.create_task(self.acknowledge(acknowledgements))
        overflow = False
        last_frame = time.monotonic()

        def frame_received(event, session_id):
            """Queue acknowledgements and retain only a current-target image.

            Args:
                event: CDP frame with image, timestamp and acknowledgement ID.
                session_id: CDP session that produced the frame, including old tabs.
            """
            nonlocal pending, overflow
            if active is None:
                return
            browser, cdp, target = active
            try:
                acknowledgements.put_nowait(
                    (cdp.cdp_client, session_id, event["sessionId"])
                )
            except asyncio.QueueFull:
                overflow = True
            if (
                session_id == cdp.session_id
                and self.server.browser_session is browser
                and browser.agent_focus_target_id == target
                and event.get("metadata", {}).get("timestamp", 0) >= not_before
                and len(event["data"]) <= MAX_IMAGE_BYTES * 4 // 3 + 4
            ):
                pending = event

        try:
            while self.subscribers:
                try:
                    browser, cdp, state = await asyncio.wait_for(self.read_state(), 3)
                    target = state["tab_id"]
                    changed = (
                        self._refresh_stream
                        or active is None
                        or active[0] is not browser
                        or active[1].session_id != cdp.session_id
                        or active[2] != target
                    )
                    navigated = metadata is not None and (
                        metadata["url"] != state["url"]
                        or metadata.get("document_id") != state.get("document_id")
                    )
                    if changed or navigated or overflow:
                        self._refresh_stream = False
                        if active is not None:
                            previous = active[1]
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(
                                    previous.cdp_client.send.Page.stopScreencast(
                                        session_id=previous.session_id
                                    ),
                                    2,
                                )
                            previous.cdp_client._event_registry.unregister(
                                "Page.screencastFrame"
                            )
                        active = None
                        pending = None
                        last_screencast = None
                        overflow = False
                        self.publish("state", state)
                        if cdp is not None:
                            active = (browser, cdp, target)
                            cdp.cdp_client.register.Page.screencastFrame(frame_received)
                            not_before = time.time()
                            await asyncio.wait_for(
                                cdp.cdp_client.send.Page.startScreencast(
                                    params={
                                        "format": "jpeg",
                                        "quality": 90,
                                        "maxWidth": MAX_FRAME_WIDTH,
                                        "maxHeight": MAX_FRAME_HEIGHT,
                                        "everyNthFrame": 1,
                                    },
                                    session_id=cdp.session_id,
                                ),
                                2,
                            )
                    elif metadata is not None and metadata["title"] != state["title"]:
                        self.publish("state", state)
                    metadata = state
                    event, pending = pending, None
                    frame_metadata = event.get("metadata", {}) if event else {}
                    # Frame metadata belongs to these pixels; read_state() is a
                    # later independent sample and must not relabel an old view.
                    coordinates_match = all(
                        isinstance(actual, (int, float))
                        and isinstance(expected, (int, float))
                        and math.isfinite(actual)
                        and math.isfinite(expected)
                        and math.isclose(actual, expected, rel_tol=0, abs_tol=0.001)
                        for actual, expected in (
                            (
                                frame_metadata.get("scrollOffsetX"),
                                state.get("scroll_x"),
                            ),
                            (
                                frame_metadata.get("scrollOffsetY"),
                                state.get("scroll_y"),
                            ),
                            (
                                frame_metadata.get("deviceWidth"),
                                state.get("viewport_width"),
                            ),
                            (
                                frame_metadata.get("deviceHeight"),
                                state.get("viewport_height"),
                            ),
                            (frame_metadata.get("pageScaleFactor"), 1),
                            (frame_metadata.get("offsetTop"), 0),
                        )
                    )
                    if event is not None and coordinates_match:
                        visual = (
                            event["data"],
                            *(
                                frame_metadata.get(key)
                                for key in (
                                    "deviceWidth",
                                    "deviceHeight",
                                    "scrollOffsetX",
                                    "scrollOffsetY",
                                    "pageScaleFactor",
                                    "offsetTop",
                                )
                            ),
                        )
                        # Capturing a screenshot can emit an unchanged cast.
                        # Repeated pixels are not a new visual change; the idle
                        # fallback and human input feedback still capture below.
                        if visual == last_screencast:
                            event = None
                        else:
                            last_screencast = visual
                    if cdp is not None and (
                        self.control_mode == "human"
                        or (event is not None and coordinates_match)
                        or (
                            event is not None
                            and not coordinates_match
                            and time.monotonic() - last_frame >= 1
                        )
                        or (event is None and time.monotonic() - last_frame >= 1)
                    ):
                        # Screencast only signals changes: its native-resolution
                        # pixels differ from scaled screenshots. Use one capture
                        # path for live, idle and human-control frames so text and
                        # geometry cannot alternate between rasterization scales.
                        frame = await asyncio.wait_for(
                            self.capture_snapshot(browser, cdp, state), 3
                        )
                        self.publish("frame", frame)
                        last_frame = time.monotonic()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Focus recovery/navigation can temporarily detach a CDP target.
                    if metadata is not None:
                        self.publish(
                            "state",
                            {
                                **metadata,
                                "available": False,
                                "tab_id": None,
                                "url": "",
                                "title": "",
                            },
                        )
                    if active is not None:
                        cdp = active[1]
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                cdp.cdp_client.send.Page.stopScreencast(
                                    session_id=cdp.session_id
                                ),
                                2,
                            )
                        cdp.cdp_client._event_registry.unregister(
                            "Page.screencastFrame"
                        )
                    active = None
                    pending = None
                await asyncio.sleep(
                    HUMAN_FRAME_INTERVAL
                    if self.control_mode == "human"
                    else FRAME_INTERVAL
                )
        finally:
            if active is not None:
                cdp = active[1]
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        cdp.cdp_client.send.Page.stopScreencast(
                            session_id=cdp.session_id
                        ),
                        2,
                    )
                cdp.cdp_client._event_registry.unregister("Page.screencastFrame")
            ack_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ack_task

    async def close(self) -> None:
        """Release the bridge and remove only this process's discovery record."""
        self._closed = True
        self.control_mode = "paused"
        self.human = None
        self.control_epoch += 1
        with contextlib.suppress(Exception):
            await self.release_keys()
        if self._human_expiry is not None:
            self._human_expiry.cancel()
        for queue in self.subscribers:
            queue.put_nowait(None)
        if self._producer is not None:
            self._producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._producer
        if self.runner is not None:
            await self.runner.cleanup()
        async with self._snapshot_lock:
            await self._close_snapshot_session()
        if self.coordinate_guard is not None:
            await self.coordinate_guard.close()
        if self.discovery.exists():
            record = json.loads(self.discovery.read_text(encoding="utf-8"))
            if record.get("generation") == self.generation:
                self.discovery.unlink()
