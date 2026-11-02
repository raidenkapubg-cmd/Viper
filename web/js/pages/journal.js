// Page 4 — Journal: dual ledger (live / shadow) + granular CSV export.
import { on, state } from "../store.js";
import { api, h, fmt, money, cls, dur, istDateTime, STRAT, REGIME, toast } from "../util.js";

export function mount(root) {
  const offs = [];
  const today = new Date(Date.now() + 330 * 60000).toISOString().slice(0, 10);
  const mode = h("select", {}, h("option", { value: "live" }, "Viper Live Only"), h("option", { value: "shadow" }, "Shadow Only"), h("option", { value: "both", selected: true }, "Both"));
  const dateMode = h("select", {}, h("option", { value: "today", selected: true }, "Today"), h("option", { value: "date" }, "Custom Date"), h("option", { value: "range" }, "Date Range"), h("option", { value: "all" }, "All Dates"));
  const day = h("input", { type: "date", value: today });
  const start = h("input", { type: "date", value: today });
  const end = h("input", { type: "date", value: today });
  const inst = h("select", {}, ...state.instruments.map((i) => h("option", { value: i.key }, i.label)), h("option", { value: "ALL", selected: true }, "All Instruments"));
  const dayWrap = h("label", { class: "fld hide" }, "Date", day);
  const rangeWrap = h("div", { class: "row hide" }, h("label", { class: "fld" }, "From", start), h("label", { class: "fld" }, "To", end));
  const liveBody = h("tbody"), shBody = h("tbody");
  const liveStats = h("span", { class: "note" }), shStats = h("span", { class: "note" });

  function q() {
    const p = new URLSearchParams({ mode: mode.value, date_mode: dateMode.value, instrument: inst.value });
    if (dateMode.value === "date") p.set("day", day.value);
    if (dateMode.value === "range") { p.set("start", start.value); p.set("end", end.value); }
    return p;
  }
  function syncInputs() {
    dayWrap.classList.toggle("hide", dateMode.value !== "date");
    rangeWrap.classList.toggle("hide", dateMode.value !== "range");
  }
  [mode, dateMode, day, start, end, inst].forEach((el) => el.addEventListener("change", () => { syncInputs(); load(); }));

  const exportBtn = h("button", { class: "btn primary", onclick: async () => {
    // Fetch first so a validation error shows as a toast instead of a broken download.
    try {
      const r = await fetch(`/api/journal/export.csv?${q()}`);
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || `HTTP ${r.status}`); }
      const blob = await r.blob();
      const name = (r.headers.get("content-disposition") || "").match(/filename="([^"]+)"/)?.[1] || "viper.csv";
      const a = h("a", { href: URL.createObjectURL(blob), download: name });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    } catch (e) { toast(e.message, true); }
  } }, "Export CSV");

  const HEAD = ["#", "Entry (IST)", "Exit (IST)", "Instrument", "Strike / symbol", "Qty", "Entry", "Exit", "Gross", "Net", "Hold", "Strategy", "Regime", "Exit reason", "MFE / MAE"];
  const table = (body) => h("div", { class: "tablewrap", style: { maxHeight: "460px" } }, h("table", { class: "t" },
    h("thead", {}, h("tr", {}, ...HEAD.map((x, i) => h("th", { class: [1, 2, 3, 4, 11, 12, 13].includes(i) ? "l" : "" }, x)))), body));

  root.append(h("div", { class: "page" },
    h("div", { class: "ptitle" }, h("h1", {}, "Journal"), h("span", { class: "sub" }, "every Viper trade, live and shadow, with full parity metrics")),
    h("div", { class: "sect" }, h("h2", {}, "Filter & export")),
    h("div", { class: "row", style: { alignItems: "flex-end" } },
      h("label", { class: "fld" }, "Mode", mode), h("label", { class: "fld" }, "Date filter", dateMode), dayWrap, rangeWrap,
      h("label", { class: "fld" }, "Instrument", inst), exportBtn),
    h("div", { class: "sect" }, h("h2", {}, "A · Viper live trades"), liveStats), table(liveBody),
    h("div", { class: "sect" }, h("h2", {}, "B · Shadow trades"), shStats), table(shBody)));

  function rowEl(r) {
    const hold = r.exit_ts ? r.exit_ts - r.entry_ts : null;
    return h("tr", {},
      h("td", {}, String(r.id)), h("td", { class: "l" }, istDateTime(r.entry_ts)), h("td", { class: "l" }, r.exit_ts ? istDateTime(r.exit_ts) : h("span", { class: "up" }, "open")),
      h("td", { class: "l" }, r.instrument), h("td", { class: "l", title: r.symbol }, r.strike ? `${fmt(r.strike, 0)} ${r.option_type || ""}` : r.symbol),
      h("td", {}, String(r.qty)), h("td", {}, fmt(r.entry_price)), h("td", {}, fmt(r.exit_price)),
      h("td", { class: cls(r.gross_pnl) }, money(r.gross_pnl)), h("td", { class: cls(r.net_pnl) }, money(r.net_pnl)),
      h("td", {}, dur(hold)), h("td", { class: "l" }, STRAT[r.strategy] || r.strategy), h("td", { class: "l" }, REGIME[r.regime] || r.regime || "—"),
      h("td", { class: "l" }, r.exit_reason || "—"), h("td", {}, `${fmt(r.mfe)} / ${fmt(r.mae)}`));
  }
  const statTxt = (s) => s.trades ? `${s.trades} closed · win ${(s.win_rate * 100).toFixed(0)}% · net ${money(s.net)} · avg ${money(s.avg)} · avg hold ${dur(s.avg_hold_s)}` : "no closed trades in filter";

  async function load() {
    let res;
    try {
      const p = q();
      p.set("mode", "both");
      res = await api(`/api/journal?${p}`);
    } catch (e) { toast(e.message, true); return; }
    const live = res.rows.filter((r) => r.mode === "live"), sh = res.rows.filter((r) => r.mode === "shadow");
    for (const [body, rows, m] of [[liveBody, live, "live"], [shBody, sh, "shadow"]]) {
      body.textContent = "";
      const hidden = mode.value !== "both" && mode.value !== m;
      if (hidden) body.append(h("tr", { class: "empty" }, h("td", { colspan: HEAD.length }, "Hidden by the Mode filter.")));
      else if (!rows.length) body.append(h("tr", { class: "empty" }, h("td", { colspan: HEAD.length }, "No trades for this filter.")));
      else rows.forEach((r) => body.append(rowEl(r)));
    }
    liveStats.textContent = statTxt(res.stats.live);
    shStats.textContent = statTxt(res.stats.shadow);
  }
  load();
  for (const t of ["live_close", "shadow_close", "shadow_open", "live_pending"]) offs.push(on(t, () => load()));
  return () => offs.forEach((f) => f());
}
