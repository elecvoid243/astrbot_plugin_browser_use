"""Verify MCP ownership and cleanup across activation, failure, and reload."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from runtime import SERVER_NAME


class Manager:
    """Model the public MCP manager lifecycle without installing AstrBot."""

    def __init__(self):
        self.mcp_server_runtime_view = {}
        self.func_list = [
            SimpleNamespace(
                mcp_server_name="other", mcp_tool=SimpleNamespace(name="foreign_tool")
            )
        ]
        self.enable_calls = []
        self.disable_calls = []
        self.close_calls = []
        self.started = asyncio.Event()
        self.gate = None
        self.start_error = False
        self.close_error = False
        self.stop_error = False

    async def enable_mcp_server(self, name, config, shutdown_event, timeout):
        async def call_tool(tool_name, arguments):
            self.close_calls.append(tool_name)
            if self.close_error:
                raise RuntimeError("Browser was already gone")

        self.enable_calls.append(config)
        self.mcp_server_runtime_view[name] = SimpleNamespace(
            shutdown_event=shutdown_event,
            client=SimpleNamespace(
                session=SimpleNamespace(call_tool=call_tool),
                _reconnect_lock=asyncio.Lock(),
                _mcp_server_config=config,
            ),
        )
        self.func_list.extend(
            SimpleNamespace(
                mcp_server_name=name,
                mcp_tool=SimpleNamespace(name=tool_name),
                mcp_client=self.mcp_server_runtime_view[name].client,
                active=True,
            )
            for tool_name in (
                "browser_navigate",
                "retry_with_browser_use_agent",
                "browser_extract_content",
            )
        )
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.start_error:
            raise RuntimeError("MCP initialization failed after registration")

    async def disable_mcp_server(self, name, timeout):
        self.disable_calls.append(name)
        if self.stop_error:
            raise RuntimeError("MCP shutdown failed")
        self.mcp_server_runtime_view.pop(name, None)
        self.func_list = [
            tool for tool in self.func_list if tool.mcp_server_name != name
        ]


@pytest.fixture
def plugin(plugin_class, tmp_path):
    """Create an unstarted plugin with test-only executable placeholders.

    Args:
        plugin_class: Entry point imported against the test host.
        tmp_path: Per-test filesystem workspace.

    Returns:
        Plugin instance and its MCP manager.
    """
    manager = Manager()
    instance = plugin_class(
        SimpleNamespace(
            get_llm_tool_manager=lambda: manager, register_web_api=lambda *args: None
        ),
        {},
    )
    instance._plugin_dir = tmp_path / "plugin"
    for executable in ("runtime/python/python.exe", "runtime/chromium/chrome.exe"):
        path = instance._plugin_dir / executable
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return instance, manager


async def test_enable_disable_and_reenable(plugin):
    instance, manager = plugin
    await instance.initialize()
    assert SERVER_NAME in manager.mcp_server_runtime_view
    assert [tool.mcp_tool.name for tool in manager.func_list] == [
        "foreign_tool",
        "browser_navigate",
    ]
    await instance.initialize()
    assert len(manager.enable_calls) == 1
    await instance.terminate()
    assert manager.close_calls == ["browser_close_all"]
    assert manager.disable_calls == [SERVER_NAME]
    assert [tool.mcp_tool.name for tool in manager.func_list] == ["foreign_tool"]
    await instance.terminate()
    assert len(manager.disable_calls) == 1
    await instance.initialize()
    await instance.terminate()
    assert len(manager.enable_calls) == 2


async def test_start_failure_rolls_back_only_owned_server(plugin):
    instance, manager = plugin
    manager.start_error = True
    with pytest.raises(RuntimeError, match="initialization failed"):
        await instance.initialize()
    assert SERVER_NAME not in manager.mcp_server_runtime_view
    assert [tool.mcp_tool.name for tool in manager.func_list] == ["foreign_tool"]
    assert instance._shutdown_event is None


async def test_foreign_server_is_not_restarted_or_stopped(plugin):
    instance, manager = plugin
    foreign = SimpleNamespace(shutdown_event=asyncio.Event())
    manager.mcp_server_runtime_view[SERVER_NAME] = foreign
    with pytest.raises(RuntimeError, match="another owner"):
        await instance.initialize()
    await instance.terminate()
    assert manager.mcp_server_runtime_view[SERVER_NAME] is foreign
    assert not manager.enable_calls
    assert not manager.disable_calls


async def test_shutdown_does_not_stop_replaced_owner(plugin):
    instance, manager = plugin
    await instance.initialize()
    foreign = SimpleNamespace(shutdown_event=asyncio.Event())
    manager.mcp_server_runtime_view[SERVER_NAME] = foreign
    await instance.terminate()
    assert manager.mcp_server_runtime_view[SERVER_NAME] is foreign
    assert not manager.disable_calls


async def test_browser_close_error_still_stops_mcp(plugin):
    instance, manager = plugin
    await instance.initialize()
    manager.close_error = True
    await instance.terminate()
    assert not manager.mcp_server_runtime_view
    assert manager.disable_calls == [SERVER_NAME]


async def test_disconnected_session_is_not_reconnected(plugin):
    instance, manager = plugin
    await instance.initialize()
    manager.mcp_server_runtime_view[SERVER_NAME].client.session = None
    await instance.terminate()
    assert not manager.close_calls
    assert manager.disable_calls == [SERVER_NAME]


async def test_stale_client_cannot_reconnect_after_disable(plugin):
    instance, manager = plugin
    await instance.initialize()
    client = manager.mcp_server_runtime_view[SERVER_NAME].client
    await instance.terminate()
    assert client.session is None
    assert client._mcp_server_config is None


async def test_shutdown_waits_for_an_existing_reconnection(plugin):
    instance, manager = plugin
    await instance.initialize()
    client = manager.mcp_server_runtime_view[SERVER_NAME].client
    async with client._reconnect_lock:
        stopping = asyncio.create_task(instance.terminate())
        await asyncio.sleep(0)
        assert not manager.disable_calls
        assert client._mcp_server_config is not None
    await stopping
    assert client._mcp_server_config is None
    assert manager.disable_calls == [SERVER_NAME]


async def test_shutdown_failure_can_be_retried(plugin):
    instance, manager = plugin
    await instance.initialize()
    manager.stop_error = True
    with pytest.raises(RuntimeError, match="shutdown failed"):
        await instance.terminate()
    assert instance._shutdown_event is not None
    manager.stop_error = False
    await instance.terminate()
    assert not manager.mcp_server_runtime_view


async def test_disable_waits_for_inflight_start(plugin):
    instance, manager = plugin
    manager.gate = asyncio.Event()
    starting = asyncio.create_task(instance.initialize())
    await manager.started.wait()
    stopping = asyncio.create_task(instance.terminate())
    await asyncio.sleep(0)
    assert not instance._enabled
    manager.gate.set()
    await asyncio.gather(starting, stopping)
    assert manager.disable_calls == [SERVER_NAME]
    assert not manager.mcp_server_runtime_view


async def test_retained_tools_rejected_after_reenable(plugin):
    instance, manager = plugin
    await instance.initialize()
    retained = manager.func_list[-1]
    await instance.terminate()
    await instance.initialize()
    event = SimpleNamespace(
        unified_msg_origin="trusted", get_platform_name=lambda: "qq"
    )
    with pytest.raises(RuntimeError, match="disabled"):
        await retained.call(SimpleNamespace(context=SimpleNamespace(event=event)))
    assert instance._owner is None
    await instance.terminate()


async def test_cancelled_start_does_not_leak_mcp(plugin):
    instance, manager = plugin
    manager.gate = asyncio.Event()
    starting = asyncio.create_task(instance.initialize())
    await manager.started.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert not manager.mcp_server_runtime_view
    assert instance._shutdown_event is None


async def test_missing_runtime_does_not_start_service(plugin):
    instance, manager = plugin
    (instance._plugin_dir / "runtime/python/python.exe").unlink()
    with pytest.raises(FileNotFoundError, match="offline plugin ZIP"):
        await instance.initialize()
    assert not manager.enable_calls
