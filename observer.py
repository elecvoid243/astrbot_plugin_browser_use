"""Read-only observation of the MCP server's actual Chromium session."""

from __future__ import annotations

import base64
import hmac
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from PIL import Image


class BrowserObserver:
    """Expose a credential-protected loopback bridge in the private runtime."""

    def __init__(self, server):
        """Attach to the existing MCP server without starting another browser.

        Args:
            server: Upstream BrowserUseServer that executes Agent tools.
        """
        self.server = server
        self.generation = uuid4().hex
        self.sequence = 0
        self.token = os.environ["ASTRBOT_BROWSER_BRIDGE_TOKEN"]
        self.discovery = Path(os.environ["ASTRBOT_BROWSER_BRIDGE_FILE"])
        self.runner = None

    async def start(self) -> None:
        """Bind an ephemeral loopback port and publish its discovery record.

        Raises:
            OSError: The socket or discovery file cannot be created.
        """
        app = web.Application()
        app.router.add_get("/status", self.observe)
        app.router.add_get("/snapshot", self.observe)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        temporary = self.discovery.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"port": port, "generation": self.generation}), encoding="utf-8"
        )
        temporary.replace(self.discovery)

    async def observe(self, request: web.Request) -> web.Response:
        """Read metadata or capture the focused target without rebuilding the DOM.

        Args:
            request: Authenticated internal status or snapshot request.

        Returns:
            Bounded image and identity metadata, or an explicit HTTP error.
        """
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), f"Bearer {self.token}"
        ):
            raise web.HTTPUnauthorized()
        if request.query.get("generation") != self.generation:
            raise web.HTTPConflict(text="Browser generation expired.")
        browser = self.server.browser_session
        state = {
            "generation": self.generation,
            "browser_session_id": browser.id if browser else None,
            "tab_id": None,
            "sequence": self.sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "available": browser is not None,
            "url": "",
            "title": "",
            "width": 0,
            "height": 0,
        }
        if browser is None or not browser.agent_focus_target_id:
            return web.json_response(state, headers={"Cache-Control": "no-store"})
        target = browser.agent_focus_target_id
        cdp = await browser.get_or_create_cdp_session(target_id=target, focus=False)
        info = await cdp.cdp_client.send.Runtime.evaluate(
            params={
                "expression": "JSON.stringify({url:location.href,title:document.title})",
                "returnByValue": True,
            },
            session_id=cdp.session_id,
        )
        state.update(json.loads(info["result"]["value"]))
        state["tab_id"] = target
        if request.path == "/snapshot":
            shot = await cdp.cdp_client.send.Page.captureScreenshot(
                params={
                    "format": "jpeg",
                    "quality": 65,
                    "captureBeyondViewport": False,
                },
                session_id=cdp.session_id,
            )
            after = await cdp.cdp_client.send.Runtime.evaluate(
                params={
                    "expression": "JSON.stringify({url:location.href,title:document.title})",
                    "returnByValue": True,
                },
                session_id=cdp.session_id,
            )
            if after["result"]["value"] != info["result"]["value"]:
                raise web.HTTPConflict(text="Page changed; refresh observation.")
            raw = base64.b64decode(shot["data"])
            with Image.open(io.BytesIO(raw)) as picture:
                state["width"], state["height"] = picture.size
            self.sequence += 1
            state.update(
                sequence=self.sequence, image=shot["data"], mime_type="image/jpeg"
            )
        # A concurrent tab switch must never label an old image as the new page.
        if (
            self.server.browser_session is not browser
            or browser.agent_focus_target_id != target
        ):
            raise web.HTTPConflict(text="Browser target changed; refresh observation.")
        return web.json_response(state, headers={"Cache-Control": "no-store"})

    async def close(self) -> None:
        """Release the bridge and remove only this process's discovery record."""
        if self.runner is not None:
            await self.runner.cleanup()
        if self.discovery.exists():
            record = json.loads(self.discovery.read_text(encoding="utf-8"))
            if record.get("generation") == self.generation:
                self.discovery.unlink()
