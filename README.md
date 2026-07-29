# Claude Tab Relay

Lets Claude Code attach to a **real, already-logged-in browser tab** on any of your
devices — for pentest/debug work where a disposable headless browser is no good because
it never has your actual session, cookies, or login state.

This is a companion to a separate disposable-headless-browser tool; this one is for
when you need Claude looking at *your own live session*, not a fresh one.

## How it works

```
Browser extension  <--WebSocket-->  relay server  <--loopback HTTP-->  Claude (Bash)
(chrome.debugger)        :8822    (server/browser_relay.py)     :8823
```

Four moving parts. Each one is described below with what it does, what it needs, which
parts of it you're allowed to change, and where the hard edges are.

### 1. Browser extension (`browser-extension/`)

- **What it is**: a Manifest V3 extension (`background.js` + `popup.html`/`popup.js`)
  that runs inside the browser itself.
- **What it needs**: Chrome, Chromium, Edge, or Brave, with Developer mode enabled to
  load it (`chrome://extensions` → Load unpacked). It is **not** published to any
  extension store - every device needs it loaded manually.
- **What's configurable**: the relay URL, device name, and token are all entered by hand
  in the popup and saved via `chrome.storage.local` - nothing is hardcoded in the
  extension's code. Point it at any relay server, on any host/port, just by typing a
  different URL in.
- **Limitations**: attaches to exactly one tab at a time, only the tab that was active
  *when you clicked Attach* (switching tabs afterward doesn't move the attachment).
  Cannot attach to a browser-internal `chrome://` page - Chrome blocks the debugger API
  there entirely, not something this tool can work around. Chromium-family only; there
  is no Firefox/Safari equivalent of `chrome.debugger`, so this extension will not work
  in those browsers at all.

### 2. The WebSocket connection (extension ↔ relay server)

- **What it is**: a single persistent connection the extension opens to the relay,
  default port **8822**. Carries small JSON messages both ways - an auth handshake once
  at connect time, then commands flowing to the browser and results/events flowing back.
- **What's configurable**: the relay's listening host/port are read from environment
  variables at startup - `RELAY_WS_HOST` (default `0.0.0.0`) and `RELAY_WS_PORT`
  (default `8822`). Change either by setting the env var before running
  `server/browser_relay.py` (or uncommenting the matching line in the systemd example).
  The extension side needs no code change either way - it just connects to whatever
  `ws://host:port` you type into its popup.
- **Limitations**: this is plain `ws://`, not `wss://` (no TLS) - fine on a trusted LAN
  or over a VPN like Tailscale, genuinely not fine across the open internet. If you need
  that, put it behind a reverse proxy that terminates TLS (e.g. Caddy/nginx) rather than
  exposing it directly - not something this project implements itself.

### 3. Relay server (`server/browser_relay.py`)

- **What it is**: a small Python daemon with no framework dependency beyond the
  `websockets` package. Validates each device's token (hash comparison, never stores
  the raw token), tracks who's currently connected, and appends every connect/disconnect/
  command to `state/browser-relay-audit.log`.
- **What it needs**: Python 3.9+, the `websockets` package, and somewhere to keep running
  (foreground terminal, `systemd`, or your OS's equivalent - see the systemd example for
  Linux).
- **What's configurable**: all four of `RELAY_WS_HOST`, `RELAY_WS_PORT`,
  `RELAY_CONTROL_HOST`, `RELAY_CONTROL_PORT`, plus `RELAY_COMMAND_TIMEOUT_SECONDS` (how
  long to wait for a browser tab to answer a command before giving up, default 20s) are
  environment-variable overrides - see the top of `server/browser_relay.py` for the exact
  names/defaults.
- **Limitations**: single-process, in-memory connection tracking - restarting the relay
  drops every attached device and they'll need to click Attach again. No built-in
  persistence of *which tab* was attached across a restart, by design (nothing should
  reattach itself without you clicking the button again).

### 4. Loopback HTTP control API (also inside `browser_relay.py`, port 8823)

- **What it is**: a second, much simpler listener - plain HTTP, JSON in and out, no
  WebSocket - that `server/browser_relay_ctl.py` (and Claude, via that same script) talks
  to in order to actually issue a command to a connected device.
- **What's configurable**: `RELAY_CONTROL_HOST`/`RELAY_CONTROL_PORT` (defaults
  `127.0.0.1`/`8823`), same env vars as above. `browser_relay_ctl.py` reads the same two
  variables, so change them together, on the same machine.
- **What you should not change, and why**: `RELAY_CONTROL_HOST` defaults to
  `127.0.0.1` (loopback-only) deliberately. This port has **no authentication of its
  own** - anything that can reach it can command any currently-attached browser tab.
  That's an acceptable trust boundary only because "anything that can reach it" is
  restricted to processes on the same machine. Binding it to `0.0.0.0` (or any
  network-reachable address) removes that boundary entirely and is not a supported
  configuration - if you need the driving side on a different machine from the relay,
  put a real authenticating proxy in front of this port yourself; this project doesn't
  do it for you.

### 5. Claude Code (or whatever is actually driving it)

- **What it is**: in the intended use, Claude Code's own shell access, running
  `server/browser_relay_ctl.py run --device-name ... --script steps.json` and reading
  the JSON result back. There's nothing Claude-specific about the wire protocol, though
  - anything capable of POSTing JSON to a local HTTP endpoint could drive this the same
  way, so the "Claude" box in the diagram really just means "whatever process has shell
  access to the relay machine."
- **Requirement**: must run on the same machine as the relay server, because of the
  loopback restriction described above. This is the one component with no separate
  "port" of its own - it's a client of the control API, not a listener.

## What you need before you start

No prior experience with this specific tool assumed, but you should be comfortable
copy-pasting commands into a terminal. Here's everything to have ready first:

1. **A computer to run the "relay" on.** This can be the same laptop you browse from,
   or a separate machine that's on whenever you want to use the tool (a home server, a
   spare PC, a Raspberry Pi, a cloud VPS - anything that can run Python). This machine
   does **not** need to be powerful - it just relays messages, it doesn't do any heavy
   lifting itself.
   - Needs **Python 3.9 or newer** installed. Check with `python3 --version` in a
     terminal - if that fails, install Python from [python.org](https://www.python.org/)
     (Windows/Mac) or your package manager (Linux, e.g. `sudo apt install python3`).
   - Works on Linux, macOS, or Windows. The optional "run forever in the background"
     instructions below (`systemd`) are Linux-only - on macOS/Windows, just leave a
     terminal window open running the relay, or look into `launchd`/Task Scheduler if
     you want it fully automatic (not covered here).

2. **A Chromium-family browser** to install the extension in - **Chrome, Chromium, Edge,
   or Brave**. Firefox and Safari will not work; this tool relies on a Chrome-specific
   API (`chrome.debugger`) that only Chromium-based browsers have.
   - You need to be able to turn on "Developer mode" in that browser's extensions
     settings. On a personal computer this is always available; on a work/managed
     computer it may be locked by IT policy - check that first if it's not your own
     device.

3. **The relay machine and the browser device need to be able to reach each other over
   the network.** This is the part most likely to trip you up, so in order of easiest:
   - **Easiest**: run the relay on the *same* computer you're browsing from
     (`ws://127.0.0.1:8822`) - nothing to configure, works immediately.
   - **Same WiFi/network**: relay on one device, browser on another, both on the same
     home/office network - use the relay machine's local network address (e.g.
     `ws://192.168.1.23:8822` - find yours with `ip addr` on Linux, `ipconfig` on
     Windows, or System Settings → Wi-Fi → Details on Mac).
   - **Different networks entirely** (e.g. relay at home, browsing on the road): you'll
     need something that bridges the two networks - a VPN like [Tailscale](https://tailscale.com/)
     (free for personal use, easiest option) or manual router port-forwarding (more
     advanced, and means opening a port to the internet - only do this if you understand
     the exposure and use a strong, unique token). Not covered step-by-step here.

4. **If the relay machine has a firewall enabled**, you'll need to allow the relay's
   port through it (default `8822`). On Linux with `ufw`:
   `sudo ufw allow 8822/tcp` (narrow this to a specific source if you can - see the
   commented-out example in step 1 below). On Windows, allow the port through Windows
   Defender Firewall when prompted, or via Settings → Network → Firewall. On macOS,
   System Settings → Network → Firewall.

5. **A way to get the code onto both places it's needed**: the relay server needs the
   whole repo; the browser device only needs the `browser-extension/` folder. Either
   `git clone https://github.com/CharlieLima0391/claude-tab-relay.git` (no account
   needed, it's public), or click **Code → Download ZIP** on the GitHub page and unzip
   it - whichever you're more comfortable with.

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

Open the WebSocket port (default `8822`) to whichever devices need to reach it. On Linux
with `ufw`, prefer narrowing this to your own network rather than allowing it from
anywhere, e.g. `sudo ufw allow from 192.168.1.0/24 to any port 8822 proto tcp` (swap in
your own LAN range, or your VPN/Tailscale range if using one). The control port (`8823`)
should stay loopback-only; it never needs a firewall rule.

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
