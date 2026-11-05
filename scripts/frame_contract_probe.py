"""Prove frame-tree token rejection, recovery and reliable native input."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
from aiohttp import web
from browser_use import BrowserSession
from PIL import Image
from video_capture_barrier_candidate import CaptureBarrierCandidate

from frame_coordinates import FrameCoordinates
from observer import BrowserObserver

CHILD = r"""<!doctype html><meta charset="utf-8"><style>body{margin:0;height:3000px;background:#fff}canvas{position:fixed;left:0;top:0;z-index:5}button,input,#drag{position:absolute;left:80px;width:180px;height:40px}#nested{position:absolute;left:300px;top:80px;width:180px;height:200px;overflow:auto;background:#ddd}#drag{top:350px;background:#38bdf8;height:60px}::view-transition-old(root),::view-transition-new(root){animation-duration:2.5s}</style><canvas id="stamp" width="576" height="40"></canvas><button style="top:80px" onclick="parent.postMessage({clicked:'A'},'*')">A</button><button style="top:200px" onclick="parent.postMessage({clicked:'B'},'*')">B</button><input id="input" style="top:280px"><div id="drag">Drag</div><div id="nested"><div style="height:1600px">Nested __DOC__</div></div><script>
let dragging=false,moves=0,keys=[];const ctx=stamp.getContext('2d');
const report=(ack=null)=>parent.postMessage({ack,state:{doc:__DOC__,scroll:document.scrollingElement.scrollTop,nested:nested.scrollTop,moves,dragging,keys:[...keys],text:input.value}},'*');
onmessage=e=>{const a=e.data;if(a.clicked){parent.postMessage({innerClicked:a.clicked},'*');return}if(a.scroll!==undefined)scrollTo(0,a.scroll);if(a.nested!==undefined)nested.scrollTop=a.nested;if(a.transition)document.startViewTransition(()=>{document.body.dataset.changed=String(Date.now())});if(a.frame){document.querySelector('#inner')?.remove();const f=document.createElement('iframe');f.id='inner';f.src=a.frame;f.style='position:absolute;left:20px;top:220px;width:220px;height:120px';document.body.append(f)}if(a.removeFrame)document.querySelector('#inner')?.remove();if(a.forge){Object.defineProperty(window,'scrollY',{value:0});window.coordinate_token=a.forge}requestAnimationFrame(()=>requestAnimationFrame(()=>report(a.request_id)))};
drag.onpointerdown=e=>{dragging=true;drag.setPointerCapture(e.pointerId);report()};drag.onpointermove=e=>{if(dragging&&e.buttons){moves++;scrollBy(0,4);report()}};drag.onpointerup=()=>{dragging=false;report()};input.onkeydown=e=>{if(!keys.includes(e.code))keys.push(e.code);report()};input.onkeyup=e=>{keys=keys.filter(k=>k!==e.code);report()};input.oninput=report;
function draw(){const words=[0x41535452,__DOC__,document.scrollingElement.scrollTop,nested.scrollTop];for(let r=0;r<4;r++)for(let b=0;b<32;b++){ctx.fillStyle=(words[r]>>>b)&1?'#fff':'#000';ctx.fillRect(b*18,r*10,18,10)}requestAnimationFrame(draw)}draw();report();</script>"""


async def main():
    """Execute a bounded real Chromium regression matrix for contract version two.

    Raises:
        AssertionError: An actual capture, stale input or recovery violates the contract.
        TimeoutError: A bounded fixture acknowledgement or capture cannot complete.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--start-gate", type=Path)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--frame-contract", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        ANONYMIZED_TELEMETRY="false",
        BROWSER_USE_CLOUD_SYNC="false",
        ASTRBOT_BROWSER_BRIDGE_TOKEN=uuid4().hex,
        ASTRBOT_BROWSER_BRIDGE_FILE=str(args.out / "bridge.json"),
    )
    browser = BrowserSession(
        executable_path=args.chromium,
        headless=True,
        user_data_dir=args.out / "profile",
        enable_default_extensions=False,
        viewport={"width": 1366, "height": 768},
        args=[
            "--site-per-process",
            "--no-proxy-server",
            "--disable-background-networking",
        ],
    )
    guard = FrameCoordinates(browser)
    observer = BrowserObserver(
        SimpleNamespace(browser_session=browser), coordinate_guard=guard
    )
    observer.owner = "frame-contract-fixture"
    capture = CaptureBarrierCandidate(observer)
    report = {"cases": [], "violations": [], "passed": False}
    serial = 0

    async def child(request):
        """Serve a real child document with independently rendered scroll markers.

        Args:
            request: Loopback child request.

        Returns:
            A fresh child document, optionally with a bounded slow navigation.
        """
        nonlocal serial
        if request.query.get("slow"):
            await asyncio.sleep(0.4)
        serial += 1
        if request.query.get("stream"):
            response = web.StreamResponse(
                headers={"Content-Type": "text/html", "Cache-Control": "no-store"}
            )
            await response.prepare(request)
            await response.write(b'<!doctype html><meta charset="utf-8"><body>')
            await asyncio.sleep(0.4)
            await response.write(CHILD.replace("__DOC__", str(serial)).encode())
            await response.write_eof()
            return response
        return web.Response(
            text=CHILD.replace("__DOC__", str(serial)),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def parent(request):
        """Serve the actual top document used for native input.

        Args:
            request: Loopback top navigation.

        Returns:
            A top document with a root control outside the child frame.
        """
        return web.Response(
            content_type="text/html",
            text="""<!doctype html><style>body{margin:0;height:3000px}iframe{position:absolute;left:700px;top:150px;width:576px;height:400px;border:0}#root{position:fixed;left:30px;top:650px;width:140px;height:40px}::view-transition-old(root),::view-transition-new(root){animation-duration:2.5s}</style><button id="root" onclick="clicks++">Root</button><script>window.clicks=0;window.messages=[];onmessage=e=>messages.push(e.data);</script>""",
        )

    app = web.Application()
    app.router.add_get("/", parent)
    app.router.add_get("/child", child)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    child_app = web.Application()
    child_app.router.add_get("/child", child)
    child_runner = web.AppRunner(child_app, access_log=None)
    await child_runner.setup()
    child_site = web.TCPSite(child_runner, "127.0.0.1", 0)
    await child_site.start()
    child_port = child_site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    input_id = 0
    http = None

    async def evaluate(expression):
        """Evaluate fixture-only setup without routing user input around the bridge.

        Args:
            expression: Trusted local test setup/inspection JavaScript.

        Returns:
            The actual top document's by-value result.
        """
        cdp = await browser.get_or_create_cdp_session(
            target_id=browser.agent_focus_target_id, focus=False
        )
        reply = await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": expression, "returnByValue": True},
            session_id=cdp.session_id,
        )
        if reply.get("exceptionDetails"):
            raise RuntimeError(reply["exceptionDetails"])
        return reply["result"].get("value")

    async def ready():
        """Wait within a bounded interval for authoritative child inspection.

        Returns:
            Full ready coordinate metadata.
        """
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            _, _, state = await observer.read_state()
            if state.get("coordinate_ready"):
                return state
            await asyncio.sleep(0.02)
        raise TimeoutError(guard.last_error)

    async def send(operation, target=None, **fields):
        """Send all real actions through the unchanged owned manual HTTP bridge.

        Args:
            operation: Typed manual operation.
            target: Previously captured immutable target.
            **fields: Typed action arguments.

        Returns:
            Actual HTTP status and acknowledgement.
        """
        nonlocal input_id
        input_id += 1
        async with http.post(
            manual_url,
            json={
                **identity,
                "operation": operation,
                "control_epoch": epoch,
                "input_id": input_id,
                **({"target": target} if target is not None else {}),
                **fields,
            },
        ) as response:
            body = await response.text()
            try:
                data = json.loads(body)
            except ValueError:
                data = {"message": body}
            report.setdefault("input", []).append(
                {
                    "id": input_id,
                    "operation": operation,
                    "phase": fields.get("phase"),
                    "status": response.status,
                    "ack": data.get("status"),
                }
            )
            return response.status, data

    async def fresh():
        """Acquire actual current pixels and the exact token bound to them.

        Returns:
            Capture envelope with immutable source metadata.
        """
        await ready()
        await capture.barrier()
        record = await capture.next_frame()
        rect = await evaluate(
            "document.querySelector('#child')?.getBoundingClientRect().toJSON()||null"
        )
        if rect:
            truth = await action({})
            expected = [0x5452, truth["doc"], truth["scroll"], truth["nested"]]
            for kind, encoded in (
                ("screencast", record["event"]["data"]),
                ("snapshot", capture.snapshot["image"]),
            ):
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                    scale = image.width / record["state"]["viewport_width"]
                    image = image.convert("L")
                    words = [
                        sum(
                            1 << bit
                            for bit in range(16)
                            if image.getpixel(
                                (
                                    round((rect["x"] + bit * 18 + 9) * scale),
                                    round((rect["y"] + row * 10 + 5) * scale),
                                )
                            )
                            > 128
                        )
                        for row in range(4)
                    ]
                assert words == expected, (kind, words, expected)
                check = {"source": kind, "words": words, "expected": expected}
                report.setdefault("optical_checks", []).append(check)
        return record

    async def action(payload):
        """Wait for fixture messages that report actual child state changes.

        Args:
            payload: Child fixture instruction, never an identity input.

        Returns:
            Actual fixture state, used only as independent test ground truth.
        """
        payload = {**payload, "request_id": uuid4().hex}
        start = await evaluate("messages.length")
        await evaluate(f"child.contentWindow.postMessage({json.dumps(payload)},'*')")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            events = await evaluate(f"messages.slice({start})")
            states = [
                e["state"]
                for e in events
                if "state" in e and e.get("ack") == payload["request_id"]
            ]
            if states:
                return states[-1]
            await asyncio.sleep(0.01)
        raise TimeoutError("Child fixture did not report state.")

    async def reject_and_recover(name, old):
        """Prove old native targets fail and a newly captured target works.

        Args:
            name: Regression case label.
            old: Previously captured source envelope.
        """
        before = await evaluate("clicks")
        status, reply = await send("click", old["state"], x=80, y=670)
        assert status == 200 and reply.get("status") == "stale", (name, status, reply)
        assert await evaluate("clicks") == before, name
        new = await fresh()
        if "root-scroll-events-delayed" in name:
            message_start = await evaluate("messages.length")
            status, recovered = await send("click", new["state"], x=800, y=250)
            await asyncio.sleep(0.05)
            events = await evaluate(f"messages.slice({message_start})")
            assert (
                status == 200
                and recovered.get("status") == "applied"
                and any(e.get("clicked") == "B" for e in events)
            ), (name, recovered, events)
            assert await evaluate("clicks") == before
        else:
            status, recovered = await send("click", new["state"], x=80, y=670)
            assert (
                status == 200
                and recovered.get("status") == "applied"
                and await evaluate("clicks") == before + 1
            ), (name, status, recovered)
        case = {
            "name": name,
            "old": "stale",
            "new": "applied",
            "old_token": old["state"]["coordinate_token"],
            "new_token": new["state"]["coordinate_token"],
            "frames": guard.last_frames,
            "old_record_matches": capture.matches(old, await ready()),
        }
        assert not case["old_record_matches"]
        if name in {
            "0-oopif-root-scroll-events-delayed",
            "same-document-transition-capture-race",
            "cancelled-identity-read-recovers",
        }:
            for kind, encoded in (
                ("snapshot", capture.snapshot["image"]),
                ("screencast", new["event"]["data"]),
            ):
                (args.out / f"{name}-{kind}.jpg").write_bytes(base64.b64decode(encoded))
        report["cases"].append(case)
        (args.out / "result.json").write_text(json.dumps(report, indent=2))
        print("PASS", name, flush=True)

    try:
        if args.start_gate:
            deadline = time.monotonic() + 10
            while not args.start_gate.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("Startup gate expired.")
                await asyncio.sleep(0.01)
        await browser.start()
        await browser.navigate_to(base + "/")
        await observer.start()
        report["browser"] = await browser.cdp_client.send.Browser.getVersion()
        bridge = json.loads(observer.discovery.read_text())
        manual_url = f"http://127.0.0.1:{bridge['port']}/manual"
        identity = {
            "owner": observer.owner,
            "client_id": uuid4().hex,
            "generation": observer.generation,
            "browser_session_id": browser.id,
        }
        http = aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + observer.token}
        )
        async with http.post(
            manual_url, json={**identity, "operation": "acquire"}
        ) as response:
            epoch = (await response.json())["control_epoch"]
        for iteration in range(args.rounds):
            for label, url in (
                ("same-origin", base + "/child"),
                ("cross-origin", f"http://127.0.0.1:{child_port}/child"),
                ("oopif", f"http://localhost:{child_port}/child"),
            ):
                await evaluate("document.querySelector('#child')?.remove()")
                empty = await fresh()
                await evaluate(
                    f"var f=document.createElement('iframe');f.id='child';f.src={json.dumps(url)};document.body.append(f)"
                )
                await asyncio.sleep(0.08)
                await reject_and_recover(f"{iteration}-{label}-attach", empty)
                for name, payload in (
                    ("root-scroll", {"scroll": 120}),
                    ("nested-scroll", {"nested": 130}),
                ):
                    old = await fresh()
                    guard.deliver_events = False
                    truth = await action(payload)
                    await reject_and_recover(
                        f"{iteration}-{label}-{name}-events-delayed", old
                    )
                    guard.deliver_events = True
                    if name == "root-scroll":
                        assert truth["scroll"] == 120
                    else:
                        assert truth["nested"] == 130
                old = await fresh()
                await evaluate(f"child.src={json.dumps(url + '?navigation=1')}")
                await asyncio.sleep(0.08)
                await reject_and_recover(f"{iteration}-{label}-navigate", old)
                old = await fresh()
                await evaluate(
                    f"var f=child.cloneNode();f.src={json.dumps(url + '?replacement=1')};child.replaceWith(f)"
                )
                await asyncio.sleep(0.08)
                await reject_and_recover(f"{iteration}-{label}-replace", old)
                old = await fresh()
                await evaluate("child.style.left='740px'")
                await reject_and_recover(f"{iteration}-{label}-owner-move", old)
                old = await fresh()
                await evaluate("child.style.width='520px'")
                await reject_and_recover(f"{iteration}-{label}-owner-size", old)
                old = await fresh()
                await evaluate("child.remove()")
                await reject_and_recover(f"{iteration}-{label}-detach", old)
            # Use a real OOPIF for input continuation and key termination.
            url = f"http://localhost:{child_port}/child"
            await evaluate(
                f"var f=document.createElement('iframe');f.id='child';f.src={json.dumps(url)};document.body.append(f)"
            )
            await asyncio.sleep(0.08)
            source = await fresh()
            frames = list(guard.last_frames)
            remote = [f for f in frames if f["session"] != guard.root_session]
            assert remote, "No actual OOPIF was attached."
            info = await browser.cdp_client.send.Target.getTargetInfo(
                params={"targetId": remote[0]["frame"]}
            )
            assert info["targetInfo"]["type"] == "iframe"
            report["oopif_target"] = info["targetInfo"]
            old = await fresh()
            await action({"frame": f"http://127.0.0.1:{child_port}/child?inner=1"})
            await asyncio.sleep(0.08)
            await reject_and_recover("nested-oopif-attach", old)
            assert (
                len(guard.last_frames) == 3
                and len({f["session"] for f in guard.last_frames}) >= 2
            )
            nested_source = await fresh()
            message_start = await evaluate("messages.length")
            status, nested_click = await send(
                "click", nested_source["state"], x=820, y=470
            )
            await asyncio.sleep(0.05)
            events = await evaluate(f"messages.slice({message_start})")
            assert nested_click.get("status") == "applied" and any(
                e.get("innerClicked") == "A" for e in events
            ), (nested_click, events)
            report["cases"].append(
                {
                    "name": "nested-oopif-native-input",
                    "status": nested_click["status"],
                    "frames": guard.last_frames,
                }
            )
            old = await fresh()
            await action({"removeFrame": True})
            await asyncio.sleep(0.05)
            await reject_and_recover("nested-oopif-detach", old)
            old = await fresh()
            await evaluate("scrollTo(0,100)")
            await asyncio.sleep(0.03)
            await reject_and_recover("parent-page-scroll", old)
            source_parent = await fresh()
            message_start = await evaluate("messages.length")
            status, clicked_parent = await send(
                "click", source_parent["state"], x=800, y=150
            )
            await asyncio.sleep(0.05)
            parent_events = await evaluate(f"messages.slice({message_start})")
            assert clicked_parent.get("status") == "applied" and any(
                e.get("clicked") == "A" for e in parent_events
            ), (clicked_parent, parent_events)
            report["cases"].append(
                {
                    "name": "oopif-route-after-parent-scroll",
                    "status": clicked_parent["status"],
                }
            )
            await evaluate("scrollTo(0,0)")
            await asyncio.sleep(0.03)
            source = await fresh()
            # Missing tokens cannot downgrade an enabled version-two contract.
            missing = {
                k: v for k, v in source["state"].items() if k != "coordinate_token"
            }
            status, reply = await send("click", missing, x=80, y=670)
            assert reply.get("status") == "stale"
            report["cases"].append({"name": "missing-token", "old": reply["status"]})
            # Capture/input checks do not trust forged main-world globals.
            old = await fresh()
            await action({"scroll": 120, "forge": old["state"]["coordinate_token"]})
            await reject_and_recover("forged-page-globals", old)
            await action({"scroll": 0})
            source = await fresh()
            status, down = await send(
                "pointer", source["state"], phase="down", x=800, y=520
            )
            assert down.get("status") == "applied", down
            delivered = []
            for i in range(1, 22):
                status, moved = await send(
                    "pointer",
                    source["state"],
                    phase="move",
                    x=800 + i * 2,
                    y=520 - i * 0.2,
                )
                delivered.append(moved.get("status"))
            status, up = await send(
                "pointer", source["state"], phase="up", x=842, y=515.8
            )
            child_state = await action({})
            assert (
                delivered == ["applied"] * 21
                and up.get("status") == "applied"
                and child_state["moves"] == 21
                and not child_state["dragging"]
                and observer._held_mouse is None
            ), (delivered, up, child_state)
            report["cases"].append(
                {
                    "name": "drag-self-scroll",
                    "moves": delivered,
                    "child": child_state,
                    "held": observer._held_mouse is not None,
                }
            )
            # Child replacement cancels a held gesture and still consumes up.
            await action({"scroll": 0})
            source = await fresh()
            status, down = await send(
                "pointer", source["state"], phase="down", x=800, y=520
            )
            assert down.get("status") == "applied"
            await evaluate(f"child.src={json.dumps(base + '/child?swapped=1')}")
            await asyncio.sleep(0.08)
            status, cancelled = await send(
                "pointer", source["state"], phase="move", x=810, y=520
            )
            status, up = await send(
                "pointer", source["state"], phase="up", x=810, y=520
            )
            assert (
                cancelled.get("status") == "stale"
                and up.get("status") == "applied"
                and observer._held_mouse is None
                and observer.control_mode == "human"
            ), (cancelled, up, observer.control_mode)
            report["cases"].append(
                {
                    "name": "drag-child-replaced",
                    "move": cancelled["status"],
                    "up": up["status"],
                }
            )
            # Focus an OOPIF input, then release while its transition is unknown.
            await evaluate(f"child.src={json.dumps(url + '?keys=1')}")
            await asyncio.sleep(0.08)
            source = await fresh()
            status, clicked = await send("click", source["state"], x=800, y=450)
            assert clicked.get("status") == "applied"
            source = await fresh()
            status, typed = await send(
                "text", source["state"], text="frame text", source="typing"
            )
            assert typed.get("status") == "applied"
            status, pressed = await send(
                "key", source["state"], phase="down", code="ShiftLeft", key="Shift"
            )
            assert pressed.get("status") == "applied"
            await action({"transition": True})
            await asyncio.sleep(0.05)
            _, _, unknown = await observer.read_state()
            assert not unknown["coordinate_ready"]
            status, released = await send(
                "key", phase="up", code="ShiftLeft", key="Shift"
            )
            status, reset = await send("reset_keys")
            child_state = await action({})
            assert (
                released.get("status") == "applied"
                and reset.get("status") == "applied"
                and not child_state["keys"]
                and not observer._held_keys
                and child_state["text"] == "frame text"
            ), (released, child_state)
            report["cases"].append(
                {
                    "name": "key-release-during-transition",
                    "coordinate_ready": unknown["coordinate_ready"],
                    "release": released["status"],
                    "child": child_state,
                }
            )
            await asyncio.sleep(2.6)
            await fresh()
            # Interleave a child mutation between pre-check and actual snapshot.
            old = await fresh()
            original = observer.capture_snapshot
            fired = False
            conflicts = 0

            async def raced(*values):
                """Inject one actual child scroll between capture checks.

                Args:
                    *values: Unchanged observer snapshot arguments.

                Returns:
                    A snapshot only after the original validator accepts it.
                """
                nonlocal fired, conflicts
                if not fired:
                    fired = True
                    await action({"scroll": 160})
                try:
                    return await original(*values)
                except web.HTTPConflict:
                    conflicts += 1
                    raise

            observer.capture_snapshot = raced
            try:
                new = await fresh()
            finally:
                observer.capture_snapshot = original
            assert (
                conflicts >= 1
                and new["state"]["coordinate_token"] != old["state"]["coordinate_token"]
            )
            report["cases"].append(
                {
                    "name": "capture-race",
                    "rejected_captures": conflicts,
                    "old_matches": capture.matches(old, await ready()),
                }
            )
            old = await fresh()
            await evaluate(f"child.src={json.dumps(url + '?stream=1')}")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                _, _, unknown = await observer.read_state()
                if not unknown["coordinate_ready"]:
                    break
                await asyncio.sleep(0.01)
            assert not unknown["coordinate_ready"]
            status, rejected = await send("click", old["state"], x=80, y=670)
            status, terminated = await send("pointer", phase="cancel")
            assert (
                rejected.get("status") == "stale"
                and terminated.get("status") == "applied"
                and observer.control_mode == "human"
            )
            await reject_and_recover("loading-child-recovers", old)
            old = await fresh()
            original = observer.capture_snapshot
            fired = False

            async def transition_race(*values):
                """Start an actual transition after the coordinate pre-check.

                Args:
                    *values: Original snapshot arguments.

                Returns:
                    The original validated snapshot if the barrier remains ready.
                """
                nonlocal fired
                if not fired:
                    fired = True
                    await evaluate(
                        "document.startViewTransition(()=>child.style.left='720px')"
                    )
                    await asyncio.sleep(0.03)
                return await original(*values)

            observer.capture_snapshot = transition_race
            frozen = False
            try:
                await capture.barrier()
            except (TimeoutError, web.HTTPConflict):
                frozen = capture.anchor is None
            finally:
                observer.capture_snapshot = original
            assert frozen and not capture.matches(old, (await observer.read_state())[2])
            status, rejected = await send("click", old["state"], x=80, y=670)
            status, released = await send("reset_keys")
            assert (
                rejected.get("status") == "stale"
                and released.get("status") == "applied"
            )
            await asyncio.sleep(2.6)
            await reject_and_recover("same-document-transition-capture-race", old)
            old = await fresh()
            original_trees = guard._trees
            blocked = asyncio.Event()

            async def stalled_read():
                """Hold one real guard check until its bounded caller cancels it."""
                blocked.set()
                await asyncio.Event().wait()

            guard._trees = stalled_read
            task = asyncio.create_task(guard.sample(browser.agent_focus_target_id))
            try:
                await asyncio.wait_for(blocked.wait(), 1)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            finally:
                guard._trees = original_trees
            assert guard.client is None and guard.root_session is None
            await reject_and_recover("cancelled-identity-read-recovers", old)
        # Small diagnostics: repeated unchanged checks must not rescan the DOM.
        samples = []
        baseline = []
        for _ in range(12):
            started = time.monotonic()
            await observer.read_state(coordinates=False)
            baseline.append((time.monotonic() - started) * 1000)
            started = time.monotonic()
            await observer.read_state()
            samples.append((time.monotonic() - started) * 1000)
        scanned = [v["scanned"] for v in guard.metrics[-12:]]
        assert len(set(scanned)) == 1, scanned
        report["diagnostic"] = {
            "sample_ms": samples,
            "same_page_top_only_ms": baseline,
            "median_top_only_ms": statistics.median(baseline),
            "median_ms": statistics.median(samples),
            "max_ms": max(samples),
            "frames": len(guard.last_frames),
            "sessions": len({f["session"] for f in guard.last_frames}),
            "dom_scan_count_unchanged": scanned,
        }
        report["passed"] = True
    finally:
        report["coordinate_last_error"] = guard.last_error
        report["coordinate_frames"] = guard.last_frames
        report["coordinate_metrics"] = guard.metrics
        await capture.close()
        await observer.close()
        await browser.kill()
        await runner.cleanup()
        await child_runner.cleanup()
        if http:
            await http.close()
        (args.out / "result.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    asyncio.run(main())
