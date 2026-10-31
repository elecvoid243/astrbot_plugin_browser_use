"""Read-only observation of the MCP server's actual Chromium session."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from PIL import Image

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

    async def start(self) -> None:
        """Bind an ephemeral loopback port and publish its discovery record.

        Raises:
            OSError: The socket or discovery file cannot be created.
        """
        app = web.Application()
        app.router.add_get("/status", self.observe)
        app.router.add_get("/snapshot", self.observe)
        app.router.add_get("/stream", self.stream)
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
            "url": "",
            "title": "",
            "width": 0,
            "height": 0,
        }
        if browser is None or not browser.agent_focus_target_id:
            return browser, None, state
        target = browser.agent_focus_target_id
        cdp = await browser.get_or_create_cdp_session(target_id=target, focus=False)
        info = await cdp.cdp_client.send.Runtime.evaluate(
            params={
                "expression": "JSON.stringify({url:location.href,title:document.title})",
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
        if (
            self.server.browser_session is not browser
            or browser.agent_focus_target_id != target
        ):
            raise web.HTTPConflict(text="Browser target changed; refresh observation.")
        return browser, cdp, state

    def publish(self, kind: str, state: dict) -> dict:
        """Order observations and replace each subscriber's pending event.

        Args:
            kind: Event discriminator, currently state, frame, or error.
            state: Self-contained browser identity and observation payload.

        Returns:
            The ordered event, shared by bounded subscriber queues.
        """
        self.sequence += 1
        event = {**state, "type": kind, "sequence": self.sequence}
        for queue in self.subscribers:
            if queue.full():
                queue.get_nowait()
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
                    "expression": "JSON.stringify({url:location.href,title:document.title})",
                    "returnByValue": True,
                },
                session_id=cdp.session_id,
            )
            if json.loads(after["result"]["value"]) != {
                "url": state["url"],
                "title": state["title"],
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
        queue = asyncio.Queue(maxsize=1)
        async with self._stream_lock:
            if len(self.subscribers) >= 4:
                raise web.HTTPTooManyRequests()
            self.subscribers.add(queue)
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
            while not self._closed and self.server.browser_session is browser:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1)
                    if (
                        self.server.browser_session is not browser
                        or event.get("browser_session_id") != browser.id
                    ):
                        break
                    data = f"id: {event['sequence']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
                    if len(data) > MAX_EVENT_BYTES:
                        break
                except asyncio.TimeoutError:
                    data = b": hb\n\n"
                await asyncio.wait_for(response.write(data), timeout=2)
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
                    navigated = metadata is not None and metadata["url"] != state["url"]
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
                    if event is not None and cdp is not None:
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
