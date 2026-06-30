// trading_bot control plane — list strategies, start/stop, switch mode.
// Pure HTTP client of /api/strategies; switching to LIVE asks for a typed
// confirmation that maps to {confirm:true} on the mode endpoint (the server
// also refuses live without it).
"use strict";

const MODES = ["paper", "testnet", "live"];
const body = document.getElementById("strategies-body");
const conn = document.getElementById("conn");
const msg = document.getElementById("msg");

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
    const detail = (data && data.detail) || resp.statusText;
    throw new Error(detail);
  }
  return data;
}

function modeSelect(s) {
  const opts = MODES.map(
    (m) => `<option value="${m}"${m === s.mode ? " selected" : ""}>${m}</option>`
  ).join("");
  return `<select data-name="${s.name}" class="mode-select">${opts}</select>`;
}

function row(s) {
  const statusBadge = s.running
    ? '<span class="side-buy">running</span>'
    : '<span class="conn">stopped</span>';
  const action = s.running
    ? `<button data-name="${s.name}" data-act="stop">Stop</button>`
    : `<button data-name="${s.name}" data-act="start">Start</button>`;
  const pnl = s.realised_pnl === null ? "—" : s.realised_pnl;
  return `<tr>
    <td>${s.name}</td>
    <td>${s.kind}</td>
    <td>${modeSelect(s)}</td>
    <td>${statusBadge}</td>
    <td class="num">${pnl}</td>
    <td class="num">${s.open_orders}</td>
    <td>${action}</td>
  </tr>`;
}

async function refresh() {
  try {
    const list = await api("/api/strategies");
    setConn(true);
    if (!list.length) {
      body.innerHTML = '<tr class="empty"><td colspan="7">No strategies declared.</td></tr>';
      return;
    }
    body.innerHTML = list.map(row).join("");
  } catch (e) {
    setConn(false);
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

async function onModeChange(ev) {
  const sel = ev.target.closest("select.mode-select");
  if (!sel) return;
  const name = sel.dataset.name;
  const mode = sel.value;
  let confirm = false;
  if (mode === "live") {
    // Real money — deliberate, typed acknowledgement (the server also enforces it).
    const typed = window.prompt(
      `Switch "${name}" to LIVE (REAL MONEY)?\nType I UNDERSTAND to confirm:`
    );
    if (typed !== "I UNDERSTAND") {
      flash(`${name}: live switch cancelled`, true);
      await refresh();
      return;
    }
    confirm = true;
  }
  try {
    await api(`/api/strategies/${encodeURIComponent(name)}/mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, confirm }),
    });
    flash(`${name}: mode → ${mode}`, false);
  } catch (e) {
    flash(`${name}: ${e.message}`, true);
  } finally {
    await refresh();
  }
}

body.addEventListener("click", onClick);
body.addEventListener("change", onModeChange);
refresh();
setInterval(refresh, 3000);
