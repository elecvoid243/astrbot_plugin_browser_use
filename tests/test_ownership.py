"""Verify admission before any browser operation reaches MCP."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


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
