"""Protect releases when the server replaces its current browser reference."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from frame_coordinates import FrameCoordinates
from observer import BrowserObserver


async def test_release_uses_original_guard_browser_registry(monkeypatch, tmp_path):
    """Do not forget a live held key based on a different browser's target list.

    Args:
        monkeypatch: Isolate bridge discovery environment variables.
        tmp_path: Temporary discovery location; no server is started.
    """
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_TOKEN", "isolated-test-token")
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_FILE", str(tmp_path / "bridge.json"))
    original_registry = AsyncMock(
        return_value={"targetInfos": [{"targetId": "original-iframe"}]}
    )
    replacement_registry = AsyncMock(return_value={"targetInfos": []})
    original = SimpleNamespace(
        cdp_client=SimpleNamespace(
            send=SimpleNamespace(Target=SimpleNamespace(getTargets=original_registry))
        )
    )
    replacement = SimpleNamespace(
        cdp_client=SimpleNamespace(
            send=SimpleNamespace(
                Target=SimpleNamespace(getTargets=replacement_registry)
            )
        )
    )
    release = AsyncMock()
    held = SimpleNamespace(
        target_id="original-iframe",
        session_id="original-input",
        cdp_client=SimpleNamespace(
            send=SimpleNamespace(Input=SimpleNamespace(dispatchKeyEvent=release))
        ),
    )
    observer = BrowserObserver(
        SimpleNamespace(browser_session=replacement),
        coordinate_guard=SimpleNamespace(browser=original),
    )
    observer._held_keys["ShiftLeft"] = (held, {"key": "Shift", "code": "ShiftLeft"})
    await observer.release_keys()
    original_registry.assert_awaited_once()
    replacement_registry.assert_not_awaited()
    release.assert_awaited_once()
    assert release.await_args.kwargs["session_id"] == "original-input"
    assert release.await_args.kwargs["params"]["type"] == "keyUp"
    assert not observer._held_keys


@pytest.mark.parametrize("fault", ["hit", "describe", "session", "shared"])
async def test_manual_routing_deadline_releases_queued_reset(
    monkeypatch, tmp_path, fault
):
    """A silent native routing reply cannot own the input lock indefinitely.

    Args:
        monkeypatch: Isolated bridge credentials and discovery location.
        tmp_path: Disposable runtime discovery directory.
        fault: Native route query or pooled input-session lookup to stall.
    """
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_TOKEN", "isolated-test-token")
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_FILE", str(tmp_path / "bridge.json"))
    entered = asyncio.Event()
    late = asyncio.get_running_loop().create_future()
    calls = []
    fail_release = True

    async def key_event(**kwargs):
        """Fail the first release to prove retained state can be retried.

        Args:
            **kwargs: Unchanged native input dispatch parameters.
        """
        nonlocal fail_release
        calls.append(kwargs)
        if fail_release:
            fail_release = False
            raise ConnectionError("Injected first release failure")

    async def stalled(**kwargs):
        """Delay a response beyond cancellation without sending any input.

        Args:
            **kwargs: Original native query or session lookup arguments.

        Returns:
            A delayed native result if the caller has not cancelled its wait.
        """
        entered.set()
        return await asyncio.shield(late)

    dispatch = AsyncMock()
    cdp = SimpleNamespace(
        target_id="tab",
        session_id="input-session",
        cdp_client=SimpleNamespace(
            send=SimpleNamespace(
                Input=SimpleNamespace(
                    dispatchMouseEvent=dispatch, dispatchKeyEvent=key_event
                )
            )
        ),
    )
    browser = SimpleNamespace(
        id="browser",
        agent_focus_target_id="tab",
        get_or_create_cdp_session=AsyncMock(return_value=cdp),
        cdp_client=SimpleNamespace(
            send=SimpleNamespace(
                Target=SimpleNamespace(
                    getTargets=AsyncMock(
                        return_value={"targetInfos": [{"targetId": "tab"}]}
                    )
                )
            )
        ),
    )
    guard = FrameCoordinates(browser)
    guard.target = "tab"
    guard.root_session = "root-session"
    guard.sessions = {"tab": "root-session", "remote": "remote-session"}
    guard.last_frames = [
        {"frame": "tab", "parent": None, "session": "root-session", "focused": True},
        {
            "frame": "remote",
            "parent": "tab",
            "session": "remote-session",
            "viewport": [500, 400, 1],
            "focused": False,
        },
    ]
    guard.last_owners = {
        "remote": {"node": 10, "quad": [600, 100, 1100, 100, 1100, 500, 600, 500]}
    }
    hit = AsyncMock(return_value={"backendNodeId": 20 if fault == "describe" else 10})
    describe = AsyncMock(return_value={"node": {"nodeName": "DIV"}})
    if fault == "hit":
        hit.side_effect = stalled
    if fault == "describe":
        describe.side_effect = stalled
    if fault in {"session", "shared"}:
        browser.get_or_create_cdp_session.side_effect = stalled
    if fault == "shared":

        async def slow_hit(**kwargs):
            """Spend most of the shared deadline before the pool wait.

            Args:
                **kwargs: Original native hit-test arguments.

            Returns:
                The actual known remote frame owner after a bounded delay.
            """
            await asyncio.sleep(0.8)
            return {"backendNodeId": 10}

        hit.side_effect = slow_hit
    guard.client = SimpleNamespace(
        stop=AsyncMock(),
        send=SimpleNamespace(
            DOM=SimpleNamespace(getNodeForLocation=hit, describeNode=describe)
        ),
    )
    observer = BrowserObserver(
        SimpleNamespace(browser_session=browser), coordinate_guard=guard
    )
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
        "position_revision": 0,
        "coordinate_contract_version": 2,
        "coordinate_ready": True,
        "coordinate_token": "captured-token",
        "frame_tree_token": "tree",
    }
    observer.read_state = AsyncMock(return_value=(browser, cdp, state))
    await observer.start()
    bridge = json.loads(observer.discovery.read_text())
    identity = {
        "owner": observer.owner,
        "client_id": "operator",
        "generation": observer.generation,
        "browser_session_id": browser.id,
    }
    tasks = []
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer isolated-test-token"}
        ) as http:
            url = f"http://127.0.0.1:{bridge['port']}/manual"
            async with http.post(
                url, json={**identity, "operation": "acquire"}
            ) as response:
                epoch = (await response.json())["control_epoch"]
            observer._held_keys["ShiftLeft"] = (
                cdp,
                {"key": "Shift", "code": "ShiftLeft"},
            )

            async def command(number, operation, **fields):
                """Use the real HTTP admission path for concurrent queued commands.

                Args:
                    number: Ordered input sequence number.
                    operation: Typed manual operation.
                    **fields: Target and operation arguments.

                Returns:
                    HTTP status and actual acknowledgement.
                """
                async with http.post(
                    url,
                    json={
                        **identity,
                        "control_epoch": epoch,
                        "input_id": number,
                        "operation": operation,
                        **fields,
                    },
                ) as response:
                    return response.status, await response.json()

            started = asyncio.get_running_loop().time()
            click = asyncio.create_task(command(1, "click", target=state, x=800, y=250))
            tasks.append(click)
            await asyncio.wait_for(entered.wait(), 1)
            reset = asyncio.create_task(
                command(2, "pointer", phase="up")
                if fault == "shared"
                else command(2, "reset_keys")
            )
            tasks.append(reset)
            try:
                reset_status, reset_ack = await asyncio.wait_for(
                    asyncio.shield(reset), 2
                )
            except asyncio.TimeoutError:
                pytest.fail(
                    "Native route owns the control lock; queued reset did not complete"
                )
            click_status, click_ack = await asyncio.wait_for(click, 1)
            assert asyncio.get_running_loop().time() - started < 1.6
            assert click_status == reset_status == 200
            assert (
                click_ack["status"] == "uncertain" and reset_ack["status"] == "applied"
            )
            next_input = 3
            if fault == "shared":
                assert observer.control_mode == "paused" and observer._held_keys
                assert len(calls) == 1
                _, retried = await command(3, "reset_keys")
                assert retried["status"] == "applied"
                next_input = 4
            assert len(calls) == 2 and not observer._held_keys
            assert observer.human is not None and observer.control_mode == "human"
            dispatch.assert_not_awaited()
            late.set_result(
                cdp
                if fault in {"session", "shared"}
                else {"backendNodeId": 10, "node": {"nodeName": "DIV"}}
            )
            await asyncio.sleep(0)
            dispatch.assert_not_awaited()
            # Rebuild only the isolated reader; the owned input session is retained.
            observer.coordinate_guard = SimpleNamespace(
                browser=browser,
                route=AsyncMock(return_value=("tab", 80, 80)),
                close=AsyncMock(),
            )
            browser.get_or_create_cdp_session.side_effect = None
            status, recovered = await command(
                next_input, "click", target=state, x=80, y=80
            )
            assert status == 200 and recovered["status"] == "applied"
            assert dispatch.await_count == 2
    finally:
        if not late.done():
            late.set_result(
                cdp
                if fault in {"session", "shared"}
                else {"backendNodeId": 10, "node": {"nodeName": "DIV"}}
            )
        await asyncio.gather(*tasks, return_exceptions=True)
        await observer.close()
