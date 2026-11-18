"use strict";

const byId = id => document.getElementById(id);
const number = (value, digits = 0) => Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: digits}) : "—";
const clock = timestamp => new Date(timestamp * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
const short = (value, length = 12) => value ? `${value.slice(0, length)}…${value.slice(-6)}` : "—";
let state = null;
let history = [];
let metric = "hashrate";
let actionPending = false;
let historyRequest = 0;
let selectedMiner = new URLSearchParams(location.search).get("miner") || "fleet";
let shareHistoryLoaded = false;
let sharePageNumber = 1;
let sharePageRanges = [];
let loadingSharePage = false;

function text(id, value) { byId(id).textContent = value; }
function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (className) node.className = className;
  return node;
}
function source(name) {
  if (name === "miner" && selectedMiner === "fleet") return state?.fleet || null;
  if (name === "miner") name = "miner-" + selectedMiner;
  const value = state?.sources?.[name];
  const ageLimit = name === "wallet" ? 125 : 35;
  return value?.ok && Date.now() / 1000 - value.updated_at < ageLimit ? value.data : null;
}
function badge(id, content, kind = "") {
  text(id, content);
  byId(id).className = "badge" + (kind ? " " + kind : "");
}
function stat(id, value, unit = "") {
  const target = byId(id);
  target.replaceChildren(document.createTextNode(value));
  if (unit) target.append(element("small", unit));
}
function uptime(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const hours = Math.floor(seconds / 3600);
  return `${hours >= 24 ? Math.floor(hours / 24) + "d " : ""}${hours % 24}h ${Math.floor(seconds / 60) % 60}m`;
}
function updateControls() {
  const supported = source("miner")?.supported_actions || [];
  document.querySelectorAll("[data-action]").forEach(button => {
    button.disabled = actionPending || !supported.includes(button.dataset.action) || !source("miner") || !state?.updated_at || Date.now() / 1000 - state.updated_at > 35;
    button.title = supported.includes(button.dataset.action) ? `Control Bitaxe .${selectedMiner}` : "Not supported by this firmware";
  });
  document.querySelectorAll("[data-miner]").forEach(button => button.disabled = actionPending);
  text("controls-note", selectedMiner === "fleet" ? "Select an individual miner for its controls. Fleet totals include responding devices; session share counts reset when miners reboot." : supported.includes("pause") ? `Controls apply to ${state?.miners?.[selectedMiner]?.url}.` : "Pause/resume and identify controls require a connected miner with supported firmware.");
}
function minerBadge(info) {
  if (!info) return ["Unavailable", "warning"];
  if (info.power_fault || info.overheat_mode) return ["Fault reported", "danger"];
  if (info.temp >= 80) return ["High temperature", "danger"];
  if (info.miningPaused) return ["Paused", "warning"];
  if (info.workReceived === 0 && (info.poolConnectionInfo || "").toLowerCase().includes("not connected")) return ["No pool work", "warning"];
  return info.hashRate > 0 ? ["Hashing", ""] : ["Idle", "warning"];
}
function renderDevices() {
  const previous = selectedMiner;
  if (selectedMiner !== "fleet" && !state.miners[selectedMiner]) selectedMiner = "fleet";
  document.querySelectorAll("[data-miner]").forEach(button => {
    if (button.dataset.miner !== "fleet" && !state.miners[button.dataset.miner]) button.remove();
  });
  for (const [id, device] of Object.entries(state.miners)) {
    if (Array.from(document.querySelectorAll("[data-miner]")).some(button => button.dataset.miner === id)) continue;
    const button = element("button", undefined, "device-card"); button.dataset.miner = id;
    const heading = element("span", undefined, "device-card-top");
    const status = element("span", "Connecting", "badge"); status.dataset.field = "status";
    heading.append(element("strong", device.name), status);
    const reading = element("span", "Waiting for telemetry", "device-reading"); reading.dataset.field = "reading";
    const pool = element("span", device.url, "muted"); pool.dataset.field = "pool";
    button.append(heading, reading, pool); byId("device-selector").append(button);
  }
  if (previous !== selectedMiner) { history = []; ++historyRequest; refreshHistory(); }
  document.querySelectorAll("[data-miner]").forEach(button => {
    const id = button.dataset.miner;
    const isFleet = id === "fleet";
    const info = isFleet ? state.fleet : source("miner-" + id);
    const status = isFleet ? [info.reporting ? `${info.reporting}/${info.configured} reporting` : "No telemetry", info.reporting === info.configured && info.configured ? "" : "warning"] : minerBadge(info);
    const statusNode = button.querySelector('[data-field="status"]');
    statusNode.textContent = status[0]; statusNode.className = "badge " + status[1];
    button.querySelector('[data-field="reading"]').textContent = info ? `${number(info.hashRate, 1)} GH/s · ${number(info.temp, 1)} °C · ${number(info.power, 1)} W` : "Telemetry unavailable";
    const fallback = info?.isUsingFallbackStratum;
    const pool = fallback ? info?.fallbackStratumURL : info?.stratumURL;
    button.querySelector('[data-field="pool"]').textContent = isFleet ? `Combined hashrate and power · hottest ASIC · ${number(info.sharesAccepted)} session shares` : `${state.miners[id].url} · ${pool || "Pool unavailable"} · ${number(info?.sharesAccepted)} accepted`;
    button.classList.toggle("selected", id === selectedMiner);
    button.setAttribute("aria-pressed", String(id === selectedMiner));
  });
  const device = state.miners[selectedMiner];
  text("selected-label", device ? `Viewing ${device.name} · charts and controls follow this selection` : `Fleet overview · ${state.fleet.reporting}/${state.fleet.configured} miners reporting · totals may be partial when a miner is offline`);
  text("device-label", device ? `DEVICE ${selectedMiner}` : "FLEET");
  text("miner-ip", device ? device.url : `${state.fleet.configured} registered miners`);
  byId("miner-link").href = device ? device.url + "/" : "#devices";
  byId("miner-link").lastElementChild.textContent = device ? "Open AxeOS ↗" : "Discover / manage →";
}
function render() {
  renderDevices();
  const isFleet = selectedMiner === "fleet";
  const miner = source("miner") || {};
  const bitcoin = source("bitcoin") || {};
  const chain = source("chain") || {};
  const peers = source("peers");
  const metrics = source("metrics") || {};
  const values = metrics.values || {};
  const wallet = source("wallet") || {};
  const fresh = state.updated_at && Date.now() / 1000 - state.updated_at < 35;
  text("last-update", fresh ? `Live · updated ${clock(state.updated_at)}` : "Waiting for fresh telemetry…");
  byId("live-dot").classList.toggle("offline", !fresh);

  byId("alerts").replaceChildren(...state.alerts.map(alert => {
    const node = element("div", undefined, `alert ${alert.level}`);
    node.append(element("span", "!", "alert-icon"), element("span", alert.message));
    return node;
  }));
  stat("hashrate", number(miner.hashRate, 1), "GH/s");
  text("hashrate-scope", isFleet ? "FLEET" : "BITAXE");
  text("hashrate-detail", isFleet ? `${state.fleet.reporting}/${state.fleet.configured} reporting · combined` : `10m average ${number(miner.hashRate_10m, 1)} GH/s`);
  stat("temperature", number(miner.temp, 1), "°C");
  text("temperature-label", isFleet ? "HOTTEST ASIC" : "ASIC TEMPERATURE");
  byId("temperature").classList.toggle("hot", miner.temp >= 80);
  text("temperature-tag", !Number.isFinite(miner.temp) ? "UNAVAILABLE" : miner.temp >= 80 ? "HIGH READING" : isFleet ? "MAX" : "SENSOR");
  text("temperature-detail", isFleet ? "Maximum reported temperature" : `${number(miner.fanrpm)} RPM · ${number(miner.fanspeed)}% fan`);
  stat("power", number(miner.power, 1), "W");
  text("power-scope", isFleet ? "FLEET" : "BITAXE");
  text("power-detail", isFleet ? "Combined reported device power" : `${number(miner.voltage / 1000, 2)} V input`);
  stat("accepted", number(miner.sharesAccepted));
  text("shares-detail", isFleet ? `${number(miner.sharesRejected)} rejected · sum of current sessions` : `${number(miner.sharesRejected)} rejected · difficulty ${number(miner.poolDifficulty)}`);
  stat("bitcoin-height", number(bitcoin.blocks));
  text("bitcoin-tag", !source("bitcoin") ? "UNAVAILABLE" : bitcoin.initialblockdownload ? "SYNCING" : "SYNCED");
  text("bitcoin-detail", `${number(bitcoin.connections)} peers · headers ${number(bitcoin.headers)}`);

  text("miner-name", isFleet ? "Fleet overview" : miner.hostname || "Bitaxe");
  if (isFleet) badge("miner-badge", `${state.fleet.reporting}/${state.fleet.configured} reporting`, state.fleet.reporting === state.fleet.configured && state.fleet.configured ? "" : "warning");
  else badge("miner-badge", ...minerBadge(source("miner")));
  text("model-label", isFleet ? "Inventory" : "ASIC / firmware");
  text("power-label", isFleet ? "Combined power" : "Power / input voltage");
  text("uptime-label", isFleet ? "Details" : "Uptime / Wi-Fi");
  text("connection-label", isFleet ? "Monitoring" : "Pool connection");
  text("miner-model", `${miner.ASICModel || "—"} / ${miner.version || "—"}`);
  text("miner-power", `${number(miner.power, 1)} W / ${number(miner.voltage / 1000, 2)} V`);
  text("clock-label", Number.isFinite(miner.actualFrequency) ? "Actual clock" : "Configured clock");
  text("miner-frequency", `${number(miner.actualFrequency ?? miner.frequency)} MHz · ${number(miner.coreVoltageActual)} mV`);
  text("miner-uptime", `${uptime(miner.uptimeSeconds)} / ${number(miner.wifiRSSI)} dBm`);
  text("miner-connection", miner.poolConnectionInfo || "Not reported by firmware");
  byId("miner-controls").hidden = isFleet;
  byId("pool-target-controls").hidden = isFleet;
  if (isFleet) {
    text("miner-model", `${state.fleet.configured} miners registered`);
    text("miner-power", `${number(miner.power, 1)} W`);
    text("clock-label", "Settings"); text("miner-frequency", "Managed per miner");
    text("miner-uptime", "Select a device above");
    text("miner-connection", `${state.fleet.reporting} reporting · ${state.fleet.configured - state.fleet.reporting} unavailable`);
  }
  byId("pause-button").dataset.action = miner.miningPaused ? "resume" : "pause";
  text("pause-button", miner.miningPaused ? "Resume mining" : "Pause mining");
  const fallback = Boolean(miner.isUsingFallbackStratum);
  const host = fallback ? miner.fallbackStratumURL : miner.stratumURL;
  const port = fallback ? miner.fallbackStratumPort : miner.stratumPort;
  text("miner-pool", host ? `${host}:${port}` : "—");
  const local = host === state.settings.stratum_host && port === state.settings.stratum_port;
  text("pool-config-note", `Saves primary + fallback to ${state.settings.stratum_host}:${state.settings.stratum_port}. Restart the miner to apply.`);
  byId("explorer-link").href = state.settings.explorer_url;
  text("pool-target-badge", !host ? "UNAVAILABLE" : local ? "LOCAL POOL" : "OTHER POOL");
  text("miner-pool-detail", host ? `${fallback ? "Fallback selected" : "Primary selected"} · ${short(fallback ? miner.fallbackStratumUser : miner.stratumUser, 18)}` : "Waiting for the device");

  text("share-height", number(chain.chain_tip_height));
  text("candidate-height", number(chain.top_candidate_height));
  text("peer-count", Array.isArray(peers) ? peers.length : "—");
  text("pool-accepted", number(values.shares_accepted_total));
  text("pool-rejected", number(values.shares_rejected_total));
  text("network-difficulty", number(values.network_difficulty));
  if (!source("chain")) badge("chain-badge", "Unavailable", "warning");
  else if (!peers?.length) badge("chain-badge", "No peers", "warning");
  else if (chain.chain_tip_height < chain.top_candidate_height) badge("chain-badge", "Syncing", "warning");
  else badge("chain-badge", "In sync");

  text("wallet-balance", number(wallet.confirmed));
  text("wallet-detail", `${number(wallet.immature)} immature · ${number((wallet.trusted_pending ?? NaN) + (wallet.untrusted_pending ?? NaN))} pending test sats`);
  badge("wallet-badge", source("wallet") ? "Synced " + clock(state.sources.wallet.updated_at).slice(0, 5) : "Unavailable", source("wallet") ? "" : "warning");
  text("payout-address", state.settings.bitcoin_payout_address);
  text("share-address", state.settings.share_owner_address);
  text("lab-blocks-count", number(metrics.lab_blocks_found));
  text("blocks-count", number(values.bitcoin_blocks_found_total));
  const rows = (metrics.blocks || []).map(block => {
    const row = element("tr");
    const date = new Date(block.timestamp * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
    const hash = element("td", short(block.blockhash, 18)); hash.title = block.blockhash;
    const minerCell = element("td", short(block.miner, 14)); minerCell.title = block.miner;
    row.append(element("td", number(Number(block.height))), element("td", date), hash, minerCell);
    return row;
  });
  if (!rows.length) {
    const row = element("tr"), cell = element("td", "No pool block data available", "empty");
    cell.colSpan = 4; row.append(cell); rows.push(row);
  }
  byId("blocks").replaceChildren(...rows);
  byId("events").replaceChildren(...state.events.map(event => {
    const row = element("div", undefined, "event");
    const time = element("time", clock(event.timestamp)); time.dateTime = new Date(event.timestamp * 1000).toISOString();
    row.append(time, element("span", undefined, "event-dot " + event.level), element("span", event.message));
    return row;
  }));
  if (!state.events.length) byId("events").append(element("p", "No events yet. Device alerts and control actions will appear here.", "muted"));
  updateControls();
  window.dispatchEvent(new CustomEvent("fleet-state", {detail: state}));
}

function svgElement(tag, attributes, content) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (content !== undefined) node.textContent = content;
  return node;
}
function drawChart() {
  const fleet = selectedMiner === "fleet";
  const captions = {hashrate: [fleet ? "Combined fleet hashrate" : "Live ASIC hashrate", "GH/s"], temperature: [fleet ? "Hottest ASIC in the fleet" : "ASIC temperature reported by device", "°C"], power: [fleet ? "Combined fleet power draw" : "Device power draw", "W"]};
  text("chart-caption", captions[metric][0]); text("chart-unit", captions[metric][1]);
  const points = history.filter(point => Number.isFinite(point[metric]));
  if (!points.length) {
    byId("chart").replaceChildren(element("p", "Collecting telemetry. The first samples will appear shortly."));
    return;
  }
  const end = Math.max(Date.now() / 1000, history.at(-1).timestamp);
  const start = Math.min(history[0].timestamp, end - 60);
  const maximum = Math.max(1, ...points.map(point => point[metric])) * 1.12;
  const width = 780, height = 265, left = 48, right = 12, top = 12, bottom = 14;
  const x = timestamp => left + (timestamp - start) / (end - start) * (width - left - right);
  const y = value => height - bottom - value / maximum * (height - top - bottom);
  const svg = svgElement("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${captions[metric][0]}, ${points.length} recorded samples`});
  for (let tick = 0; tick <= 4; tick++) {
    const value = maximum * tick / 4;
    svg.append(svgElement("line", {x1: left, x2: width - right, y1: y(value), y2: y(value), stroke: "#2a3038", "stroke-dasharray": "3 5"}));
    svg.append(svgElement("text", {x: left - 10, y: y(value) + 4, fill: "#929caa", "font-size": 10, "text-anchor": "end"}, number(value, maximum < 10 ? 1 : 0)));
  }
  let path = "", connected = false, previous = null;
  const expectedGap = Math.max(20, Number(byId("history-range").value) * 3600 / 720 * 3);
  for (const point of history) {
    if (!Number.isFinite(point[metric])) { connected = false; continue; }
    if (previous !== null && point.timestamp - previous > expectedGap) connected = false;
    path += `${connected ? "L" : "M"}${x(point.timestamp).toFixed(2)},${y(point[metric]).toFixed(2)} `;
    connected = true; previous = point.timestamp;
  }
  svg.append(svgElement("path", {d: path, stroke: metric === "temperature" ? "#ff9595" : metric === "power" ? "#7bccaa" : "#f5a85a", "stroke-width": 2.5, fill: "none", "stroke-linejoin": "round", "stroke-linecap": "round"}));
  const last = points.at(-1);
  svg.append(svgElement("circle", {cx: x(last.timestamp), cy: y(last[metric]), r: 3.5, fill: "#edf0f4"}));
  byId("chart").replaceChildren(svg);
  text("chart-start", new Date(start * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}));
  text("chart-end", clock(end));
}

async function jsonRequest(url, options = {}) {
  const {timeout = 12000, ...requestOptions} = options;
  const response = await fetch(url, {...requestOptions, signal: AbortSignal.timeout(timeout)});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "Request failed");
    error.status = response.status;
    throw error;
  }
  return data;
}
function sharePayoutCell(share, page) {
  const cell = element("td");
  const nextHeight = page.tip_height + 1;
  const blocksToGo = share.payout_from_height - nextHeight;
  if (nextHeight > share.payout_through_height) {
    cell.append(element("span", "Window ended"), element("small", `After ${number(share.payout_through_height)}`));
  } else if (blocksToGo <= 0) {
    cell.append(element("span", "In window"), element("small", `Through ${number(share.payout_through_height)}`));
  } else {
    const when = element("span", "Waiting");
    if (page.seconds_per_share) {
      const hours = (page.estimate_time + blocksToGo * page.seconds_per_share) / 3600;
      const date = new Date(Math.round(hours) * 3600000);
      const label = date.toLocaleString([], {month: "short", day: "numeric", hour: "numeric"});
      const time = element("time", label);
      time.dateTime = date.toISOString();
      when.replaceChildren("Est. ", time);
    }
    cell.append(when, element("small", `From ${number(share.payout_from_height)}`));
  }
  return cell;
}
function renderSharePagination() {
  const pagination = byId("share-pagination");
  const total = sharePageRanges.length;
  if (shareHistoryLoaded && !total) {
    pagination.replaceChildren();
    return;
  }
  const button = (label, target, disabled = false) => {
    const node = element("button", label);
    node.type = "button";
    node.disabled = disabled;
    node.addEventListener("click", () => loadSharePage(target));
    return node;
  };
  if (!total) {
    pagination.replaceChildren(button("Retry", 1));
    return;
  }
  const controls = [button("Previous", sharePageNumber - 1, sharePageNumber === 1)];
  const pages = [...new Set([1, sharePageNumber - 1, sharePageNumber, sharePageNumber + 1, total])]
    .filter(page => page >= 1 && page <= total).sort((a, b) => a - b);
  for (const [index, page] of pages.entries()) {
    if (index && page > pages[index - 1] + 1) {
      const gap = element("span", "…", "muted");
      gap.setAttribute("aria-hidden", "true");
      controls.push(gap);
    }
    const node = button(number(page), page);
    node.setAttribute("aria-label", `Page ${page}`);
    if (page === sharePageNumber && shareHistoryLoaded) node.setAttribute("aria-current", "page");
    controls.push(node);
  }
  controls.push(button("Next", sharePageNumber + 1, sharePageNumber === total));
  pagination.replaceChildren(...controls);
}

async function loadSharePage(pageNumber = 1) {
  if (loadingSharePage) return;
  loadingSharePage = true;
  const pagination = byId("share-pagination");
  const restoreFocus = pagination.contains(document.activeElement);
  pagination.setAttribute("aria-busy", "true");
  try {
    let ranges = pageNumber === 1 ? await jsonRequest("/api/owner-share-pages") : sharePageRanges;
    let page = ranges.length ? await jsonRequest("/api/owner-shares?to=" + ranges[pageNumber - 1]) : null;
    if (page && !page.shares.length) {
      // A reorg can empty a range after the index was read.
      ranges = await jsonRequest("/api/owner-share-pages");
      pageNumber = 1;
      page = ranges.length ? await jsonRequest("/api/owner-shares?to=" + ranges[0]) : null;
      if (page && !page.shares.length) throw new Error("Share history changed while loading");
    }
    const rows = (page?.shares ?? []).map(share => {
      const row = element("tr");
      const date = new Date(share.timestamp * 1000);
      const time = element("time", date.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}));
      time.dateTime = date.toISOString();
      const found = element("td", undefined, "share-found");
      found.append(time);
      const height = element("td");
      const link = element("a", share.height === null ? "View share" : number(share.height), "text-link");
      link.title = share.blockhash;
      link.href = new URL("/share?hash=" + encodeURIComponent(share.blockhash), state.settings.explorer_url);
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      height.append(link, element("small", share.kind, "share-type-mobile"));
      const hash = element("td", short(share.blockhash, 14));
      hash.title = share.blockhash;
      const sats = share.estimated_block_sats;
      const amount = sats === null ? "—" : sats === 0 ? "<1" : "≈ " + number(sats);
      row.append(
        height,
        found,
        element("td", share.kind, "share-type"),
        sharePayoutCell(share, page),
        element("td", amount, "share-value"),
        hash,
      );
      return row;
    });
    if (!rows.length) {
      const row = element("tr");
      const cell = element("td", "No confirmed shares or referenced uncles for this owner yet.", "empty");
      cell.colSpan = 6;
      row.append(cell);
      rows.push(row);
    }
    byId("share-rows").replaceChildren(...rows);
    if (pageNumber !== sharePageNumber) byId("share-table").scrollTop = 0;
    shareHistoryLoaded = true;
    sharePageNumber = pageNumber;
    sharePageRanges = ranges;
    text("share-page-status", page ? `${number(page.shares.length)} in share heights ${number(page.from_height)}–${number(page.to_height)}` : "0 shares");
  } catch (error) {
    text("share-page-status", shareHistoryLoaded ? "Could not reload shares. Showing the last loaded range." : "Could not load shares. Select Retry to try again.");
  } finally {
    loadingSharePage = false;
    renderSharePagination();
    pagination.removeAttribute("aria-busy");
    if (restoreFocus) (pagination.querySelector('[aria-current="page"]') || pagination.querySelector("button"))?.focus();
  }
}
async function refreshHistory() {
  if (!selectedMiner) { byId("chart").replaceChildren(element("p", "Configure a miner to begin collecting history.")); return; }
  const request = ++historyRequest;
  try {
    const data = await jsonRequest("/api/history?miner=" + selectedMiner + "&hours=" + byId("history-range").value);
    if (request !== historyRequest) return;
    history = data; drawChart();
    text("history-note", selectedMiner === "fleet" ? "Fleet totals include available readings · hottest temperature per interval · 7 days of local history" : "Sampled every 5 seconds · saved locally for 7 days · gaps mean unavailable telemetry");
  } catch (error) { text("history-note", "History unavailable: " + error.message); }
}
async function refresh() {
  try {
    const firstLoad = !state;
    state = await jsonRequest("/api/state");
    render();
    if (firstLoad) loadSharePage();
  }
  catch (error) {
    text("last-update", "Dashboard unavailable · readings may be stale");
    byId("live-dot").classList.add("offline");
    document.querySelectorAll("[data-action]").forEach(button => button.disabled = true);
  }
  setTimeout(refresh, 5000);
}
async function minerAction(action) {
  if (actionPending || !state) return;
  actionPending = true; updateControls();
  const target = selectedMiner;
  byId("action-result").classList.remove("error"); text("action-result", "Sending request to Bitaxe…");
  try {
    const result = await jsonRequest("/api/miner/" + target + "/" + action, {method: "POST", headers: {"Content-Type": "application/json", "X-Dashboard-Token": state.token}, body: "{}"});
    text("action-result", result.message);
  } catch (error) { byId("action-result").classList.add("error"); text("action-result", error.message); }
  finally { actionPending = false; updateControls(); }
}

document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", () => minerAction(button.dataset.action)));
byId("device-selector").addEventListener("click", event => {
  const button = event.target.closest("button[data-miner]");
  if (!button) return;
  if (actionPending || !state) return;
  selectedMiner = button.dataset.miner;
  window.history.replaceState(null, "", "?miner=" + selectedMiner);
  history = []; ++historyRequest; drawChart();
  text("action-result", ""); render(); refreshHistory();
});
document.querySelectorAll("[data-metric]").forEach(button => button.addEventListener("click", () => {
  metric = button.dataset.metric;
  document.querySelectorAll("[data-metric]").forEach(other => { other.classList.toggle("selected", other === button); other.setAttribute("aria-pressed", String(other === button)); });
  drawChart();
}));
document.querySelectorAll("[data-copy]").forEach(button => button.addEventListener("click", async () => {
  const value = byId(button.dataset.copy).textContent;
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
    else {
      const input = element("textarea", value, "sr-only"); document.body.append(input); input.select();
      const success = document.execCommand("copy"); input.remove();
      if (!success) throw new Error("Copy unavailable");
    }
    button.textContent = "Copied";
  } catch (error) { button.textContent = "Select text"; }
  setTimeout(() => button.textContent = "Copy", 2000);
}));
byId("history-range").addEventListener("change", refreshHistory);
setInterval(() => {
  const table = byId("share-table");
  if (state && !loadingSharePage &&
      !document.hidden && !table.scrollTop && !byId("owner-shares").contains(document.activeElement)) {
    loadSharePage(sharePageNumber);
  }
}, 30000);
byId("load-logs").addEventListener("click", async () => {
  byId("logs").hidden = false; text("logs", "Loading service logs…");
  try { const result = await jsonRequest("/api/logs?source=" + byId("log-source").value); text("logs", result.text || "No log entries."); }
  catch (error) { text("logs", error.message); }
});
refresh(); refreshHistory(); setInterval(refreshHistory, 15000);
