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

from observer import BrowserObserver


async def test_delayed_cdp_frame_is_acknowledged_but_not_published(
    monkeypatch, tmp_path
):
    """Reject an old target's frame while keeping its CDP acknowledgement alive.

    Args:
        monkeypatch: Isolated bridge credentials.
        tmp_path: Disposable discovery directory.
    """
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_TOKEN", "secret")
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_FILE", str(tmp_path / "bridge.json"))
    browser = SimpleNamespace(id="browser", agent_focus_target_id="current")
    observer = BrowserObserver(SimpleNamespace(browser_session=browser))
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
    }
    observer.read_state = AsyncMock(return_value=(browser, cdp, state))
    queue = asyncio.Queue(maxsize=1)
    observer.subscribers.add(queue)
    capture = asyncio.create_task(observer.capture())
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert (await queue.get())["type"] == "state"
        event = {
            "sessionId": 10,
            "data": "stale-image",
            "metadata": {"timestamp": time.time()},
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
        assert ack.await_count == 2
    finally:
        capture.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await capture


def test_slow_subscriber_keeps_only_latest_event(monkeypatch, tmp_path):
    """A blocked subscriber cannot retain obsolete frames or block other viewers.

    Args:
        monkeypatch: Isolated bridge credentials.
        tmp_path: Disposable discovery directory.
    """
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_TOKEN", "secret")
    monkeypatch.setenv("ASTRBOT_BROWSER_BRIDGE_FILE", str(tmp_path / "bridge.json"))
    observer = BrowserObserver(SimpleNamespace(browser_session=None))
    slow = asyncio.Queue(maxsize=1)
    fast = asyncio.Queue(maxsize=1)
    observer.subscribers.update((slow, fast))
    for index in range(1000):
        event = observer.publish(
            "frame", {"browser_session_id": "browser", "image": str(index)}
        )
        assert fast.get_nowait() == event
    assert slow.qsize() == 1
    assert slow.get_nowait()["sequence"] == 1000


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
