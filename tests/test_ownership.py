"""Verify admission before any browser operation reaches MCP."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web


async def test_host_cancellation_drains_without_cancelling_sdk_handler(
    plugin_class, tmp_path
):
    """Cancel only the private execution child while the host request drains.

    Args:
        plugin_class: Isolated host plugin.
        tmp_path: Temporary authenticated bridge discovery record.
    """
    from browser_use_plugin_under_test.owned_tool import OwnedBrowserTool

    plugin = plugin_class(SimpleNamespace(), {})
    plugin._enabled = True
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = []
    received = []

    async def execute(**kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return SimpleNamespace(isError=True)

    async def cancel(request):
        received.append(await request.json())
        release.set()
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/action/cancel", cancel)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    plugin._bridge_file = tmp_path / "bridge.json"
    plugin._bridge_file.write_text(
        json.dumps(
            {"port": site._server.sockets[0].getsockname()[1], "generation": "run"}
        )
    )
    original = SimpleNamespace(
        mcp_tool=SimpleNamespace(name="browser_navigate"),
        mcp_client=SimpleNamespace(call_tool_with_reconnect=execute),
        mcp_server_name="browser-use",
        active=True,
    )
    event = SimpleNamespace(unified_msg_origin="owner", get_platform_name=lambda: "qq")
    context = SimpleNamespace(
        context=SimpleNamespace(event=event), tool_call_timeout=30
    )
    try:
        running = asyncio.create_task(OwnedBrowserTool(original, plugin).call(context))
        await entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not cancelled
        assert received[0]["stop_execution"] is True
        assert received[0]["lease"] == 0
        assert plugin._stopped
    finally:
        release.set()
        await runner.cleanup()


async def test_trusted_ownership_serialization_and_close(plugin_class):
    plugin = plugin_class(SimpleNamespace(), {})
    plugin._enabled = True
    from browser_use_plugin_under_test.owned_tool import OwnedBrowserTool

    entered = []
    active = 0

    async def execute(**kwargs):
        nonlocal active
        assert active == 0
        active += 1
        entered.append(
            {
                key: value
                for key, value in kwargs["arguments"].items()
                if key != "_astrbot_action"
            }
        )
        assert kwargs["arguments"]["_astrbot_action"]["action_id"] != "forged"
        await asyncio.sleep(0)
        active -= 1
        return SimpleNamespace(isError=False)

    client = SimpleNamespace(call_tool_with_reconnect=AsyncMock(side_effect=execute))
    original = SimpleNamespace(
        mcp_tool=SimpleNamespace(name="browser_navigate"),
        mcp_client=client,
        mcp_server_name="browser-use",
        active=True,
    )
    tool = OwnedBrowserTool(original, plugin)
    event = SimpleNamespace(
        unified_msg_origin="webchat:FriendMessage:webchat!alice!chat-1",
        get_platform_name=lambda: "webchat",
        get_session_id=lambda: "webchat!alice!chat-1",
    )
    context = SimpleNamespace(
        context=SimpleNamespace(event=event), tool_call_timeout=30
    )
    await asyncio.gather(
        tool.call(context, url="one", _astrbot_action={"action_id": "forged"}),
        tool.call(context, url="two"),
    )
    assert entered == [{"url": "one"}, {"url": "two"}]
    assert plugin._webchat_owner == ("alice", "chat-1")
    other = SimpleNamespace(
        context=SimpleNamespace(
            event=SimpleNamespace(
                unified_msg_origin="other", get_platform_name=lambda: "qq"
            )
        ),
        tool_call_timeout=30,
    )
    with pytest.raises(RuntimeError, match="busy"):
        await tool.call(other, owner="spoofed", url="private")
    assert len(entered) == 2
    with pytest.raises(ValueError, match="trusted"):
        await tool.call(SimpleNamespace(context=None))
    original.mcp_tool.name = "browser_close_all"
    client.call_tool_with_reconnect.side_effect = None
    client.call_tool_with_reconnect.return_value = SimpleNamespace(isError=True)
    await tool.call(context)
    assert plugin._owner == event.unified_msg_origin
    client.call_tool_with_reconnect.side_effect = execute
    await tool.call(context)
    assert plugin._owner is None
    await tool.call(other)
    plugin._enabled = False
    with pytest.raises(RuntimeError, match="disabled"):
        await tool.call(other)


async def test_observation_reports_availability_without_starting_browser(
    plugin_class, monkeypatch
):
    """Expose stable availability reasons without creating a browser.

    Args:
        plugin_class: Plugin using an isolated host lifecycle adapter.
        monkeypatch: Cleanup for the authenticated request adapter.
    """
    import json
    import sys

    plugin = plugin_class(
        SimpleNamespace(
            get_db=lambda: SimpleNamespace(
                get_platform_session_by_id=AsyncMock(
                    return_value=SimpleNamespace(creator="alice", platform_id="webchat")
                )
            )
        ),
        {},
    )
    entry = sys.modules[plugin_class.__module__]
    monkeypatch.setattr(
        entry,
        "request",
        SimpleNamespace(
            username="alice", query={"chat_id": "chat-1"}, path="/browser-use/status"
        ),
    )
    disabled = await plugin.browser_observation()
    assert disabled.status_code == 503
    assert json.loads(disabled.body)["data"]["reason"] == "disabled"
    plugin._enabled = True
    idle = await plugin.browser_observation()
    assert json.loads(idle.body)["data"] == {"available": False}
    assert idle.headers["cache-control"] == "no-store"
    assert plugin._owner is None
    plugin._owner = "trusted-other"
    plugin._webchat_owner = ("alice", "other")
    busy = await plugin.browser_observation()
    assert busy.status_code == 409
    assert json.loads(busy.body)["data"]["reason"] == "busy"
    plugin._webchat_owner = ("alice", "chat-1")
    unavailable = await plugin.browser_observation()
    assert unavailable.status_code == 503
    assert json.loads(unavailable.body)["data"]["reason"] == "runtime_unavailable"


@pytest.mark.parametrize("transition", ["disable", "cancel", "restart"])
async def test_late_handback_response_cannot_restore_admission(
    plugin_class, monkeypatch, tmp_path, transition
):
    """Reject a private response invalidated while its host request was waiting.

    Args:
        plugin_class: Isolated plugin with the actual host request implementation.
        monkeypatch: Authenticated request context binding.
        tmp_path: Disposable private bridge discovery record.
        transition: Terminal or generation change during the pending handback.
    """
    import sys

    plugin = plugin_class(
        SimpleNamespace(
            get_db=lambda: SimpleNamespace(
                get_platform_session_by_id=AsyncMock(
                    return_value=SimpleNamespace(creator="alice", platform_id="webchat")
                )
            )
        ),
        {},
    )
    plugin._enabled = True
    plugin._owner = "trusted-owner"
    plugin._webchat_owner = ("alice", "chat")
    plugin._manual_blocked = True
    resumed = []
    plugin._agent_pause = SimpleNamespace(resume=lambda *args: resumed.append(args))
    entered, finish = asyncio.Event(), asyncio.Event()

    async def identity(request):
        """Return private lifetime metadata without page access.

        Args:
            request: Internal identity request.

        Returns:
            Current test lifetime.
        """
        return web.json_response(
            {"generation": "run", "browser_session_id": "browser", "available": True}
        )

    async def manual(request):
        """Hold a successful handback response until a terminal transition.

        Args:
            request: Authenticated internal handback.

        Returns:
            A successful but now stale private acknowledgement.
        """
        entered.set()
        await finish.wait()
        return web.json_response(
            {
                "control_epoch": 2,
                "agent_observation": "fresh",
                "state": {"control_mode": "agent"},
            }
        )

    app = web.Application()
    app.router.add_get("/identity", identity)
    app.router.add_post("/manual", manual)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    plugin._bridge_file = tmp_path / "bridge.json"
    plugin._bridge_file.write_text(
        json.dumps(
            {"generation": "run", "port": site._server.sockets[0].getsockname()[1]}
        )
    )
    monkeypatch.setattr(
        sys.modules[plugin_class.__module__],
        "request",
        SimpleNamespace(
            username="alice",
            query={
                "chat_id": "chat",
                "generation": "run",
                "browser_session_id": "browser",
            },
            path="/browser-use/manual",
            browser_control={
                "operation": "release",
                "client_id": "client",
                "control_epoch": 1,
            },
        ),
    )
    try:
        pending = asyncio.create_task(plugin.browser_observation())
        await asyncio.wait_for(entered.wait(), 2)
        if transition == "disable":
            plugin._enabled = False
            plugin._disable_revision += 1
        elif transition == "cancel":
            plugin._stopped = True
        else:
            plugin._bridge_token = "new-runtime"
            plugin._lease += 1
        finish.set()
        response = await pending
        assert response.status_code == 409
        assert plugin._manual_blocked and not resumed
    finally:
        finish.set()
        await runner.cleanup()


@pytest.mark.parametrize("transition", ["stop", "same_owner_reopen"])
async def test_queued_calls_do_not_cross_a_lifetime(plugin_class, transition):
    """Reject queued work even when a new lifetime has the same owner.

    Args:
        plugin_class: Isolated host plugin.
        transition: Lifetime change while the admitted call waits for its lock.
    """
    from browser_use_plugin_under_test.owned_tool import OwnedBrowserTool

    plugin = plugin_class(SimpleNamespace(), {})
    plugin._enabled = True
    execute = AsyncMock(return_value=SimpleNamespace(isError=False))
    original = SimpleNamespace(
        mcp_tool=SimpleNamespace(name="browser_navigate"),
        mcp_client=SimpleNamespace(call_tool_with_reconnect=execute),
        mcp_server_name="browser-use",
        active=True,
    )
    tool = OwnedBrowserTool(original, plugin)
    event = SimpleNamespace(unified_msg_origin="owner", get_platform_name=lambda: "qq")
    context = SimpleNamespace(
        context=SimpleNamespace(event=event), tool_call_timeout=30
    )
    async with plugin._operation_lock:
        queued = asyncio.create_task(tool.call(context, url="old"))
        await asyncio.sleep(0)
        plugin._lease += 1
        plugin._stopped = transition == "stop"
    with pytest.raises(RuntimeError, match="expired"):
        await queued
    execute.assert_not_awaited()
    if transition == "same_owner_reopen":
        await tool.call(context, url="new")
        execute.assert_awaited_once()
