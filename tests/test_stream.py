"""Check bounded observation delivery and subscription cleanup races."""

import asyncio
import base64
import contextlib
import io
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from PIL import Image

from observation_queue import ObservationQueue
from observer import BrowserObserver


@pytest.mark.parametrize(
    "change",
    [
        {"scroll_y": 500},
        {"scroll_x": 100},
        {"viewport_width": 1024},
        {"viewport_height": 600},
    ],
)
@pytest.mark.parametrize("execution_state", ["active", "stopped"])
async def test_delayed_cdp_frame_is_acknowledged_but_not_published(
    monkeypatch, tmp_path, change, execution_state
):
    """Keep live and idle frames on one high-resolution capture path.

    Args:
        monkeypatch: Isolated bridge credentials.
        tmp_path: Disposable discovery directory.
        change: Coordinate-space change after the first accepted frame.
        execution_state: Observation continues after Agent execution stops.
    """
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_TOKEN", "secret")
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_FILE", str(tmp_path / "bridge.json"))
    browser = SimpleNamespace(id="browser", agent_focus_target_id="current")
    observer = BrowserObserver(SimpleNamespace(browser_session=browser))
    observer.execution_state = execution_state
    callbacks = []
    started = asyncio.Event()
    acked = asyncio.Event()
    ack = AsyncMock(side_effect=lambda **kwargs: acked.set())
    client = SimpleNamespace(
        register=SimpleNamespace(
            Page=SimpleNamespace(screencastFrame=callbacks.append)
        ),
        send=SimpleNamespace(
            Page=SimpleNamespace(
                startScreencast=AsyncMock(side_effect=lambda **kwargs: started.set()),
                stopScreencast=AsyncMock(),
                screencastFrameAck=ack,
            )
        ),
        _event_registry=SimpleNamespace(unregister=lambda method: None),
    )
    cdp = SimpleNamespace(cdp_client=client, session_id="current-session")
    state = {
        "generation": observer.generation,
        "browser_session_id": "browser",
        "tab_id": "current",
        "url": "local",
        "title": "Current",
        "viewport_width": 1280,
        "viewport_height": 720,
        "scroll_x": 0,
        "scroll_y": 0,
    }
    observer.read_state = AsyncMock(return_value=(browser, cdp, state))
    image = io.BytesIO()
    Image.new("RGB", (1920, 1080)).save(image, format="JPEG")
    high_resolution = base64.b64encode(image.getvalue()).decode()
    observer.capture_snapshot = AsyncMock(
        side_effect=lambda browser, cdp, state: {
            **state,
            "image": high_resolution,
            "mime_type": "image/jpeg",
            "width": 1920,
            "height": 1080,
            "capture_timestamp": time.time(),
        }
    )
    queue = ObservationQueue()
    observer.subscribers.add(queue)
    capture = asyncio.create_task(observer.capture())
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert (await queue.get())["type"] == "state"
        event = {
            "sessionId": 10,
            "data": "stale-image",
            "metadata": {
                "timestamp": time.time(),
                "deviceWidth": 1280,
                "deviceHeight": 720,
                "scrollOffsetX": 0,
                "scrollOffsetY": 0,
                "pageScaleFactor": 1,
                "offsetTop": 0,
            },
        }
        callbacks[0](event, "old-session")
        await asyncio.wait_for(acked.wait(), 2)
        assert ack.call_args.kwargs["session_id"] == "old-session"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), 0.3)
        image = io.BytesIO()
        Image.new("RGB", (1280, 720)).save(image, format="JPEG")
        event.update(sessionId=11, data=base64.b64encode(image.getvalue()).decode())
        callbacks[0](event, "current-session")
        frame = await asyncio.wait_for(queue.get(), 2)
        assert frame["type"] == "frame" and frame["tab_id"] == "current"
        assert frame["image"] == high_resolution
        assert (frame["width"], frame["height"]) == (1920, 1080)
        assert ack.await_count == 2
        # Sampling new metadata cannot relabel a delayed old-space CDP image.
        state.update(change)
        event.update(sessionId=12)
        event["metadata"]["timestamp"] = time.time()
        callbacks[0](event, "current-session")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), 0.3)
        assert ack.await_count == 3
        for frame_key, state_key in (
            ("deviceWidth", "viewport_width"),
            ("deviceHeight", "viewport_height"),
            ("scrollOffsetX", "scroll_x"),
            ("scrollOffsetY", "scroll_y"),
        ):
            event["metadata"][frame_key] = state[state_key]
        image = io.BytesIO()
        Image.new("RGB", (state["viewport_width"], state["viewport_height"])).save(
            image, format="JPEG"
        )
        event.update(sessionId=13, data=base64.b64encode(image.getvalue()).decode())
        event["metadata"]["timestamp"] = time.time()
        callbacks[0](event, "current-session")
        matched = await asyncio.wait_for(queue.get(), 2)
        assert matched["type"] == "frame"
        assert all(matched[key] == value for key, value in change.items())
        assert matched["image"] == high_resolution
        # Screenshot-induced duplicate casts cannot create a capture feedback loop.
        capture_count = observer.capture_snapshot.await_count
        event.update(sessionId=14)
        event["metadata"]["timestamp"] = time.time()
        callbacks[0](event, "current-session")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), 0.3)
        assert observer.capture_snapshot.await_count == capture_count
        # Static-page fallback must use the same rasterization as live updates.
        idle = await asyncio.wait_for(queue.get(), 2)
        assert idle["type"] == "frame"
        assert idle["image"] == matched["image"]
        assert (idle["width"], idle["height"]) == (matched["width"], matched["height"])
        assert idle["execution_state"] == execution_state
    finally:
        capture.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await capture


async def test_slow_subscriber_keeps_only_latest_event(monkeypatch, tmp_path):
    """A blocked subscriber cannot retain obsolete frames or block other viewers.

    Args:
        monkeypatch: Isolated bridge credentials.
        tmp_path: Disposable discovery directory.
    """
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_TOKEN", "secret")
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_FILE", str(tmp_path / "bridge.json"))
    observer = BrowserObserver(SimpleNamespace(browser_session=None))
    slow = ObservationQueue()
    fast = ObservationQueue()
    observer.subscribers.update((slow, fast))
    for index in range(1000):
        event = observer.publish(
            "frame", {"browser_session_id": "browser", "image": str(index)}
        )
        assert await fast.get() == event
    assert not slow.actions
    assert (await slow.get())["sequence"] == 1000


@pytest.mark.parametrize("close_bridge", [False, True])
async def test_disconnect_releases_capture_without_closing_browser(
    monkeypatch, tmp_path, close_bridge
):
    """Last-viewer disconnect and bridge shutdown both release capture promptly.

    Args:
        monkeypatch: Isolated bridge credentials and capture adapter.
        tmp_path: Disposable discovery directory.
        close_bridge: Close the bridge while the viewer is still connected.
    """
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_TOKEN", "secret")
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_FILE", str(tmp_path / "bridge.json"))
    browser = SimpleNamespace(id="browser")
    server = SimpleNamespace(browser_session=browser)
    observer = BrowserObserver(server)
    state = {"browser_session_id": browser.id, "tab_id": "tab", "url": "local"}
    observer.read_state = AsyncMock(return_value=(browser, None, state))
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def capture():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    observer.capture = capture
    await observer.start()
    import json

    port = json.loads(observer.discovery.read_text())["port"]
    try:
        async with aiohttp.ClientSession() as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/stream",
                params={
                    "generation": observer.generation,
                    "browser_session_id": browser.id,
                },
                headers={"Authorization": "Bearer secret"},
            )
            assert response.status == 200
            await asyncio.wait_for(started.wait(), 2)
            if close_bridge:
                await asyncio.wait_for(observer.close(), 4)
            else:
                response.close()
            await asyncio.wait_for(stopped.wait(), 4)
            assert server.browser_session is browser
            if not close_bridge:
                # Capture cancellation happens after removing the last subscriber.
                assert not observer.subscribers
            response.close()
    finally:
        await observer.close()
