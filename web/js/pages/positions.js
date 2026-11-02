// Page 3 — Positions & Risk Control (live trades only; shadow is excluded by design).
import { on, state } from "../store.js";
import { api, h, fmt, money, cls, dur, toast, STRAT, istTime } from "../util.js";

const CAP = [5, 10, 20, 25, 50];
const SL = [0.5, 1, 2, 3, 5];
const HOLD = [3, 5, 15, 30, 60];

export function mount(root) {
  const offs = [];
  let risk = state.risk;
  const body = h("tbody");
  const autoBox = h("div");
  const guard = h("div", { class: "guard" });
  const blockersEl = h("div", { class: "mono muted", style: { fontSize: "11.5px", marginTop: "6px" } });
  const equityIn = h("input", { type: "number", min: 0, step: 1000, style: { width: "160px" } });
  const marginEl = h("span", { class: "mono" });
  const deployEl = h("span", { class: "mono" });

  root.append(h("div", { class: "page" },
    h("div", { class: "ptitle" }, h("h1", {}, "Positions & risk control"), h("span", { class: "sub" }, "real FYERS positions only · shadow trades live on the Shadow page")),
    h("div", { class: "sect" }, h("h2", {}, "Live positions"), h("span", { class: "note" }, "synced from the FYERS order socket; trades closed in the FYERS app reconcile automatically"),
      h("span", { class: "spacer" }), h("button", { class: "btn primary", onclick: () => deployModal() }, "Manual deploy order")),
    h("div", { class: "tablewrap" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, ...["Symbol", "Side", "Qty", "Avg entry", "LTP", "Unrealized", "Hold", "Stop", "Target", "Strategy", ""].map((x, i) => h("th", { class: [0, 1, 9].includes(i) ? "l" : "" }, x)))),
      body)),

    h("div", { class: "sect" }, h("h2", {}, "Per-instrument automation"), h("span", { class: "note" }, "ON = rule execution at fixed sizing · AUTO = regime switching + dynamic sizing · OFF = never touch")),
    autoBox, blockersEl,

    h("div", { class: "sect" }, h("h2", {}, "Equity & sizing")),
    h("div", { class: "row" },
      h("label", { class: "fld" }, "Equity allocation (₹)", equityIn),
      h("button", { class: "btn", onclick: () => save({ equity_allocation: +equityIn.value }) }, "Set"),
      h("div", { class: "kv", style: { minWidth: "320px", marginLeft: "18px" } },
        h("div", {}, "FYERS available margin"), marginEl,
        h("div", {}, "Deployable (min of both)"), deployEl)),

    h("div", { class: "sect" }, h("h2", {}, "Risk & margin guardrails"), h("span", { class: "note" }, "choose a preset or type an exact value")),
    guard,
    h("div", { class: "kill" },
      h("div", { style: { flex: 1 } }, h("b", {}, "Emergency kill switch"), h("div", { class: "muted", style: { fontSize: "12px" } }, "Squares off every live position at FYERS (exit-all), cancels Viper's stop orders and blocks new live entries until released.")),
      h("button", { class: "btn danger", id: "killBtn", onclick: async () => {
        if (risk.kill_switch) { await save({ kill_switch: false }); return; }
        if (!confirm("Square off ALL live positions now and block new entries?")) return;
        await api("/api/kill", { method: "POST" });
        toast("Kill switch engaged");
      } }, "Engage"))));

  // ------------------------------------------------------- positions table
  function paintPositions(live) {
    body.textContent = "";
    if (!live.open.length) { body.append(h("tr", { class: "empty" }, h("td", { colspan: 11 }, "No live positions."))); return; }
    for (const p of live.open) {
      body.append(h("tr", {},
        h("td", { class: "l" }, p.symbol, p.origin === "manual" ? h("span", { class: "muted" }, "  (opened in FYERS app)") : null),
        h("td", { class: "l" }, p.side === 1 ? "BUY" : "SELL"),
        h("td", {}, String(p.qty)), h("td", {}, fmt(p.entry_price)), h("td", {}, fmt(p.ltp)),
        h("td", { class: cls(p.upnl) }, money(p.upnl)), h("td", {}, dur(p.held_s)),
        h("td", {}, p.stop ? fmt(p.stop) : "—"), h("td", {}, Number.isFinite(p.target) ? fmt(p.target) : "—"),
        h("td", { class: "l" }, STRAT[p.strategy] || p.strategy),
        h("td", {}, h("div", { class: "row", style: { justifyContent: "flex-end", gap: "6px" } },
          h("button", { class: "btn sm", onclick: () => modifyModal(p) }, "Modify SL/TP"),
          h("button", { class: "btn sm danger", onclick: async () => {
            if (!confirm(`Square off ${p.symbol} x${p.qty} at market?`)) return;
            try { await api(`/api/positions/${p.id}/close`, { method: "POST" }); toast("Exit order sent"); } catch (e) { toast(e.message, true); }
          } }, "Close")))));
    }
  }
  offs.push(on("books", (b) => paintPositions(b.live)));
  if (state.books) paintPositions(state.books.live);

  // ---------------------------------------------------- automation matrix
  function tri(key, value) {
    const knob = h("i", { class: "knob" });
    const wrap = h("div", { class: "tri", "data-v": value, role: "radiogroup", "aria-label": key });
    const states = ["ON", "OFF", "AUTO"];
    const place = (v) => { knob.style.transform = `translateX(${states.indexOf(v) * 58}px)`; wrap.dataset.v = v; [...wrap.querySelectorAll("button")].forEach((b) => b.classList.toggle("on", b.dataset.v === v)); };
    wrap.append(knob, ...states.map((s) => h("button", { "data-v": s, role: "radio", onclick: async () => {
      if (s !== "OFF" && !state.boot.live_trading_enabled) toast("Saved. Note: LIVE_TRADING_ENABLED=false on the server, so no real orders will be sent.");
      place(s);
      await save({ automation: { [key]: s } });
    } }, s)));
    requestAnimationFrame(() => place(value));
    return wrap;
  }
  function paintRisk(r) {
    risk = r;
    autoBox.textContent = "";
    for (const inst of state.instruments) {
      autoBox.append(h("div", { class: "auto-row" }, h("b", { class: "mx-name", style: { fontSize: "13px" } }, inst.label), tri(inst.key, r.automation[inst.key] || "OFF"), h("span", { class: "mono muted", "data-block": inst.key, style: { fontSize: "11px" } })));
    }
    if (document.activeElement !== equityIn) equityIn.value = r.equity_allocation;
    guard.textContent = "";
    guard.append(
      field("Max capital use / trade", "max_capital_pct", CAP, "%", r.max_capital_pct, "premium outlay ≤ this % of deployable equity"),
      field("Max stop loss / trade", "max_sl_pct", SL, "%", r.max_sl_pct, "worst-case loss ≤ this % of deployable equity"),
      field("Max hold time / trade", "max_hold_min", HOLD, "min", r.max_hold_min, "auto square-off when it expires"),
      field("Daily loss limit", "daily_loss_pct", [1, 2, 3, 5], "%", r.daily_loss_pct, "blocks new live entries for the day"),
      field("Max concurrent live", "max_open_live", [1, 2, 3, 4], "", r.max_open_live, "across all instruments"));
    const kb = document.getElementById("killBtn");
    if (kb) kb.textContent = r.kill_switch ? "Release kill switch" : "Engage";
    refreshMeta();
  }
  function field(label, k, presets, unit, val, hint) {
    const sel = h("select", {}, ...presets.map((p) => h("option", { value: p, selected: +p === +val }, `${p}${unit === "min" ? "m" : unit}`)), h("option", { value: "custom", selected: !presets.map(Number).includes(+val) }, "custom"));
    const inp = h("input", { type: "number", step: "any", min: 0, value: val });
    sel.addEventListener("change", () => { if (sel.value !== "custom") { inp.value = sel.value; save({ [k]: +sel.value }); } else inp.focus(); });
    inp.addEventListener("change", () => save({ [k]: +inp.value }));
    return h("div", {}, h("label", { class: "fld" }, label), h("div", { class: "pair" }, sel, inp, h("span", { class: "muted mono", style: { alignSelf: "center" } }, unit)), h("div", { class: "muted", style: { fontSize: "11px", marginTop: "4px" } }, hint));
  }
  async function save(patch) {
    try { paintRisk(await api("/api/risk", { json: patch })); toast("Saved"); } catch (e) { toast(e.message, true); }
  }
  async function refreshMeta() {
    try {
      const m = await api("/api/risk");
      marginEl.textContent = m.available_margin != null ? money(m.available_margin) : "not connected";
      deployEl.textContent = money(m.deployable);
      for (const [k, b] of Object.entries(m.blockers)) {
        const el = autoBox.querySelector(`[data-block="${k}"]`);
        if (el) el.textContent = b.length ? "live blocked: " + b.join(" · ") : "live ready";
      }
    } catch {}
  }
  offs.push(on("risk", paintRisk));
  paintRisk(risk);

  // ------------------------------------------------------------- modals
  function modal(title, bodyEls, onOk, okLabel = "Confirm") {
    const scrim = h("div", { class: "scrim", onclick: (e) => e.target === scrim && scrim.remove() });
    const ok = h("button", { class: "btn primary", onclick: async () => { ok.disabled = true; try { await onOk(); scrim.remove(); } catch (e) { toast(e.message, true); ok.disabled = false; } } }, okLabel);
    scrim.append(h("div", { class: "modal", role: "dialog", "aria-label": title },
      h("header", {}, h("h3", {}, title), h("button", { class: "btn sm", onclick: () => scrim.remove() }, "Esc")),
      h("div", { class: "body" }, bodyEls), h("footer", {}, h("button", { class: "btn", onclick: () => scrim.remove() }, "Cancel"), ok)));
    document.body.append(scrim);
    const esc = (e) => { if (e.key === "Escape") { scrim.remove(); window.removeEventListener("keydown", esc); } };
    window.addEventListener("keydown", esc);
  }
  function modifyModal(p) {
    const s = h("input", { type: "number", step: 0.05, value: p.stop || "" });
    const t = h("input", { type: "number", step: 0.05, value: Number.isFinite(p.target) ? p.target : "" });
    modal(`Modify ${p.symbol}`, [h("label", { class: "fld" }, "Stop (premium)", s), h("label", { class: "fld" }, "Target (premium)", t),
      h("div", { class: "muted", style: { fontSize: "12px" } }, "The protective SL-M order at FYERS is modified to match.")],
      () => api(`/api/positions/${p.id}/modify`, { json: { stop: s.value ? +s.value : null, target: t.value ? +t.value : null } }));
  }
  function deployModal() {
    const inst = h("select", {}, state.instruments.map((i) => h("option", { value: i.key }, i.label)));
    const side = h("select", {}, h("option", { value: "CE" }, "Buy CALL (CE)"), h("option", { value: "PE" }, "Buy PUT (PE)"));
    const off = h("select", {}, ...[[-2, "ITM 2"], [-1, "ITM 1"], [0, "ATM"], [1, "OTM 1"], [2, "OTM 2"]].map(([v, l]) => h("option", { value: v, selected: v === 0 }, l)));
    const lots = h("input", { type: "number", min: 1, value: 1 });
    const sl = h("input", { type: "number", min: 1, value: 20 });
    const tp = h("input", { type: "number", min: 1, value: 30 });
    const hold = h("input", { type: "number", min: 1, value: risk.max_hold_min });
    modal("Manual deploy order", [
      h("div", { class: "row" }, h("label", { class: "fld" }, "Instrument", inst), h("label", { class: "fld" }, "Side", side), h("label", { class: "fld" }, "Strike", off)),
      h("div", { class: "row" }, h("label", { class: "fld" }, "Lots", lots), h("label", { class: "fld" }, "Stop % of premium", sl), h("label", { class: "fld" }, "Target %", tp), h("label", { class: "fld" }, "Max hold (min)", hold)),
      h("div", { class: "muted", style: { fontSize: "12px" } }, "Nearest expiry. Sent as a marketable IOC limit on the trading API (IP_A), with a protective SL-M at FYERS. Max Capital Use still applies."),
    ], async () => {
      const r = await api("/api/orders/deploy", { json: { key: inst.value, option_type: side.value, offset: +off.value, lots: +lots.value, stop_pct: +sl.value, target_pct: +tp.value, hold_min: +hold.value } });
      toast(`Order sent: ${r.symbol} x${r.qty} (${istTime(r.entry_ts)})`);
    }, "Send order");
  }

  const iv = setInterval(refreshMeta, 15000);
  offs.push(on("order", (o) => { if (o.status === "rejected" || o.status === "exit_failed") toast(`Order ${o.status}: ${o.error || ""}`, true); }));
  return () => { offs.forEach((f) => f()); clearInterval(iv); };
}
