"""Run a disposable, loopback-only real Chromium/WebRTC feasibility fixture."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import json
import os
import struct
import sys
import time
from collections import deque
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
import av
import psutil
from aiohttp import web
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
    VideoStreamTrack,
)
from aiortc.codecs import vpx
from browser_use import BrowserSession
from cdp_use import CDPClient

from observer import BrowserObserver


async def main():
    """Exercise real capture, software encoding, transport and ordered input.

    Raises:
        TimeoutError: The bounded fixture lifetime expires.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--mode", choices=["jpeg", "video"], required=True)
    parser.add_argument("--lifetime", type=float, default=180)
    parser.add_argument("--bitrate", type=int, default=4000000)
    parser.add_argument("--every-nth-frame", type=int, default=1)
    parser.add_argument("--fps", type=float, default=30)
    parser.add_argument("--start-gate", type=Path)
    parser.add_argument("--fault-mode", choices=["pre-ready", "post-ready"])
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    vpx.DEFAULT_BITRATE = args.bitrate
    vpx.MAX_BITRATE = args.bitrate
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
    os.environ["ASTRBOT_BROWSER_BRIDGE_TOKEN"] = uuid4().hex
    os.environ["ASTRBOT_BROWSER_BRIDGE_FILE"] = str(args.out / "bridge.json")
    token = uuid4().hex
    origin = round(time.time() * 1000)
    start = time.monotonic()
    metrics = {
        "mode": args.mode,
        "size": [args.width, args.height],
        "origin": origin,
        "capture": [],
        "decode": [],
        "encode": [],
        "send": [],
        "resources": [],
        "input": [],
        "dropped": 0,
        "queue_max": 0,
        "ice_servers": [],
        "software": "libvpx VP8",
        "bitrate": args.bitrate,
        "every_nth_frame": args.every_nth_frame,
        "fps_limit": args.fps,
        "probe_version": "v01-interframes-rendered-state-2",
    }
    browser = BrowserSession(
        executable_path=args.chromium,
        headless=True,
        user_data_dir=args.out / "profile",
        enable_default_extensions=False,
        viewport={"width": args.width, "height": args.height},
        args=["--no-proxy-server", "--disable-background-networking"],
    )
    observer = BrowserObserver(SimpleNamespace(browser_session=browser))
    observer.owner = "video-probe"
    stopped = asyncio.Event()
    peers, sockets, tasks = [], [], []
    latest = asyncio.Queue(maxsize=1)
    acknowledgements = asyncio.Queue(maxsize=8)
    encodings = deque()
    sent_meta = deque()
    client = None
    capture_session = None
    video_sender = None
    baseline_capture = observer.capture_snapshot

    async def measured_capture(*params):
        """Record the unchanged JPEG baseline capture duration.

        Args:
            params: Existing observer snapshot arguments.

        Returns:
            The original snapshot without changing metadata validation.
        """
        before = time.monotonic()
        result = await baseline_capture(*params)
        metrics["capture"].append(
            {
                "t": time.monotonic() - start,
                "ms": (time.monotonic() - before) * 1000,
                "bytes": len(result["image"]) * 3 // 4,
            }
        )
        return result

    observer.capture_snapshot = measured_capture
    original_encode = vpx.Vp8Encoder.encode

    async def acknowledge_frames():
        """Apply screencast backpressure instead of capturing unused JPEGs."""
        while True:
            source, session_id, frame_id = await acknowledgements.get()
            before = time.monotonic()
            await source.send.Page.screencastFrameAck(
                params={"sessionId": frame_id}, session_id=session_id
            )
            await asyncio.sleep(max(0, 1 / args.fps - (time.monotonic() - before)))

    def measured_encode(encoder, frame, force_keyframe=False):
        """Time the real encoder on its existing executor thread.

        Args:
            encoder: aiortc software VP8 encoder.
            frame: Decoded screencast frame.
            force_keyframe: Receiver keyframe request.

        Returns:
            The unchanged packet payloads and timestamp.
        """
        before = time.monotonic()
        picture_type = int(frame.pict_type)
        result = original_encode(encoder, frame, force_keyframe)
        record = {
            "t": time.monotonic() - start,
            "pts": frame.pts,
            "ms": (time.monotonic() - before) * 1000,
            "bytes": sum(map(len, result[0])),
            "input_picture_type": picture_type,
            "force_keyframe": force_keyframe,
            "keyframe": bool(result[0])
            and not (vpx.VpxPayloadDescriptor.parse(result[0][0])[1][0] & 1),
        }
        metrics["encode"].append(record)
        if result[0]:
            encodings.append(record)
        return result

    vpx.Vp8Encoder.encode = measured_encode

    class CastTrack(VideoStreamTrack):
        """Keep one pending JPEG; decoding and VP8 run off the input loop."""

        last_delivery = 0.0

        async def recv(self):
            """Decode the most recent captured image without replaying a backlog.

            Returns:
                A timestamped real Chromium video frame.
            """
            await asyncio.sleep(
                max(0, 1 / args.fps - (time.monotonic() - self.last_delivery))
            )
            while True:
                event, expected, received = await latest.get()
                _, _, state = await observer.read_state()
                if any(
                    state[key] != expected[key]
                    for key in (
                        "generation",
                        "browser_session_id",
                        "tab_id",
                        "document_id",
                        "viewport_width",
                        "viewport_height",
                        "control_epoch",
                    )
                ):
                    metrics["identity_changed"] = True
                    stopped.set()
                    raise RuntimeError("The fixed-document probe identity changed.")
                if (
                    state["scroll_x"],
                    state["scroll_y"],
                    state["viewport_width"],
                    state["viewport_height"],
                ) == (
                    event["metadata"]["scrollOffsetX"],
                    event["metadata"]["scrollOffsetY"],
                    event["metadata"]["deviceWidth"],
                    event["metadata"]["deviceHeight"],
                ):
                    break
                metrics["dropped"] += 1
            before = time.monotonic()

            def decode():
                """Decode a single independent JPEG using FFmpeg.

                Returns:
                    The decoded source image.
                """
                container = av.open(
                    io.BytesIO(base64.b64decode(event["data"])), format="mjpeg"
                )
                try:
                    frame = next(container.decode(video=0))
                    # JPEG is intra-only. Do not force every independent decoded
                    # source image to become an intra frame in the video encoder.
                    # aiortc can still set I when RTCP requests a recovery frame.
                    frame.pict_type = av.video.frame.PictureType.NONE
                    return frame
                finally:
                    container.close()

            frame = await asyncio.to_thread(decode)
            frame.pts = round((received - start) * 90000)
            frame.time_base = Fraction(1, 90000)
            metrics["decode"].append(
                {
                    "t": time.monotonic() - start,
                    "pts": frame.pts,
                    "ms": (time.monotonic() - before) * 1000,
                    "queue_ms": (before - received) * 1000,
                }
            )
            sent_meta.append(
                {
                    "pts": frame.pts,
                    "state": state,
                    "capture": event["metadata"],
                    "received": received - start,
                }
            )
            self.last_delivery = time.monotonic()
            return frame

    async def resources():
        """Sample this runtime and its owned browser processes once per second."""
        known = {}
        while True:
            rows = []
            for proc in [psutil.Process(), *psutil.Process().children(recursive=True)]:
                with contextlib.suppress(psutil.Error):
                    current = known.setdefault(proc.pid, proc)
                    rows.append(
                        {
                            "pid": proc.pid,
                            "cpu": current.cpu_percent(),
                            "rss": current.memory_info().rss,
                            "name": current.name(),
                        }
                    )
            metrics["resources"].append(
                {"t": time.monotonic() - start, "processes": rows}
            )
            await asyncio.sleep(1)

    async def authorize(request):
        """Reject requests without the ephemeral local fixture credential.

        Args:
            request: Loopback fixture request.

        Raises:
            HTTPUnauthorized: The caller lacks the probe token.
        """
        if request.query.get("token") != token:
            raise web.HTTPUnauthorized()

    async def target(request):
        """Serve the deterministic local target page.

        Args:
            request: Local navigation request.

        Returns:
            HTML fixture with an optical timing stamp.
        """
        return web.FileResponse(Path(__file__).with_name("video_probe_target.html"))

    async def viewer(request):
        """Serve the disposable real browser receiver.

        Args:
            request: Token-bearing viewer request.

        Returns:
            The video or JPEG receiver HTML.
        """
        await authorize(request)
        return web.FileResponse(Path(__file__).with_name("video_probe_viewer.html"))

    async def control(request):
        """Proxy ordered input through the unchanged private observer lease.

        Args:
            request: Authenticated local WebSocket handshake.

        Returns:
            The closed input socket.
        """
        await authorize(request)
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        sockets.append(socket)
        identity = {
            "owner": observer.owner,
            "client_id": uuid4().hex,
            "generation": observer.generation,
            "browser_session_id": browser.id,
        }
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + observer.token}
        ) as http:
            bridge = json.loads(observer.discovery.read_text())
            url = f"http://127.0.0.1:{bridge['port']}/manual"
            async with http.post(
                url, json={**identity, "operation": "acquire"}
            ) as response:
                ack = await response.json()
                epoch = ack["control_epoch"]
                await socket.send_json(ack)
            try:
                async for message in socket:
                    body = json.loads(message.data)
                    before = time.monotonic()
                    # Instrument only the local fixture; production input is untouched.
                    _, cdp, _ = await observer.read_state()
                    await cdp.cdp_client.send.Runtime.evaluate(
                        params={"expression": f"probe.pending={int(body['input_id'])}"},
                        session_id=cdp.session_id,
                    )
                    async with http.post(url, json={**identity, **body}) as response:
                        if response.status != 200:
                            await socket.send_json(
                                {"status": "error", "message": await response.text()}
                            )
                            break
                        ack = await response.json()
                        metrics["input"].append(
                            {
                                "t": time.monotonic() - start,
                                "id": body["input_id"],
                                "status": ack.get("status"),
                                "ms": (time.monotonic() - before) * 1000,
                            }
                        )
                        await socket.send_json(ack)
            finally:
                async with http.post(
                    url,
                    json={
                        **identity,
                        "operation": "disconnect",
                        "control_epoch": epoch,
                    },
                ):
                    pass
                sockets.remove(socket)
        return socket

    async def stream(request):
        """Relay the real baseline SSE bytes without modifying its producer.

        Args:
            request: Token-bearing EventSource request.

        Returns:
            Existing observer stream.
        """
        await authorize(request)
        bridge = json.loads(observer.discovery.read_text())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + observer.token}
        ) as http:
            async with http.get(
                f"http://127.0.0.1:{bridge['port']}/stream",
                params={
                    "generation": observer.generation,
                    "browser_session_id": browser.id,
                },
            ) as upstream:
                with contextlib.suppress(ConnectionError):
                    async for chunk in upstream.content.iter_any():
                        await response.write(chunk)
        return response

    async def offer(request):
        """Create one host-only WebRTC transport; never use public ICE services.

        Args:
            request: Token-bearing local SDP offer.

        Returns:
            SDP answer with host candidates only.

        Raises:
            HTTPConflict: This V01 fixture already has a viewer.
        """
        nonlocal client, capture_session, video_sender
        await authorize(request)
        if peers:
            raise web.HTTPConflict(text="V01 supports one viewer.")
        body = await request.json()
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peers.append(peer)
        sender = peer.addTrack(CastTrack())
        video_sender = sender
        transceiver = peer.getTransceivers()[0]
        transceiver.setCodecPreferences(
            [
                codec
                for codec in RTCRtpSender.getCapabilities("video").codecs
                if codec.mimeType == "video/VP8"
            ]
        )
        original_send = sender.transport._send_rtp
        last_rtp = None

        async def send_rtp(data):
            """Bind immutable capture identity to the actual randomized RTP clock.

            Args:
                data: Actual RTP/RTCP bytes from aiortc.
            """
            nonlocal last_rtp
            before = time.monotonic()
            await original_send(data)
            if len(data) >= 12 and data[1] & 127 < 64:
                return
            if len(data) >= 12 and data[1] & 127 in (97, 96, 98, 99):
                stamp = struct.unpack_from("!I", data, 4)[0]
                if stamp != last_rtp and encodings and sent_meta:
                    encoded = encodings.popleft()
                    meta = sent_meta.popleft()
                    last_rtp = stamp
                    record = {
                        "kind": "media",
                        "rtp_timestamp": stamp,
                        **meta,
                        "encoded": encoded,
                        "send_ms": (time.monotonic() - before) * 1000,
                        "t": time.monotonic() - start,
                    }
                    metrics["send"].append(record)
                    for socket in sockets:
                        await socket.send_json(record)
                if data[1] & 128 and metrics["send"]:
                    metrics["send"][-1]["last_packet_t"] = time.monotonic() - start

        sender.transport._send_rtp = send_rtp
        _, _, state = await observer.read_state()
        client = CDPClient(browser.cdp_url)
        await client.start()
        attached = await client.send.Target.attachToTarget(
            params={"targetId": state["tab_id"], "flatten": True}
        )
        capture_session = attached["sessionId"]

        def received(event, session_id):
            """Retain the latest actual screencast image and bounded acknowledgements.

            Args:
                event: Chromium JPEG screencast event.
                session_id: Source CDP session.
            """
            if session_id != capture_session:
                return
            try:
                acknowledgements.put_nowait((client, session_id, event["sessionId"]))
            except asyncio.QueueFull:
                metrics["ack_overflow"] = True
                stopped.set()
            received_at = time.monotonic()
            metrics["capture"].append(
                {
                    "t": received_at - start,
                    "timestamp": event["metadata"].get("timestamp"),
                    "bytes": len(event["data"]) * 3 // 4,
                }
            )
            if latest.full():
                latest.get_nowait()
                metrics["dropped"] += 1
            # V01 is a fixed-document fixture. This binding is deliberately not a
            # production navigation barrier; V02 must validate per-frame identity.
            frame_state = {
                **state,
                "scroll_x": event["metadata"]["scrollOffsetX"],
                "scroll_y": event["metadata"]["scrollOffsetY"],
            }
            latest.put_nowait((event, frame_state, received_at))
            metrics["queue_max"] = max(metrics["queue_max"], latest.qsize())

        client.register.Page.screencastFrame(received)
        tasks.append(asyncio.create_task(acknowledge_frames()))
        await client.send.Page.startScreencast(
            params={
                "format": "jpeg",
                "quality": 90,
                "maxWidth": 1920,
                "maxHeight": 1080,
                "everyNthFrame": args.every_nth_frame,
            },
            session_id=capture_session,
        )
        await peer.setRemoteDescription(RTCSessionDescription(**body))
        await peer.setLocalDescription(await peer.createAnswer())
        connection = sender.transport.transport._connection
        assert connection.stun_server is None and connection.turn_server is None
        metrics["ice_verified"] = {
            "stun_server": connection.stun_server,
            "turn_server": connection.turn_server,
        }
        metrics["answer"] = peer.localDescription.sdp
        return web.json_response(
            {"type": peer.localDescription.type, "sdp": peer.localDescription.sdp}
        )

    async def reference(request):
        """Capture one lossless quality reference outside the timed phase.

        Args:
            request: Authenticated reference request.

        Returns:
            Lossless viewport PNG.
        """
        await authorize(request)
        _, cdp, state = await observer.read_state()
        shot = await cdp.cdp_client.send.Page.captureScreenshot(
            params={
                "format": "png",
                "captureBeyondViewport": False,
                "clip": {
                    "x": state["scroll_x"],
                    "y": state["scroll_y"],
                    "width": state["viewport_width"],
                    "height": state["viewport_height"],
                    "scale": 1,
                },
            },
            session_id=cdp.session_id,
        )
        _, _, after = await observer.read_state()
        metrics["reference_viewport"] = {
            "before": [state["viewport_width"], state["viewport_height"]],
            "after": [after["viewport_width"], after["viewport_height"]],
        }
        assert (
            metrics["reference_viewport"]["before"]
            == metrics["reference_viewport"]["after"]
        )
        return web.Response(
            body=base64.b64decode(shot["data"]), content_type="image/png"
        )

    async def inspect(request):
        """Read target verification state without changing the display.

        Args:
            request: Authenticated inspection request.

        Returns:
            Target event log and current observer identity.
        """
        await authorize(request)
        _, cdp, state = await observer.read_state()
        data = await cdp.cdp_client.send.Runtime.evaluate(
            params={
                "expression": "JSON.stringify({probe,slider:slider.value,drag:[drag.offsetLeft,drag.offsetTop],scroll:scrollY,width:innerWidth,height:innerHeight,rects:Object.fromEntries(['slider','drag'].map(id=>[id,document.getElementById(id).getBoundingClientRect().toJSON()]))})",
                "returnByValue": True,
            },
            session_id=cdp.session_id,
        )
        return web.json_response(
            {"target": json.loads(data["result"]["value"]), "state": state}
        )

    async def shutdown(request):
        """Stop the bounded fixture.

        Args:
            request: Authenticated shutdown request.

        Returns:
            Stop acknowledgement.
        """
        await authorize(request)
        stopped.set()
        return web.json_response({"stopped": True})

    async def recovery(request):
        """Exercise the encoder request used by aiortc's RTCP PLI handler.

        Args:
            request: Authenticated recovery probe request.

        Returns:
            Recovery request acknowledgement; not an injected receiver RTCP PLI.
        """
        await authorize(request)
        video_sender._send_keyframe()
        metrics.setdefault("recovery_requests", []).append(time.monotonic() - start)
        return web.json_response({"requested": True})

    async def fault(request):
        """Abruptly exit this disposable runtime for ownership cleanup testing.

        Args:
            request: Authenticated injected fault request.
        """
        await authorize(request)
        if args.fault_mode != "post-ready":
            raise web.HTTPForbidden()
        (args.out / "fault-fired.json").write_text(
            json.dumps({"phase": "post-ready", "pid": os.getpid()})
        )
        os._exit(87)

    app = web.Application()
    app.router.add_get("/target", target)
    app.router.add_get("/viewer", viewer)
    app.router.add_get("/input", control)
    app.router.add_get("/stream", stream)
    app.router.add_post("/offer", offer)
    app.router.add_get("/inspect", inspect)
    app.router.add_get("/reference", reference)
    app.router.add_post("/shutdown", shutdown)
    app.router.add_post("/recovery", recovery)
    app.router.add_post("/fault", fault)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        if args.start_gate:
            deadline = time.monotonic() + 10
            while not args.start_gate.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("The probe startup ownership gate expired.")
                await asyncio.sleep(0.01)
        await browser.start()
        await browser.navigate_to(f"http://127.0.0.1:{port}/target?origin={origin}")
        if args.fault_mode == "pre-ready":
            (args.out / "fault-fired.json").write_text(
                json.dumps({"phase": "pre-ready", "pid": os.getpid()})
            )
            await asyncio.Event().wait()
        await observer.start()
        tasks.append(asyncio.create_task(resources()))
        (args.out / "ready.json").write_text(
            json.dumps(
                {"port": port, "token": token, "origin": origin, "pid": os.getpid()}
            ),
            encoding="utf-8",
        )
        await asyncio.wait_for(stopped.wait(), args.lifetime)
    finally:
        for peer in peers:
            await peer.close()
        if client:
            with contextlib.suppress(Exception):
                await client.send.Page.stopScreencast(session_id=capture_session)
            await client.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await observer.close()
        await browser.kill()
        await runner.cleanup()
        metrics["elapsed"] = time.monotonic() - start
        (args.out / "runtime.json").write_text(
            json.dumps(metrics, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    asyncio.run(main())
