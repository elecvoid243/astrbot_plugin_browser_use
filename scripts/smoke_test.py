"""Exercise the bundled MCP over stdio against a loopback form without an LLM."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

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
            '<p id="clock"></p><div style="height:2400px">Scroll fixture</div>'
            '<script>setInterval(()=>document.querySelector("#clock").textContent=Date.now(),100)</script>'
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
        token = secrets.token_urlsafe(32)
        bridge_file = data_dir / "observer.json"
        environment.update(
            ASTRBOT_BROWSER_BRIDGE_TOKEN=token,
            ASTRBOT_BROWSER_BRIDGE_FILE=str(bridge_file),
        )
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
                if method == "tools/call":
                    # This probe is the trusted host of its disposable runtime.
                    bridge = json.loads(bridge_file.read_text(encoding="utf-8"))
                    params = {
                        **params,
                        "arguments": {
                            **params.get("arguments", {}),
                            "_astrbot_action": {
                                "owner": "offline-smoke",
                                "generation": bridge["generation"],
                                "lease": "offline-smoke",
                                "control_epoch": 0,
                            },
                        },
                    }
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
                bridge = json.loads(bridge_file.read_text(encoding="utf-8"))
                base = f"http://127.0.0.1:{bridge['port']}"
                headers = {"Authorization": f"Bearer {token}"}
                with urlopen(
                    Request(
                        f"{base}/status?generation={bridge['generation']}",
                        headers=headers,
                    ),
                    timeout=15,
                ) as response:
                    observed = json.load(response)
                session_id = observed["browser_session_id"]
                identity = (
                    f"generation={bridge['generation']}&browser_session_id={session_id}"
                )
                assert observed["recent_actions"][-1]["status"] == "succeeded"
                with urlopen(
                    Request(f"{base}/snapshot?{identity}", headers=headers), timeout=15
                ) as response:
                    picture = json.load(response)
                assert len(base64.b64decode(picture["image"])) > 100
                latencies = []
                with urlopen(
                    Request(
                        f"{base}/stream?{identity}&action_cursor=0", headers=headers
                    ),
                    timeout=15,
                ) as response:
                    while len(latencies) < 4:
                        line = response.readline().decode()
                        if line.startswith("data:"):
                            event = json.loads(line[5:])
                            if event["type"] == "frame":
                                assert event["browser_session_id"] == session_id
                                latencies.append(
                                    round(
                                        (time.time() - event["capture_timestamp"])
                                        * 1000,
                                        2,
                                    )
                                )
                request(
                    "tools/call",
                    {"name": "browser_scroll", "arguments": {"direction": "down"}},
                )
                request(
                    "tools/call",
                    {
                        "name": "browser_navigate",
                        "arguments": {
                            "url": f"http://127.0.0.1:{server.server_port}/tab",
                            "new_tab": True,
                        },
                    },
                )
                for operation in ("stop", "close"):
                    with urlopen(
                        Request(
                            f"{base}/control",
                            headers={**headers, "Content-Type": "application/json"},
                            data=json.dumps(
                                {
                                    "generation": bridge["generation"],
                                    "browser_session_id": session_id,
                                    "operation": operation,
                                }
                            ).encode(),
                        ),
                        timeout=30,
                    ) as response:
                        controlled = json.load(response)
                    assert controlled["execution_state"] == (
                        "stopped" if operation == "stop" else "closed"
                    )
                print(
                    f"OBSERVER_SMOKE_METRICS={json.dumps({'frame_delivery_ms': latencies, 'cleanup': controlled['execution_state']})}",
                    flush=True,
                )
                browser_opened = False
                process.stdin.close()
                process.wait(timeout=20)
                assert process.returncode == 0, process.returncode
                assert not bridge_file.exists(), (
                    "Bridge discovery file survived shutdown."
                )
                print(
                    f"MCP smoke test passed: protocol={initialized['protocolVersion']}, {len(tools)} tools, UTF-8 form, screenshot, clean exit.",
                    flush=True,
                )
            finally:
                if process.poll() is None and bridge_file.is_file():
                    try:
                        bridge = json.loads(bridge_file.read_text(encoding="utf-8"))
                        with urlopen(
                            Request(
                                f"http://127.0.0.1:{bridge['port']}/control",
                                headers={
                                    "Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json",
                                },
                                data=json.dumps(
                                    {
                                        "generation": bridge["generation"],
                                        "operation": "disable",
                                    }
                                ).encode(),
                            ),
                            timeout=15,
                        ) as response:
                            response.read()
                    except Exception:
                        pass
                if browser_opened and process.poll() is None:
                    try:
                        request(
                            "tools/call", {"name": "browser_close_all", "arguments": {}}
                        )
                    except Exception:
                        pass
                if process.poll() is None:
                    if not process.stdin.closed:
                        process.stdin.close()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
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
