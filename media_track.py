"""One bounded video track per viewer, using independent encoder input frames."""

from __future__ import annotations

import asyncio

import av
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError


class BrowserVideoTrack(VideoStreamTrack):
    """Retain one source frame and never share mutable frames between encoders."""

    def __init__(self, live_epoch):
        """Attach this track to its source epoch validator.

        Args:
            live_epoch: Non-blocking predicate for current source ownership.
        """
        super().__init__()
        self.queue = asyncio.Queue(maxsize=1)
        self.current = None
        self.live_epoch = live_epoch

    def push(self, frame, metadata):
        """Replace only pending video pixels, never ordered operator input.

        Args:
            frame: Read-only shared decoded source frame.
            metadata: Its immutable capture metadata.
        """
        if self.readyState != "live":
            return
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait((frame, metadata))

    async def recv(self):
        """Copy native planes off-thread and bind the exact encoder input size.

        Returns:
            A private mutable PyAV frame owned only by this sender.

        Raises:
            MediaStreamError: The viewer has stopped.
        """
        while self.readyState == "live":
            item = await self.queue.get()
            if item is None:
                break
            original, metadata = item
            if not self.live_epoch(metadata["media_epoch"]):
                continue

            def clone():
                """Copy pixels with native strides without a NumPy dependency.

                Returns:
                    An independent frame with the same source geometry and clock.
                """
                frame = av.VideoFrame(
                    original.width, original.height, original.format.name
                )
                for source, target in zip(original.planes, frame.planes):
                    source_bytes = memoryview(source)
                    if source.line_size == target.line_size:
                        target.update(source_bytes[: target.buffer_size])
                    else:
                        buffer = bytearray(target.buffer_size)
                        for row in range(target.height):
                            width = min(source.line_size, target.line_size)
                            buffer[
                                row * target.line_size : row * target.line_size + width
                            ] = source_bytes[
                                row * source.line_size : row * source.line_size + width
                            ]
                        target.update(buffer)
                frame.pts = original.pts
                frame.time_base = original.time_base
                frame.pict_type = av.video.frame.PictureType.NONE
                return frame

            frame = await asyncio.to_thread(clone)
            if not self.live_epoch(metadata["media_epoch"]):
                continue
            self.current = {**metadata, "width": frame.width, "height": frame.height}
            return frame
        raise MediaStreamError

    def stop(self):
        """Unblock a sender and release its only pending decoded frame."""
        super().stop()
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(None)
        self.current = None
