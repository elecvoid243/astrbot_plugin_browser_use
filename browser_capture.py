"""Bind native screencast pixels to a validated document/coordinate epoch."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections import deque
from uuid import uuid4

from aiohttp import web

IDENTITY_FIELDS = (
    "generation",
    "browser_session_id",
    "tab_id",
    "document_id",
    "url",
    "viewport_width",
    "viewport_height",
    "scroll_x",
    "scroll_y",
    "position_revision",
    "control_epoch",
    "coordinate_contract_version",
    "coordinate_token",
    "frame_tree_token",
    "coordinate_event_revision",
)


class BrowserCapture:
    """Use a fresh validated snapshot before admitting a native capture epoch.

    Ownership and coordinates are immutable source metadata. A later state read
    can reject a record but cannot relabel its pixels. Input watermarks are lower
    bounds until the producer establishes another validated capture barrier.
    """

    def __init__(self, observer, wait_for_transition=True):
        """Attach a bounded capture candidate to the existing private observer.

        Args:
            observer: Actual runtime observer with existing identity validation.
            wait_for_transition: Test whether waiting for cross-document transitions
                repairs the two-rAF/snapshot barrier counterexample.
        """
        self.observer = observer
        self.wait_for_transition = wait_for_transition
        self.session_id = uuid4().hex
        self.epoch = 0
        self.sequence = 0
        self.anchor = None
        self.snapshot = None
        self.cutoff = math.inf
        self.client = None
        self.cdp_session = None
        self.ack_task = None
        self.pending = asyncio.Queue(maxsize=1)
        self.acks = asyncio.Queue(maxsize=8)
        self.rejected = deque(maxlen=128)

    @staticmethod
    def identity(state):
        """Produce the full immutable target key used at each barrier check.

        Args:
            state: Existing observer target metadata.

        Returns:
            Target, document, coordinates, revision and control identity tuple.
        """
        return tuple(state.get(name) for name in IDENTITY_FIELDS)

    async def close(self):
        """Invalidate old source callbacks and release only this capture session."""
        self.epoch += 1
        self.anchor = None
        self.snapshot = None
        self.cutoff = math.inf
        self.pending = asyncio.Queue(maxsize=1)
        if self.ack_task:
            self.ack_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.ack_task
            self.ack_task = None
        client, self.client = self.client, None
        if client:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    client.send.Page.stopScreencast(session_id=self.cdp_session), 1
                )
            await asyncio.wait_for(client.stop(), 2)
        self.cdp_session = None

    async def acknowledge(self):
        """Bound native capture pacing without adding an encoder delivery delay."""
        while True:
            client, session_id, frame_id = await self.acks.get()
            started = time.monotonic()
            try:
                await asyncio.wait_for(
                    client.send.Page.screencastFrameAck(
                        params={"sessionId": frame_id}, session_id=session_id
                    ),
                    2,
                )
            except Exception:
                self.cutoff = math.inf
            await asyncio.sleep(max(0, 1 / 30 - (time.monotonic() - started)))

    async def barrier(self):
        """Establish a new epoch only after a consistent existing snapshot.

        Returns:
            The validated snapshot used as the explicit safe recovery image.

        Raises:
            HTTPConflict: Identity keeps changing during bounded capture attempts.
        """
        from cdp_use import CDPClient

        await self.close()
        for attempt in range(3):
            browser, cdp, state = await self.observer.read_state()
            try:
                # A new Runtime document can exist before its first compositor
                # frame. Bound two paint opportunities before the snapshot check.
                if self.wait_for_transition:
                    await asyncio.wait_for(
                        cdp.cdp_client.send.Runtime.evaluate(
                            params={
                                "expression": "document.activeViewTransition ? document.activeViewTransition.finished.then(()=>true) : true",
                                "awaitPromise": True,
                            },
                            session_id=cdp.session_id,
                        ),
                        2,
                    )
                # Production JPEG capture already waits for two paint opportunities.
                if not self.observer.protect_coordinates:
                    await asyncio.wait_for(
                        cdp.cdp_client.send.Runtime.evaluate(
                            params={
                                "expression": "new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))",
                                "awaitPromise": True,
                            },
                            session_id=cdp.session_id,
                        ),
                        2,
                    )
                _, _, state = await self.observer.read_state()
                snapshot = await asyncio.wait_for(
                    self.observer.capture_snapshot(browser, cdp, state), 3
                )
                _, _, after = await self.observer.read_state()
                if self.identity(snapshot) != self.identity(after):
                    raise web.HTTPConflict(text="Capture barrier identity changed.")
                break
            except web.HTTPConflict:
                if attempt == 2:
                    raise
        self.snapshot = snapshot
        self.anchor = {key: value for key, value in snapshot.items() if key != "image"}
        self.cutoff = time.time()
        self.client = client = CDPClient(browser.cdp_url)
        await asyncio.wait_for(client.start(), 2)
        attached = await client.send.Target.attachToTarget(
            params={"targetId": state["tab_id"], "flatten": True}
        )
        self.cdp_session = attached["sessionId"]
        epoch = self.epoch
        source = uuid4().hex
        anchor = dict(self.anchor)
        self.acks = asyncio.Queue(maxsize=8)

        def received(event, session_id):
            """Bind callback ownership before any asynchronous frame processing.

            Args:
                event: Actual Chromium screencast event.
                session_id: Independent read-only capture session.
            """
            if (
                self.client is not client
                or epoch != self.epoch
                or session_id != self.cdp_session
            ):
                return
            try:
                self.acks.put_nowait((client, session_id, event["sessionId"]))
            except asyncio.QueueFull:
                self.cutoff = math.inf
                return
            item = {
                "media_session_id": self.session_id,
                "media_epoch": epoch,
                "source": source,
                "state": anchor,
                "event": event,
                "received_at": time.time(),
                "received_monotonic": time.monotonic(),
            }
            if self.pending.full():
                self.pending.get_nowait()
            self.pending.put_nowait(item)

        client.register.Page.screencastFrame(received)
        self.ack_task = asyncio.create_task(self.acknowledge())
        await client.send.Page.startScreencast(
            params={
                "format": "jpeg",
                "quality": 90,
                "maxWidth": 1920,
                "maxHeight": 1080,
                "everyNthFrame": 1,
            },
            session_id=self.cdp_session,
        )
        return snapshot

    def matches(self, record, state):
        """Reject expired source records without relabeling their captured state.

        Args:
            record: Original callback envelope or previously emitted capture.
            state: Actual current observer state.

        Returns:
            Whether ownership, identity and compositor metadata pass the candidate.
        """
        if (
            record["media_session_id"] != self.session_id
            or record["media_epoch"] != self.epoch
            or self.anchor is None
            or (
                state.get("coordinate_contract_version") == 2
                and not state.get("coordinate_ready")
            )
            or self.identity(record["state"]) != self.identity(state)
            or (
                self.observer.coordinate_guard is not None
                and record["state"].get("coordinate_event_revision")
                != self.observer.coordinate_guard.dirty
            )
        ):
            return False
        meta = record["event"]["metadata"]
        stamp = meta.get("timestamp")
        if (
            not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
            or stamp < self.cutoff
        ):
            return False
        return all(
            math.isclose(meta.get(name, math.inf), state[key], abs_tol=0.001, rel_tol=0)
            for name, key in (
                ("deviceWidth", "viewport_width"),
                ("deviceHeight", "viewport_height"),
                ("scrollOffsetX", "scroll_x"),
                ("scrollOffsetY", "scroll_y"),
            )
        ) and all(
            math.isclose(meta.get(name, math.inf), expected, abs_tol=0.001, rel_tol=0)
            for name, expected in (("pageScaleFactor", 1), ("offsetTop", 0))
        )

    async def next_frame(self, timeout=5):
        """Read a current-epoch frame while rechecking all existing target guards.

        Args:
            timeout: Maximum seconds to wait for a valid source frame.

        Returns:
            Original pixels, immutable state and media session/epoch identity.

        Raises:
            TimeoutError: No candidate frame passes the barrier in time.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = await asyncio.wait_for(
                self.pending.get(), max(0.001, deadline - time.monotonic())
            )
            _, _, state = await self.observer.read_state()
            if self.anchor is not None and self.identity(self.anchor) != self.identity(
                state
            ):
                raise web.HTTPConflict(
                    text="Capture identity changed; establish a new epoch."
                )
            if self.matches(record, state):
                self.sequence += 1
                record["capture_sequence"] = self.sequence
                return record
            self.rejected.append(
                {
                    "media_epoch": record["media_epoch"],
                    "timestamp": record["event"]["metadata"].get("timestamp"),
                    "state": record["state"],
                }
            )
        raise TimeoutError("No current capture frame crossed the identity barrier.")
