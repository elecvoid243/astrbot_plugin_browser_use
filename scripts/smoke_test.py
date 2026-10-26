"""Exercise the bundled MCP over stdio against a loopback form without an LLM."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import build_mcp_config  # noqa: E402


class LocalPage(BaseHTTPRequestHandler):
    """Serve the local fixture used to verify DOM navigation and form submission."""

    def do_GET(self) -> None:
        """Return a form or its submitted UTF-8 value."""
        value = parse_qs(urlsplit(self.path).query).get("value", [""])[0]
        content = (
            '<!doctype html><meta charset="utf-8"><title>Offline MCP test</title>'
            '<form action="/result"><input name="value" placeholder="offline-value">'
            '<button type="submit">Submit</button></form>'
            f'<p id="result">{html.escape(value)}</p>'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args) -> None:
        """Keep HTTP diagnostics out of the probe output.

        Args:
            format: Unused standard HTTP log template.
            *args: Unused standard HTTP log values.
        """


def main() -> None:
    """Verify handshake, tool discovery, browser actions, images, and shutdown.

    Raises:
        Exception: The bundled server or any browser operation fails validation.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plugin-dir", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="browser-use-smoke-") as temporary:
        data_dir = Path(temporary)
        config = build_mcp_config(
            args.plugin_dir, data_dir, {"allowed_domains": ["http://127.0.0.1"]}
        )
        environment = {**os.environ, **config["env"]}
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalPage)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with (data_dir / "stderr.log").open("w", encoding="utf-8") as error_log:
            process = subprocess.Popen(
                [config["command"], *config["args"]],
                cwd=config["cwd"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=error_log,
                text=True,
                encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32"
                else 0,
            )
            responses: queue.Queue = queue.Queue()

            def read_responses() -> None:
                """Read JSON-RPC lines without blocking the timeout checks."""
                for line in process.stdout:
                    responses.put(line)
                responses.put(None)

            reader = threading.Thread(target=read_responses, daemon=True)
            reader.start()
            request_id = 0

            def request(method: str, params: dict) -> dict:
                """Send an MCP request and validate its JSON-RPC response.

                Args:
                    method: MCP method name.
                    params: Method-specific JSON parameters.

                Returns:
                    The matching response's result.

                Raises:
                    RuntimeError: Transport, protocol, or tool execution fails.
                """
                nonlocal request_id
                request_id += 1
                process.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": method,
                            "params": params,
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                deadline = time.monotonic() + 90
                while True:
                    try:
                        line = responses.get(
                            timeout=max(0, deadline - time.monotonic())
                        )
                    except queue.Empty as exc:
                        diagnostics = (data_dir / "stderr.log").read_text(
                            encoding="utf-8"
                        )
                        raise RuntimeError(
                            f"MCP timeout in {method}: {diagnostics[-8000:]}"
                        ) from exc
                    if line is None:
                        raise RuntimeError(
                            f"MCP exited: {(data_dir / 'stderr.log').read_text(encoding='utf-8')[-8000:]}"
                        )
                    response = json.loads(line)
                    if response.get("id") != request_id:
                        continue
                    if "error" in response:
                        raise RuntimeError(str(response["error"]))
                    result = response["result"]
                    if result.get("isError"):
                        raise RuntimeError(str(result))
                    return result

            browser_opened = False
            try:
                initialized = request(
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "astrbot-browser-use-probe",
                            "version": "0.1.0",
                        },
                    },
                )
                process.stdin.write(
                    '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                )
                process.stdin.flush()
                tools = request("tools/list", {})["tools"]
                assert {
                    "browser_navigate",
                    "browser_get_state",
                    "browser_close_all",
                } <= {tool["name"] for tool in tools}
                browser_opened = True
                request(
                    "tools/call",
                    {
                        "name": "browser_navigate",
                        "arguments": {"url": f"http://127.0.0.1:{server.server_port}"},
                    },
                )
                state = request(
                    "tools/call", {"name": "browser_get_state", "arguments": {}}
                )
                elements = json.loads(state["content"][0]["text"])[
                    "interactive_elements"
                ]
                field = next(
                    element
                    for element in elements
                    if element.get("placeholder") == "offline-value"
                )
                request(
                    "tools/call",
                    {
                        "name": "browser_type",
                        "arguments": {"index": field["index"], "text": "离线验证成功"},
                    },
                )
                state = request(
                    "tools/call", {"name": "browser_get_state", "arguments": {}}
                )
                elements = json.loads(state["content"][0]["text"])[
                    "interactive_elements"
                ]
                button = next(
                    element for element in elements if element["tag"] == "button"
                )
                request(
                    "tools/call",
                    {"name": "browser_click", "arguments": {"index": button["index"]}},
                )
                result = request(
                    "tools/call",
                    {"name": "browser_get_html", "arguments": {"selector": "#result"}},
                )
                assert "离线验证成功" in str(result)
                screenshot = request(
                    "tools/call", {"name": "browser_screenshot", "arguments": {}}
                )
                assert any(
                    item["type"] == "image"
                    and len(base64.b64decode(item["data"])) > 100
                    for item in screenshot["content"]
                )
                request("tools/call", {"name": "browser_close_all", "arguments": {}})
                browser_opened = False
                process.stdin.close()
                process.wait(timeout=20)
                assert process.returncode == 0, process.returncode
                print(
                    f"MCP smoke test passed: protocol={initialized['protocolVersion']}, {len(tools)} tools, UTF-8 form, screenshot, clean exit.",
                    flush=True,
                )
            finally:
                if browser_opened and process.poll() is None:
                    try:
                        request(
                            "tools/call", {"name": "browser_close_all", "arguments": {}}
                        )
                    except Exception:
                        pass
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                process.stdout.close()
                if not process.stdin.closed:
                    process.stdin.close()
                reader.join(timeout=5)
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    main()
