"""Real shadow-root coordinate and bounded manual-routing repair proof."""

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

HTML = r"""<!doctype html><style>body{margin:0;background:#eef4ff}#host,iframe{position:absolute;left:700px;top:150px;width:576px;height:400px;border:0}#root{position:fixed;left:30px;top:650px;width:150px;height:40px}canvas{position:fixed;left:0;top:0;z-index:5}</style><canvas id="stamp" width="576" height="40"></canvas><input id="native" style="position:absolute;top:80px;left:30px" value="Native UA root"><div id="host"></div><button id="root" onclick="proof.rootClicks++">Root</button><script>
window.proof={doc:__DOC__,generation:0,clicked:[],rootClicks:0,keys:[],scrollEvents:0,moves:0,dragging:false};window.scroller=null;
onkeydown=e=>{if(!proof.keys.includes(e.code))proof.keys.push(e.code)};onkeyup=e=>proof.keys=proof.keys.filter(c=>c!==e.code);
window.mount=(mode='open',depth=1,scroll=0,element=document.getElementById('host'))=>{let leaf=element.attachShadow({mode});for(let i=1;i<depth;i++){const h=document.createElement('div');leaf.append(h);leaf=h.attachShadow({mode:i%2?'closed':'open'})}leaf.innerHTML=`<style>#scroller{height:400px;width:576px;overflow:auto;background:white}#content{height:2000px;position:relative}button{position:absolute;left:80px;width:180px;height:40px}</style><div id="scroller"><div id="content"><button id="a" style="top:80px">A</button><button id="b" style="top:200px">B</button><div id="drag" style="position:absolute;left:80px;top:350px;width:180px;height:60px;background:#38bdf8">Drag</div></div></div>`;window.leaf=leaf;scroller=leaf.querySelector('#scroller');leaf.querySelector('#a').onclick=()=>proof.clicked.push('A');leaf.querySelector('#b').onclick=()=>proof.clicked.push('B');const drag=leaf.querySelector('#drag');drag.onpointerdown=e=>{proof.dragging=true;drag.setPointerCapture(e.pointerId)};drag.onpointermove=e=>{if(proof.dragging&&e.buttons){proof.moves++;scroller.scrollTop+=4}};drag.onpointerup=()=>proof.dragging=false;drag.onlostpointercapture=()=>proof.dragging=false;scroller.addEventListener('scroll',()=>proof.scrollEvents++);scroller.scrollTop=scroll;proof.generation++};
window.replaceRoot=(mode,depth=1,scroll=0)=>{const h=document.createElement('div');h.id='host';host.replaceWith(h);mount(mode,depth,scroll)};
window.addInner=()=>{const previous=leaf;const h=document.createElement('div');h.style='position:absolute;left:0;top:0;width:576px;height:400px';previous.append(h);mount('closed',1,120,h)};
window.truth=()=>({...proof,scroll:scroller?.isConnected?scroller.scrollTop:0});
const params=new URLSearchParams(location.search);if(!params.has('empty'))mount(params.get('mode')||'open',Number(params.get('depth')||1));
const ctx=stamp.getContext('2d');function draw(){const t=truth(),words=[0x41535452,t.doc,t.generation,t.scroll];for(let row=0;row<4;row++)for(let bit=0;bit<32;bit++){ctx.fillStyle=(words[row]>>>bit)&1?'#fff':'#000';ctx.fillRect(bit*18,row*10,18,10)}requestAnimationFrame(draw)}draw();</script>"""


async def main():
    """Run actual Chromium capture/input proofs through the private HTTP bridge.

    Raises:
        AssertionError: A stale click executes, pixels mismatch, or release fails.
        TimeoutError: A bounded fixture or native input recovery cannot finish.
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
        viewport={"width": 1280, "height": 720},
        args=[
            "--no-proxy-server",
            "--disable-background-networking",
            "--site-per-process",
        ],
    )
    guard = FrameCoordinates(browser)
    observer = BrowserObserver(
        SimpleNamespace(browser_session=browser), coordinate_guard=guard
    )
    observer.owner = "shadow-repair-fixture"
    capture = CaptureBarrierCandidate(observer)
    report = {"passed": False, "violations": [], "cases": [], "optical": []}
    serial = 0

    async def page(request):
        """Serve an independently stamped real document.

        Args:
            request: Fixture navigation request.

        Returns:
            Actual page with open/closed/native shadow roots and real buttons.
        """
        nonlocal serial
        serial += 1
        return web.Response(
            text=HTML.replace("__DOC__", str(serial)), content_type="text/html"
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}/"
    http = None
    number = 0

    async def evaluate(expression, target=None):
        """Read fixture ground truth independently of the coordinate reader.

        Args:
            expression: Trusted fixture setup/inspection JavaScript.
            target: Actual OOPIF target for independent child inspection.

        Returns:
            Actual by-value fixture result.
        """
        cdp = await browser.get_or_create_cdp_session(
            target_id=target or browser.agent_focus_target_id, focus=False
        )
        reply = await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": expression, "returnByValue": True},
            session_id=cdp.session_id,
        )
        if reply.get("exceptionDetails"):
            raise RuntimeError(reply["exceptionDetails"])
        return reply["result"].get("value")

    async def ready():
        """Wait for native root ownership and raw coordinates to become stable.

        Returns:
            Authoritative current target metadata.

        Raises:
            TimeoutError: Native root inspection does not recover within four seconds.
        """
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            _, _, state = await observer.read_state()
            if state.get("coordinate_ready"):
                return state
            await asyncio.sleep(0.01)
        raise TimeoutError(guard.last_error)

    async def send(operation, target=None, **fields):
        """Deliver ordered operator commands through actual HTTP admission.

        Args:
            operation: Typed manual command.
            target: Captured immutable target for a new input.
            **fields: Typed native input parameters.

        Returns:
            Actual HTTP status and acknowledgement.
        """
        nonlocal number
        number += 1
        body = {
            **identity,
            "operation": operation,
            "control_epoch": epoch,
            "input_id": number,
            **fields,
        }
        if target is not None:
            body["target"] = target
        async with http.post(manual_url, json=body) as response:
            text = await response.text()
            try:
                reply = json.loads(text)
            except ValueError:
                reply = {"message": text}
            report.setdefault("input", []).append(
                {
                    "id": number,
                    "operation": operation,
                    "status": response.status,
                    "ack": reply.get("status"),
                }
            )
            return response.status, reply

    async def fresh(child=None):
        """Validate actual JPEG and screencast pixels against independent words.

        Args:
            child: Actual OOPIF target whose visible stamp must also match.

        Returns:
            Original immutable capture record.
        """
        await ready()
        await capture.barrier()
        record = await capture.next_frame()
        for target, left, top in [(None, 0, 0)] + (
            [(child, 700, 150)] if child else []
        ):
            truth = await evaluate("truth()", target)
            expected = [0x5452, truth["doc"], truth["generation"], truth["scroll"]]
            for kind, encoded in [
                ("snapshot", capture.snapshot["image"]),
                ("screencast", record["event"]["data"]),
            ]:
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                    image = image.convert("L")
                    scale = image.width / record["state"]["viewport_width"]
                    words = [
                        sum(
                            1 << bit
                            for bit in range(16)
                            if image.getpixel(
                                (
                                    round((left + bit * 18 + 9) * scale),
                                    round((top + row * 10 + 5) * scale),
                                )
                            )
                            > 128
                        )
                        for row in range(4)
                    ]
                assert words == expected, (kind, words, expected)
                report["optical"].append(
                    {
                        "source": kind,
                        "child": bool(child and target),
                        "words": words,
                        "expected": expected,
                    }
                )
        return record

    async def reject_recover(name, old, expected="B", child=None):
        """Reject old source identity, then hit the real new button after capture.

        Args:
            name: Reproducible regression label.
            old: Actual previously captured pixels and token.
            expected: Actual child button, or Root after detachment.
            child: Actual OOPIF target for independent hit inspection.
        """
        before = await evaluate("truth()", child)
        state = await ready()
        assert not capture.matches(old, state), name
        status, reply = await send("click", old["state"], x=800, y=250)
        assert status == 200 and reply.get("status") == "stale", (name, reply)
        assert (await evaluate("truth()", child))["clicked"] == before["clicked"], name
        new = await fresh(child)
        status, reply = await send(
            "click",
            new["state"],
            x=60 if expected == "Root" else 800,
            y=670 if expected == "Root" else 250,
        )
        after = await evaluate("truth()", child)
        assert status == 200 and reply.get("status") == "applied", (name, reply)
        assert (
            after["rootClicks"] == before["rootClicks"] + 1
            if expected == "Root"
            else after["clicked"] == before["clicked"] + [expected]
        ), (name, after, before)
        report["cases"].append(
            {
                "name": name,
                "old": "stale",
                "new": "applied",
                "old_token": old["state"]["coordinate_token"],
                "new_token": new["state"]["coordinate_token"],
                "frames": guard.last_frames,
            }
        )
        if "-closed-depth1-scroll-" in name:
            (args.out / "closed-scroll-before.jpg").write_bytes(
                base64.b64decode(old["event"]["data"])
            )
            (args.out / "closed-scroll-after.jpg").write_bytes(
                base64.b64decode(new["event"]["data"])
            )
        print("PASS", name, flush=True)

    try:
        if args.start_gate:
            deadline = time.monotonic() + 10
            while not args.start_gate.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("Startup gate expired.")
                await asyncio.sleep(0.01)
        await browser.start()
        await browser.navigate_to(base)
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
            for mode, depth in [("open", 1), ("closed", 1), ("open", 3)]:
                label = f"{iteration}-{mode}-depth{depth}"
                await browser.navigate_to(base + f"?mode={mode}&depth={depth}")
                old = await fresh()
                guard.deliver_events = False
                await evaluate("scroller.scrollTop=120")
                await asyncio.sleep(0.03)
                await reject_recover(label + "-scroll-delayed-hints", old)
                guard.deliver_events = True
                if mode == "closed":
                    await evaluate("scroller.scrollTop=0")
                    source = await fresh()
                    before_moves = (await evaluate("truth()"))["moves"]
                    _, down = await send(
                        "pointer", source["state"], phase="down", x=800, y=520
                    )
                    assert down.get("status") == "applied"
                    moves = []
                    for i in range(1, 22):
                        _, moved = await send(
                            "pointer",
                            source["state"],
                            phase="move",
                            x=800 + i * 2,
                            y=520 - i * 0.2,
                        )
                        moves.append(moved.get("status"))
                    _, up = await send(
                        "pointer", source["state"], phase="up", x=842, y=515.8
                    )
                    truth = await evaluate("truth()")
                    assert (
                        moves == ["applied"] * 21
                        and up.get("status") == "applied"
                        and truth["moves"] == before_moves + 21
                        and truth["scroll"] == 84
                        and not truth["dragging"]
                        and observer._held_mouse is None
                    ), (moves, up, truth)
                    report["cases"].append(
                        {
                            "name": "closed-shadow-drag-self-scroll",
                            "moves": moves,
                            "scroll": truth["scroll"],
                            "held": False,
                        }
                    )
                    await evaluate("scroller.scrollTop=0")
                    source = await fresh()
                    _, down = await send(
                        "pointer", source["state"], phase="down", x=800, y=520
                    )
                    assert down.get("status") == "applied"
                    await evaluate("replaceRoot('closed',1,0)")
                    _, cancelled = await send(
                        "pointer", source["state"], phase="move", x=810, y=520
                    )
                    _, up = await send("pointer", phase="up", x=810, y=520)
                    assert (
                        cancelled.get("status") == "stale"
                        and up.get("status") == "applied"
                        and observer._held_mouse is None
                        and observer.human is not None
                    )
                    report["cases"].append(
                        {
                            "name": "closed-shadow-drag-root-replaced",
                            "move": "stale",
                            "up": "applied",
                            "lease_retained": True,
                        }
                    )
                old = await fresh()
                await evaluate(f"replaceRoot('{mode}',{depth},0)")
                await reject_recover(label + "-replace-root", old, "A")
                old = await fresh()
                await evaluate("addInner()")
                await reject_recover(label + "-new-nested-closed-root", old)
                old = await fresh()
                await evaluate("host.remove()")
                await reject_recover(label + "-detach-root", old, "Root")
                await evaluate(
                    "var h=document.createElement('div');h.id='host';document.body.append(h)"
                )
                old = await fresh()
                await evaluate(f"mount('{mode}',{depth},120)")
                await reject_recover(label + "-attach-to-existing-host", old)
            # Native UA roots are read through generic Node APIs, never .mode.
            assert any(
                kind == "user-agent"
                for f in guard.last_frames
                for _, kind in f["shadow_roots"]
            )
            old = await fresh()
            native_ids = {
                backend
                for f in guard.last_frames
                for backend, kind in f["shadow_roots"]
                if kind == "user-agent"
            }
            original_resolve = guard.client.send.DOM.resolveNode

            async def unavailable_root(**kwargs):
                """Make an actual native root temporarily unresolvable.

                Args:
                    **kwargs: Original native node resolution parameters.

                Returns:
                    An unavailable wrapper only for the selected real UA root.
                """
                if kwargs["params"].get("backendNodeId") in native_ids:
                    return {"object": {}}
                return await original_resolve(**kwargs)

            guard.root_objects.clear()
            guard.client.send.DOM.resolveNode = unavailable_root
            try:
                _, _, unknown = await observer.read_state()
                assert not unknown["coordinate_ready"]
                status, rejected = await send("click", old["state"], x=800, y=250)
                status, released = await send("reset_keys")
                assert (
                    rejected.get("status") == "stale"
                    and released.get("status") == "applied"
                    and observer.human is not None
                )
            finally:
                guard.client.send.DOM.resolveNode = original_resolve
            await fresh()
            report["cases"].append(
                {
                    "name": "native-ua-unavailable-recovers",
                    "frozen": True,
                    "lease_retained": observer.human is not None,
                }
            )
            # Delay the native root event itself: the same CDP stream cannot
            # process the authoritative barrier response ahead of that event.
            await browser.navigate_to(base + "?empty=1")
            old = await fresh()
            client = guard.client
            original_event = client._event_registry._handlers["DOM.shadowRootPushed"]
            entered = asyncio.Event()
            release = asyncio.Event()

            async def delayed_event(event, session_id):
                """Hold native lifecycle delivery ahead of its ordered read barrier.

                Args:
                    event: Actual native shadowRootPushed event.
                    session_id: Its actual owning CDP session.
                """
                entered.set()
                await release.wait()
                original_event(event, session_id)

            client.register.DOM.shadowRootPushed(delayed_event)
            guard.deliver_events = False
            await evaluate("mount('closed',2,120)")
            await asyncio.wait_for(entered.wait(), 1)
            pending = asyncio.create_task(send("click", old["state"], x=800, y=250))
            await asyncio.sleep(0.1)
            assert not pending.done() and not (await evaluate("truth()"))["clicked"]
            release.set()
            status, rejected = await asyncio.wait_for(pending, 2)
            client.register.DOM.shadowRootPushed(original_event)
            guard.deliver_events = True
            assert status == 200 and rejected.get("status") == "stale"
            new = await fresh()
            status, recovered = await send("click", new["state"], x=800, y=250)
            assert recovered.get("status") == "applied" and (await evaluate("truth()"))[
                "clicked"
            ] == ["B"]
            report["cases"].append(
                {
                    "name": "native-root-event-delayed",
                    "old": "stale",
                    "new": "applied",
                    "barrier_waited": True,
                }
            )
            # Use real OOPIF owners to exercise native hit/describe and pool waits.
            await evaluate(
                f"var f=document.createElement('iframe');f.id='child';f.src='http://localhost:{port}/?mode=closed';document.body.append(f)"
            )
            await asyncio.sleep(0.08)
            await fresh()
            remote = next(
                frame["frame"]
                for frame in guard.last_frames
                if frame["session"] != guard.root_session
            )
            await evaluate("scroller.scrollTop=120", remote)
            await asyncio.sleep(0.03)
            source = await fresh(remote)
            # The child fixture positions its host at 700px; move it into its
            # own viewport so top-level (800,250) hits its shadow button.
            await evaluate("host.style.left='0px';host.style.top='0px'", remote)
            source = await fresh(remote)
            for fault, ending in [
                ("hit", "reset_keys"),
                ("describe", "disconnect"),
                ("session", "up"),
            ]:
                source = await fresh(remote)
                await send(
                    "key", source["state"], phase="down", code="ShiftLeft", key="Shift"
                )
                source = await fresh(remote)
                if fault == "session":
                    _, down = await send(
                        "pointer", source["state"], phase="down", x=60, y=670
                    )
                    assert down.get("status") == "applied", down
                before = await evaluate("truth()", remote)
                root_before = await evaluate("truth()")
                entered = asyncio.Event()
                late = asyncio.get_running_loop().create_future()
                original_lookup = browser.get_or_create_cdp_session
                reader = guard.client
                original_send = reader.send_raw
                pool_reads = 0

                async def delayed_native(method, params=None, session_id=None):
                    """Withhold one real routing query's response.

                    Args:
                        method: Native CDP command.
                        params: Unchanged native query arguments.
                        session_id: Actual target session.

                    Returns:
                        A normal response unless this is the selected route query.
                    """
                    selected = (
                        fault == "hit" and method == "DOM.getNodeForLocation"
                    ) or (
                        fault == "describe"
                        and method == "DOM.describeNode"
                        and "backendNodeId" in (params or {})
                    )
                    if selected:
                        entered.set()
                        return await asyncio.shield(late)
                    return await original_send(method, params, session_id)

                async def delayed_lookup(target_id=None, focus=True):
                    """Withhold the actual remote input-session lookup only.

                    Args:
                        target_id: Native target selected by verified routing.
                        focus: Existing pool focus option.

                    Returns:
                        The actual pooled session after normal lookups.
                    """
                    nonlocal pool_reads
                    if target_id == browser.agent_focus_target_id:
                        pool_reads += 1
                        if pool_reads == 2:
                            entered.set()
                            return await asyncio.shield(late)
                    return await original_lookup(target_id=target_id, focus=focus)

                if fault == "session":
                    object.__setattr__(
                        browser, "get_or_create_cdp_session", delayed_lookup
                    )
                else:
                    reader.send_raw = delayed_native
                pending = asyncio.create_task(
                    send("pointer", source["state"], phase="move", x=60, y=670)
                    if fault == "session"
                    else send(
                        "click",
                        source["state"],
                        x=60 if fault == "describe" else 800,
                        y=670 if fault == "describe" else 250,
                    )
                )
                await asyncio.wait_for(entered.wait(), 1)
                started = time.monotonic()
                if ending == "up":
                    terminal = asyncio.create_task(
                        send("pointer", phase="up", x=60, y=670)
                    )
                else:
                    terminal = asyncio.create_task(send(ending))
                try:
                    status, rejected = await asyncio.wait_for(pending, 3)
                    end_status, end_ack = await asyncio.wait_for(terminal, 3)
                finally:
                    reader.send_raw = original_send
                    object.__setattr__(
                        browser, "get_or_create_cdp_session", original_lookup
                    )
                assert (
                    status == end_status == 200
                    and rejected.get("status") == "stale"
                    and end_ack.get("status") == "applied"
                ), (rejected, end_ack)
                assert not observer._held_keys and observer._held_mouse is None
                late.set_result({"backendNodeId": 1})
                await asyncio.sleep(0.03)
                assert (await evaluate("truth()", remote))["clicked"] == before[
                    "clicked"
                ]
                assert (await evaluate("truth()"))["rootClicks"] == root_before[
                    "rootClicks"
                ]
                if ending == "disconnect":
                    assert observer.human is None
                    async with http.post(
                        manual_url, json={**identity, "operation": "acquire"}
                    ) as response:
                        epoch = (await response.json())["control_epoch"]
                    number = 0
                else:
                    assert (
                        observer.human is not None and observer.control_mode == "human"
                    )
                new = await fresh(remote)
                status, recovered = await send("click", new["state"], x=800, y=250)
                assert recovered.get("status") == "applied" and (
                    await evaluate("truth()", remote)
                )["clicked"] == before["clicked"] + ["B"]
                report["cases"].append(
                    {
                        "name": "real-route-" + fault,
                        "terminal": ending,
                        "elapsed_s": time.monotonic() - started,
                        "old": "stale",
                        "new": "applied",
                        "late_clicks": 0,
                        "held_keys": len(observer._held_keys),
                        "held_mouse": observer._held_mouse is not None,
                    }
                )
                print("PASS real-route-" + fault, flush=True)
            await evaluate("child.remove()")
            await fresh()
        samples = []
        baseline = []
        metrics = []
        for _ in range(12):
            started = time.monotonic()
            await observer.read_state(coordinates=False)
            baseline.append((time.monotonic() - started) * 1000)
            started = time.monotonic()
            await ready()
            samples.append((time.monotonic() - started) * 1000)
            metrics.append(guard.metrics[-1])
        assert len({m["scanned"] for m in metrics}) == 1
        assert len({m["native_dom_full_scans"] for m in metrics}) == 1
        assert len({m["native_dom_nodes_seen"] for m in metrics}) == 1
        report["diagnostic"] = {
            "top_only_ms": baseline,
            "full_ms": samples,
            "median_top_only_ms": statistics.median(baseline),
            "median_full_ms": statistics.median(samples),
            "metrics": metrics,
        }
        report["passed"] = True
    finally:
        report["last_error"] = guard.last_error
        report["last_frames"] = guard.last_frames
        report["metrics"] = guard.metrics
        await capture.close()
        await observer.close()
        await browser.kill()
        await runner.cleanup()
        if http:
            await http.close()
        (args.out / "result.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    asyncio.run(main())
