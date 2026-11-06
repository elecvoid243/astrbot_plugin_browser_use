"""Bound action delivery separately from replaceable browser images."""

import asyncio
from collections import deque

ACTION_LIMIT = 128


class ObservationQueue:
    """Keep one image, one state and a bounded ordered action backlog per viewer."""

    def __init__(self, *, include_frames=True):
        self.include_frames = include_frames
        self.actions = deque()
        self.latest = None
        self.state = None
        self.ready = asyncio.Event()
        self.closed = False

    def put_nowait(self, event):
        """Retain actions or replace pixels, resetting from state on overflow.

        Args:
            event: Self-contained observation, or None to close the reader.
        """
        if event is None:
            self.closed = True
        elif event.get("type") == "action":
            if len(self.actions) >= ACTION_LIMIT:
                self.actions.clear()
                event = {**event, "type": "state", "action_reset": True}
            self.actions.append(event)
        elif event.get("type") == "frame":
            if not self.include_frames:
                return
            self.latest = event
        else:
            # Control/heartbeat metadata must not erase the only new pixels of
            # a static page. Retain one state and one frame in sequence order.
            self.state = event
        self.ready.set()

    async def get(self):
        """Return the oldest retained event, preserving global sequence order.

        Returns:
            An observation, or None after buffered events have been drained.
        """
        while True:
            candidates = [
                event
                for event in (
                    self.actions[0] if self.actions else None,
                    self.latest,
                    self.state,
                )
                if event is not None
            ]
            if candidates:
                event = min(candidates, key=lambda item: item["sequence"])
                if self.actions and event is self.actions[0]:
                    return self.actions.popleft()
                if event is self.latest:
                    self.latest = None
                else:
                    self.state = None
                return event
            if self.closed:
                return None
            self.ready.clear()
            await self.ready.wait()
