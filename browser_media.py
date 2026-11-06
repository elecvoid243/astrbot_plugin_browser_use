"""Authenticated private media service for the actual owned browser."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import io
import json
import time
from fractions import Fraction
from importlib.metadata import PackageNotFoundError, version
from uuid import uuid4

from aiohttp import web

from browser_capture import BrowserCapture


class BrowserMedia:
    """Share bounded capture/decode across at most four independent video peers."""

    def __init__(self, observer):
        """Detect optional private-runtime dependencies without opening media.

        Args:
            observer: Lifetime owner, issuer of pixel records and input authority.
        """
        self.observer = observer
        self.capability = {"frame_provenance_version": 1, "video_stream_version": 0}
        try:
            if version("aiortc") == "1.14.0" and version("av") == "16.0.1":
                import av

                from media_rtp import RtpFrameAdapter  # noqa: F401
                from media_track import BrowserVideoTrack  # noqa: F401

                av.CodecContext.create("libvpx", "w")
                self.capability["video_stream_version"] = 1
        except (PackageNotFoundError, ImportError, OSError, ValueError):
            pass
        if not self.capability["video_stream_version"]:
            self.capability["video_unavailable_reason"] = "video_runtime_unavailable"
        self.viewers = {}
        self.capture = BrowserCapture(observer)
        self.browser = None
        self.owner = None
        self.producer = None
        self.lock = asyncio.Lock()
        self.started = time.monotonic()
        self.last_pts = -1
        self.last_epoch = -1
        self.metrics = []
        self.latest_frame = None
        self.decoded_frames = 0
        self.decode_ms = 0.0

    def authorize(self, request, body, *, closing=False):
        """Check the activation credential and the host-selected browser lifetime.

        Args:
            request: Protected loopback request, never a public browser endpoint.
            body: Host-selected generation, owner, browser and viewer identifiers.
            closing: Permit cleanup of a previous owned browser's existing peer.

        Returns:
            The existing viewer, if one has been established.

        Raises:
            HTTPUnauthorized: The activation credential is missing or invalid.
            HTTPConflict: Browser ownership or viewer identity has expired.
        """
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), f"Bearer {self.observer.token}"
        ):
            raise web.HTTPUnauthorized()
        viewer = self.viewers.get(body.get("viewer_id"))
        browser = (
            viewer["browser"]
            if closing and viewer
            else self.observer.server.browser_session
        )
        owner = viewer["owner"] if closing and viewer else self.observer.owner
        if (
            self.observer._closed
            or browser is None
            or body.get("generation") != self.observer.generation
            or body.get("browser_session_id") != browser.id
            or body.get("owner") != owner
            or not isinstance(body.get("viewer_id"), str)
            or len(body["viewer_id"]) != 32
        ):
            raise web.HTTPConflict(text="Browser media ownership expired.")
        return viewer

    def accepts(self, target):
        """Verify that a video-issued record still belongs to a live source epoch.

        Args:
            target: Issued displayed-frame target from the ordered input channel.

        Returns:
            Whether the exact peer/source epoch remains admitted.
        """
        return bool(
            self.capture.anchor is not None
            and type(target.get("media_epoch")) is int
            and 0 < target["media_epoch"] <= self.capture.epoch
            and any(
                v["media_session_id"] == target.get("media_session_id")
                and not v["closed"]
                and v["browser"] is self.observer.server.browser_session
                for v in self.viewers.values()
            )
        )

    def emit(self, viewer, event):
        """Queue metadata without allowing a slow receiver to block RTP or input.

        Args:
            viewer: Actual admitted peer state.
            event: Bounded metadata or media state; never an image or credential.
        """
        if viewer["closed"]:
            return
        try:
            viewer["events"].put_nowait(event)
        except asyncio.QueueFull:
            asyncio.create_task(self.drop(viewer["id"], "metadata_backpressure"))

    async def offer(self, request):
        """Accept one host-authorized offer with host-only ICE and negotiated VP8.

        Args:
            request: Private authenticated JSON offer.

        Returns:
            A native answer and this viewer's opaque media identity.

        Raises:
            HTTPConflict: The owned browser or optional adapter is unavailable.
            HTTPTooManyRequests: Four viewers already own media resources.
            HTTPBadRequest: SDP or identity fields are invalid.
        """
        body = await request.json()
        self.authorize(request, body)
        if set(body) - {
            "owner",
            "generation",
            "browser_session_id",
            "viewer_id",
            "sdp",
            "type",
        }:
            raise web.HTTPBadRequest(text="Unsupported media fields.")
        if (
            body.get("type") != "offer"
            or not isinstance(body.get("sdp"), str)
            or len(body["sdp"]) > 131072
        ):
            raise web.HTTPBadRequest(text="Invalid media offer.")
        if not self.capability["video_stream_version"]:
            raise web.HTTPConflict(text="Video runtime unavailable; use JPEG.")
        from aiortc import (
            RTCConfiguration,
            RTCPeerConnection,
            RTCRtpSender,
            RTCSessionDescription,
        )

        from media_rtp import RtpFrameAdapter
        from media_track import BrowserVideoTrack

        async with self.lock:
            if len(self.viewers) >= 4:
                raise web.HTTPTooManyRequests()
            if body["viewer_id"] in self.viewers:
                raise web.HTTPConflict(text="Media viewer already exists.")
            browser = self.observer.server.browser_session
            if self.browser is not None and self.browser is not browser:
                raise web.HTTPConflict(
                    text="Previous browser media is closing; retry explicitly."
                )
            self.browser = browser
            self.owner = body["owner"]
            peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            viewer = {
                "id": body["viewer_id"],
                "browser": browser,
                "owner": self.owner,
                "media_session_id": uuid4().hex,
                "peer": peer,
                "events": asyncio.Queue(maxsize=128),
                "closed": False,
                "subscribed": False,
                "created": time.monotonic(),
                "connected": False,
            }
            track = BrowserVideoTrack(
                lambda epoch: (
                    not viewer["closed"]
                    and self.capture.anchor is not None
                    and epoch == self.capture.epoch
                )
            )
            viewer["track"] = track
            self.viewers[viewer["id"]] = viewer

            def mapped(source):
                """Issue a record only for a complete sent frame of this live epoch.

                Args:
                    source: Real sent-RTP timestamp and immutable encoded source.
                """
                if (
                    viewer["closed"]
                    or self.capture.anchor is None
                    or source["media_epoch"] != self.capture.epoch
                    or self.observer.server.browser_session is not viewer["browser"]
                ):
                    return
                record = self.observer.frame_records.add(
                    source["state"],
                    media_session_id=viewer["media_session_id"],
                    media_epoch=source["media_epoch"],
                    rtp_timestamp=source["rtp_timestamp"],
                    capture_sequence=source["capture_sequence"],
                    media_sequence=source["media_sequence"],
                    width=source["width"],
                    height=source["height"],
                    mime_type="video/VP8",
                )
                self.emit(viewer, {"type": "frame", **record})
                if source["media_sequence"] % 60 == 0:
                    adapter = viewer["adapter"]
                    encoder = viewer["sender"]._RTCRtpSender__encoder
                    self.emit(
                        viewer,
                        {
                            "type": "media-stats",
                            "media_session_id": viewer["media_session_id"],
                            "elapsed_s": time.monotonic() - viewer["created"],
                            "encoder": dict(encoder.metrics),
                            "sent_bytes": adapter.sent_bytes,
                            "track_replaced": track.replaced,
                            "track_stale": track.stale,
                            "track_high_water": track.high_water,
                            "metadata_queue": viewer["events"].qsize(),
                            "shared_source": {
                                "decoded_frames": self.decoded_frames,
                                "decode_ms": self.decode_ms,
                                "capture_sequence": self.capture.sequence,
                                "capture_pending": self.capture.pending.qsize(),
                                "ack_pending": self.capture.acks.qsize(),
                                "viewers": len(self.viewers),
                            },
                        },
                    )

            try:
                sender = peer.addTrack(track)
                viewer["sender"] = sender
                peer.getTransceivers()[0].setCodecPreferences(
                    [
                        c
                        for c in RTCRtpSender.getCapabilities("video").codecs
                        if c.mimeType.lower() == "video/vp8"
                    ]
                )
                viewer["adapter"] = RtpFrameAdapter(
                    sender,
                    track,
                    mapped,
                    lambda reason: asyncio.create_task(self.drop(viewer["id"], reason)),
                )

                @peer.on("connectionstatechange")
                async def changed():
                    """Retire a failed peer without changing the human input lease."""
                    if peer.connectionState == "failed":
                        await self.drop(viewer["id"], "peer_failed")
                    elif peer.connectionState == "connected":
                        viewer["connected"] = True

                await asyncio.wait_for(
                    peer.setRemoteDescription(
                        RTCSessionDescription(sdp=body["sdp"], type="offer")
                    ),
                    5,
                )
                answer = await peer.createAnswer()
                await asyncio.wait_for(peer.setLocalDescription(answer), 5)
                if self.observer.server.browser_session is not browser:
                    raise web.HTTPConflict(
                        text="Browser changed during media negotiation."
                    )
                if self.producer is None:
                    self.producer = asyncio.create_task(self.produce())
                elif self.latest_frame is not None:
                    # A static page may not emit another cast for a new viewer.
                    # Its independent track still needs the last captured source.
                    track.push(*self.latest_frame)
                return web.json_response(
                    {
                        "type": "answer",
                        "sdp": peer.localDescription.sdp,
                        "media_session_id": viewer["media_session_id"],
                        "frame_metadata_version": 1,
                    },
                    headers={"Cache-Control": "no-store"},
                )
            except BaseException:
                self.viewers.pop(viewer["id"], None)
                viewer["closed"] = True
                track.stop()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(peer.close(), 3)
                if viewer.get("adapter"):
                    viewer["adapter"].restore()
                if not self.viewers:
                    self.browser = None
                raise

    async def events(self, request):
        """Stream actual sent-frame records to the host's authorized media socket.

        Args:
            request: Private host subscription with the admitted viewer identity.

        Returns:
            Bounded SSE records; cancellation retires only this viewer.

        Raises:
            HTTPConflict: The subscription is missing, duplicated or expired.
        """
        viewer = self.authorize(request, request.query)
        if viewer is None or viewer["subscribed"] or viewer["closed"]:
            raise web.HTTPConflict(text="Media subscription expired.")
        viewer["subscribed"] = True
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        try:
            while True:
                if (
                    self.observer.server.browser_session is not viewer["browser"]
                    or self.observer.owner != viewer["owner"]
                    or self.observer._closed
                ):
                    await asyncio.wait_for(
                        response.write(b'data: {"type":"revoked"}\n\n'), 2
                    )
                    break
                try:
                    event = await asyncio.wait_for(viewer["events"].get(), 1)
                    if event is None:
                        break
                    data = (
                        f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
                    )
                    if len(data) > 65536:
                        break
                except asyncio.TimeoutError:
                    data = b": hb\n\n"
                await asyncio.wait_for(response.write(data), 2)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            await self.drop(viewer["id"], "viewer_disconnected")
        return response

    async def disconnect(self, request):
        """Retire only the host-authorized viewer's media resources.

        Args:
            request: Private host cleanup request for a previously admitted peer.

        Returns:
            Acknowledgement without changing browser or input ownership.
        """
        body = await request.json()
        self.authorize(request, body, closing=True)
        await self.drop(body["viewer_id"], "viewer_disconnected")
        return web.json_response({"closed": True})

    async def drop(self, viewer_id, reason):
        """Release a peer, and stop capture/decode when the final viewer leaves.

        Args:
            viewer_id: Actual admitted host connection ID.
            reason: Bounded media-only lifecycle reason.
        """
        async with self.lock:
            viewer = self.viewers.pop(viewer_id, None)
            if viewer is None:
                return
            viewer["closed"] = True
            self.observer.frame_records.forget_media(viewer["media_session_id"])
            viewer["track"].stop()
            # Preserve a terminal event even when a slow viewer filled its queue.
            while viewer["events"].qsize() > 126:
                viewer["events"].get_nowait()
            viewer["events"].put_nowait({"type": "media-error", "reason": reason})
            viewer["events"].put_nowait(None)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(viewer["peer"].close(), 3)
            if viewer.get("adapter"):
                viewer["adapter"].restore()
            if not self.viewers:
                task, self.producer = self.producer, None
                if task and task is not asyncio.current_task():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await self.capture.close()
                self.latest_frame = None
                self.browser = None

    async def produce(self):
        """Capture once, decode off-thread, and fan out one pending frame per peer.

        Capture identity changes rebuild only the private capture epoch, never the
        PeerConnections, control socket or human lease. An applied-input watermark
        stays a truthful lower bound during a held drag; final release establishes
        a new painted barrier before confirming its visible feedback. Issued
        records still require a full authoritative coordinate match at input.
        """
        import av

        previous = None
        watermark = -1
        watermark_changed = 0.0
        try:
            while self.viewers:
                for viewer in list(self.viewers.values()):
                    age = time.monotonic() - viewer["created"]
                    if (not viewer["subscribed"] and age > 10) or (
                        not viewer["connected"] and age > 15
                    ):
                        asyncio.create_task(
                            self.drop(viewer["id"], "negotiation_expired")
                        )
                if (
                    self.browser is not self.observer.server.browser_session
                    or self.owner != self.observer.owner
                ):
                    for viewer in list(self.viewers.values()):
                        self.emit(viewer, {"type": "revoked"})
                    break
                try:
                    _, _, state = await asyncio.wait_for(self.observer.read_state(), 3)
                    anchor = self.capture.anchor
                    if not state.get("coordinate_ready"):
                        if anchor is not None:
                            await self.capture.close()
                            self.latest_frame = None
                            for viewer in list(self.viewers.values()):
                                self.emit(
                                    viewer,
                                    {
                                        "type": "epoch",
                                        "media_epoch": self.capture.epoch,
                                        "ready": False,
                                    },
                                )
                        # Unknown geometry freezes once without repeatedly
                        # opening capture or encoding the retained picture.
                        await asyncio.sleep(0.1)
                        continue
                    if watermark != state.get("applied_input_id"):
                        watermark = state.get("applied_input_id")
                        watermark_changed = time.monotonic()
                    typing = ((self.observer.human or {}).get("last_ack") or {}).get(
                        "operation"
                    ) in {"text", "key"}
                    changed = (
                        anchor is None
                        or self.capture.identity(anchor) != self.capture.identity(state)
                        or (
                            self.observer._held_mouse is None
                            and not self.observer._held_keys
                            and anchor.get("applied_input_id")
                            != state.get("applied_input_id")
                            # Keep a truthful lower watermark while text is still
                            # arriving. A painted-feedback barrier after each
                            # character would interrupt the next native input.
                            and (
                                not typing
                                or time.monotonic() - watermark_changed >= 0.25
                            )
                        )
                    )
                    if changed:
                        self.latest_frame = None
                        for viewer in list(self.viewers.values()):
                            self.emit(
                                viewer,
                                {
                                    "type": "epoch",
                                    "media_epoch": self.capture.epoch + 1,
                                    "ready": False,
                                },
                            )
                        await asyncio.wait_for(self.capture.barrier(), 8)
                        previous = None
                        for viewer in list(self.viewers.values()):
                            viewer["sender"]._send_keyframe()
                            self.emit(
                                viewer,
                                {
                                    "type": "epoch",
                                    "media_epoch": self.capture.epoch,
                                    "ready": False,
                                },
                            )
                    record = await self.capture.next_frame(timeout=0.5)
                    if previous == (record["event"]["data"], record["media_epoch"]):
                        continue
                    previous = (record["event"]["data"], record["media_epoch"])
                    started = time.monotonic()

                    def decode():
                        """Decode only this independent JPEG on the worker thread.

                        Returns:
                            Native source pixels with no inherited intra-frame flag.
                        """
                        with av.open(
                            io.BytesIO(base64.b64decode(record["event"]["data"])),
                            format="mjpeg",
                        ) as container:
                            frame = next(container.decode(video=0))
                            frame.pict_type = av.video.frame.PictureType.NONE
                            return frame

                    frame = await asyncio.to_thread(decode)
                    self.decoded_frames += 1
                    self.decode_ms += (time.monotonic() - started) * 1000
                    if (
                        self.capture.anchor is None
                        or record["media_epoch"] != self.capture.epoch
                        or self.browser is not self.observer.server.browser_session
                        or self.observer.coordinate_guard.dirty
                        != record["state"].get("coordinate_event_revision")
                    ):
                        continue
                    if not 0 < frame.width <= 1920 or not 0 < frame.height <= 1080:
                        raise ValueError(
                            "Native video dimensions exceed the capture budget."
                        )
                    self.last_pts = max(
                        self.last_pts + 1,
                        round((record["received_monotonic"] - self.started) * 90000),
                    )
                    frame.pts = self.last_pts
                    frame.time_base = Fraction(1, 90000)
                    metadata = {
                        "state": dict(record["state"]),
                        "media_epoch": record["media_epoch"],
                        "capture_sequence": record["capture_sequence"],
                    }
                    self.latest_frame = (frame, metadata)
                    for viewer in list(self.viewers.values()):
                        viewer["track"].push(frame, metadata)
                    self.metrics.append(
                        {
                            "decode_ms": (time.monotonic() - started) * 1000,
                            "capture_sequence": record["capture_sequence"],
                            "epoch": record["media_epoch"],
                            "width": frame.width,
                            "height": frame.height,
                        }
                    )
                    self.metrics = self.metrics[-128:]
                except (web.HTTPConflict, asyncio.TimeoutError):
                    # Static pages may not emit another native cast. Unknown
                    # coordinates stay frozen; a dead transport ends media only.
                    _, _, current = await asyncio.wait_for(
                        self.observer.read_state(), 3
                    )
                    if not current.get("available"):
                        break
                    await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception:
            for viewer in list(self.viewers.values()):
                self.emit(
                    viewer, {"type": "media-error", "reason": "capture_unavailable"}
                )
        finally:
            self.latest_frame = None
            await self.capture.close()
            for viewer_id in list(self.viewers):
                asyncio.create_task(self.drop(viewer_id, "capture_stopped"))

    async def close(self):
        """Release every peer and this service's capture connection."""
        await asyncio.gather(
            *(self.drop(key, "browser_closed") for key in list(self.viewers))
        )
        await self.capture.close()
