// trading_bot dashboard — a pure HTTP client of the read-only API.
//
// On load it fetches the three JSON endpoints (/api/positions, /api/orders,
// /api/kpi) and fills the table shells the server rendered; it then opens an
// EventSource on /api/events (SSE) and re-fetches the tables whenever the engine
// emits an order/fill event. It NEVER calls the application layer directly and
// has no mutation path — the dashboard can only observe the engine.
//
// Money rule: every money field arrives from the API as an exact Decimal STRING
// (the API stringifies Decimals precisely; JSON has no decimal type). This JS
// renders those strings VERBATIM and never parseFloat()s a money field — doing
// so would reintroduce the binary-float rounding the API took care to avoid.
// The KPI ratios (Sharpe/Sortino/…) are statistical estimators, not money, so
// they come back as JSON numbers and are shown as-is.

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

  // Render a money STRING verbatim (no numeric parsing). A null optional shows
  // as an em dash.
  function money(value) {
    return value === null || value === undefined ? "—" : esc(value);
  }

  // A KPI ratio is a plain JSON number — show a few significant digits.
  function ratio(value) {
    if (value === null || value === undefined || isNaN(value)) return "—";
    return Number(value).toFixed(4);
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
      return "<tr>" +
        "<td>" + esc(p.instrument) + "</td>" +
        '<td class="num">' + money(p.net_qty) + "</td>" +
        '<td class="num">' + money(p.avg_entry_price) + "</td>" +
        '<td class="num">' + money(p.realised_pnl) + "</td>" +
        '<td class="num">' + money(p.fees_paid) + "</td>" +
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
      return "<tr>" +
        "<td>" + esc(o.client_order_id) + "</td>" +
        "<td>" + esc(o.instrument) + "</td>" +
        '<td class="side-' + esc(o.side) + '">' + esc(o.side) + "</td>" +
        "<td>" + esc(o.type) + "</td>" +
        '<td class="num">' + money(o.qty) + "</td>" +
        '<td class="num">' + money(o.limit_price) + "</td>" +
        '<td class="num">' + money(o.filled_qty) + "</td>" +
        "<td>" + esc(o.status) + "</td>" +
        "</tr>";
    }).join("");
  }

  function renderKpi(kpi) {
    var body = el("kpi-body");
    if (!body) return;
    var rows = [
      ["Realised PnL", money(kpi.realised_pnl)],
      ["Fees paid", money(kpi.fees_paid)],
      ["Equity end", money(kpi.equity_end)],
      ["Sharpe", ratio(kpi.sharpe)],
      ["Sortino", ratio(kpi.sortino)],
      ["Max drawdown", ratio(kpi.max_drawdown)],
      ["Calmar", ratio(kpi.calmar)]
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
