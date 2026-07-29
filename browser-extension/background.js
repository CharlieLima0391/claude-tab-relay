// Claude Tab Relay - background service worker.
//
// Only ever acts on the tab the user explicitly attaches via the popup. Uses
// chrome.debugger (CDP) on that one tab, which makes Chrome show its own
// "<extension> is debugging this browser" banner for as long as it's attached -
// that banner is not something this extension can hide, by design.

let ws = null;
let attachedTabId = null;
let deviceName = null;

function setBadge(text, color) {
  chrome.action.setBadgeText({ text });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
}

async function getConfig() {
  const { relayUrl, token, deviceName: storedName } = await chrome.storage.local.get([
    "relayUrl",
    "token",
    "deviceName",
  ]);
  return { relayUrl, token, deviceName: storedName };
}

async function attach(tabId) {
  if (attachedTabId !== null) {
    await detach();
  }
  const { relayUrl, token, deviceName: name } = await getConfig();
  if (!relayUrl || !token || !name) {
    throw new Error("Relay URL, token, and device name must be set in the popup first.");
  }
  deviceName = name;

  await chrome.debugger.attach({ tabId }, "1.3");
  attachedTabId = tabId;

  // Enable the domains that actually produce useful events.
  await chrome.debugger.sendCommand({ tabId }, "Runtime.enable", {});
  await chrome.debugger.sendCommand({ tabId }, "Log.enable", {});
  await chrome.debugger.sendCommand({ tabId }, "Network.enable", {});

  setBadge("...", "#9a6700");

  await new Promise((resolve, reject) => {
    let settled = false;
    const timeoutId = setTimeout(() => {
      if (settled) return;
      settled = true;
      cleanupFailedAttach();
      reject(new Error(`Timed out waiting for the relay at ${relayUrl} to respond. Check the URL/port and that the relay server is running.`));
    }, 8000);

    let socket;
    try {
      socket = new WebSocket(relayUrl);
    } catch (err) {
      clearTimeout(timeoutId);
      cleanupFailedAttach();
      reject(new Error(`Invalid relay URL "${relayUrl}": ${err}. It must start with ws:// or wss://, not http(s)://.`));
      return;
    }
    ws = socket;

    socket.onopen = () => {
      socket.send(JSON.stringify({ type: "auth", device_name: deviceName, token }));
    };
    socket.onmessage = async (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === "auth_ok") {
        if (!settled) {
          settled = true;
          clearTimeout(timeoutId);
          setBadge("ON", "#1a7f37");
          resolve();
        }
        return;
      }
      if (msg.type === "command") {
        try {
          const result = await chrome.debugger.sendCommand({ tabId: attachedTabId }, msg.method, msg.params || {});
          socket.send(JSON.stringify({ type: "result", request_id: msg.request_id, result }));
        } catch (err) {
          socket.send(JSON.stringify({ type: "result", request_id: msg.request_id, result: { error: String(err) } }));
        }
      }
    };
    socket.onclose = (event) => {
      setBadge("", null);
      if (!settled) {
        settled = true;
        clearTimeout(timeoutId);
        cleanupFailedAttach();
        // The relay closes with 4003 specifically for a bad device_name/token pair.
        const reason = event.code === 4003
          ? "the relay rejected this device name/token - re-pair with browser_relay_ctl.py and check for typos"
          : `connection closed (code ${event.code}${event.reason ? ": " + event.reason : ""})`;
        reject(new Error(`Could not attach: ${reason}.`));
      }
    };
    socket.onerror = () => {
      setBadge("ERR", "#cf222e");
      if (!settled) {
        settled = true;
        clearTimeout(timeoutId);
        cleanupFailedAttach();
        reject(new Error(`Could not reach ${relayUrl} - check the address, that you're on the same network as the relay host, and that it's running.`));
      }
    };
  });
}

async function cleanupFailedAttach() {
  if (attachedTabId !== null) {
    try {
      await chrome.debugger.detach({ tabId: attachedTabId });
    } catch (e) {
      // already gone - fine
    }
  }
  attachedTabId = null;
  ws = null;
}

async function detach() {
  if (attachedTabId !== null) {
    try {
      await chrome.debugger.detach({ tabId: attachedTabId });
    } catch (e) {
      // already detached (e.g. tab closed) - fine
    }
  }
  attachedTabId = null;
  if (ws) {
    ws.close();
    ws = null;
  }
  setBadge("", null);
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (source.tabId !== attachedTabId || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "event", cdp_method: method, params }));
});

chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId === attachedTabId) {
    attachedTabId = null;
    if (ws) {
      ws.close();
      ws = null;
    }
    setBadge("", null);
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "attach") {
    chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
      attach(tab.id)
        .then(() => sendResponse({ ok: true }))
        .catch((err) => sendResponse({ ok: false, error: String(err) }));
    });
    return true;
  }
  if (msg.action === "detach") {
    detach().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.action === "status") {
    sendResponse({ attached: attachedTabId !== null, deviceName });
    return true;
  }
});
