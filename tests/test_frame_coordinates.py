"""Protect releases when the server replaces its current browser reference."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
