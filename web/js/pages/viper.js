// Page 1 — Viper: instrument matrix with tight-bound sparklines + three executive briefs.
import { on, state, tick } from "../store.js";
import { Sparkline } from "../sparkline.js";
import { Ticker } from "../ticker.js";
import { api, h, fmt, money, pct, signed, cls, REGIME, istTime } from "../util.js";

export function mount(root) {
  const offs = [];
  const rows = {};
  let sparkMode = "area";
  try { sparkMode = localStorage.getItem("viper.spark") || "area"; } catch {}

  const modeSeg = h("div", { class: "seg" },
    ["area", "candle"].map((m) => h("button", { class: m === sparkMode ? "on" : "", onclick: (e) => {
      sparkMode = m;
      try { localStorage.setItem("viper.spark", m); } catch {}
      [...e.target.parentNode.children].forEach((b) => b.classList.toggle("on", b === e.target));
      Object.values(rows).forEach((r) => r.spark.setMode(m));
    } }, m === "area" ? "AREA" : "CANDLE")));

  const matrix = h("div", { class: "matrix" });
  const briefs = h("div");
  root.append(
    h("div", { class: "page" },
      h("div", { class: "ptitle" }, h("h1", {}, "Viper"), h("span", { class: "sub" }, "command & overview · 1m session bars, auto-fit to the visible range")),
      h("div", { class: "vp-grid" },
        h("section", {},
          h("div", { class: "sect" }, h("h2", {}, "Instrument matrix"), h("span", { class: "note" }, "spot · vs prev close"), h("span", { class: "spacer" }), modeSeg),
          matrix),
        h("section", {}, h("div", { class: "sect" }, h("h2", {}, "Briefs"), h("span", { class: "spacer" }), h("span", { class: "note", id: "briefTs" }, "")), briefs))));

  for (const inst of state.instruments) {
    const ltp = h("span");
    const chg = h("div", { class: "mx-chg" }, "—");
    const regime = h("span", { class: "regime-tag" }, "—");
    const sparkHost = h("div", { class: "mx-spark" });
    matrix.append(h("div", { class: "mx-row" },
      h("div", { class: "mx-name" }, inst.label, h("small", {}, inst.spot), regime),
      h("div", {}, h("div", { class: "mx-ltp" }, ltp), h("small", { class: "faint mono", style: { fontSize: "10.5px" } }, "LTP")),
      chg, sparkHost));
    const spark = new Sparkline(sparkHost, { mode: sparkMode, tf: 60, maxBars: 375 });
    rows[inst.spot] = { inst, ticker: new Ticker(ltp), chg, spark, regime };
    api(`/api/candles?key=${inst.key}&tf=1m&n=375`).then((r) => spark.setBars(r.candles)).catch(() => {});
    const t = tick(inst.spot);
    if (t) paint(t);
  }

  function paint(t) {
    const r = rows[t.symbol];
    if (!r || !(t.ltp > 0)) return;
    r.ticker.set(t.ltp);
    if (t.prev_close > 0) {
      const ch = t.ltp - t.prev_close, p = (ch / t.prev_close) * 100;
      r.chg.className = "mx-chg " + cls(ch);
      r.chg.textContent = "";
      r.chg.append(h("div", {}, signed(ch)), h("div", {}, pct(p)));
      r.spark.prevClose = t.prev_close;
    }
    r.spark.push(t.ts || Date.now() / 1000, t.ltp);
  }
  offs.push(on("ticks", (ts) => ts.forEach(paint)));
  offs.push(on("books", (b) => {
    for (const r of Object.values(rows)) r.regime.textContent = REGIME[b.regimes?.[r.inst.key]] || "warming up";
  }));

  async function loadBriefs() {
    let o;
    try { o = await api("/api/overview"); } catch { return; }
    const s = o.shadow, l = o.live, ov = o.overall;
    const wr = (x) => (x == null ? "—" : (x * 100).toFixed(0) + "%");
    const patch = s.last_patch;
    briefs.textContent = "";
    briefs.append(
      h("div", { class: "brief" },
        h("h3", {}, h("span", {}, "Shadow simulator"), h("a", { href: "#shadow", class: "muted" }, "open →")),
        h("div", { class: "big " + cls(s.realized + s.unrealized) }, money(s.realized + s.unrealized), h("small", { class: "muted", style: { fontSize: "11px", marginLeft: "8px" } }, "today")),
        h("div", { class: "kv" },
          h("div", {}, "Virtual equity"), h("div", {}, money(s.equity)),
          h("div", {}, "Win rate today / 4-wk"), h("div", {}, `${wr(s.today.win_rate)} / ${wr(s.window.win_rate)}`),
          h("div", {}, "Trades today · open"), h("div", {}, `${s.today.trades} · ${s.open}`),
          h("div", {}, "4-wk net"), h("div", { class: cls(s.window.net) }, money(s.window.net))),
        h("div", { class: "kicker", style: { marginTop: "12px" } }, `Learning cycle ${s.cycle.cycle} · day ${Math.floor(s.cycle.day) + 1} of ${s.cycle.span_days}`),
        h("div", { class: "bar accent" }, h("i", { style: { width: s.cycle.pct + "%" } })),
        patch ? h("div", { class: "note-line" }, h("b", {}, "Latest adaptation: "), patch.patch, h("span", { class: "muted" }, ` · ${istTime(patch.ts, false)}`))
              : h("div", { class: "note-line muted" }, "No adaptations yet: the ledger fills as shadow trades close.")),
      h("div", { class: "brief" },
        h("h3", {}, h("span", {}, "Auto-trade (live)"), h("a", { href: "#positions", class: "muted" }, "control →")),
        h("div", { class: "big " + cls(l.realized + l.unrealized) }, money(l.realized + l.unrealized), h("small", { class: "muted", style: { fontSize: "11px", marginLeft: "8px" } }, "today")),
        h("div", { class: "kv" },
          h("div", {}, "Deployable equity"), h("div", {}, money(l.deployable)),
          h("div", {}, "Deployed now"), h("div", {}, money(l.deployed)),
          h("div", {}, "Realized · unrealized"), h("div", {}, `${money(l.realized)} · ${money(l.unrealized)}`),
          h("div", {}, "Active trades"), h("div", {}, String(l.open)),
          h("div", {}, "Trades today · per hour"), h("div", {}, `${l.today.trades} · ${fmt(l.per_hour, 1)}`),
          h("div", {}, "Automation"), h("div", {}, Object.entries(l.automation).map(([k, v]) => `${k.replace("NIFTY", "N")}:${v}`).join(" "))),
        l.kill_switch ? h("div", { class: "note-line down" }, "Kill switch engaged: no new live orders.") : null),
      h("div", { class: "brief" },
        h("h3", {}, h("span", {}, "Overall"), h("span", { class: "muted" }, ov.feed.kind.toUpperCase())),
        h("div", { class: "kv" },
          h("div", {}, "Market feed"), h("div", { class: ov.feed.connected ? "up" : "down" }, ov.feed.connected ? `connected · ${ov.feed.symbols} syms` : "down"),
          h("div", {}, "Order socket"), h("div", { class: ov.feed.order_socket ? "up" : "muted" }, ov.feed.order_socket ? "connected" : "not connected"),
          h("div", {}, "Trading API (IP_A)"), h("div", {}, ov.network.apps.trading.authed ? "authed" : "not authed"),
          h("div", {}, "Data API (IP_B)"), h("div", {}, ov.network.apps.data.authed ? "authed" : ov.network.data_via === "trading" ? "via trading" : "not authed"),
          ...Object.entries(ov.regimes).flatMap(([k, v]) => [h("div", {}, `Regime ${k}`), h("div", {}, REGIME[v] || v)]),
          h("div", {}, "Risk utilisation"), h("div", {}, `${fmt(ov.risk_util, 1)}%`),
          h("div", {}, "Daily loss limit used"), h("div", { class: ov.daily_loss_used > 70 ? "down" : "" }, `${fmt(ov.daily_loss_used, 1)}%`)),
        h("div", { class: "bar" }, h("i", { style: { width: Math.min(100, ov.risk_util) + "%" } }))),
    );
    const ts = document.getElementById("briefTs");
    if (ts) ts.textContent = "updated " + istTime(Date.now() / 1000);
  }
  loadBriefs();
  // Briefs are local aggregates (SQLite), refreshed on trade events plus a slow 10s sweep. No Fyers calls.
  const iv = setInterval(loadBriefs, 10000);
  for (const t of ["shadow_close", "live_close", "shadow_open", "live_pending", "lesson", "risk"]) offs.push(on(t, () => loadBriefs()));

  return () => { clearInterval(iv); offs.forEach((f) => f()); Object.values(rows).forEach((r) => r.spark.destroy()); };
}
