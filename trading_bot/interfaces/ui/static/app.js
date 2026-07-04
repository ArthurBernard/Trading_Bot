// trading_bot dashboard — a pure HTTP client of the read-only API.
//
// On load it fetches the three JSON endpoints (/api/positions, /api/orders,
// /api/kpi) and fills the table shells the server rendered; it then opens an
// EventSource on /api/events (SSE) and re-fetches the tables whenever the engine
// emits an order/fill event. It NEVER calls the application layer directly and
// has no mutation path — the dashboard can only observe the engine.
//
// Money rule: every money field arrives from the API as an exact Decimal STRING
// (the API stringifies Decimals precisely; JSON has no decimal type). Display
// formatting is handed to format.js's `tbFmt` namespace (loaded before this
// file): figures are rounded + thousands-grouped for readability, but the exact
// string the API sent always survives in a `title` tooltip and is NEVER parsed
// back into a computation — display rounding never touches money-exactness.
// The KPI ratios (Sharpe/Sortino/…) are statistical estimators, not money, so
// they come back as JSON numbers and are formatted (not re-derived) likewise.

(function () {
  "use strict";

  // --- DOM helpers --------------------------------------------------------- //

  function el(id) {
    return document.getElementById(id);
  }

  // Escape a value for safe insertion as text content (defence in depth; the
  // API only emits trusted instrument/enum strings, but never trust a feed).
  function esc(value) {
    if (value === null || value === undefined) return "—";
    return String(value).replace(/[&<>"']/g, function (c) {
      return {
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[c];
    });
  }

  function setConn(state, label) {
    var conn = el("conn");
    if (!conn) return;
    conn.className = "conn " + state;
    conn.innerHTML = '<span class="dot"></span>' + esc(label);
  }

  // --- fetch helpers ------------------------------------------------------- //

  async function getJson(path) {
    var resp = await fetch(path, { headers: { Accept: "application/json" } });
    if (!resp.ok) throw new Error(path + " → " + resp.status);
    return resp.json();
  }

  // --- renderers ----------------------------------------------------------- //

  function renderPositions(rows) {
    var body = el("positions-body");
    if (!body) return;
    if (!rows.length) {
      body.innerHTML = '<tr class="empty"><td colspan="5">No positions.</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (p) {
      var unit = tbFmt.splitInstrument(p.instrument);
      return "<tr>" +
        "<td>" + esc(p.instrument) + "</td>" +
        '<td class="num">' + tbFmt.qtyCell(p.net_qty, { base: unit.base }) + "</td>" +
        '<td class="num">' + tbFmt.priceCell(p.avg_entry_price, { quote: unit.quote }) + "</td>" +
        '<td class="num">' + tbFmt.moneyCell(p.realised_pnl, { quote: unit.quote }) + "</td>" +
        '<td class="num">' + tbFmt.moneyCell(p.fees_paid, { quote: unit.quote }) + "</td>" +
        "</tr>";
    }).join("");
  }

  function renderOrders(rows) {
    var body = el("orders-body");
    if (!body) return;
    if (!rows.length) {
      body.innerHTML = '<tr class="empty"><td colspan="8">No orders.</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (o) {
      var unit = tbFmt.splitInstrument(o.instrument);
      return "<tr>" +
        "<td>" + esc(o.client_order_id) + "</td>" +
        "<td>" + esc(o.instrument) + "</td>" +
        '<td class="side-' + esc(o.side) + '">' + esc(o.side) + "</td>" +
        "<td>" + esc(o.type) + "</td>" +
        '<td class="num">' + tbFmt.qtyCell(o.qty, { base: unit.base }) + "</td>" +
        '<td class="num">' + tbFmt.priceCell(o.limit_price, { quote: unit.quote }) + "</td>" +
        '<td class="num">' + tbFmt.qtyCell(o.filled_qty, { base: unit.base }) + "</td>" +
        "<td>" + esc(o.status) + "</td>" +
        "</tr>";
    }).join("");
  }

  function renderKpi(kpi) {
    var body = el("kpi-body");
    if (!body) return;
    // No quote is known at this (single-engine, cross-instrument) KPI level, so
    // the money cells carry no unit suffix — the exact value still survives in
    // the title tooltip via tbFmt.moneyCell().
    var rows = [
      ["Realised PnL", tbFmt.moneyCell(kpi.realised_pnl)],
      ["Fees paid", tbFmt.moneyCell(kpi.fees_paid)],
      ["Equity end", tbFmt.moneyCell(kpi.equity_end)],
      ["Sharpe", tbFmt.ratioCell(kpi.sharpe)],
      ["Sortino", tbFmt.ratioCell(kpi.sortino)],
      ["Max drawdown (%)", tbFmt.pctCell(kpi.max_drawdown)],
      ["Calmar", tbFmt.ratioCell(kpi.calmar)]
    ];
    body.innerHTML = rows.map(function (r) {
      return "<tr><th>" + esc(r[0]) + '</th><td class="num">' + r[1] + "</td></tr>";
    }).join("");
  }

  // --- refresh ------------------------------------------------------------- //

  async function refresh() {
    try {
      var results = await Promise.all([
        getJson("/api/positions"),
        getJson("/api/orders"),
        getJson("/api/kpi")
      ]);
      renderPositions(results[0]);
      renderOrders(results[1]);
      renderKpi(results[2]);
      setConn("ok", "live");
    } catch (err) {
      setConn("down", "API unreachable");
    }
  }

  // --- live updates (SSE) -------------------------------------------------- //

  function connectEvents() {
    var source = new EventSource("/api/events");
    source.onmessage = function (ev) {
      // An order or fill changed the engine's state — re-fetch the tables. The
      // event payload itself is ignored here (the JSON endpoints are the single
      // source of truth); we only use the signal that *something* changed.
      try {
        var data = JSON.parse(ev.data);
        if (data && (data.type === "order" || data.type === "fill")) {
          refresh();
        }
      } catch (e) {
        // A heartbeat / non-JSON comment frame — ignore.
      }
    };
    source.onerror = function () {
      setConn("down", "reconnecting…");
      // EventSource auto-reconnects; nothing to do here.
    };
  }

  // --- boot ---------------------------------------------------------------- //

  document.addEventListener("DOMContentLoaded", function () {
    refresh();
    connectEvents();
  });
})();
