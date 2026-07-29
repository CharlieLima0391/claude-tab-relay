#!/usr/bin/env python3
"""Relay between a paired browser extension (attached to a real, logged-in tab via
Chrome's `chrome.debugger` CDP API) and a local control API I can drive from Bash.

Two listeners:
  - a WebSocket server (default 0.0.0.0:8822) that paired browser extensions connect to.
    First message on each connection must be `{"type": "auth", "device_name": ..., "token": ...}` -
    validated against the hashed tokens in `state/browser-relay-devices.json`
    (see browser_relay_ctl.py for pairing/revoking). Never trusts a device_name without a
    matching, valid token.
  - a loopback-only HTTP control API (127.0.0.1:8823) that `browser_relay_ctl.py run`
    talks to: POST /command forwards a CDP method+params to a named, currently-connected
    device and waits for its result; GET /devices lists who's currently attached;
    GET /events returns buffered console/network events sent by an attached tab.

Every command sent and every device (dis)connection is appended to
state/browser-relay-audit.log as one JSON line - an engagement record, not just a debug
log.

Nothing here auto-attaches anything - the extension only opens a debugger session when
the user clicks Attach in its own popup, and Chrome's own "this extension is debugging
this tab" banner is visible for the whole time it's attached.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import websockets
from websockets.asyncio.server import serve as ws_serve

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
DEVICES_FILE = STATE_DIR / "browser-relay-devices.json"
AUDIT_LOG = STATE_DIR / "browser-relay-audit.log"

WS_HOST = "0.0.0.0"
WS_PORT = 8822
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 8823
COMMAND_TIMEOUT_SECONDS = 20
EVENT_BUFFER_PER_DEVICE = 500

_loop: asyncio.AbstractEventLoop | None = None
_connections: dict[str, "DeviceConnection"] = {}
_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def audit(event: str, **fields) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": now_iso(), "event": event, **fields}, ensure_ascii=True)
    with AUDIT_LOG.open("a") as f:
        f.write(line + "\n")


def load_devices() -> dict:
    if not DEVICES_FILE.exists():
        return {"devices": {}}
    return json.loads(DEVICES_FILE.read_text())


def token_valid(device_name: str, token: str) -> bool:
    devices = load_devices().get("devices", {})
    entry = devices.get(device_name)
    if not entry:
        return False
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return entry.get("token_hash") == token_hash


class DeviceConnection:
    def __init__(self, device_name: str, ws) -> None:
        self.device_name = device_name
        self.ws = ws
        self.pending: dict[str, asyncio.Future] = {}
        self.events: deque = deque(maxlen=EVENT_BUFFER_PER_DEVICE)

    async def send_command(self, method: str, params: dict) -> dict:
        request_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending[request_id] = fut
        await self.ws.send(json.dumps({"type": "command", "request_id": request_id, "method": method, "params": params}))
        try:
            return await asyncio.wait_for(fut, timeout=COMMAND_TIMEOUT_SECONDS)
        finally:
            self.pending.pop(request_id, None)


async def handle_connection(ws) -> None:
    device_name = None
    try:
        raw_auth = await asyncio.wait_for(ws.recv(), timeout=10)
        auth = json.loads(raw_auth)
        if auth.get("type") != "auth":
            await ws.close(code=4001, reason="expected auth message first")
            return
        device_name = str(auth.get("device_name", "")).strip()
        token = str(auth.get("token", ""))
        if not device_name or not token_valid(device_name, token):
            audit("auth_rejected", device_name=device_name or "(unknown)")
            await ws.close(code=4003, reason="invalid device_name/token")
            return

        with _lock:
            _connections[device_name] = DeviceConnection(device_name, ws)
        audit("device_connected", device_name=device_name)
        await ws.send(json.dumps({"type": "auth_ok"}))

        conn = _connections[device_name]
        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "result":
                fut = conn.pending.get(msg.get("request_id"))
                if fut and not fut.done():
                    fut.set_result(msg.get("result"))
            elif mtype == "event":
                conn.events.append({"ts": now_iso(), "cdp_method": msg.get("cdp_method"), "params": msg.get("params")})
                audit("cdp_event", device_name=device_name, cdp_method=msg.get("cdp_method"))
    except (websockets.ConnectionClosed, asyncio.TimeoutError, json.JSONDecodeError) as exc:
        audit("connection_error", device_name=device_name or "(unknown)", error=str(exc))
    finally:
        if device_name:
            with _lock:
                _connections.pop(device_name, None)
            audit("device_disconnected", device_name=device_name)


class ControlHandler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/devices":
            with _lock:
                names = list(_connections.keys())
            self._json(200, {"devices": names})
            return
        if parsed.path == "/events":
            qs = parse_qs(parsed.query)
            device_name = (qs.get("device") or [""])[0]
            with _lock:
                conn = _connections.get(device_name)
            if not conn:
                self._json(404, {"error": f"device not connected: {device_name}"})
                return
            self._json(200, {"events": list(conn.events)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/command":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return

        device_name = body.get("device_name")
        method = body.get("method")
        params = body.get("params", {})
        with _lock:
            conn = _connections.get(device_name)
        if not conn:
            self._json(404, {"error": f"device not connected: {device_name}"})
            return
        if not method:
            self._json(400, {"error": "missing method"})
            return

        assert _loop is not None
        future = asyncio.run_coroutine_threadsafe(conn.send_command(method, params), _loop)
        try:
            result = future.result(timeout=COMMAND_TIMEOUT_SECONDS + 2)
        except Exception as exc:  # noqa: BLE001
            audit("command_failed", device_name=device_name, method=method, error=str(exc))
            self._json(504, {"error": str(exc)})
            return
        audit("command_ok", device_name=device_name, method=method)
        self._json(200, {"result": result})

    def log_message(self, fmt: str, *args) -> None:  # quiet, journald already captures stdout
        pass


def run_control_server() -> None:
    httpd = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), ControlHandler)
    httpd.serve_forever()


async def run_ws_server() -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    async with ws_serve(handle_connection, WS_HOST, WS_PORT):
        print(f"browser_relay: websocket listening on {WS_HOST}:{WS_PORT}, control API on {CONTROL_HOST}:{CONTROL_PORT}")
        await asyncio.Future()  # run forever


def main() -> None:
    control_thread = threading.Thread(target=run_control_server, daemon=True)
    control_thread.start()
    asyncio.run(run_ws_server())


if __name__ == "__main__":
    main()
