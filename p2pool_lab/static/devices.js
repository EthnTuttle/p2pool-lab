"use strict";

(() => {
  let current = null;
  let busy = false;
  let scanning = false;
  let scanTimer = null;
  let results = [];

  function message(value, error = false) {
    text("discovery-status", value);
    byId("discovery-status").classList.toggle("error", error);
  }
  function controls() {
    byId("discover-button").disabled = busy || scanning;
    byId("add-device-button").disabled = busy;
    document.querySelectorAll("[data-add-ip]").forEach(button => button.disabled = busy || button.dataset.registered === "true");
    document.querySelectorAll("[data-remove-device]").forEach(button => button.disabled = busy);
  }
  async function post(action, payload) {
    if (!state?.token) throw new Error("Wait for the dashboard to connect, then try again.");
    return jsonRequest("/api/devices/" + action, {method: "POST", headers: {"Content-Type": "application/json", "X-Dashboard-Token": state.token}, body: JSON.stringify(payload), timeout: 15000});
  }
  async function refreshInventory() {
    state = await jsonRequest("/api/state"); render();
  }
  function renderFound() {
    const urls = new Set(Object.values(current?.miners || {}).map(device => device.url));
    byId("discovered-devices").replaceChildren(...results.map(device => {
      const row = element("div", undefined, "discovered-device");
      row.append(element("span", `${device.name} · ${device.address} · ${device.model} · ${device.version}`));
      const added = urls.has("http://" + device.address);
      const button = element("button", added ? "Registered" : "Add miner");
      button.dataset.addIp = device.address; button.dataset.registered = String(added);
      button.addEventListener("click", () => add(device.address));
      row.append(button); return row;
    }));
    controls();
  }
  function renderInventory(value) {
    current = value;
    const entries = Object.entries(value.miners);
    text("device-count", `${entries.length} registered`);
    const input = byId("discovery-cidr");
    if (!input.value && !input.dataset.edited) {
      const host = value.settings.stratum_host;
      if (/^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host) && /^\d+\.\d+\.\d+\.\d+$/.test(host)) input.value = host.split(".").slice(0, 3).join(".") + ".0/24";
    }
    const rows = entries.map(([id, device]) => {
      const row = element("tr");
      const actions = element("td");
      const buttons = element("div", undefined, "device-actions");
      const select = element("button", "View");
      select.addEventListener("click", () => {
        const card = Array.from(document.querySelectorAll("[data-miner]")).find(button => button.dataset.miner === id);
        if (card) { card.click(); byId("overview").scrollIntoView(); }
      });
      const remove = element("button", "Remove"); remove.dataset.removeDevice = id;
      remove.title = "Remove from dashboard monitoring";
      remove.addEventListener("click", () => removeDevice(id));
      buttons.append(select, remove); actions.append(buttons);
      row.append(element("td", device.name), element("td", device.url), element("td", device.worker), actions);
      return row;
    });
    if (!rows.length) {
      const row = element("tr"), cell = element("td", "Discover your LAN or add a Bitaxe IP to begin.", "empty");
      cell.colSpan = 4; row.append(cell); rows.push(row);
    }
    byId("registered-devices").replaceChildren(...rows);
    renderFound();
  }
  async function add(address) {
    if (busy) return;
    busy = true; controls(); message(`Checking AxeOS at ${address}…`);
    try {
      const device = await post("add", {address});
      await refreshInventory();
      message(`Monitoring ${device.name} at ${device.url}. History is saved locally.`);
      byId("add-device-ip").value = "";
    } catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  }
  async function removeDevice(id) {
    if (busy) return;
    busy = true; controls();
    try { const result = await post("remove", {id}); await refreshInventory(); message(result.message); }
    catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  }
  async function pollDiscovery() {
    clearTimeout(scanTimer);
    try {
      const result = await jsonRequest("/api/devices/discovery");
      scanning = result.running; results = result.results; renderFound();
      if (result.cidr) message(result.error || `${scanning ? "Scanning" : "Scan complete"}: ${result.cidr} · ${result.checked}/${result.total} checked · ${results.length} AxeOS devices found`, Boolean(result.error));
      if (scanning) scanTimer = setTimeout(pollDiscovery, 1500);
    } catch (error) { scanning = false; message(error.message, true); }
    controls();
  }
  byId("discovery-cidr").addEventListener("input", () => { byId("discovery-cidr").dataset.edited = "true"; });
  byId("discovery-form").addEventListener("submit", async event => {
    event.preventDefault(); if (busy || scanning) return;
    scanning = true; controls(); message("Starting LAN discovery…");
    try { await post("discover", {cidr: byId("discovery-cidr").value.trim()}); await pollDiscovery(); }
    catch (error) { scanning = false; message(error.message, true); controls(); }
  });
  byId("add-device-form").addEventListener("submit", event => { event.preventDefault(); add(byId("add-device-ip").value.trim()); });
  window.addEventListener("fleet-state", event => renderInventory(event.detail));
  if (state) renderInventory(state);
  pollDiscovery();
})();
