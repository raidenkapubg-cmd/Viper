// Page 6 — Shadow: live simulation lab.
import { on, state } from "../store.js";
import { api, h, fmt, money, cls, dur, istTime, STRAT, REGIME, DIR } from "../util.js";

export function mount(root) {
  const offs = [];
  const curve = h("div", { class: "curve" });
  const openBody = h("tbody");
  const feed = h("div", { class: "feed" });
  const lessons = h("div", { class: "feed" });
  const head = h("div", { class: "kv" });
  const cls_ = h("div", { class: "kv" });
  const prog = h("div");
  const regimeBox = h("div", { class: "kv" });

  root.append(h("div", { class: "page" },
    h("div", { class: "ptitle" }, h("h1", {}, "Shadow"), h("span", { class: "sub" }, "simulated capital · live ticks · real bid/ask, slippage and charges")),
    h("div", { class: "sh-grid" },
      h("section", {},
        h("div", { class: "sect" }, h("h2", {}, "Virtual equity curve")), curve,
        h("div", { class: "sect" }, h("h2", {}, "Open shadow trades"), h("span", { class: "note" }, "marked to the live bid")),
        h("div", { class: "tablewrap" }, h("table", { class: "t" },
          h("thead", {}, h("tr", {}, ...["Symbol", "Strategy", "Regime", "Qty", "Entry", "Bid", "Stop", "Target", "uPnL", "MFE", "Held"].map((x, i) => h("th", { class: i < 3 ? "l" : "" }, x)))), openBody)),
        h("div", { class: "sect" }, h("h2", {}, "Hypotheses under test"), h("span", { class: "note" }, "every setup the engine evaluated: entered, skipped or blocked, with the reason")), feed),
      h("section", {},
        h("div", { class: "sect" }, h("h2", {}, "Book")), head,
        h("div", { class: "sect" }, h("h2", {}, "4-week adaptation")), prog,
        h("div", { class: "sect" }, h("h2", {}, "Win / error classification")), cls_,
        h("div", { class: "sect" }, h("h2", {}, "Live regimes")), regimeBox,
        h("div", { class: "sect" }, h("h2", {}, "Lessons pushed"), h("a", { href: "#learn?tab=mistakes", class: "note" }, "ledger →")), lessons))));

  function paintCurve(pts, start) {
    const w = curve.clientWidth, H = curve.clientHeight;
    if (!w || pts.length < 2) { curve.innerHTML = `<div class="muted" style="padding:12px;font-size:12px">The curve starts after the first minute of shadow activity.</div>`; return; }
    const ys = pts.map((p) => p.equity);
    let lo = Math.min(...ys, start), hi = Math.max(...ys, start);
    const pad = Math.max((hi - lo) * 0.1, hi * 0.0005); lo -= pad; hi += pad;
    const X = (i) => (i / (pts.length - 1)) * (w - 70);
    const Y = (v) => 8 + ((hi - v) / (hi - lo)) * (H - 16);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(p.equity).toFixed(1)}`).join("");
    const last = ys[ys.length - 1];
    const col = last >= start ? "var(--up)" : "var(--down)";
    curve.innerHTML = `<svg viewBox="0 0 ${w} ${H}" preserveAspectRatio="none">
      <line x1="0" x2="${w - 70}" y1="${Y(start)}" y2="${Y(start)}" stroke="var(--faint)" stroke-dasharray="2 3"/>
      <path d="${d}" fill="none" stroke="${col}" stroke-width="1.5" pathLength="1" class="draw" style="stroke-dasharray:1;stroke-dashoffset:1;animation:draw 900ms ease-out forwards"/>
      <text x="${w - 64}" y="${Y(last) + 4}" font-family="IBM Plex Mono" font-size="11" fill="${col}">${fmt(last, 0)}</text>
      <text x="${w - 64}" y="${Y(start) + 4}" font-family="IBM Plex Mono" font-size="10" fill="var(--muted)">start</text></svg>`;
  }

  function paintOpen(sh) {
    openBody.textContent = "";
    if (!sh.open.length) { openBody.append(h("tr", { class: "empty" }, h("td", { colspan: 11 }, "No open shadow trades."))); return; }
    for (const t of sh.open) openBody.append(h("tr", {},
      h("td", { class: "l" }, t.symbol), h("td", { class: "l" }, STRAT[t.strategy] || t.strategy), h("td", { class: "l" }, REGIME[t.regime] || t.regime),
      h("td", {}, String(t.qty)), h("td", {}, fmt(t.entry_price)), h("td", {}, fmt(t.bid)), h("td", {}, fmt(t.stop)), h("td", {}, fmt(t.target)),
      h("td", { class: cls(t.upnl) }, money(t.upnl)), h("td", {}, fmt(t.mfe)), h("td", {}, dur(t.held_s))));
  }

  function decisionEl(d) {
    return h("div", { class: "it " + d.action },
      h("span", { class: "mono faint" }, istTime(d.ts)),
      h("span", { class: "a" }, `${d.action}${d.book === "live" ? " · live" : ""}`),
      h("span", {}, h("b", {}, `${d.key} ${DIR(d.direction)} `), `${STRAT[d.strategy] || d.strategy} in ${REGIME[d.regime] || d.regime}`,
        d.score != null ? h("span", { class: "muted" }, ` · score ${fmt(d.score)}`) : null,
        h("div", { class: "muted" }, d.reason || d.why || "")));
  }

  async function load() {
    const s = await api("/api/shadow");
    paintCurve(s.curve, s.start_capital);
    head.textContent = "";
    const ret = ((s.equity - s.start_capital) / s.start_capital) * 100;
    head.append(h("div", {}, "Virtual equity"), h("div", { class: cls(ret) }, `${money(s.equity)} (${ret >= 0 ? "+" : ""}${fmt(ret)}%)`),
      h("div", {}, "4-wk closed trades"), h("div", {}, String(s.stats.trades)),
      h("div", {}, "4-wk win rate"), h("div", {}, s.stats.win_rate == null ? "—" : (s.stats.win_rate * 100).toFixed(0) + "%"),
      h("div", {}, "4-wk net"), h("div", { class: cls(s.stats.net) }, money(s.stats.net)),
      h("div", {}, "Avg hold"), h("div", {}, dur(s.stats.avg_hold_s)));
    prog.textContent = "";
    prog.append(h("div", { class: "kicker" }, `Cycle ${s.cycle.cycle} · day ${Math.floor(s.cycle.day) + 1} of ${s.cycle.span_days} · since ${s.cycle.started.slice(0, 10)}`),
      h("div", { class: "bar accent" }, h("i", { style: { width: s.cycle.pct + "%" } })));
    feed.textContent = "";
    s.decisions.forEach((d) => feed.append(decisionEl(d)));
    if (!s.decisions.length) feed.append(h("div", { class: "muted", style: { padding: "10px 0" } }, "Waiting for the first evaluations. Setups need ~22 one-minute bars of history per instrument."));
    regimeBox.textContent = "";
    for (const [k, f] of Object.entries(s.features)) {
      regimeBox.append(h("div", {}, k), h("div", {}, `${REGIME[s.regimes[k]] || "—"} · ER ${fmt(f.er)} · RSI ${fmt(f.rsi, 0)} · ATR ${fmt(f.atr, 1)}`));
    }
    paintClasses();
  }
  async function paintClasses() {
    const r = await api("/api/learn/lessons?mode=shadow&limit=500");
    const counts = {};
    r.lessons.forEach((l) => (counts[l.kind] = (counts[l.kind] || 0) + 1));
    cls_.textContent = "";
    const kinds = Object.keys(counts);
    if (!kinds.length) cls_.append(h("div", { class: "muted" }, "No audited trades yet"), h("div", {}, ""));
    kinds.sort((a, b) => counts[b] - counts[a]).forEach((k) => cls_.append(h("div", {}, k.replace(/_/g, " ")), h("div", { class: k === "good_trade" ? "up" : "down" }, String(counts[k]))));
    lessons.textContent = "";
    r.lessons.slice(0, 30).forEach((l) => lessons.append(lessonEl(l)));
  }
  const lessonEl = (l) => h("div", { class: "it" }, h("span", { class: "mono faint" }, istTime(l.ts)), h("span", { class: "a" }, l.kind.replace(/_/g, " ")), h("span", {}, l.mistake, h("div", { class: "muted mono", style: { fontSize: "11px" } }, l.patch)));

  load().catch(() => {});
  offs.push(on("books", (b) => { paintOpen(b.shadow); }));
  if (state.books) paintOpen(state.books.shadow);
  offs.push(on("decision", (d) => { if (d.book === "shadow" || d.book === "live") { feed.prepend(decisionEl(d)); while (feed.children.length > 150) feed.lastChild.remove(); } }));
  offs.push(on("lesson", (l) => { if (l.mode === "shadow") { lessons.prepend(lessonEl(l)); paintClasses(); } }));
  offs.push(on("shadow_close", () => load().catch(() => {})));
  let first = true;
  const ro2 = new ResizeObserver(() => { if (first) { first = false; return; } api("/api/shadow").then((s) => paintCurve(s.curve, s.start_capital)).catch(() => {}); });
  ro2.observe(curve);
  const iv = setInterval(() => load().catch(() => {}), 60000);
  return () => { offs.forEach((f) => f()); clearInterval(iv); ro2.disconnect(); };
}
