const relayUrlEl = document.getElementById("relayUrl");
const deviceNameEl = document.getElementById("deviceName");
const tokenEl = document.getElementById("token");
const statusEl = document.getElementById("status");
const toggleBtn = document.getElementById("toggleBtn");

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls;
}

async function refresh() {
  const { relayUrl, deviceName, token } = await chrome.storage.local.get(["relayUrl", "deviceName", "token"]);
  relayUrlEl.value = relayUrl || "";
  deviceNameEl.value = deviceName || "";
  tokenEl.value = token || "";

  const resp = await chrome.runtime.sendMessage({ action: "status" });
  if (resp.attached) {
    setStatus(`Attached (${resp.deviceName})`, "status-on");
    toggleBtn.textContent = "Detach";
  } else {
    setStatus("Not attached", "status-off");
    toggleBtn.textContent = "Attach current tab";
  }
}

toggleBtn.addEventListener("click", async () => {
  await chrome.storage.local.set({
    relayUrl: relayUrlEl.value.trim(),
    deviceName: deviceNameEl.value.trim(),
    token: tokenEl.value.trim(),
  });

  const resp = await chrome.runtime.sendMessage({ action: "status" });
  if (resp.attached) {
    await chrome.runtime.sendMessage({ action: "detach" });
    await refresh();
    return;
  }

  toggleBtn.disabled = true;
  toggleBtn.textContent = "Attaching...";
  const result = await chrome.runtime.sendMessage({ action: "attach" });
  toggleBtn.disabled = false;
  if (!result.ok) {
    setStatus(`Error: ${result.error}`, "status-err");
    toggleBtn.textContent = "Attach current tab";
    return;
  }
  await refresh();
});

refresh();
