"""Run the pinned upstream MCP server with browser cleanup on stdio disconnect."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

from browser_use.mcp.server import BrowserUseServer

# Windows embedded Python does not add the script directory to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from observer import BrowserObserver  # noqa: E402


class LocalBrowserServer(BrowserUseServer):
    """Give each browser lifetime private storage before admitting a new owner."""

    async def _execute_tool(self, tool_name, arguments):
        """Track actual execution with safe summaries and exactly one terminal state.

        Args:
            tool_name: Discovered upstream browser tool name.
            arguments: Tool arguments plus host-owned invocation metadata.

        Returns:
            The upstream result; observations never enter tool results.

        Raises:
            Exception: The action fails, times out, or returns an upstream error.
            CancelledError: The action is cancelled by the host.
        """
        metadata = arguments.pop("_astrbot_action", {})
        observer = getattr(self, "observer", None)
        if observer is None:
            return await super()._execute_tool(tool_name, arguments)
        if (
            observer.execution_state in {"stopped", "disabled"}
            or metadata.get("lease") in observer.revoked_leases
        ):
            raise RuntimeError(
                "Browser execution stopped; close the session before restarting."
            )
        observer.execution_state = "active"
        action_id = metadata.get("action_id") or uuid4().hex
        if action_id in observer.cancelled_actions:
            raise RuntimeError("Browser action was cancelled before execution.")
        if observer.current_action is not None:
            raise RuntimeError("Another browser action is still executing.")
        kind = {
            "browser_navigate": "navigate",
            "browser_go_back": "navigate",
            "browser_click": "click",
            "browser_type": "type",
            "browser_scroll": "scroll",
            "browser_switch_tab": "tab",
            "browser_close_tab": "tab",
            "browser_close_all": "close",
            "browser_close_session": "close",
            "browser_close": "close",
        }.get(tool_name, "inspect")
        browser = self.browser_session
        action = {
            "action_id": action_id,
            "invocation_id": metadata.get("invocation_id"),
            "generation": observer.generation,
            "tab_id": browser.agent_focus_target_id if browser else None,
            "kind": kind,
            "status": "running",
            "started_at": time.time(),
            "browser_session_id": browser.id if browser else None,
        }
        observer.current_action = action
        observer.action_done.clear()
        execution = None
        observer.highlight = None
        status = "failed"
        try:
            await observer.action_changed()
            if action_id in observer.cancelled_actions:
                status = "cancelled"
                raise RuntimeError("Browser action cancelled before execution.")
            execution = asyncio.create_task(
                self._execute_browser_action(tool_name, arguments)
            )
            observer.action_task = execution
            result = await asyncio.wait_for(
                execution,
                timeout=float(metadata.get("timeout", 120)),
            )
            # Upstream sometimes returns an error as ordinary successful text.
            if isinstance(result, str) and (
                result.lower().startswith(("error", "failed"))
                or (
                    result.startswith(("Element with index", "Tab ", "Session "))
                    and "not found" in result
                )
            ):
                raise RuntimeError("Browser action failed; check the page and target.")
            status = "succeeded"
            return result
        except asyncio.TimeoutError:
            status = "timed_out"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            if action_id in observer.cancelled_actions:
                # Cancelling an SDK request handler can tear down MCP's task group.
                # Internal reconciliation cancels only its child execution task.
                raise RuntimeError("Browser action cancelled.") from None
            raise
        finally:
            try:
                # A host reconciliation can have finalized this action already.
                if observer.current_action is action:
                    observer.current_action = None
                    if action["browser_session_id"] is None and self.browser_session:
                        action["browser_session_id"] = self.browser_session.id
                    if status != "succeeded":
                        observer.highlight = None
                    observer.recent_actions.append(
                        {**action, "status": status, "finished_at": time.time()}
                    )
                    await observer.action_changed(
                        closed_session_id=action["browser_session_id"]
                        if self.browser_session is None
                        else None
                    )
            finally:
                if observer.action_task is execution:
                    observer.action_task = None
                observer.action_done.set()

    async def _execute_browser_action(self, tool_name, arguments):
        """Require event handler success for navigation, scrolling and tab actions.

        Upstream awaits event completion without inspecting handler failures.

        Args:
            tool_name: Actual MCP tool name.
            arguments: Browser operation arguments only.

        Returns:
            Safe result text or an unmodified read-only upstream result.
        """
        if tool_name not in {
            "browser_navigate",
            "browser_go_back",
            "browser_scroll",
            "browser_switch_tab",
            "browser_close_tab",
            "browser_close",
        }:
            return await super()._execute_tool(tool_name, arguments)
        from browser_use.browser.events import (
            BrowserStopEvent,
            CloseTabEvent,
            GoBackEvent,
            NavigateToUrlEvent,
            ScrollEvent,
            SwitchTabEvent,
        )

        if self.browser_session is None:
            await self._init_browser_session()
        browser = self.browser_session
        self._update_session_activity(browser.id)
        if tool_name == "browser_navigate":
            event = NavigateToUrlEvent(
                url=arguments["url"], new_tab=arguments.get("new_tab", False)
            )
        elif tool_name == "browser_go_back":
            event = GoBackEvent()
        elif tool_name == "browser_scroll":
            event = ScrollEvent(
                direction=arguments.get("direction", "down"), amount=500
            )
        elif tool_name == "browser_close":
            event = BrowserStopEvent()
        else:
            target = await browser.get_target_id_from_tab_id(arguments["tab_id"])
            event = (
                SwitchTabEvent(target_id=target)
                if tool_name == "browser_switch_tab"
                else CloseTabEvent(target_id=target)
            )
        dispatched = browser.event_bus.dispatch(event)
        await dispatched
        await dispatched.event_result(raise_if_any=True, raise_if_none=False)
        if tool_name == "browser_close":
            self.browser_session = None
            self.tools = None
        return "Browser action completed."

    async def _click(
        self, index=None, coordinate_x=None, coordinate_y=None, new_tab=False
    ):
        """Execute upstream clicks and retain only their actual point metadata.

        Args:
            index: Cached DOM target index.
            coordinate_x: Viewport x coordinate.
            coordinate_y: Viewport y coordinate.
            new_tab: Use upstream link-opening behavior when requested.

        Returns:
            Safe click result text.
        """
        if not self.browser_session or getattr(self, "observer", None) is None:
            return await super()._click(index, coordinate_x, coordinate_y, new_tab)
        from browser_use.browser.events import ClickCoordinateEvent, ClickElementEvent

        browser = self.browser_session
        before = None
        with contextlib.suppress(Exception):
            _, _, before = await self.observer.read_state()
        self._update_session_activity(browser.id)
        if coordinate_x is not None and coordinate_y is not None:
            event = ClickCoordinateEvent(
                coordinate_x=coordinate_x, coordinate_y=coordinate_y
            )
        else:
            node = await browser.get_dom_element_by_index(index)
            if node is None:
                raise ValueError("Browser click target not found.")
            if new_tab and node.attributes.get("href"):
                from urllib.parse import urljoin

                return await self._execute_browser_action(
                    "browser_navigate",
                    {
                        "url": urljoin(
                            await browser.get_current_page_url(),
                            node.attributes["href"],
                        ),
                        "new_tab": True,
                    },
                )
            event = ClickElementEvent(node=node)
        dispatched = browser.event_bus.dispatch(event)
        await dispatched
        details = await dispatched.event_result(raise_if_any=True)
        if before is not None:
            await self._mark_target(before, details, "click")
        return "Clicked browser target."

    async def _type_text(self, index, text):
        """Hide all input in summaries and upstream action logging.

        Args:
            index: Cached DOM target index.
            text: Input delivered to Chromium only.

        Returns:
            A result that never repeats the input.
        """
        if getattr(self, "observer", None) is None:
            return await super()._type_text(index, text)
        from browser_use.browser.events import TypeTextEvent

        if not self.browser_session:
            raise RuntimeError("No browser session active.")
        browser = self.browser_session
        before = None
        with contextlib.suppress(Exception):
            _, _, before = await self.observer.read_state()
        node = await browser.get_dom_element_by_index(index)
        if node is None:
            raise ValueError("Browser input target not found.")
        dispatched = browser.event_bus.dispatch(
            TypeTextEvent(
                node=node, text=text, is_sensitive=True, sensitive_key_name="input"
            )
        )
        await dispatched
        details = await dispatched.event_result(raise_if_any=True)
        if before is not None:
            await self._mark_target(before, details, "input")
        return "Entered hidden text into browser target."

    async def _mark_target(self, before, details, prefix):
        """Bind actual action coordinates to the unchanged document and viewport.

        Args:
            before: Page identity before execution.
            details: Actual upstream execution metadata; values are not copied.
            prefix: Coordinate keys used by the upstream action.
        """
        if not isinstance(details, dict):
            return
        observer = self.observer
        with contextlib.suppress(Exception):
            _, _, state = await observer.read_state()
            if any(
                before.get(key) != state.get(key)
                for key in ("browser_session_id", "tab_id", "document_id", "url")
            ):
                return
            x, y = details.get(f"{prefix}_x"), details.get(f"{prefix}_y")
            width, height = state["viewport_width"], state["viewport_height"]
            if not all(
                isinstance(value, (int, float)) and math.isfinite(value)
                for value in (x, y, width, height)
            ) or not (0 <= x < width and 0 <= y < height):
                return
            if observer.current_action is None:
                return
            observer.highlight = {
                key: state[key]
                for key in (
                    "generation",
                    "browser_session_id",
                    "tab_id",
                    "document_id",
                    "url",
                    "viewport_width",
                    "viewport_height",
                    "scroll_x",
                    "scroll_y",
                )
            }
            observer.highlight.update(
                action_id=observer.current_action["action_id"],
                x=x,
                y=y,
                expires_at=time.time() + 2,
            )

    async def _init_browser_session(self, allowed_domains=None, **kwargs):
        """Create a fresh profile without touching previously saved user data.

        Args:
            allowed_domains: Upstream navigation allowlist.
            **kwargs: Upstream profile overrides.
        """
        if self.browser_session is None:
            root = Path(os.environ["BROWSER_USE_CONFIG_DIR"]) / "sessions" / uuid4().hex
            for name in ("profile", "downloads", "workspace"):
                (root / name).mkdir(parents=True, exist_ok=True)
            kwargs.update(
                user_data_dir=str(root / "profile"),
                downloads_path=str(root / "downloads"),
                file_system_path=str(root / "workspace"),
            )
        await super()._init_browser_session(allowed_domains=allowed_domains, **kwargs)

    async def _close_all_sessions(self):
        """Refuse an ownership handoff while upstream still tracks a live session.

        Returns:
            The upstream close result after every tracked session is released.

        Raises:
            RuntimeError: A failed close left a previous owner's session behind.
        """
        browser = self.browser_session
        tools = self.tools
        result = await super()._close_all_sessions()
        if self.active_sessions:
            # Upstream clears the current pointer even when a close failed.
            # Retain the failed owned lifetime so authenticated cleanup can retry.
            if browser is not None and browser.id in self.active_sessions:
                self.browser_session = browser
                self.tools = tools
            raise RuntimeError("Browser cleanup failed; ownership remains reserved.")
        return result


async def main() -> None:
    """Serve upstream browser tools and release browsers when AstrBot disconnects.

    Raises:
        Exception: MCP serving or browser cleanup fails.
    """
    server = LocalBrowserServer()
    observer = (
        BrowserObserver(server)
        if os.environ.get("ASTRBOT_BROWSER_BRIDGE_TOKEN")
        else None
    )
    server.observer = observer
    try:
        if observer is not None:
            await observer.start()
        await server.run()
    finally:
        try:
            if observer is not None:
                await observer.close()
        finally:
            # The upstream entry point does not close active sessions on stdin EOF.
            await server._close_all_sessions()


if __name__ == "__main__":
    asyncio.run(main())
