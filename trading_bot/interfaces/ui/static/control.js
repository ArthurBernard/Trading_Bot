// trading_bot control plane — list strategies, start/stop, switch mode.
// Pure HTTP client of /api/strategies. Switching to LIVE opens a typed-confirmation
// modal that maps to {confirm:true} on the mode endpoint (the server also refuses
// live without it).
"use strict";

const MODES = ["paper", "testnet", "live"];
const PHRASE = "I UNDERSTAND";

const body = document.getElementById("strategies-body");
const conn = document.getElementById("conn");
const msg = document.getElementById("msg");
const sumTotal = document.getElementById("sum-total");
const sumRunning = document.getElementById("sum-running");

const modal = document.getElementById("live-modal");
const modalName = document.getElementById("live-modal-name");
const modalInput = document.getElementById("live-modal-input");
const modalConfirm = document.getElementById("live-modal-confirm");
const modalCancel = document.getElementById("live-modal-cancel");

let pendingLive = null; // {name} awaiting confirmation

function setConn(ok) {
  conn.className = "conn " + (ok ? "ok" : "down");
  conn.innerHTML = '<span class="dot"></span>' + (ok ? "connected" : "disconnected");
}

function flash(text, isError) {
  msg.textContent = text;
  msg.style.color = isError ? "var(--err)" : "var(--muted)";
}

async function api(path, opts) {
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (e) { /* no body */ }
  if (!resp.ok) {
    throw new Error((data && data.detail) || resp.statusText);
  }
  return data;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function modeSelect(s) {
  const opts = MODES.map(
    (m) => `<option value="${m}"${m === s.mode ? " selected" : ""}>${m}</option>`
  ).join("");
  return `<select data-name="${escapeHtml(s.name)}" class="mode-select" aria-label="mode">${opts}</select>`;
}

function row(s) {
  const status = s.running
    ? '<span class="run-pill is-running"><span class="dot"></span>running</span>'
    : '<span class="run-pill is-stopped"><span class="dot"></span>stopped</span>';
  const action = s.running
    ? `<button data-name="${escapeHtml(s.name)}" data-act="stop" class="btn-stop">Stop</button>`
    : `<button data-name="${escapeHtml(s.name)}" data-act="start" class="btn-start">Start</button>`;
  const pnl = s.realised_pnl === null ? '<span class="conn">—</span>' : escapeHtml(s.realised_pnl);
  return `<tr>
    <td>${escapeHtml(s.name)}</td>
    <td>${escapeHtml(s.kind)}</td>
    <td><span class="badge badge-${s.mode}">${s.mode}</span> ${modeSelect(s)}</td>
    <td>${status}</td>
    <td class="num">${pnl}</td>
    <td class="num">${s.open_orders}</td>
    <td>${action}</td>
  </tr>`;
}

function groupHeader(exchange, count) {
  return `<tr class="group"><td colspan="7">
    <span class="group-name">${escapeHtml(exchange)}</span>
    <span class="group-count">${count}</span>
  </td></tr>`;
}

async function refresh() {
  try {
    const list = await api("/api/strategies");
    setConn(true);
    sumTotal.textContent = list.length;
    sumRunning.textContent = list.filter((s) => s.running).length;
    if (!list.length) {
      body.innerHTML = '<tr class="empty"><td colspan="7">No strategies declared.</td></tr>';
      return;
    }
    // Group strategies by exchange (alphabetical), each under a header row.
    const byEx = {};
    for (const s of list) (byEx[s.exchange] = byEx[s.exchange] || []).push(s);
    let html = "";
    for (const ex of Object.keys(byEx).sort()) {
      html += groupHeader(ex, byEx[ex].length);
      html += byEx[ex].map(row).join("");
    }
    body.innerHTML = html;
  } catch (e) {
    setConn(false);
  }
}

async function setMode(name, mode, confirm) {
  try {
    await api(`/api/strategies/${encodeURIComponent(name)}/mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, confirm: !!confirm }),
    });
    flash(`${name}: mode → ${mode}`, false);
  } catch (e) {
    flash(`${name}: ${e.message}`, true);
  } finally {
    await refresh();
  }
}

async function onClick(ev) {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const { name, act } = btn.dataset;
  btn.disabled = true;
  try {
    await api(`/api/strategies/${encodeURIComponent(name)}/${act}`, { method: "POST" });
    flash(`${name}: ${act}ed`, false);
  } catch (e) {
    flash(`${name}: ${e.message}`, true);
  } finally {
    await refresh();
  }
}

function onModeChange(ev) {
  const sel = ev.target.closest("select.mode-select");
  if (!sel) return;
  const name = sel.dataset.name;
  const mode = sel.value;
  if (mode === "live") {
    openLiveModal(name); // deliberate, typed acknowledgement
    return;
  }
  setMode(name, mode, false); // paper / testnet — no confirmation
}

// --- live confirmation modal ---------------------------------------------- //

function openLiveModal(name) {
  pendingLive = { name };
  modalName.textContent = name;
  modalInput.value = "";
  modalConfirm.disabled = true;
  modal.classList.add("open");
  modalInput.focus();
}

function closeLiveModal() {
  modal.classList.remove("open");
  pendingLive = null;
  refresh(); // re-render so a cancelled select reverts to the server's mode
}

modalInput.addEventListener("input", () => {
  modalConfirm.disabled = modalInput.value.trim() !== PHRASE;
});
modalInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !modalConfirm.disabled) modalConfirm.click();
  if (e.key === "Escape") closeLiveModal();
});
modalConfirm.addEventListener("click", async () => {
  const name = pendingLive && pendingLive.name;
  modal.classList.remove("open");
  if (name) await setMode(name, "live", true);
  pendingLive = null;
});
modalCancel.addEventListener("click", () => {
  flash(`${pendingLive ? pendingLive.name : ""}: live switch cancelled`, true);
  closeLiveModal();
});
modal.addEventListener("click", (e) => { if (e.target === modal) closeLiveModal(); });

body.addEventListener("click", onClick);
body.addEventListener("change", onModeChange);
refresh();
setInterval(refresh, 3000);
