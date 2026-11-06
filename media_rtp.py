"""Per-peer, version-pinned aiortc adapter for actual sent RTP provenance."""

from __future__ import annotations

import inspect
import struct
from collections import deque
from importlib.metadata import version

from aiortc import RTCRtpSender
from aiortc.codecs.vpx import Vp8Encoder


class BrowserVp8Encoder(Vp8Encoder):
    """Keep the verified text-quality bitrate local to this encoder instance."""

    def __init__(self):
        """Initialize software inter-frame encoding without changing global defaults."""
        super().__init__()
        self.target_bitrate = 4_000_000

    @Vp8Encoder.target_bitrate.setter
    def target_bitrate(self, bitrate):
        """Apply receiver feedback within this browser stream's quality envelope.

        Args:
            bitrate: Receiver estimate in bits per second.
        """
        self._Vp8Encoder__target_bitrate = max(500_000, min(int(bitrate), 4_000_000))


class RtpFrameAdapter:
    """Bind one sender's immutable encoded source to its actual random RTP clock.

    aiortc has no public sent-frame metadata hook. This adapter changes only its
    owned sender and DTLS transport, rejects unknown versions/interfaces, and is
    removed after that peer closes. No codec globals or payload numbers change.
    """

    def __init__(self, sender, track, publish, fail):
        """Install the two local hooks before starting this owned sender.

        Args:
            sender: Newly created, single-video peer sender.
            track: Its private track with immutable current-source metadata.
            publish: Non-blocking callback for a completed sent frame.
            fail: Non-blocking callback that freezes/closes this media viewer.

        Raises:
            RuntimeError: The pinned adapter contract is unavailable.
        """
        if (
            version("aiortc") != "1.14.0"
            or tuple(inspect.signature(RTCRtpSender._next_encoded_frame).parameters)
            != ("self", "codec")
            or not hasattr(sender, "_RTCRtpSender__encoder")
            or not callable(getattr(sender.transport, "_send_rtp", None))
        ):
            raise RuntimeError(
                "The sent-RTP adapter requires the verified aiortc 1.14.0 interface."
            )
        self.sender = sender
        self.track = track
        self.publish = publish
        self.fail = fail
        self.original_next = sender._next_encoded_frame
        self.original_send = sender.transport._send_rtp
        self.pending = None
        self.origin = None
        self.payload_type = None
        self.seen = deque(maxlen=128)
        self.sequence = 0
        sender._RTCRtpSender__encoder = BrowserVp8Encoder()
        sender._next_encoded_frame = self.next_frame
        sender.transport._send_rtp = self.send_rtp

    async def next_frame(self, codec):
        """Retain source ownership across the real off-thread encoder call.

        Args:
            codec: Actually negotiated RTP codec, including its dynamic payload type.

        Returns:
            The unchanged native encoded frame.

        Raises:
            RuntimeError: An unexpected codec or missing source cannot be mapped.
        """
        if codec.mimeType.lower() != "video/vp8":
            raise RuntimeError("The browser stream requires negotiated VP8.")
        self.payload_type = codec.payloadType
        encoded = await self.original_next(codec)
        if encoded is not None:
            if self.track.current is None:
                raise RuntimeError("Encoded video has no immutable source record.")
            self.pending = (encoded.timestamp, self.track.current)
        return encoded

    async def send_rtp(self, data):
        """Publish only after the matching frame's final real RTP packet is sent.

        Args:
            data: Actual RTP or RTCP packet from this owned aiortc transport.
        """
        await self.original_send(data)
        if (
            len(data) < 12
            or data[0] >> 6 != 2
            or 192 <= data[1] <= 223  # RTCP packet types, not negotiated RTP.
            or (data[1] & 127) != self.payload_type
            or struct.unpack_from("!I", data, 8)[0] != self.sender._ssrc
            or self.pending is None
        ):
            return
        stamp = struct.unpack_from("!I", data, 4)[0]
        encoded_timestamp, source = self.pending
        if self.origin is None:
            self.origin = (stamp - encoded_timestamp) & 0xFFFFFFFF
        if stamp != (self.origin + encoded_timestamp) & 0xFFFFFFFF or not data[1] & 128:
            return
        self.pending = None
        if stamp in self.seen:
            # A normal 32-bit wrap remains unambiguous in the bounded window.
            # A repeated retained timestamp cannot identify new pixels safely.
            self.fail("ambiguous_rtp_timestamp")
            return
        self.seen.append(stamp)
        self.sequence += 1
        self.publish(
            {**source, "rtp_timestamp": stamp, "media_sequence": self.sequence}
        )

    def restore(self):
        """Remove only this peer's hooks after its sender has stopped."""
        self.sender._next_encoded_frame = self.original_next
        self.sender.transport._send_rtp = self.original_send
        self.pending = None
