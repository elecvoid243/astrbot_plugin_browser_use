"""Run the pinned upstream MCP server with browser cleanup on stdio disconnect."""

from __future__ import annotations

import asyncio

from browser_use.mcp.server import BrowserUseServer


async def main() -> None:
    """Serve upstream browser tools and release browsers when AstrBot disconnects.

    Raises:
        Exception: MCP serving or browser cleanup fails.
    """
    server = BrowserUseServer()
    try:
        await server.run()
    finally:
        # The upstream entry point does not close active sessions on stdin EOF.
        await server._close_all_sessions()


if __name__ == "__main__":
    asyncio.run(main())
