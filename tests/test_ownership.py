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
        entered.append(kwargs["arguments"])
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
    await asyncio.gather(tool.call(context, url="one"), tool.call(context, url="two"))
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
