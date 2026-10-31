"""Read-only observation of the MCP server's actual Chromium session."""

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
                "expression": "JSON.stringify({url:location.href,title:document.title,document_id:String(performance.timeOrigin),viewport_width:innerWidth,viewport_height:innerHeight,scroll_x:scrollX,scroll_y:scrollY})",
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
            self.execution_state = "stopped"
            self.revoked_leases.append(body.get("lease"))
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
            self.revoked_leases.append(body.get("lease"))
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
                self.execution_state = (
                    "disabled" if operation == "disable" else "closed"
                )
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
            **self.action_state(state.get("browser_session_id")),
            "type": kind,
            "sequence": self.sequence,
        }
        for queue in self.subscribers:
            queue.put_nowait(event)
        return event

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
                },
                headers={"Cache-Control": "no-store"},
            )
        browser, cdp, state = await self.read_state()
        if cdp is None:
            return web.json_response(state, headers={"Cache-Control": "no-store"})
        target = state["tab_id"]
        if request.path == "/snapshot":
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
                    "expression": "JSON.stringify({url:location.href,title:document.title,document_id:String(performance.timeOrigin),viewport_width:innerWidth,viewport_height:innerHeight,scroll_x:scrollX,scroll_y:scrollY})",
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
                )
            }:
                raise web.HTTPConflict(text="Page changed; refresh observation.")
            raw = base64.b64decode(shot["data"])
            if len(raw) > MAX_IMAGE_BYTES:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=MAX_IMAGE_BYTES, actual_size=len(raw)
                )
            with Image.open(io.BytesIO(raw)) as picture:
                state["width"], state["height"] = picture.size
            self.sequence += 1
            state.update(
                sequence=self.sequence, image=shot["data"], mime_type="image/jpeg"
            )
        # A concurrent tab switch must never label an old image as the new page.
        if (
            self.server.browser_session is not browser
            or browser.agent_focus_target_id != target
        ):
            raise web.HTTPConflict(text="Browser target changed; refresh observation.")
        state.update(self.action_state(state["browser_session_id"]))
        return web.json_response(state, headers={"Cache-Control": "no-store"})

    async def stream(self, request: web.Request) -> web.StreamResponse:
        """Subscribe to the existing browser using a single pending event slot.

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
        """Follow Agent focus and publish bounded screencast images at most 4 Hz.

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
                    if event is not None and cdp is not None and coordinates_match:
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
                await asyncio.sleep(FRAME_INTERVAL)
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
