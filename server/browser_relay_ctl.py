#!/usr/bin/env python3
"""Pair/revoke devices for browser_relay.py, and drive an attached tab from the CLI.

  pair --device-name "my-laptop-chrome"
      Generates a token, stores only its hash, prints the raw token once - paste it
      into that device's extension popup. Re-pairing an existing device name replaces
      its token (the old one stops working immediately).

  revoke --device-name "my-laptop-chrome"
      Removes a device's access immediately - any live connection is not force-closed,
      but future auth attempts with the old token will fail.

  list-devices
      Shows every paired device name and which ones are currently connected.

  run --device-name "..." --script steps.json
      Same JSON step shape as scripts/browser_agent.py, but posted to the relay's local
      control API and executed against a REAL attached tab via CDP, not a fresh headless
      one. Steps: {"action": "eval", "expression": "..."} -> Runtime.evaluate,
      {"action": "click", "x": .., "y": ..} -> Input dispatch, {"action": "screenshot",
      "path": "..."} -> Page.captureScreenshot. Requires the target device to already be
      connected (paired AND attached via the popup) - use list-devices to check first.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEVICES_FILE = REPO_ROOT / "state" / "browser-relay-devices.json"
# Must match RELAY_CONTROL_HOST/RELAY_CONTROL_PORT if you changed those for
# server/browser_relay.py - this CLI only ever needs to reach it on localhost.
CONTROL_BASE = "http://{}:{}".format(
    os.environ.get("RELAY_CONTROL_HOST", "127.0.0.1"),
    os.environ.get("RELAY_CONTROL_PORT", "8823"),
)


def load_devices() -> dict:
    if not DEVICES_FILE.exists():
        return {"devices": {}}
    return json.loads(DEVICES_FILE.read_text())


def save_devices(data: dict) -> None:
    DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEVICES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(DEVICES_FILE)
    DEVICES_FILE.chmod(0o600)


def cmd_pair(device_name: str) -> None:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    data = load_devices()
    data.setdefault("devices", {})[device_name] = {
        "token_hash": token_hash,
        "paired_at": int(time.time()),
    }
    save_devices(data)
    print(f"Paired '{device_name}'. Paste this token into the extension popup on that device:")
    print(token)
    print("(shown once - not recoverable from disk, only re-pairing generates a new one)")


def cmd_revoke(device_name: str) -> None:
    data = load_devices()
    if device_name in data.get("devices", {}):
        del data["devices"][device_name]
        save_devices(data)
        print(f"Revoked '{device_name}'.")
    else:
        print(f"No such paired device: {device_name}", file=sys.stderr)
        sys.exit(1)


def _control_get(path: str) -> dict:
    with urllib.request.urlopen(f"{CONTROL_BASE}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def _control_post(path: str, payload: dict, timeout: int = 25) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{CONTROL_BASE}{path}", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def cmd_list_devices() -> None:
    paired = load_devices().get("devices", {})
    try:
        connected = set(_control_get("/devices").get("devices", []))
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach relay control API ({exc}) - is the relay server running?", file=sys.stderr)
        connected = set()
    for name in paired:
        status = "connected" if name in connected else "paired, not attached"
        print(f"{name}: {status}")


def cmd_run(device_name: str, script_path: str) -> None:
    steps = json.loads(Path(script_path).read_text())
    results = []
    for step in steps:
        action = step.get("action")
        if action == "eval":
            resp = _control_post("/command", {
                "device_name": device_name,
                "method": "Runtime.evaluate",
                "params": {"expression": step["expression"], "returnByValue": True},
            })
            results.append(resp)
        elif action == "screenshot":
            resp = _control_post("/command", {
                "device_name": device_name,
                "method": "Page.captureScreenshot",
                "params": {},
            })
            data = (resp.get("result") or {}).get("data")
            if data:
                Path(step["path"]).write_bytes(base64.b64decode(data))
            results.append({"screenshot": step["path"]})
        elif action == "click":
            for ev_type in ("mousePressed", "mouseReleased"):
                _control_post("/command", {
                    "device_name": device_name,
                    "method": "Input.dispatchMouseEvent",
                    "params": {"type": ev_type, "x": step["x"], "y": step["y"], "button": "left", "clickCount": 1},
                })
            results.append({"clicked": [step["x"], step["y"]]})
        else:
            results.append({"error": f"unsupported action for real-tab mode: {action}"})
    print(json.dumps(results, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pair = sub.add_parser("pair")
    p_pair.add_argument("--device-name", required=True)

    p_revoke = sub.add_parser("revoke")
    p_revoke.add_argument("--device-name", required=True)

    sub.add_parser("list-devices")

    p_run = sub.add_parser("run")
    p_run.add_argument("--device-name", required=True)
    p_run.add_argument("--script", required=True)

    args = ap.parse_args()
    if args.cmd == "pair":
        cmd_pair(args.device_name)
    elif args.cmd == "revoke":
        cmd_revoke(args.device_name)
    elif args.cmd == "list-devices":
        cmd_list_devices()
    elif args.cmd == "run":
        cmd_run(args.device_name, args.script)
    return 0


if __name__ == "__main__":
    sys.exit(main())
