"""Exercise action terminal states, privacy and bounded recovery delivery."""

import asyncio
import importlib.util
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from observation_queue import ACTION_LIMIT, ObservationQueue
from observer import BrowserObserver


async def test_failed_close_retains_owned_lifetime_for_retry(
    observed_server, monkeypatch
):
    """Preserve identity when upstream loses its pointer after a failed close.

    Args:
        observed_server: Execution wrapper with a controlled upstream server.
        monkeypatch: Temporary upstream close implementation.
    """
    server, observer, upstream = observed_server
    browser = server.browser_session
    server.tools = object()
    server.active_sessions = {browser.id: {"session": browser}}
    fail = True

    async def close_all():
        server.browser_session = None
        server.tools = None
        if not fail:
            server.active_sessions.clear()
        return "Closed sessions"

    monkeypatch.setattr(
        type(server).__mro__[1],
        "_close_all_sessions",
        AsyncMock(side_effect=close_all),
        raising=False,
    )
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await server._close_all_sessions()
    assert server.browser_session is browser
    fail = False
    await server._close_all_sessions()
    assert server.browser_session is None
    assert not server.active_sessions


async def test_control_waits_for_execution_and_revokes_old_lease(observed_server):
    """Stop waits for child cleanup; close does not admit an old queued request."""
    server, observer, upstream = observed_server
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def execute(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    upstream.side_effect = execute
    running = asyncio.create_task(
        server._execute_tool(
            "browser_type", {"_astrbot_action": {"action_id": "inflight", "lease": 1}}
        )
    )
    await entered.wait()
    await observer.start()
    bridge = json.loads(observer.discovery.read_text())
    payload = {
        "generation": observer.generation,
        "browser_session_id": "browser",
        "operation": "stop",
        "lease": 1,
    }
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer secret"}
        ) as http:
            url = f"http://127.0.0.1:{bridge['port']}/control"
            async with http.post(
                url, json={**payload, "generation": "old"}
            ) as response:
                assert response.status == 409
            stopping = asyncio.create_task(http.post(url, json=payload))
            await asyncio.wait_for(cleaning.wait(), 2)
            assert not stopping.done()
            release.set()
            with pytest.raises(RuntimeError, match="cancelled"):
                await running
            response = await stopping
            state = await response.json()
            response.release()
            assert state["execution_state"] == "stopped"
            assert state["current_action"] is None
            assert state["recent_actions"][-1]["status"] == "cancelled"
            assert observer.action_task is None
            with pytest.raises(RuntimeError, match="stopped"):
                await server._execute_tool(
                    "browser_type", {"_astrbot_action": {"lease": 2}}
                )
            server._close_all_sessions = AsyncMock()
            server._close_all_sessions.side_effect = RuntimeError("cleanup failed")
            async with http.post(
                url, json={**payload, "operation": "close"}
            ) as response:
                assert response.status == 500
            assert observer.execution_state == "stopped"
            server._close_all_sessions.side_effect = None
            async with http.post(
                url, json={**payload, "operation": "close"}
            ) as response:
                assert response.status == 200
                assert (await response.json())["execution_state"] == "closed"
            with pytest.raises(RuntimeError, match="stopped"):
                await server._execute_tool(
                    "browser_type", {"_astrbot_action": {"lease": 1}}
                )
            upstream.side_effect = None
            await server._execute_tool(
                "browser_type", {"_astrbot_action": {"lease": 3}}
            )
    finally:
        release.set()
        await observer.close()


@pytest.fixture
def observed_server(monkeypatch, tmp_path):
    """Load the real execution wrapper with a controllable upstream operation.

    Args:
        monkeypatch: Isolated imports and bridge credentials.
        tmp_path: Disposable discovery file location.

    Returns:
        Server, observer and upstream coroutine mock.
    """
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_TOKEN", "secret")
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_FILE", str(tmp_path / "bridge.json"))
    upstream = AsyncMock(return_value="Done")
    for name in ("browser_use", "browser_use.mcp", "browser_use.mcp.server"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["browser_use.mcp.server"].BrowserUseServer = type(
        "Upstream", (), {"_execute_tool": upstream}
    )
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "observed_action_server", Path(__file__).resolve().parents[1] / "mcp_entry.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.LocalBrowserServer()
    server.browser_session = SimpleNamespace(id="browser", agent_focus_target_id="tab")
    observer = BrowserObserver(server)
    server.observer = observer
    observer.read_state = AsyncMock(
        return_value=(
            server.browser_session,
            None,
            {
                "generation": observer.generation,
                "browser_session_id": "browser",
                "tab_id": "tab",
                "url": "local",
                "width": 0,
                "height": 0,
            },
        )
    )
    return server, observer, upstream


@pytest.mark.parametrize(
    "result,status",
    [
        ("Done", "succeeded"),
        ("Error: secret raw result", "failed"),
        ("Element with index 999 not found", "failed"),
    ],
)
async def test_action_start_and_terminal_are_safe(observed_server, result, status):
    server, observer, upstream = observed_server
    upstream.return_value = result
    queue = ObservationQueue()
    observer.subscribers.add(queue)
    try:
        await server._execute_tool(
            "browser_type",
            {
                "text": "password-value",
                "_astrbot_action": {
                    "action_id": "actual",
                    "invocation_id": "trusted-call",
                },
            },
        )
    except RuntimeError:
        assert status == "failed"
    start, end = await queue.get(), await queue.get()
    assert start["current_action"]["status"] == "running"
    assert end["current_action"] is None
    action = end["recent_actions"][-1]
    assert action["status"] == status and action["invocation_id"] == "trusted-call"
    assert end["action_cursor"] == end["sequence"]
    assert "password-value" not in json.dumps(end) and "raw result" not in json.dumps(
        end
    )


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_finish_running_action(observed_server, cancel):
    server, observer, upstream = observed_server
    entered = asyncio.Event()

    async def block(*args):
        entered.set()
        await asyncio.Event().wait()

    upstream.side_effect = block
    operation = asyncio.create_task(
        server._execute_tool(
            "browser_click",
            {"_astrbot_action": {"timeout": 0.05 if not cancel else 10}},
        )
    )
    await entered.wait()
    if cancel:
        operation.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
        await operation
    assert observer.current_action is None
    assert observer.recent_actions[-1]["status"] == (
        "cancelled" if cancel else "timed_out"
    )


async def test_frames_do_not_overwrite_actions_and_overflow_resets():
    queue = ObservationQueue()
    for index in range(ACTION_LIMIT):
        queue.put_nowait(
            {"type": "action", "sequence": index * 2, "action_cursor": index * 2}
        )
        queue.put_nowait({"type": "frame", "sequence": index * 2 + 1})
    events = [await queue.get() for _ in range(ACTION_LIMIT + 1)]
    assert len([event for event in events if event["type"] == "action"]) == ACTION_LIMIT
    assert events[-1]["type"] == "frame"
    for index in range(ACTION_LIMIT + 1):
        queue.put_nowait(
            {"type": "action", "sequence": index, "recent_actions": ["authoritative"]}
        )
    reset = await queue.get()
    assert reset["action_reset"] and reset["recent_actions"] == ["authoritative"]
    queue.put_nowait(None)
    assert await queue.get() is None


async def test_private_cancellation_finalizes_and_refuses_delayed_execution(
    observed_server,
):
    server, observer, upstream = observed_server
    entered = asyncio.Event()

    async def block(*args):
        entered.set()
        await asyncio.Event().wait()

    upstream.side_effect = block
    await observer.start()
    port = json.loads(observer.discovery.read_text())["port"]
    task = asyncio.create_task(
        server._execute_tool(
            "browser_click", {"_astrbot_action": {"action_id": "cancel-me"}}
        )
    )
    await entered.wait()
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{port}/action/cancel",
                headers={"Authorization": "Bearer secret"},
                json={
                    "generation": observer.generation,
                    "action_id": "cancel-me",
                    "status": "cancelled",
                },
            ) as response:
                assert response.status == 200
        with pytest.raises(RuntimeError, match="Browser action cancelled"):
            await task
        assert [item["status"] for item in observer.recent_actions] == ["cancelled"]
        with pytest.raises(RuntimeError, match="cancelled before execution"):
            await server._execute_tool(
                "browser_click", {"_astrbot_action": {"action_id": "cancel-me"}}
            )
    finally:
        await observer.close()


@pytest.mark.parametrize("expired", [False, True])
async def test_action_cursor_replays_or_resets_from_snapshot(observed_server, expired):
    server, observer, upstream = observed_server

    async def state():
        return (
            server.browser_session,
            None,
            {
                "generation": observer.generation,
                "browser_session_id": "browser",
                "tab_id": "tab",
                "sequence": observer.sequence,
                **observer.action_state("browser"),
            },
        )

    observer.read_state.side_effect = state
    for _ in range(70 if expired else 2):
        await server._execute_tool("browser_get_html", {})
    retained_cursor = observer.action_events[-2]["sequence"]
    cursor = 1 if expired else retained_cursor
    observer.capture = AsyncMock()
    await observer.start()
    port = json.loads(observer.discovery.read_text())["port"]
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(
                f"http://127.0.0.1:{port}/stream",
                params={
                    "generation": observer.generation,
                    "browser_session_id": "browser",
                    "action_cursor": cursor,
                },
                headers={"Authorization": "Bearer secret"},
            ) as response:
                assert response.status == 200
                await response.content.readline()
                event = json.loads(
                    (await response.content.readline()).decode().removeprefix("data: ")
                )
                assert event["current_action"] is None
                assert event["recent_actions"][-1]["status"] == "succeeded"
                if expired:
                    assert event["type"] == "state" and event["action_reset"]
                    assert len(event["recent_actions"]) == 32
                else:
                    assert (
                        event["type"] == "action"
                        and event["sequence"] == retained_cursor + 1
                    )
    finally:
        await observer.close()


@pytest.mark.parametrize(
    "details,changed,expected",
    [
        (None, False, False),
        ({"click_x": -1, "click_y": 2}, False, False),
        ({"click_x": 10, "click_y": 20}, True, False),
        ({"click_x": 10, "click_y": 20, "actual_value": "secret"}, False, True),
    ],
)
async def test_target_requires_actual_metadata_and_matching_document(
    observed_server, details, changed, expected
):
    server, observer, upstream = observed_server
    page = {
        "generation": observer.generation,
        "browser_session_id": "browser",
        "tab_id": "tab",
        "document_id": "doc",
        "url": "/page",
        "viewport_width": 1280,
        "viewport_height": 720,
        "scroll_x": 0,
        "scroll_y": 0,
    }
    observer.read_state.return_value = (
        server.browser_session,
        None,
        {**page, "document_id": "new-doc" if changed else "doc"},
    )
    observer.current_action = {"action_id": "actual"}
    await server._mark_target(page, details, "click")
    assert bool(observer.highlight) == expected
    assert "secret" not in json.dumps(observer.highlight)
