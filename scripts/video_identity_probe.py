"""Attack a conservative capture barrier using actual private Chromium pixels."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp
from aiohttp import web
from browser_use import BrowserSession
from browser_use.browser.events import SwitchTabEvent
from PIL import Image
from video_capture_barrier_candidate import CaptureBarrierCandidate

from observer import BrowserObserver


def optical(image, viewport_width):
    """Read independently painted identity words from actual captured pixels.

    Args:
        image: Base64 JPEG from the real capture pipeline.
        viewport_width: Current browser CSS viewport width.

    Returns:
        Nine optical identity/coordinate words.
    """
    with Image.open(io.BytesIO(base64.b64decode(image))) as picture:
        scale = picture.width / viewport_width
        picture = picture.convert("L")
        return [
            sum(
                1 << bit
                for bit in range(32)
                if picture.getpixel(
                    (round((bit * 18 + 9) * scale), round((row * 10 + 5) * scale))
                )
                > 128
            )
            for row in range(9)
        ]


async def main():
    """Run a bounded real-source identity campaign before enabling video input.

    Raises:
        AssertionError: A stale input executes or an accepted source is mislabeled.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--start-gate", type=Path)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--skip-transition-wait", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
    os.environ["ASTRBOT_BROWSER_BRIDGE_TOKEN"] = uuid4().hex
    os.environ["ASTRBOT_BROWSER_BRIDGE_FILE"] = str(args.out / "bridge.json")
    browser = BrowserSession(
        executable_path=args.chromium,
        headless=True,
        user_data_dir=args.out / "profile",
        enable_default_extensions=False,
        viewport={"width": 1280, "height": 720},
        args=["--no-proxy-server", "--disable-background-networking"],
    )
    observer = BrowserObserver(SimpleNamespace(browser_session=browser))
    observer.owner = "identity-fixture"
    capture = CaptureBarrierCandidate(
        observer, wait_for_transition=not args.skip_transition_wait
    )
    result = {
        "candidate": "independent CDP connection + two rAF paint opportunities + existing validated snapshot + post-barrier timestamp + immutable epoch/state",
        "cases": [],
        "violations": [],
        "wait_for_transition": not args.skip_transition_wait,
    }
    serial = 0

    async def page(request):
        """Serve a new real document token for each navigation or reload.

        Args:
            request: Loopback fixture navigation.

        Returns:
            Actual HTML whose token is painted independently of backend metadata.
        """
        nonlocal serial
        serial += 1
        html = (
            Path(__file__)
            .with_name("video_identity_fixture.html")
            .read_text(encoding="utf-8")
            .replace("__DOC__", str(serial))
        )
        if request.query.get("transition") == "1":
            html = html.replace(
                "</style>",
                "@view-transition{navigation:auto}::view-transition-old(root){animation:hold-old 3s linear forwards;mix-blend-mode:normal}::view-transition-new(root){animation:none;opacity:0}@keyframes hold-old{from{opacity:1;transform:translateX(0)}to{opacity:1;transform:translateX(1px)}}</style>",
            )
        return web.Response(
            text=html,
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def embedded(request):
        """Serve an independent origin with independently scrollable hit targets.

        Args:
            request: Local cross-origin iframe request.

        Returns:
            A real child document whose clicks and scrolls are observable by fixture messages.
        """
        return web.Response(
            content_type="text/html",
            text="""<!doctype html><style>body{margin:0;height:2400px;background:#fff}button{position:absolute;left:80px;width:180px;height:40px}canvas{position:fixed;left:0;top:0;z-index:2}</style><canvas id="stamp" width="576" height="10"></canvas><button style="top:80px" onclick="parent.postMessage({clicked:'A'},'*')">A before scroll</button><button style="top:200px" onclick="parent.postMessage({clicked:'B'},'*')">B after scroll</button><script>onmessage=e=>{if(e.data.scroll!==undefined){scrollTo(0,e.data.scroll);requestAnimationFrame(()=>requestAnimationFrame(()=>parent.postMessage({scrolled:scrollY},'*')))}};const ctx=stamp.getContext('2d');function frame(){for(let bit=0;bit<32;bit++){ctx.fillStyle=(scrollY>>>bit)&1?'#fff':'#000';ctx.fillRect(bit*18,0,18,10)}requestAnimationFrame(frame)}frame()</script>""",
        )

    child_app = web.Application()
    child_app.router.add_get("/child", embedded)
    child_runner = web.AppRunner(child_app, access_log=None)
    await child_runner.setup()
    child_site = web.TCPSite(child_runner, "127.0.0.1", 0)
    await child_site.start()
    child_port = child_site._server.sockets[0].getsockname()[1]
    app = web.Application()
    app.router.add_get("/{name}", page)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        if args.start_gate:
            deadline = time.monotonic() + 10
            while not args.start_gate.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("Startup gate timed out.")
                await asyncio.sleep(0.01)
        await browser.start()
        await browser.navigate_to(base + "/a")
        await observer.start()
        bridge = json.loads(observer.discovery.read_text())
        identity = {
            "owner": observer.owner,
            "client_id": uuid4().hex,
            "generation": observer.generation,
            "browser_session_id": browser.id,
        }
        version = await browser.cdp_client.send.Browser.getVersion()
        result["browser"] = version
        manual_url = f"http://127.0.0.1:{bridge['port']}/manual"
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + observer.token}
        ) as http:
            async with http.post(
                manual_url, json={**identity, "operation": "acquire"}
            ) as response:
                lease = await response.json()
            epoch = lease["control_epoch"]
            input_id = 0
            await capture.barrier()
            old = await capture.next_frame()
            for round_id in range(args.rounds):
                for scenario in (
                    "delayed-old-frame",
                    "same-url-reload",
                    "fast-navigation",
                    "cross-document-transition",
                    "tab-switch",
                    "viewport-change",
                    "nested-scroll",
                    "control-epoch",
                    "cross-origin-frame-scroll",
                ):
                    _, cdp, before = await observer.read_state()
                    original_target = browser.agent_focus_target_id
                    if scenario == "same-url-reload":
                        await cdp.cdp_client.send.Page.reload(session_id=cdp.session_id)
                        deadline = time.monotonic() + 5
                        while True:
                            _, cdp, now = await observer.read_state()
                            if now["document_id"] != before["document_id"]:
                                break
                            if time.monotonic() > deadline:
                                raise TimeoutError("Reload did not commit.")
                            await asyncio.sleep(0.01)
                    elif scenario == "fast-navigation":
                        await cdp.cdp_client.send.Page.navigate(
                            params={"url": base + "/b"}, session_id=cdp.session_id
                        )
                        await cdp.cdp_client.send.Page.navigate(
                            params={"url": base + "/a"}, session_id=cdp.session_id
                        )
                        await asyncio.sleep(0.05)
                    elif scenario == "tab-switch":
                        await browser.navigate_to(base + "/tab", new_tab=True)
                    elif scenario == "cross-document-transition":
                        await browser.navigate_to(base + "/transition-a?transition=1")
                        _, cdp, transition_before = await observer.read_state()
                        await cdp.cdp_client.send.Runtime.evaluate(
                            params={
                                "expression": f"location.href={json.dumps(base + '/transition-b?transition=1')}"
                            },
                            session_id=cdp.session_id,
                        )
                        deadline = time.monotonic() + 5
                        while True:
                            _, cdp, now = await observer.read_state()
                            if now["document_id"] != transition_before["document_id"]:
                                break
                            if time.monotonic() > deadline:
                                raise TimeoutError(
                                    "View transition navigation did not commit."
                                )
                            await asyncio.sleep(0.01)
                    elif scenario == "viewport-change":
                        await observer.resize_viewport(
                            browser,
                            cdp,
                            {
                                "width": 1366 if round_id % 2 == 0 else 1280,
                                "height": 768 if round_id % 2 == 0 else 720,
                                "device_scale_factor": 1,
                            },
                        )
                    elif scenario == "nested-scroll":
                        await cdp.cdp_client.send.Runtime.evaluate(
                            params={"expression": "nested.scrollTop+=120"},
                            session_id=cdp.session_id,
                        )
                        await asyncio.sleep(0.05)
                    elif scenario == "control-epoch":
                        async with http.post(
                            manual_url,
                            json={
                                **identity,
                                "operation": "disconnect",
                                "control_epoch": epoch,
                            },
                        ) as response:
                            await response.read()
                        async with http.post(
                            manual_url, json={**identity, "operation": "acquire"}
                        ) as response:
                            lease = await response.json()
                            epoch = lease["control_epoch"]
                        input_id = 0
                        async with http.post(
                            manual_url,
                            json={
                                **identity,
                                "operation": "click",
                                "input_id": 1,
                                "control_epoch": before["control_epoch"],
                                "x": 900,
                                "y": 220,
                                "target": before,
                            },
                        ) as response:
                            assert response.status == 409, response.status
                    if scenario == "cross-origin-frame-scroll":
                        await browser.navigate_to(base + "/iframe-parent")
                        _, cdp, before = await observer.read_state()
                        await cdp.cdp_client.send.Runtime.evaluate(
                            params={
                                "expression": f"window.childEvents=[];addEventListener('message',e=>childEvents.push(e.data));const f=document.createElement('iframe');f.id='child';f.src='http://127.0.0.1:{child_port}/child';f.style='position:absolute;left:700px;top:150px;width:576px;height:400px;border:0';document.body.append(f)"
                            },
                            session_id=cdp.session_id,
                        )
                        await asyncio.sleep(0.1)
                        await capture.barrier()
                        old = await capture.next_frame()
                        _, cdp, before = await observer.read_state()
                        (args.out / "iframe-before.jpg").write_bytes(
                            base64.b64decode(old["event"]["data"])
                        )
                        await cdp.cdp_client.send.Runtime.evaluate(
                            params={
                                "expression": "child.contentWindow.postMessage({scroll:120},'*')"
                            },
                            session_id=cdp.session_id,
                        )
                        deadline = time.monotonic() + 3
                        while True:
                            message = await cdp.cdp_client.send.Runtime.evaluate(
                                params={
                                    "expression": "childEvents.some(e=>e.scrolled===120)",
                                    "returnByValue": True,
                                },
                                session_id=cdp.session_id,
                            )
                            if message["result"].get("value"):
                                break
                            if time.monotonic() > deadline:
                                raise TimeoutError("Child scroll did not complete.")
                            await asyncio.sleep(0.01)
                        _, _, current = await observer.read_state()
                        old_still_matches = capture.matches(old, current)
                        input_id += 1
                        async with http.post(
                            manual_url,
                            json={
                                **identity,
                                "operation": "click",
                                "input_id": input_id,
                                "control_epoch": epoch,
                                "x": 800,
                                "y": 250,
                                "target": old["state"],
                            },
                        ) as response:
                            click_reply = await response.json()
                        await asyncio.sleep(0.05)
                        child_info = await cdp.cdp_client.send.Runtime.evaluate(
                            params={
                                "expression": "JSON.stringify(childEvents)",
                                "returnByValue": True,
                            },
                            session_id=cdp.session_id,
                        )
                        events = json.loads(child_info["result"]["value"])
                        fresh_child = await capture.next_frame()
                        (args.out / "iframe-after.jpg").write_bytes(
                            base64.b64decode(fresh_child["event"]["data"])
                        )
                        iframe_result = {
                            "scenario": scenario,
                            "round": round_id,
                            "old_frame_still_matches": old_still_matches,
                            "same_top_identity": capture.identity(before)
                            == capture.identity(current),
                            "click_status": click_reply.get("status"),
                            "child_events": events,
                            "old_state": old["state"],
                            "current_state": current,
                        }
                        result["cases"].append(iframe_result)
                        if (
                            old_still_matches
                            and click_reply.get("status") == "applied"
                            and any(e.get("clicked") == "B" for e in events)
                        ):
                            result["violations"].append(iframe_result)
                        print(
                            "CASE",
                            round_id,
                            scenario,
                            "old matches",
                            old_still_matches,
                            "click",
                            click_reply.get("status"),
                            events,
                            flush=True,
                        )
                        continue
                    _, cdp, current = await observer.read_state()
                    before_barrier_old_matches = capture.matches(old, current)
                    started = time.monotonic()
                    frozen = False
                    try:
                        snapshot = await capture.barrier()
                    except TimeoutError:
                        if scenario != "cross-document-transition":
                            raise
                        frozen = capture.anchor is None and not capture.matches(
                            old, current
                        )
                        await asyncio.sleep(1.2)
                        snapshot = await capture.barrier()
                    record = {
                        "scenario": scenario,
                        "round": round_id,
                        "barrier_ms": (time.monotonic() - started) * 1000,
                        "old_matched_before_barrier": before_barrier_old_matches,
                        "old_matched_after_barrier": capture.matches(old, current),
                        "media_epoch": capture.epoch,
                        "frozen_during_transition": frozen,
                        "snapshot_state": {
                            k: v for k, v in snapshot.items() if k != "image"
                        },
                        "snapshot_words": optical(
                            snapshot["image"], snapshot["viewport_width"]
                        ),
                        "frames": [],
                    }
                    if scenario == "cross-document-transition":
                        (args.out / f"transition-snapshot-{round_id}.jpg").write_bytes(
                            base64.b64decode(snapshot["image"])
                        )
                    # Replay a real previously accepted frame after the new barrier,
                    # without changing its timestamp, source or original identity.
                    if capture.pending.full():
                        capture.pending.get_nowait()
                    capture.pending.put_nowait(old)
                    for frame_index in range(3):
                        fresh = await capture.next_frame()
                        _, cdp, state = await observer.read_state()
                        info = await cdp.cdp_client.send.Runtime.evaluate(
                            params={
                                "expression": "JSON.stringify({document:identityProbe.document,nested:nested.scrollTop,frames:identityProbe.frames,activeViewTransition:'activeViewTransition' in document,transitionActive:!!document.activeViewTransition,animations:document.getAnimations().map(a=>({pseudo:a.effect?.pseudoElement,playState:a.playState}))})",
                                "returnByValue": True,
                            },
                            session_id=cdp.session_id,
                        )
                        truth = json.loads(info["result"]["value"])
                        words = optical(fresh["event"]["data"], state["viewport_width"])
                        expected = [
                            0x41535452,
                            truth["document"],
                            state["viewport_width"],
                            state["viewport_height"],
                            round(state["scroll_y"]),
                            round(truth["nested"]),
                            state["position_revision"],
                        ]
                        matches = words[:7] == expected
                        frame_result = {
                            "words": words,
                            "expected": expected,
                            "matches": matches,
                            "metadata": fresh["event"]["metadata"],
                            "bound_state": fresh["state"],
                            "runtime_truth": truth,
                        }
                        record["frames"].append(frame_result)
                        if not matches:
                            violation = {
                                "scenario": scenario,
                                "round": round_id,
                                "frame": frame_index,
                                **frame_result,
                            }
                            result["violations"].append(violation)
                            (
                                args.out / f"mismatch-{len(result['violations'])}.jpg"
                            ).write_bytes(base64.b64decode(fresh["event"]["data"]))
                        old = fresh
                    # Existing typed input validation must still reject old target
                    # coordinates when the actual identity changed.
                    if scenario not in {"delayed-old-frame", "control-epoch"}:
                        input_id += 1
                        async with http.post(
                            manual_url,
                            json={
                                **identity,
                                "operation": "click",
                                "input_id": input_id,
                                "control_epoch": epoch,
                                "x": 900,
                                "y": 220,
                                "target": before,
                            },
                        ) as response:
                            reply = await response.json()
                            record["stale_input_status"] = reply.get("status")
                            assert reply.get("status") == "stale", reply
                    result["cases"].append(record)
                    (args.out / "result.json").write_text(
                        json.dumps(result, indent=2), encoding="utf-8"
                    )
                    print(
                        "CASE",
                        round_id,
                        scenario,
                        "pixel matches",
                        sum(f["matches"] for f in record["frames"]),
                        "/3",
                        "old rejected",
                        not record["old_matched_after_barrier"],
                        flush=True,
                    )
                    assert not record["old_matched_after_barrier"]
                    if scenario == "tab-switch":
                        event = browser.event_bus.dispatch(
                            SwitchTabEvent(target_id=original_target)
                        )
                        await event
                        await event.event_result(raise_if_any=True, raise_if_none=False)
                        await capture.barrier()
                        old = await capture.next_frame()
        result["passed"] = not result["violations"]
        assert result["passed"], (
            f"Accepted {len(result['violations'])} frames whose painted identity differs."
        )
    finally:
        await capture.close()
        await observer.close()
        await browser.kill()
        await runner.cleanup()
        await child_runner.cleanup()
        result["rejected_records"] = list(capture.rejected)
        (args.out / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    asyncio.run(main())
