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
                "browser_type",
                {
                    "_astrbot_action": {
                        "lease": 3,
                        "control_epoch": observer.control_epoch,
                    }
                },
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
    execute = server._execute_tool

    async def owned_execution(name, arguments):
        """Supply trusted host metadata for action-lifecycle fixtures.

        Args:
            name: Browser tool under test.
            arguments: Explicit test inputs and optional overridden identity.

        Returns:
            The runtime admission result.
        """
        metadata = arguments.setdefault("_astrbot_action", {})
        metadata.setdefault("owner", "trusted-chat")
        metadata.setdefault("generation", observer.generation)
        return await execute(name, arguments)

    server._execute_tool = owned_execution
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


async def test_manual_admission_ordering_target_and_explicit_handback(observed_server):
    """Reject stale identities and duplicate input without changing the browser.

    Args:
        observed_server: Isolated runtime admission wrapper and bridge.
    """
    server, observer, upstream = observed_server
    dispatch = AsyncMock()
    server.browser_session.get_browser_state_summary = AsyncMock()
    server._get_browser_state = AsyncMock(return_value=("fresh page", None))
    observer.owner = "trusted-chat"
    state = {
        "generation": observer.generation,
        "browser_session_id": "browser",
        "tab_id": "tab",
        "document_id": "doc",
        "viewport_width": 1280,
        "viewport_height": 720,
        "scroll_x": 0,
        "scroll_y": 0,
    }
    observer.read_state.return_value = (
        server.browser_session,
        SimpleNamespace(
            session_id="cdp",
            cdp_client=SimpleNamespace(
                send=SimpleNamespace(Input=SimpleNamespace(dispatchMouseEvent=dispatch))
            ),
        ),
        state,
    )
    await observer.start()
    bridge = json.loads(observer.discovery.read_text())
    identity = {
        "generation": observer.generation,
        "browser_session_id": "browser",
        "owner": "trusted-chat",
        "client_id": "socket-1",
    }
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer secret"}
        ) as http:
            url = f"http://127.0.0.1:{bridge['port']}/manual"
            async with http.post(
                url, json={**identity, "generation": "old", "operation": "acquire"}
            ) as response:
                assert response.status == 409
            observer.current_action = {
                "action_id": "pending",
                "browser_session_id": "browser",
            }
            async with http.post(
                url, json={**identity, "operation": "acquire"}
            ) as response:
                assert response.status == 409
            observer.current_action = None
            async with http.post(
                url, json={**identity, "operation": "acquire"}
            ) as response:
                assert response.status == 200
                epoch = (await response.json())["control_epoch"]
            async with http.post(
                url, json={**identity, "client_id": "other", "operation": "acquire"}
            ) as response:
                assert response.status == 409
            with pytest.raises(RuntimeError, match="human control"):
                await server._execute_tool("browser_type", {})
            click = {
                **identity,
                "operation": "click",
                "control_epoch": epoch,
                "input_id": 1,
                "x": 100,
                "y": 80,
                "target": {
                    key: state[key]
                    for key in (
                        "tab_id",
                        "document_id",
                        "viewport_width",
                        "viewport_height",
                        "scroll_x",
                        "scroll_y",
                    )
                },
            }
            for patch in (
                {"client_id": "forged"},
                {"control_epoch": epoch - 1},
                {"target": {**click["target"], "document_id": "old"}},
                {"input_id": 2},
            ):
                async with http.post(url, json={**click, **patch}) as response:
                    assert response.status == 409
            for _ in range(2):
                async with http.post(url, json=click) as response:
                    assert response.status == 200
                    assert (await response.json())["status"] == "applied"
            assert dispatch.await_count == 2  # One press/release, even after retry.
            async with http.post(
                url,
                json={**identity, "operation": "disconnect", "control_epoch": epoch},
            ) as response:
                assert response.status == 200
            assert observer.control_mode == "paused"
            with pytest.raises(RuntimeError, match="paused"):
                await server._execute_tool("browser_type", {})
            async with http.post(
                url, json={**identity, "operation": "acquire"}
            ) as response:
                epoch = (await response.json())["control_epoch"]
            async with http.post(
                url, json={**identity, "operation": "release", "control_epoch": epoch}
            ) as response:
                assert response.status == 200
                renewed = (await response.json())["control_epoch"]
            assert observer.control_mode == "agent"
            server.browser_session.get_browser_state_summary.assert_awaited_once_with(
                cached=False
            )
            with pytest.raises(RuntimeError, match="epoch expired"):
                await server._execute_tool(
                    "browser_type", {"_astrbot_action": {"control_epoch": epoch}}
                )
            await server._execute_tool(
                "browser_type", {"_astrbot_action": {"control_epoch": renewed}}
            )
            assert server.browser_session.id == "browser"
            with pytest.raises(RuntimeError, match="another chat"):
                await server._execute_tool(
                    "browser_type",
                    {
                        "_astrbot_action": {
                            "owner": "forged-chat",
                            "control_epoch": renewed,
                        }
                    },
                )
            with pytest.raises(RuntimeError, match="generation"):
                await server._execute_tool(
                    "browser_type",
                    {
                        "_astrbot_action": {
                            "generation": "old",
                            "control_epoch": renewed,
                        }
                    },
                )
    finally:
        await observer.close()


@pytest.mark.parametrize("settles", [False, True])
async def test_running_takeover_waits_for_actual_dispatched_browser_events(
    observed_server, settles
):
    """Keep input disabled until both tool cleanup and page event handlers settle.

    Args:
        observed_server: Private execution wrapper and loopback bridge.
        settles: Whether the detached browser event finishes within the deadline.
    """
    server, observer, upstream = observed_server
    entered, cleaning, release, event_done = (asyncio.Event() for _ in range(4))

    async def execute(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    upstream.side_effect = execute
    observer.owner = "trusted-chat"
    observer.pending_browser_events.append(
        SimpleNamespace(event_completed_signal=event_done)
    )
    # Admission normally registers an event after starting the tool.
    pending = observer.pending_browser_events.pop()
    running = asyncio.create_task(server._execute_tool("browser_type", {}))
    await entered.wait()
    observer.pending_browser_events.append(pending)
    state = observer.read_state.return_value[2]
    observer.read_state.return_value = (server.browser_session, object(), state)
    await observer.start()
    bridge = json.loads(observer.discovery.read_text())
    identity = {
        "generation": observer.generation,
        "browser_session_id": "browser",
        "owner": "trusted-chat",
        "client_id": "human",
    }
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer secret"}
        ) as http:
            acquiring = asyncio.create_task(
                http.post(
                    f"http://127.0.0.1:{bridge['port']}/manual",
                    json={**identity, "operation": "acquire"},
                )
            )
            await asyncio.wait_for(cleaning.wait(), 2)
            assert observer.control_mode == "acquiring" and observer.human is None
            with pytest.raises(RuntimeError, match="human control"):
                await server._execute_tool("browser_type", {})
            release.set()
            with pytest.raises(RuntimeError, match="cancelled"):
                await running
            await asyncio.sleep(0.05)
            assert not acquiring.done()
            if settles:
                event_done.set()
            response = await asyncio.wait_for(acquiring, 7)
            assert response.status == (200 if settles else 504)
            assert observer.control_mode == ("human" if settles else "paused")
            assert (observer.human is not None) == settles
            response.release()
            assert observer.execution_state == "active"
            assert observer.recent_actions[-1]["status"] == "cancelled"
    finally:
        event_done.set()
        release.set()
        await observer.close()


async def test_failed_handback_keeps_agent_admission_closed(observed_server):
    """A failed fresh observation must not renew Agent admission.

    Args:
        observed_server: Private execution wrapper and authenticated bridge.
    """
    server, observer, _ = observed_server
    observer.owner = "trusted-chat"
    state = observer.read_state.return_value[2]
    observer.read_state.return_value = (server.browser_session, object(), state)
    server.browser_session.get_browser_state_summary = AsyncMock()
    server._get_browser_state = AsyncMock(
        return_value=("Error: Target unavailable", None)
    )
    await observer.start()
    bridge = json.loads(observer.discovery.read_text())
    identity = {
        "generation": observer.generation,
        "browser_session_id": "browser",
        "owner": "trusted-chat",
        "client_id": "human",
    }
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer secret"}
        ) as http:
            url = f"http://127.0.0.1:{bridge['port']}/manual"
            async with http.post(
                url, json={**identity, "operation": "acquire"}
            ) as response:
                epoch = (await response.json())["control_epoch"]
            async with http.post(
                url, json={**identity, "operation": "release", "control_epoch": epoch}
            ) as response:
                assert response.status == 409
            assert observer.control_mode == "paused" and observer.human is None
            with pytest.raises(RuntimeError, match="paused"):
                await server._execute_tool("browser_type", {})
            server._get_browser_state.return_value = ("fresh current page", None)
            async with http.post(
                url, json={**identity, "operation": "acquire"}
            ) as response:
                epoch = (await response.json())["control_epoch"]
            async with http.post(
                url, json={**identity, "operation": "release", "control_epoch": epoch}
            ) as response:
                assert response.status == 200
                assert (await response.json())[
                    "agent_observation"
                ] == "fresh current page"
            assert observer.control_mode == "agent"
    finally:
        await observer.close()


async def test_control_metadata_preserves_static_page_frame():
    """A click acknowledgement or heartbeat cannot erase its only changed frame."""
    queue = ObservationQueue()
    queue.put_nowait({"type": "frame", "sequence": 10, "image": "clicked-page"})
    queue.put_nowait({"type": "state", "sequence": 11, "control_mode": "human"})
    assert (await queue.get())["image"] == "clicked-page"
    assert (await queue.get())["control_mode"] == "human"
    queue.put_nowait({"type": "state", "sequence": 12, "control_mode": "human"})
    queue.put_nowait({"type": "frame", "sequence": 13, "image": "newer-page"})
    assert (await queue.get())["sequence"] == 12
    assert (await queue.get())["sequence"] == 13
