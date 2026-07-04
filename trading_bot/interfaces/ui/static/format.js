// trading_bot dashboard — shared display formatters (the `tbFmt` namespace).
//
// Every page fetches money/qty/price as EXACT Decimal STRINGS from the API (see
// app.py's money-exactness discipline) and ratios as plain JSON numbers. This
// file turns those raw values into READABLE, display-rounded, thousands-grouped,
// unit-labelled text for a human operator — but rounding is DISPLAY ONLY:
//   * the exact raw value the API sent always survives in a `title` tooltip
//     (hover any figure to see it), and
//   * a formatted string is NEVER parsed back into a computation anywhere.
// Plain vanilla JS, no modules, no build step (matches base.html's dependency-
// free style); a single global namespace, `tbFmt`, is attached to `window`.
// format.js does not depend on base.html — it defines its own internal escape
// so it can be dropped onto any page (incl. the legacy single-engine dashboard).

(function (global) {
  "use strict";

  // --- internal helpers ----------------------------------------------------- //

  // Escape a value for safe insertion into HTML (duplicated from base.html's
  // escapeHtml — format.js must not depend on the shell it may be loaded into).
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Parse a raw API value (a Decimal string or a JSON number) to a finite
  // Number for DISPLAY only, or null when it doesn't parse — never a lie: an
  // unparseable value falls back to showing the raw string, not "—" or "0".
  function toFiniteNumber(value) {
    if (value == null) return null;
    if (typeof value === "string" && value.trim() === "") return null;
    var n = Number(value);
    return isFinite(n) ? n : null;
  }

  // Resolve a `dp` argument that may be a bare number (the documented positional
  // form, e.g. `tbFmt.pct(v, 2)`) or an options object carrying `.dp` (the form
  // `cell()` passes through uniformly for every kind).
  function resolveDp(dp, fallback) {
    if (typeof dp === "number") return dp;
    if (dp && typeof dp === "object" && typeof dp.dp === "number") return dp.dp;
    return fallback;
  }

  // Split an "BASE/QUOTE" instrument string (every instrument the API renders)
  // into its two legs, for deriving a row's unit labels client-side.
  function splitInstrument(instrument) {
    var parts = String(instrument == null ? "" : instrument).split("/");
    return { base: parts[0] || null, quote: parts[1] || null };
  }

  // --- value formatters (plain text, no HTML) -------------------------------- //

  // money(v, {dp=2, currency=null}) -> grouped en-US string fixed at `dp`
  // decimals; "—" for null. `v` is the API's exact Decimal string (or a plain
  // Number, e.g. a chart point already Number()'d for plotting); Number()'d here
  // for DISPLAY only. An unparseable value shows the raw string verbatim (never
  // lies about it). `currency`, when given, is appended as plain text — a
  // convenience for call sites that render a bare string (e.g. the legacy
  // dashboard's KPI table); `cell()`/`moneyCell()` add the unit as a separate
  // muted `<span>` instead, so they never pass `currency` here.
  function money(v, opts) {
    opts = opts || {};
    var dp = typeof opts.dp === "number" ? opts.dp : 2;
    if (v == null) return "—";
    var n = toFiniteNumber(v);
    if (n == null) return String(v);
    var text = n.toLocaleString("en-US", {
      minimumFractionDigits: dp,
      maximumFractionDigits: dp,
    });
    return opts.currency ? text + " " + opts.currency : text;
  }

  // qty(v, {base=null, maxDp=8}) -> grouped, trailing zeros trimmed (no
  // `minimumFractionDigits`, so `1.50000000` reads as `1.5`); "—" for null.
  function qty(v, opts) {
    opts = opts || {};
    var maxDp = typeof opts.maxDp === "number" ? opts.maxDp : 8;
    if (v == null) return "—";
    var n = toFiniteNumber(v);
    if (n == null) return String(v);
    var text = n.toLocaleString("en-US", { maximumFractionDigits: maxDp });
    return opts.base ? text + " " + opts.base : text;
  }

  // price(v, {quote=null}) -> adaptive precision so both a BTC and a low-cap alt
  // read sensibly: |v| >= 1000 -> 2dp; |v| >= 1 -> 2-4dp (trailing zeros
  // trimmed down to 2); |v| < 1 -> up to 6 significant digits. "—" for null.
  function price(v, opts) {
    opts = opts || {};
    if (v == null) return "—";
    var n = toFiniteNumber(v);
    if (n == null) return String(v);
    var abs = Math.abs(n);
    var text;
    if (abs === 0) {
      text = "0";
    } else if (abs >= 1000) {
      text = n.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    } else if (abs >= 1) {
      text = n.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 4,
      });
    } else {
      text = n.toLocaleString("en-US", { maximumSignificantDigits: 6 });
    }
    return opts.quote ? text + " " + opts.quote : text;
  }

  // pct(v, dp=1) -> a ratio (e.g. max-drawdown's 0.123) as "12.3%". "—" for null.
  function pct(v, dp) {
    var d = resolveDp(dp, 1);
    if (v == null) return "—";
    var n = toFiniteNumber(v);
    if (n == null) return String(v);
    return (
      (n * 100).toLocaleString("en-US", {
        minimumFractionDigits: d,
        maximumFractionDigits: d,
      }) + "%"
    );
  }

  // ratio(v, dp=2) -> a plain fixed-dp number (Sharpe/Sortino/Calmar). "—" for null.
  function ratio(v, dp) {
    var d = resolveDp(dp, 2);
    if (v == null) return "—";
    var n = toFiniteNumber(v);
    if (n == null) return String(v);
    return n.toLocaleString("en-US", {
      minimumFractionDigits: d,
      maximumFractionDigits: d,
    });
  }

  // --- HTML cell builders ----------------------------------------------------- //

  var VALUE_FORMATTERS = { money: money, qty: qty, price: price, pct: pct, ratio: ratio };

  // cell(kind, v, opts) -> a safe HTML fragment: the formatted value, a muted
  // unit suffix (`<span class="unit">`) when `opts.unit` is a known label, and a
  // `title="<exact raw> <unit>"` tooltip carrying the value the API actually
  // sent (never the rounded display text). Every interpolated string is
  // HTML-escaped.
  //
  // `opts.unit`:
  //   - omitted           -> no unit context; the tooltip is just the raw value.
  //   - a string          -> the muted suffix + `"<raw> <unit>"` tooltip.
  //   - explicit `null`   -> a MIXED unit (e.g. a KPI row folding several quote
  //                          currencies): no suffix, `title="mixed quote
  //                          currencies"` (per-kind wrappers pass this through
  //                          from a `quote`/`base` option that is `null`).
  // `opts.title`, when given, overrides the computed tooltip outright.
  // A null value renders "—" in a `class="muted"` span (the existing convention
  // for a missing/undefined figure).
  function cell(kind, v, opts) {
    opts = opts || {};
    var format = VALUE_FORMATTERS[kind];
    var text = format ? format(v, opts) : v == null ? "—" : String(v);
    var hasUnit = Object.prototype.hasOwnProperty.call(opts, "unit");
    var unit = opts.unit;
    var mixed = hasUnit && unit == null;
    // No unit suffix on a null value — there is nothing to attach a unit to.
    var unitHtml =
      unit != null && v != null ? ' <span class="unit">' + escapeHtml(unit) + "</span>" : "";
    var title = opts.title;
    if (title == null) {
      if (mixed) {
        title = "mixed quote currencies";
      } else if (v != null) {
        title = unit != null ? String(v) + " " + unit : String(v);
      }
    }
    var titleAttr = title != null ? ' title="' + escapeHtml(title) + '"' : "";
    var cls = v == null ? ' class="muted"' : "";
    return "<span" + cls + titleAttr + ">" + escapeHtml(text) + "</span>" + unitHtml;
  }

  // moneyCell(v, {quote, dp, title}) -> cell('money', ...) with `quote` as the
  // cell's unit (the muted suffix + tooltip currency).
  function moneyCell(v, opts) {
    opts = opts || {};
    var cellOpts = { dp: opts.dp, title: opts.title };
    if ("quote" in opts) cellOpts.unit = opts.quote;
    return cell("money", v, cellOpts);
  }

  // qtyCell(v, {base, maxDp, title}) -> cell('qty', ...) with `base` as the
  // cell's unit.
  function qtyCell(v, opts) {
    opts = opts || {};
    var cellOpts = { maxDp: opts.maxDp, title: opts.title };
    if ("base" in opts) cellOpts.unit = opts.base;
    return cell("qty", v, cellOpts);
  }

  // priceCell(v, {quote, title}) -> cell('price', ...) with `quote` as the
  // cell's unit.
  function priceCell(v, opts) {
    opts = opts || {};
    var cellOpts = { title: opts.title };
    if ("quote" in opts) cellOpts.unit = opts.quote;
    return cell("price", v, cellOpts);
  }

  // pctCell(v, {dp, title}) -> cell('pct', ...). No unit suffix — the `%` is
  // already part of the formatted text (use the column header for a uniform
  // unit label, e.g. "Max DD (%)").
  function pctCell(v, opts) {
    opts = opts || {};
    return cell("pct", v, { dp: opts.dp, title: opts.title });
  }

  // ratioCell(v, {dp, title}) -> cell('ratio', ...). No unit (Sharpe/Sortino/
  // Calmar are dimensionless).
  function ratioCell(v, opts) {
    opts = opts || {};
    return cell("ratio", v, { dp: opts.dp, title: opts.title });
  }

  // --- public namespace ------------------------------------------------------- //

  global.tbFmt = {
    money: money,
    qty: qty,
    price: price,
    pct: pct,
    ratio: ratio,
    cell: cell,
    moneyCell: moneyCell,
    qtyCell: qtyCell,
    priceCell: priceCell,
    pctCell: pctCell,
    ratioCell: ratioCell,
    splitInstrument: splitInstrument,
  };
})(window);
