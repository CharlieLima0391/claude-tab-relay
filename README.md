# Claude Tab Relay

Lets Claude Code attach to a **real, already-logged-in browser tab** on any of your
devices — for pentest/debug work where a disposable headless browser is no good because
it never has your actual session, cookies, or login state.

This is a companion to a separate disposable-headless-browser tool; this one is for
when you need Claude looking at *your own live session*, not a fresh one.

## How it works

```
Browser extension  <--WebSocket-->  relay server  <--loopback HTTP-->  Claude (Bash)
(chrome.debugger)        :8822        (this repo)         :8823
```

- The **extension** runs in Chrome/Chromium/Edge/Brave. It never does anything until you
  explicitly click **Attach** in its popup, and once attached, Chrome shows its own
  "this extension is debugging this tab" banner for as long as it's active — that banner
  can't be hidden, by either of us. It uses `chrome.debugger` (the same protocol
  DevTools itself uses) on the one tab you attach, nothing else.
- The **relay server** (`server/browser_relay.py`) is a small Python/WebSocket daemon
  that authenticates each device with its own token, forwards commands one way and
  console/network events the other, and logs every single thing that happens to
  `state/browser-relay-audit.log`.
- **Claude's side** talks to a loopback-only HTTP control API (`127.0.0.1:8823`) —
  nothing on the network can issue commands except whoever already has shell access to
  the machine running the relay.

## Setup

### 1. Run the relay server

```
python3 -m venv .venv
.venv/bin/pip install websockets
.venv/bin/python server/browser_relay.py
```

For a persistent install, copy `systemd/browser-relay.service.example` to
`/etc/systemd/system/browser-relay.service`, fill in the paths, then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now browser-relay.service
```

Open the WebSocket port (default `8822`) to whichever devices need to reach it - e.g.
your LAN subnet and/or your Tailscale range. The control port (`8823`) should stay
loopback-only; it never needs a firewall rule.

### 2. Pair a device

```
python3 server/browser_relay_ctl.py pair --device-name "my-laptop-chrome"
```

Prints a one-time token. It's hashed at rest (`state/browser-relay-devices.json`) - the
raw value is shown exactly once and isn't recoverable from disk afterward. Re-pairing the
same device name issues a new token and invalidates the old one immediately.

```
python3 server/browser_relay_ctl.py revoke --device-name "my-laptop-chrome"
python3 server/browser_relay_ctl.py list-devices
```

### 3. Install the extension

`chrome://extensions` → enable **Developer mode** → **Load unpacked** → select the
`browser-extension/` folder. No build step, no Chrome Web Store listing - this is a
personal/internal tool, loaded manually per device.

In the extension's popup, fill in:
- **Relay URL** - `ws://<relay-host>:8822`
- **Device name** - must exactly match what you paired
- **Token** - from step 2

Click **Attach current tab** on whatever page you want looked at. Note: it attaches to
the *active* tab at click time, and it can't attach to Chrome's own internal `chrome://`
pages - switch to a real page first.

### 4. Drive it

```
python3 server/browser_relay_ctl.py run --device-name "my-laptop-chrome" --script steps.json
```

`steps.json` is a list of actions:

```json
[
  {"action": "eval", "expression": "document.title"},
  {"action": "screenshot", "path": "out.png"},
  {"action": "click", "x": 200, "y": 400}
]
```

## Security model

This is a genuinely more sensitive capability than a disposable headless browser,
because it's attached to a *real* logged-in session. The design leans on a few
properties rather than convenience:

- **Never silent.** Attaching always requires an explicit click in the popup. Nothing
  auto-starts on install or browser launch.
- **Always visible.** Chrome's native debugging banner is shown for the entire time a
  tab is attached and cannot be suppressed by the extension.
- **Per-device, revocable.** Each device gets its own token; revoking one doesn't affect
  any other paired device.
- **Hashes only at rest.** The relay never stores a raw token, only its SHA-256 hash.
- **Full audit trail.** Every connect, disconnect, and command is appended to
  `state/browser-relay-audit.log`.
- **Locked-down control plane.** The API that actually issues commands binds to
  `127.0.0.1` only - it's not reachable from the network under any circumstance.

Scope this appropriately for what you're doing with it - it's meant for you to look at
your own sessions on your own devices, not as a general-purpose remote access tool.

## Known limitations (v1)

- Chromium-family browsers only (uses `chrome.debugger`) - no Firefox support.
- Manual install per device, no auto-update.
- One tab at a time per device.
- No screenshot streaming - each screenshot/eval/click is a discrete, on-demand request,
  not a continuous live view.
