"""Exercise real MCP Chromium observations through the plugin HTTP boundary."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import shutil
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from starlette.responses import StreamingResponse

from runtime import build_mcp_config


async def test_live_browser_observation(plugin_class, tmp_path, monkeypatch):
    """Verify real form state, bridge credentials, ownership and stale identities.

    Args:
        plugin_class: Plugin loaded with a minimal host lifecycle adapter.
        tmp_path: Disposable browser and bridge storage.
        monkeypatch: Cleanup for the host request adapter.
    """
    root = Path(__file__).resolve().parents[1]
    if not (root / "runtime/python/python.exe").exists():
        pytest.skip("The optional bundled Windows runtime is not installed.")
    config = build_mcp_config(root, tmp_path, {})
    bridge_file = tmp_path / "bridge.json"
    config["env"].update(
        ASTRBOT_BROWSER_BRIDGE_TOKEN="test-bridge-token",
        ASTRBOT_BROWSER_BRIDGE_FILE=str(bridge_file),
    )
    app = web.Application()
    cookies_seen = []
    slow_started = asyncio.Event()

    async def slow_page(request):
        slow_started.set()
        await asyncio.sleep(0.5)
        return web.Response(
            text="<title>Slow local page</title>", content_type="text/html"
        )

    async def check_cookie(request):
        cookies_seen.append(request.cookies.get("session"))
        return web.Response(text="Cookie isolation check")

    async def page(request):
        return web.Response(
            text='<!doctype html><meta charset="utf-8"><title>Same browser 登录态</title><input placeholder="value"><p id="login"></p><p id="tick"></p><a target="_blank" href="/second">Second tab</a><div style="height:2500px;background:linear-gradient(white,blue)"></div><label>Below-fold input <input placeholder="below-fold"></label><div style="height:600px"></div><script>document.cookie="session=private";document.querySelector("#login").textContent="Logged in: " + document.cookie;let tick=0;setInterval(()=>document.querySelector("#tick").textContent="Live tick " + (++tick),100)</script>',
            content_type="text/html",
        )

    async def second(request):
        return web.Response(
            text="<title>Second observed tab</title><p>New tab</p><script>setInterval(()=>document.body.style.backgroundColor=`hsl(${Date.now()%360} 80% 70%)`,100)</script>",
            content_type="text/html",
        )

    app.router.add_get("/", page)
    app.router.add_get("/check", check_cookie)
    app.router.add_get("/second", second)
    app.router.add_get("/slow", slow_page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    error_log = (tmp_path / "stderr.log").open("w", encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        config["command"],
        *config["args"],
        cwd=config["cwd"],
        env={**os.environ, **config["env"]},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=error_log,
    )
    request_id = 0

    async def rpc(method, params, allow_error=False):
        nonlocal request_id
        request_id += 1
        process.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
                + "\n"
            ).encode()
        )
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), 60)
            assert line, (tmp_path / "stderr.log").read_text(encoding="utf-8")
            response = json.loads(line)
            if response.get("id") == request_id:
                assert "error" not in response, response
                result = response["result"]
                if not allow_error:
                    assert not result.get("isError"), result
                return result

    try:
        await rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "observation-test", "version": "1"},
            },
        )
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await process.stdin.drain()
        from browser_use_plugin_under_test.owned_tool import OwnedBrowserTool

        db = SimpleNamespace(
            get_platform_session_by_id=AsyncMock(
                return_value=SimpleNamespace(creator="alice", platform_id="webchat")
            )
        )
        plugin = plugin_class(SimpleNamespace(get_db=lambda: db), {})
        plugin._enabled = True
        plugin._bridge_file = bridge_file
        plugin._bridge_token = "test-bridge-token"

        async def call_tool(**kwargs):
            result = await rpc(
                "tools/call",
                {"name": kwargs["tool_name"], "arguments": kwargs["arguments"]},
                allow_error=True,
            )
            return SimpleNamespace(
                isError=result.get("isError", False), content=result["content"]
            )

        client = SimpleNamespace(call_tool_with_reconnect=call_tool)
        event = SimpleNamespace(
            unified_msg_origin="webchat:FriendMessage:webchat!alice!chat-1",
            get_platform_name=lambda: "webchat",
            get_session_id=lambda: "webchat!alice!chat-1",
        )
        context = SimpleNamespace(
            context=SimpleNamespace(event=event), tool_call_timeout=60
        )

        async def tool(name, **kwargs):
            allow_error = kwargs.pop("_allow_error", False)
            original = SimpleNamespace(
                mcp_tool=SimpleNamespace(name=name),
                mcp_client=client,
                mcp_server_name="browser-use",
                active=True,
            )
            result = await OwnedBrowserTool(original, plugin).call(context, **kwargs)
            if not allow_error:
                assert not result.isError, result.content
            return result

        await tool("browser_navigate", url=f"http://127.0.0.1:{port}")
        before = await tool("browser_get_state")
        elements = json.loads(before.content[0]["text"])["interactive_elements"]
        index = next(
            item["index"] for item in elements if item.get("placeholder") == "value"
        )
        await tool("browser_type", index=index, text="中文观测验证")
        import browser_use_plugin_under_test.main as entry

        async def observe(path="status", username="alice", chat_id="chat-1", **query):
            monkeypatch.setattr(
                entry,
                "request",
                SimpleNamespace(
                    username=username,
                    path=f"/browser-use/{path}",
                    query={"chat_id": chat_id, **query},
                ),
            )
            return await plugin.browser_observation()

        assert (await observe(username=None)).status_code == 401
        assert (await observe(chat_id="")).status_code == 400
        assert (await observe(username="mallory")).status_code == 403
        assert (await observe(chat_id="chat-2")).status_code == 409
        state = json.loads((await observe()).body)["data"]
        assert state["current_action"] is None
        assert state["recent_actions"][-1]["kind"] == "type"
        assert state["recent_actions"][-1]["status"] == "succeeded"
        assert "中文观测验证" not in json.dumps(state, ensure_ascii=False)
        assert state["highlight"] is not None, state
        assert (
            state["highlight"]["action_id"] == state["recent_actions"][-1]["action_id"]
        )
        print("ACTION_TARGET " + json.dumps(state["highlight"]))
        await tool("browser_click", coordinate_x=96, coordinate_y=22)
        clicked = json.loads((await observe()).body)["data"]
        assert clicked["highlight"]["x"] == 96 and clicked["highlight"]["y"] == 22
        failed = await tool("browser_click", index=999999, _allow_error=True)
        assert failed.isError
        failed_state = json.loads((await observe()).body)["data"]
        assert failed_state["recent_actions"][-1]["status"] == "failed"
        assert failed_state["highlight"] is None
        assert state["title"] == "Same browser 登录态"
        assert state["url"] == f"http://127.0.0.1:{port}/"
        identity = {key: state[key] for key in ("generation", "browser_session_id")}
        frame_response = await observe("snapshot", **identity)
        assert frame_response.status_code == 200
        frame = json.loads(frame_response.body)["data"]
        assert frame["tab_id"] == state["tab_id"]
        assert frame["width"] == 1280 and frame["height"] == 720
        assert len(base64.b64decode(frame["image"])) > 100
        # Reuse the original DOM index before any subsequent DOM refresh.
        await tool("browser_type", index=index, text="中文观测验证二")
        assert (
            await observe(
                "stream",
                generation="old",
                browser_session_id=state["browser_session_id"],
            )
        ).status_code == 409
        assert (
            await observe(
                "stream", generation=state["generation"], browser_session_id="old"
            )
        ).status_code == 409

        # Consume production plugin SSE while real MCP actions continue executing.
        stream_response = await observe("stream", **identity)
        iterator = stream_response.body_iterator
        frames = []
        sequence = frame["sequence"]

        async def next_frame(tab_id=None):
            nonlocal sequence
            while True:
                block = await asyncio.wait_for(anext(iterator), 10)
                if block.startswith(b":"):
                    continue
                event = json.loads(
                    next(
                        line[6:]
                        for line in block.split(b"\n")
                        if line.startswith(b"data: ")
                    )
                )
                assert event["sequence"] > sequence
                sequence = event["sequence"]
                assert event["generation"] == identity["generation"]
                assert event["browser_session_id"] == identity["browser_session_id"]
                if event["type"] == "frame" and (
                    tab_id is None or event["tab_id"] == tab_id
                ):
                    assert 0 < event["width"] <= 1280 and 0 < event["height"] <= 720
                    assert len(base64.b64decode(event["image"])) <= 512 * 1024
                    event["delivery_latency"] = max(
                        time.time() - event["capture_timestamp"], 0
                    )
                    return event

        try:
            first = await next_frame(state["tab_id"])
            started = time.monotonic()
            for _ in range(12):
                frames.append(await next_frame(state["tab_id"]))
            duration = time.monotonic() - started
            assert len({item["image"] for item in frames}) > 1
            print(
                f"OBSERVER_METRICS frames={len(frames)} duration={duration:.3f}s fps={len(frames) / duration:.3f} max_delivery_latency={max(item['delivery_latency'] for item in frames):.3f}s"
            )
            await tool("browser_scroll", direction="down")
            scrolled = await next_frame(state["tab_id"])
            assert scrolled["image"] != first["image"]
            await tool("browser_scroll", direction="up")
            # The existing link opens the same controlled Chromium's new target.
            elements = json.loads((await tool("browser_get_state")).content[0]["text"])[
                "interactive_elements"
            ]
            link = next(item["index"] for item in elements if item["tag"] == "a")
            await tool("browser_click", index=link)
            deadline = time.monotonic() + 5
            new_tab = None
            while new_tab is None and time.monotonic() < deadline:
                tabs = json.loads((await tool("browser_list_tabs")).content[0]["text"])
                new_tab = next(
                    (item for item in tabs if item["url"].endswith("/second")), None
                )
                if new_tab is None:
                    await asyncio.sleep(0.1)
            assert new_tab is not None, tabs
            await tool("browser_switch_tab", tab_id=new_tab["tab_id"])
            switched = json.loads((await observe()).body)["data"]
            newer = await next_frame(switched["tab_id"])
            assert newer["title"] == "Second observed tab"
            assert newer["tab_id"] != state["tab_id"]
            await tool("browser_close_tab", tab_id=new_tab["tab_id"])
            restored = await next_frame(state["tab_id"])
            assert restored["title"] == state["title"]
            # A slow consumer skips obsolete frames, keeping bounded memory.
            await asyncio.sleep(1.5)
            latest = await next_frame(state["tab_id"])
            assert latest["sequence"] > restored["sequence"] + 1
        finally:
            await iterator.aclose()
        # Disconnecting observation leaves browser execution alive.
        await tool("browser_get_html")
        assert (
            "cdp"
            not in json.dumps(
                {key: value for key, value in frame.items() if key != "image"}
            ).lower()
        )
        evidence = os.environ.get("BROWSER_OBSERVER_EVIDENCE_DIR")
        if evidence:
            destination = Path(evidence)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "snapshot.json").write_text(
                json.dumps(frame, ensure_ascii=False), encoding="utf-8"
            )
        dashboard = os.environ.get("BROWSER_OBSERVER_DASHBOARD_ROOT")
        if dashboard:
            from playwright.async_api import async_playwright
            from starlette.requests import Request

            # Use AstrBot's real context-local request binding in the HTTP probe.
            upload = types.ModuleType("astrbot.core.utils.upload")
            upload.save_upload_stream = AsyncMock()
            monkeypatch.setitem(sys.modules, upload.__name__, upload)
            spec = importlib.util.spec_from_file_location(
                "observer_host_web", Path(dashboard).parent / "astrbot/api/web.py"
            )
            host_web = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(host_web)
            subscriptions = 0
            streams_closed = asyncio.Event()

            async def plugin_http(request):
                nonlocal subscriptions
                username = (
                    "alice"
                    if request.headers.get("Authorization")
                    == "Bearer observer-probe-token"
                    else None
                )
                scope = {
                    "type": "http",
                    "method": "GET",
                    "scheme": "http",
                    "path": request.path,
                    "query_string": request.query_string.encode(),
                    "headers": list(request.raw_headers),
                    "client": ("127.0.0.1", 1),
                }
                bound = host_web.PluginRequest(Request(scope), username=username)
                monkeypatch.setattr(entry, "request", host_web.request)
                with host_web.bind_request_context(bound):
                    result = await plugin.browser_observation()
                if isinstance(result, StreamingResponse):
                    subscriptions += 1
                    streams_closed.clear()
                    response = web.StreamResponse(
                        status=result.status_code, headers=dict(result.headers)
                    )
                    await response.prepare(request)
                    try:
                        async for block in result.body_iterator:
                            await response.write(block)
                    except (ConnectionError, asyncio.CancelledError):
                        pass
                    finally:
                        await result.body_iterator.aclose()
                        subscriptions -= 1
                        if subscriptions == 0:
                            streams_closed.set()
                    return response
                return web.Response(
                    body=result.body,
                    status=result.status_code,
                    content_type="application/json",
                )

            host = web.Application()
            host.router.add_get(
                "/api/v1/plugins/extensions/browser-use/{kind}", plugin_http
            )
            host_runner = web.AppRunner(host)
            await host_runner.setup()
            host_site = web.TCPSite(host_runner, "127.0.0.1", 0)
            await host_site.start()
            host_port = host_site._server.sockets[0].getsockname()[1]
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"http://127.0.0.1:{host_port}/api/v1/plugins/extensions/browser-use/status?chat_id=chat-1"
                ) as rejected:
                    assert rejected.status == 401
            node = await asyncio.create_subprocess_exec(
                shutil.which("node"),
                str(root / "tests/dashboard_probe.mjs"),
                dashboard,
                f"http://127.0.0.1:{host_port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=error_log,
            )
            try:
                while True:
                    line = await asyncio.wait_for(node.stdout.readline(), 60)
                    assert line, (tmp_path / "stderr.log").read_text(encoding="utf-8")
                    if line.startswith(b"PROBE_URL="):
                        url = line.decode().strip().split("=", 1)[1]
                        break
                async with async_playwright() as automation:
                    viewer = await automation.chromium.launch(
                        executable_path=str(root / "runtime/chromium/chrome.exe"),
                        headless=True,
                    )
                    try:
                        ui = await viewer.new_page(
                            viewport={"width": 1000, "height": 750}
                        )
                        browser_errors = []
                        ui.on(
                            "pageerror", lambda error: browser_errors.append(str(error))
                        )
                        ui.on(
                            "console",
                            lambda message: (
                                browser_errors.append(message.text)
                                if message.type == "error"
                                else None
                            ),
                        )
                        await ui.goto(url)
                        try:
                            await ui.wait_for_function(
                                "document.querySelector('.agent-browser-image img')?.naturalWidth === 1280",
                                timeout=15000,
                            )
                        except Exception as exc:
                            raise AssertionError(
                                f"{browser_errors}; {await ui.locator('body').inner_text()}"
                            ) from exc
                        assert (
                            "Same browser 登录态"
                            in await ui.locator(".agent-browser-page").inner_text()
                        )
                        assert (
                            f"http://127.0.0.1:{port}/"
                            in await ui.locator(".agent-browser-page").inner_text()
                        )
                        initial_sequence = int(
                            await ui.locator(".agent-browser-view").get_attribute(
                                "data-sequence"
                            )
                        )
                        await ui.wait_for_function(
                            "(sequence) => Number(document.querySelector('.agent-browser-view').dataset.sequence) > sequence + 3",
                            arg=initial_sequence,
                        )
                        await ui.evaluate("window.observerProbe.setVisible(false)")
                        await asyncio.wait_for(streams_closed.wait(), 5)
                        assert subscriptions == 0
                        await tool("browser_scroll", direction="down")
                        await ui.evaluate("window.observerProbe.setVisible(true)")
                        await ui.wait_for_function(
                            "document.querySelector('.agent-browser-image img')?.naturalWidth === 1280"
                        )
                        await tool("browser_scroll", direction="up")
                        await ui.wait_for_function(
                            "document.querySelector('.agent-browser-status')?.textContent.includes('实时观测')"
                        )
                        # A real input action must render an actual target marker.
                        elements = json.loads(
                            (await tool("browser_get_state")).content[0]["text"]
                        )["interactive_elements"]
                        index = next(
                            item["index"]
                            for item in elements
                            if item.get("placeholder") == "value"
                        )
                        await tool("browser_type", index=index, text="中文动作验证")
                        await ui.locator(".agent-browser-target").wait_for(timeout=5000)
                        assert (
                            await ui.locator('[data-action-status="succeeded"]').count()
                            > 0
                        )
                        if evidence:
                            await ui.screenshot(
                                path=str(destination / "agent-browser-zh.png"),
                                animations="disabled",
                            )
                            await ui.goto(url + "?locale=en-US")
                            await ui.wait_for_function(
                                "document.querySelector('.agent-browser-image img')?.naturalWidth === 1280"
                            )
                            await ui.wait_for_function(
                                "document.querySelector('.agent-browser-status')?.textContent.includes('Live')"
                            )
                            await tool("browser_type", index=index, text="中文动作验证")
                            await ui.locator(".agent-browser-target").wait_for(
                                timeout=5000
                            )
                            await ui.screenshot(
                                path=str(destination / "agent-browser-en.png"),
                                animations="disabled",
                            )
                        # Resolve a real off-screen target, return to the top,
                        # then let the input action itself scroll the page.
                        for _ in range(6):
                            await tool("browser_scroll", direction="down")
                        elements = json.loads(
                            (await tool("browser_get_state")).content[0]["text"]
                        )["interactive_elements"]
                        below_index = next(
                            item["index"]
                            for item in elements
                            if item.get("placeholder") == "below-fold"
                        )
                        for _ in range(6):
                            await tool("browser_scroll", direction="up")
                        top_state = json.loads((await observe()).body)["data"]
                        assert top_state["scroll_y"] == 0
                        await tool(
                            "browser_type", index=below_index, text="自动滚动输入验证"
                        )
                        moved = json.loads((await observe()).body)["data"]
                        assert moved["scroll_y"] > 500
                        target = moved["highlight"]
                        assert target and target["scroll_y"] == moved["scroll_y"]
                        assert 0 <= target["y"] < moved["viewport_height"]
                        await ui.wait_for_function(
                            "(id) => document.querySelector('.agent-browser-target') && document.querySelector('.agent-browser-actions li')?.dataset.actionId === id && document.querySelector('.agent-browser-image img')?.naturalWidth === 1280",
                            arg=target["action_id"],
                            timeout=5000,
                        )
                        error = await ui.evaluate(
                            """(point) => {
                            const image = document.querySelector('.agent-browser-image img').getBoundingClientRect();
                            const marker = document.querySelector('.agent-browser-target').getBoundingClientRect();
                            return Math.max(Math.abs(marker.x + marker.width / 2 - image.x - point.x / point.viewport_width * image.width), Math.abs(marker.y + marker.height / 2 - image.y - point.y / point.viewport_height * image.height));
                        }""",
                            target,
                        )
                        assert error < 1
                        print(
                            f"AUTOSCROLL_TARGET scroll_y={moved['scroll_y']} x={target['x']} y={target['y']} render_error={error:.3f}px"
                        )
                        if evidence:
                            await ui.screenshot(
                                path=str(destination / "agent-browser-autoscroll.png"),
                                animations="disabled",
                            )
                    finally:
                        await viewer.close()
            finally:
                node.kill()
                await node.wait()
                await host_runner.cleanup()
        html = await tool("browser_get_html")
        assert "Same browser" in str(html.content)
        assert (
            await observe(
                "snapshot",
                generation="old",
                browser_session_id=state["browser_session_id"],
            )
        ).status_code == 409
        assert (
            await observe(
                "snapshot", generation=state["generation"], browser_session_id="old"
            )
        ).status_code == 409
        bridge = json.loads(bridge_file.read_text(encoding="utf-8"))
        # Real local navigation timeout and host cancellation must finalize once.
        context.tool_call_timeout = 0.05
        timed_out = await tool(
            "browser_navigate", url=f"http://127.0.0.1:{port}/slow", _allow_error=True
        )
        assert timed_out.isError
        timeout_state = json.loads((await observe()).body)["data"]
        assert timeout_state["current_action"] is None
        assert timeout_state["recent_actions"][-1]["status"] == "timed_out"
        context.tool_call_timeout = 60
        await tool("browser_navigate", url=f"http://127.0.0.1:{port}/")
        slow_started.clear()
        cancelled = asyncio.create_task(
            tool(
                "browser_navigate",
                url=f"http://127.0.0.1:{port}/slow",
                _allow_error=True,
            )
        )
        await asyncio.wait_for(slow_started.wait(), 5)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        cancelled_state = json.loads((await observe()).body)["data"]
        assert cancelled_state["current_action"] is None
        assert cancelled_state["recent_actions"][-1]["status"] == "cancelled"
        print("ACTION_TERMINALS real click,input,failure,timeout,cancellation passed")
        async with aiohttp.ClientSession() as http:
            async with http.get(
                f"http://127.0.0.1:{bridge['port']}/snapshot",
                params={"generation": state["generation"]},
            ) as response:
                assert response.status == 401
        await tool("browser_navigate", url=f"http://127.0.0.1:{port}/check")
        await tool("browser_close_all")
        assert plugin._owner is None
        event.unified_msg_origin = "webchat:FriendMessage:webchat!alice!chat-2"
        event.get_session_id = lambda: "webchat!alice!chat-2"
        await tool("browser_navigate", url=f"http://127.0.0.1:{port}/check")
        assert cookies_seen == ["private", None]
        await tool("browser_close_all")
        plugin._enabled = False
        assert (await observe()).status_code == 503
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 20)
        assert process.returncode == 0
        assert not bridge_file.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        error_log.close()
        await runner.cleanup()
