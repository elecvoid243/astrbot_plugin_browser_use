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

MAX_IMAGE_BYTES = 512 * 1024
MAX_EVENT_BYTES = 768 * 1024
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

    def __init__(self, server):
        """Attach to the existing MCP server without starting another browser.

        Args:
            server: Upstream BrowserUseServer that executes Agent tools.
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
        self.pending_browser_events = []

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

    async def read_state(self):
        """Read the actual target metadata without changing focus or DOM indices.

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
            "applied_input_id": self.applied_input_id,
            "url": "",
            "title": "",
            "width": 0,
            "height": 0,
            **self.action_state(browser.id if browser else None),
        }
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
        """Release keys on their original CDP session before changing admission.

        Raises:
            HTTPConflict: Some keys remain unconfirmed after all releases are attempted.
                Admission stays paused and failed keys are retained for retry or close.
        """
        keys = list(self._held_keys.items())
        # Releases are independent. Share one bounded deadline even if many targets died.
        results = await asyncio.gather(
            *(
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
            ),
            return_exceptions=True,
        )
        failed = False
        for (code, _), result in zip(keys, results):
            if isinstance(result, BaseException):
                failed = True
            else:
                self._held_keys.pop(code, None)
        if failed:
            self.control_mode = "paused"
            raise web.HTTPConflict(
                text="Key release unconfirmed; browser remains paused. Close the browser before continuing."
            )

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
                    if self._held_mouse is not None:
                        cdp, x, y = self._held_mouse
                        await asyncio.wait_for(
                            cdp.cdp_client.send.Input.dispatchMouseEvent(
                                params={
                                    "type": "mouseReleased",
                                    "x": x,
                                    "y": y,
                                    "button": "left",
                                    "clickCount": 1,
                                },
                                session_id=cdp.session_id,
                            ),
                            3,
                        )
                        self._held_mouse = None
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
                elif operation in {"click", "wheel", "key", "text", "reset_keys"}:
                    number = body.get("input_id")
                    if type(number) is not int or number < 1:
                        raise web.HTTPBadRequest(text="Invalid input number.")
                    if number == self.human["last_input"]:
                        return web.json_response(self.human["last_ack"])
                    if number != self.human["last_input"] + 1:
                        raise web.HTTPConflict(text="Input sequence expired.")
                    _, cdp, state = await asyncio.wait_for(self.read_state(), 3)
                    target = body.get("target", {})
                    if operation != "reset_keys" and (
                        cdp is None
                        or (
                            "position_revision" in target
                            and target["position_revision"]
                            != state.get("position_revision")
                        )
                        or any(
                            target.get(key) != state.get(key)
                            for key in (
                                "tab_id",
                                "document_id",
                                "viewport_width",
                                "viewport_height",
                                "scroll_x",
                                "scroll_y",
                            )
                        )
                    ):
                        await self.release_keys()
                        if operation == "wheel":
                            # Scroll animation can outlive a displayed frame. Reject
                            # this replaceable sample, consume its ID, and keep the
                            # explicit human owner while the client resynchronizes.
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
                        raise web.HTTPConflict(
                            text="Displayed target expired; refresh observation."
                        )
                    params = None
                    if operation in {"click", "wheel"}:
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
                        if operation == "click" and (
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
                        if operation == "click":
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
                    if operation in {"text", "key", "click", "wheel"}:
                        # The timeline contains categories only, including for passwords.
                        self.recent_actions.append(
                            {
                                "action_id": uuid4().hex,
                                "kind": "scroll"
                                if operation == "wheel"
                                else "click"
                                if operation == "click"
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
            "applied_input_id": state.get("applied_input_id", self.applied_input_id),
            **self.action_state(state.get("browser_session_id")),
            "type": kind,
            "sequence": self.sequence,
        }
        for queue in self.subscribers:
            queue.put_nowait(event)
        return event

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
        captured_at = time.time()
        shot = await cdp.cdp_client.send.Page.captureScreenshot(
            params={
                "format": "jpeg",
                "quality": 65,
                "captureBeyondViewport": False,
            },
            session_id=cdp.session_id,
        )
        after = await cdp.cdp_client.send.Runtime.evaluate(
            params={
                "expression": PAGE_STATE_EXPRESSION,
                "returnByValue": True,
            },
            session_id=cdp.session_id,
        )
        if json.loads(after["result"]["value"]) != {
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
        }:
            raise web.HTTPConflict(text="Page changed; refresh observation.")
        raw = base64.b64decode(shot["data"])
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.width > 1280 or picture.height > 720:
                picture.thumbnail((1280, 720))
                encoded = io.BytesIO()
                picture.convert("RGB").save(encoded, format="JPEG", quality=65)
                raw = encoded.getvalue()
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
            self.server.browser_session is not browser
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
        not_before = 0.0
        acknowledgements = asyncio.Queue(maxsize=8)
        ack_task = asyncio.create_task(self.acknowledge(acknowledgements))
        overflow = False

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
                                        "quality": 65,
                                        "maxWidth": 1280,
                                        "maxHeight": 720,
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
                    if cdp is not None and self.control_mode == "human":
                        # Static pages can stop emitting screencast frames after
                        # asynchronous input effects. Reuse validated snapshots
                        # while a human is interacting, at the same bounded rate.
                        frame = await asyncio.wait_for(
                            self.capture_snapshot(browser, cdp, state), 3
                        )
                        if self.control_mode == "human":
                            self.publish("frame", frame)
                    elif event is not None and cdp is not None and coordinates_match:
                        raw = base64.b64decode(event["data"], validate=True)
                        with Image.open(io.BytesIO(raw)) as picture:
                            width, height = picture.size
                        if (
                            len(raw) <= MAX_IMAGE_BYTES
                            and 0 < width <= 1280
                            and 0 < height <= 720
                            and self.server.browser_session is browser
                            and browser.agent_focus_target_id == target
                        ):
                            self.publish(
                                "frame",
                                {
                                    **state,
                                    "width": width,
                                    "height": height,
                                    "image": event["data"],
                                    "mime_type": "image/jpeg",
                                    "capture_timestamp": event["metadata"]["timestamp"],
                                },
                            )
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
        if self.discovery.exists():
            record = json.loads(self.discovery.read_text(encoding="utf-8"))
            if record.get("generation") == self.generation:
                self.discovery.unlink()
