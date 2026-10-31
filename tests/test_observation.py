"""Exercise real MCP Chromium observations through the plugin HTTP boundary."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import shutil
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

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

    async def check_cookie(request):
        cookies_seen.append(request.cookies.get("session"))
        return web.Response(text="Cookie isolation check")

    async def page(request):
        return web.Response(
            text='<!doctype html><meta charset="utf-8"><title>Same browser 登录态</title><input placeholder="value"><p id="login"></p><script>document.cookie="session=private";document.querySelector("#login").textContent="Logged in: " + document.cookie</script>',
            content_type="text/html",
        )

    app.router.add_get("/", page)
    app.router.add_get("/check", check_cookie)
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

    async def rpc(method, params):
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
            )
            return SimpleNamespace(isError=False, content=result["content"])

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
            original = SimpleNamespace(
                mcp_tool=SimpleNamespace(name=name),
                mcp_client=client,
                mcp_server_name="browser-use",
                active=True,
            )
            return await OwnedBrowserTool(original, plugin).call(context, **kwargs)

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
        assert state["title"] == "Same browser 登录态"
        assert state["url"] == f"http://127.0.0.1:{port}/"
        identity = {key: state[key] for key in ("generation", "browser_session_id")}
        frame_response = await observe("snapshot", **identity)
        assert frame_response.status_code == 200
        frame = json.loads(frame_response.body)["data"]
        assert frame["tab_id"] == state["tab_id"]
        assert frame["width"] == 1280 and frame["height"] == 720
        assert len(base64.b64decode(frame["image"])) > 100
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

            async def plugin_http(request):
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
                            await ui.locator(".agent-browser-page").inner_text()
                            == f"Same browser 登录态\nhttp://127.0.0.1:{port}/"
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
                            await ui.screenshot(
                                path=str(destination / "agent-browser-en.png"),
                                animations="disabled",
                            )
                    finally:
                        await viewer.close()
            finally:
                node.kill()
                await node.wait()
                await host_runner.cleanup()
        # Reuse the original element index after observation, proving no DOM rebuild.
        await tool("browser_type", index=index, text="中文观测验证二")
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
