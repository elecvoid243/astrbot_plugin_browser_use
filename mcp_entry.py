"""Run the pinned upstream MCP server with browser cleanup on stdio disconnect."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

from browser_use.mcp.server import BrowserUseServer

# Windows embedded Python does not add the script directory to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from observer import BrowserObserver  # noqa: E402


class LocalBrowserServer(BrowserUseServer):
    """Give each browser lifetime private storage before admitting a new owner."""

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
        result = await super()._close_all_sessions()
        if self.active_sessions:
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
