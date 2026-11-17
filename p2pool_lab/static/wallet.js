"use strict";

(() => {
  let walletData = null;
  let draft = null;
  let busy = false;
  let activeTab = "receive";
  let sending = false;
  let broadcastUncertain = false;

  function message(value, error = false) {
    text("wallet-message", value);
    byId("wallet-message").classList.toggle("error", error);
  }
  function controls() {
    for (const id of ["wallet-refresh", "wallet-new-address", "wallet-preview-button", "wallet-confirm", "wallet-cancel"]) byId(id).disabled = busy;
    byId("wallet-cancel").disabled = busy || broadcastUncertain;
    document.querySelectorAll("[data-retry-transfer]").forEach(button => button.disabled = busy);
  }
  function tab(name) {
    activeTab = name;
    document.querySelectorAll("[data-wallet-tab]").forEach(button => {
      const selected = button.dataset.walletTab === name;
      button.classList.toggle("selected", selected); button.setAttribute("aria-pressed", String(selected));
    });
    for (const view of ["receive", "send", "history"]) byId("wallet-" + view).hidden = view !== name;
  }
  function position(item) {
    const confirmed = item.chain_position?.Confirmed;
    const anchor = confirmed?.anchor;
    const height = anchor?.block_id?.height;
    const tip = source("bitcoin")?.blocks;
    const confirmations = Number.isFinite(height) && Number.isFinite(tip) ? Math.max(0, tip - height + 1) : null;
    return {confirmed: Boolean(confirmed), confirmations, time: anchor?.confirmation_time || item.chain_position?.Unconfirmed?.last_seen || 0};
  }
  function showAddress(receive) {
    if (!receive) return;
    text("wallet-receive-address", receive.address);
    text("wallet-address-index", "Address #" + receive.index);
    const url = "/api/wallet/qr?address=" + encodeURIComponent(receive.address);
    if (byId("wallet-qr").getAttribute("src") !== url) byId("wallet-qr").src = url;
    byId("wallet-qr").hidden = false;
  }
  function renderWallet(data) {
    walletData = data;
    const balance = data.balance || {};
    text("wallet-ui-confirmed", number(balance.confirmed));
    text("wallet-ui-pending", number((balance.trusted_pending ?? NaN) + (balance.untrusted_pending ?? NaN)));
    text("wallet-ui-immature", number(balance.immature));
    const stamp = data.updated_at ? `Synced ${new Date(data.updated_at * 1000).toLocaleString()} · Testnet5` : "Waiting for the first wallet sync…";
    text("wallet-sync-status", data.error ? `${data.error} ${stamp}` : stamp);
    byId("wallet-sync-status").classList.toggle("hot", Boolean(data.error));
    showAddress(data.receive);
    const transactions = [...(data.transactions || [])].sort((left, right) => {
      const a = position(left), b = position(right);
      return Number(a.confirmed) - Number(b.confirmed) || b.time - a.time;
    });
    const rows = transactions.map(item => {
      const row = element("tr");
      const identifier = element("td", short(item.txid, 18)); identifier.title = item.txid;
      const net = Number.isFinite(item.received) && Number.isFinite(item.sent) ? item.received - item.sent : null;
      const type = item.is_coinbase ? "Mining reward" : item.sent > 0 ? "Sent / self-transfer" : "Received";
      const change = element("td", net === null ? "—" : `${net > 0 ? "+" : ""}${number(net)}`, net > 0 ? "positive" : net < 0 ? "negative" : "");
      const status = position(item);
      const description = !status.confirmed ? "Pending" : status.confirmations === null ? "Confirmed" : item.is_coinbase && status.confirmations < 100 ? `Maturing · ${status.confirmations}/100` : `${number(status.confirmations)} confirmations`;
      row.append(identifier, element("td", type), change, element("td", description));
      return row;
    });
    if (!rows.length) {
      const row = element("tr"), cell = element("td", "No transactions yet. Receive testnet5 coins or wait for a mining payout.", "empty");
      cell.colSpan = 4; row.append(cell); rows.push(row);
    }
    byId("wallet-transactions").replaceChildren(...rows);
    const pending = (data.transfers || []).filter(transfer => transfer.status === "signed");
    byId("wallet-pending-transfers").replaceChildren(...pending.map(transfer => {
      const panel = element("div", undefined, "pending-transfer");
      panel.append(element("strong", "Saved transfer · broadcast not yet confirmed"));
      panel.append(element("p", `${number(transfer.amount_sat)} test sats to ${transfer.address}. Fee: ${number(transfer.fee_sat)} sats.`));
      const retry = element("button", "Retry this same transfer");
      retry.dataset.retryTransfer = transfer.draft_id;
      retry.addEventListener("click", () => { draft = transfer; broadcastUncertain = true; showPreview(); tab("send"); send(); });
      panel.append(retry); return panel;
    }));
    controls();
  }
  async function getWallet() {
    try { const data = await jsonRequest("/api/wallet"); renderWallet(data); return data; }
    catch (error) { text("wallet-sync-status", "Wallet data unavailable: " + error.message); }
  }
  async function walletPost(action, payload = {}) {
    if (!state?.token) throw new Error("Wait for the dashboard to connect, then try again.");
    return jsonRequest("/api/wallet/" + action, {method: "POST", headers: {"Content-Type": "application/json", "X-Dashboard-Token": state.token}, body: JSON.stringify(payload), timeout: 90000});
  }
  function showPreview() {
    byId("wallet-preview").hidden = !draft;
    byId("wallet-send-form").hidden = Boolean(draft);
    if (!draft) return;
    text("preview-address", draft.address);
    text("preview-amount", number(draft.amount_sat) + " sats");
    text("preview-fee", number(draft.fee_sat) + " sats");
    text("preview-total", number(draft.total_sat) + " sats");
    text("preview-change", number(draft.change_sat) + " sats");
    text("preview-expiry", broadcastUncertain ? "A retry reuses the exact signed transaction, so it cannot create a second payment." : `Preview expires ${clock(draft.expires_at)}. Requested fee rate: ${draft.fee_rate} sat/vB.`);
    text("wallet-confirm", broadcastUncertain ? "Retry this same transfer" : `Send ${number(draft.amount_sat)} test sats`);
    controls();
  }
  async function send() {
    if (busy || !draft) return;
    busy = true; sending = true; controls(); message("Signing and submitting the reviewed transaction…");
    try {
      const result = await walletPost("send", {draft_id: draft.draft_id});
      message(`Transaction submitted: ${result.txid}`);
      draft = null; broadcastUncertain = false; showPreview();
      byId("wallet-send-form").reset();
      await getWallet();
      // Sync history after submission; a refresh failure must not turn a successful send into a send error.
      walletPost("refresh").then(renderWallet).catch(() => {});
    } catch (error) {
      broadcastUncertain = true;
      const current = await getWallet();
      const transfer = current?.transfers?.find(item => item.draft_id === draft.draft_id);
      if (transfer?.status === "broadcast") {
        message(`Transaction submitted: ${transfer.txid}`);
        draft = null; broadcastUncertain = false; showPreview();
      } else {
        if (error.status && current && !transfer) broadcastUncertain = false;
        message(error.message, true); showPreview();
      }
    } finally { sending = false; busy = false; controls(); }
  }

  document.querySelectorAll("[data-wallet-tab]").forEach(button => button.addEventListener("click", () => tab(button.dataset.walletTab)));
  byId("wallet-refresh").addEventListener("click", async () => {
    if (busy) return;
    busy = true; controls(); message("Syncing with your local testnet5 node…");
    try { renderWallet(await walletPost("refresh")); message("Wallet refreshed."); }
    catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  });
  byId("wallet-new-address").addEventListener("click", async () => {
    if (busy) return;
    busy = true; controls(); message("Deriving the next receive address…");
    try { const address = await walletPost("receive"); showAddress(address); message("New receive address ready. Mining payouts still use your configured payout address."); }
    catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  });
  byId("wallet-send-form").addEventListener("submit", async event => {
    event.preventDefault(); if (busy) return;
    busy = true; controls(); message("Building a payment preview…");
    try {
      const amount = Number(byId("send-amount").value);
      if (!Number.isSafeInteger(amount) || amount <= 0) throw new Error("Enter a positive whole number of test sats.");
      draft = await walletPost("preview", {address: byId("send-address").value.trim(), amount_sat: amount, fee_rate: byId("send-fee").value});
      broadcastUncertain = false; showPreview(); message("Review the address, amount, and fee before sending.");
    } catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  });
  byId("wallet-cancel").addEventListener("click", () => { if (busy || broadcastUncertain) return; draft = null; showPreview(); message(""); });
  byId("wallet-confirm").addEventListener("click", send);
  byId("wallet-qr").addEventListener("error", () => { byId("wallet-qr").hidden = true; });
  getWallet(); setInterval(() => { if (!busy && !sending) getWallet(); }, 15000);
})();
