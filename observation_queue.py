"""Bound action delivery separately from replaceable browser images."""

import asyncio
from collections import deque

ACTION_LIMIT = 128


class ObservationQueue:
    """Keep one image and a bounded ordered action backlog per viewer."""

    def __init__(self):
        self.actions = deque()
        self.latest = None
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
        else:
            self.latest = event
        self.ready.set()

    async def get(self):
        """Return the oldest retained event, preserving global sequence order.

        Returns:
            An observation, or None after buffered events have been drained.
        """
        while True:
            if self.actions or self.latest is not None:
                if self.actions and (
                    self.latest is None
                    or self.actions[0]["sequence"] < self.latest["sequence"]
                ):
                    return self.actions.popleft()
                event, self.latest = self.latest, None
                return event
            if self.closed:
                return None
            self.ready.clear()
            await self.ready.wait()
