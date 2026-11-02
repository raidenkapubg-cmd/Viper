// Page 5 — Market: API & dual-IP routing manager, multi-interval chart with drawings, live option chain.
import { on, state, tick } from "../store.js";
import { ViperChart } from "../chart/engine.js";
import { api, h, fmt, toast, debounce, istDateTime } from "../util.js";

const TOOLS = [
  ["trend", "Trend line", '<line x1="3" y1="15" x2="15" y2="3"/><rect x="1.5" y="13.5" width="3" height="3"/><rect x="13.5" y="1.5" width="3" height="3"/>'],
  ["hline", "Horizontal line", '<line x1="2" y1="9" x2="16" y2="9"/>'],
  ["vline", "Vertical line", '<line x1="9" y1="2" x2="9" y2="16"/>'],
  ["fib", "Fibonacci retracement", '<line x1="2" y1="3" x2="16" y2="3"/><line x1="2" y1="7" x2="16" y2="7"/><line x1="2" y1="10" x2="16" y2="10"/><line x1="2" y1="15" x2="16" y2="15"/>'],
  ["rect", "Rectangle zone", '<rect x="3" y="4.5" width="12" height="9"/>'],
  ["circle", "Adjustable circle", '<circle cx="9" cy="9" r="6"/><rect x="13.5" y="7.5" width="3" height="3"/>'],
];

export function mount(root, params) {
  const offs = [];
  let key = sessionGet("viper.mk.key", state.instruments[0].key);
  let tf = sessionGet("viper.mk.tf", "1m");
  let chainKey = key, expiry = "", mirror = false;

  if (params.get("auth") === "ok") toast(`${params.get("role")} API connected`);
  if (params.get("auth") === "failed") toast(`${params.get("role")} API login failed`, true);
  if (params.get("auth") === "expired") toast("Login link expired: start again", true);

  // ---------------------------------------------------------- API manager
  const apiSel = h("select", {}, h("option", { value: "data" }, "Non-Trading API: market data, history, option chain (IP_B)"),
    h("option", { value: "trading" }, "Trading API: orders, positions, funds (IP_A)"));
  const apiTable = h("tbody");
  const connectBtn = h("button", { class: "btn primary", onclick: async () => {
    try {
      const r = await api(`/api/auth/${apiSel.value}/begin`);
      location.href = r.url;
    } catch (e) { toast(e.message, true); }
  } }, "Authenticate / reconnect");
  const restartBtn = h("button", { class: "btn", onclick: async () => { await api("/api/feed/restart", { method: "POST" }); toast("Feeds restarted"); } }, "Restart sockets");
  const logoutBtn = h("button", { class: "btn danger", onclick: async () => {
    if (!confirm(`Drop the ${apiSel.value} token?`)) return;
    await api(`/api/auth/${apiSel.value}/logout`, { method: "POST" });
  } }, "Drop token");

  function paintNet(net) {
    apiTable.textContent = "";
    for (const role of ["data", "trading"]) {
      const a = net.apps[role];
      const r = a.rest;
      apiTable.append(h("tr", {},
        h("td", { class: "l" }, role === "data" ? "Non-Trading (data)" : "Trading"),
        h("td", { class: "l" }, a.app_id || h("span", { class: "muted" }, "not configured")),
        h("td", { class: "l" }, h("b", {}, a.ip_slot), " ", a.source_ip || h("span", { class: "muted" }, net.ip_binding ? "—" : "OS route (binding off)")),
        h("td", { class: "l " + (a.authed ? "up" : "down") }, a.authed ? "authenticated" : role === "data" && net.data_via === "trading" ? "using trading token" : "needs login"),
        h("td", {}, a.token_expires ? istDateTime(a.token_expires) : "—"),
        h("td", {}, String(r.requests)), h("td", { class: r.http_429 ? "down" : "" }, String(r.http_429)),
        h("td", {}, String(r.coalesced + r.cache_hits)), h("td", {}, r.p50_ms != null ? r.p50_ms + " ms" : "—"),
        h("td", { class: "l muted", title: r.last_error }, (r.last_error || "").slice(0, 40))));
    }
    feedLine.textContent = `Feed: ${net.feed.kind}${net.feed.synthetic ? " (synthetic, staging only, live orders blocked)" : ""} · ${net.feed.connected ? "connected" : "disconnected"} · ${net.feed.symbols} symbols multiplexed · ${net.feed.ticks} ticks · order socket ${net.feed.order_socket ? "up" : "down"}`;
    bindLine.textContent = net.binding_error ? `IP binding error: ${net.binding_error}` : net.ip_binding ? "Source-IP binding active: each app leaves from its whitelisted IP." : "ENABLE_IP_BINDING=false: OS default route (staging).";
    bindLine.className = "mono " + (net.binding_error ? "down" : "muted");
  }
  const feedLine = h("div", { class: "mono", style: { fontSize: "11.5px", marginTop: "8px" } });
  const bindLine = h("div", { class: "mono muted", style: { fontSize: "11.5px", marginTop: "4px" } });

  // ---------------------------------------------------------------- chart
  const instSeg = h("div", { class: "seg" }, state.instruments.map((i) => h("button", { "data-k": i.key, class: i.key === key ? "on" : "", onclick: () => { key = i.key; sessionSet("viper.mk.key", key); markSeg(instSeg, "k", key); loadChart(); } }, i.label)));
  const tfSeg = h("div", { class: "seg" }, state.boot.timeframes.map((t) => h("button", { "data-k": t, class: t === tf ? "on" : "", onclick: () => { tf = t; sessionSet("viper.mk.tf", tf); markSeg(tfSeg, "k", tf); loadChart(); } }, t)));
  const autoBtn = h("button", { class: "btn sm", onclick: () => chart.setAutoY(!chart.autoY) }, "Auto-fit Y: on");
  const resetBtn = h("button", { class: "btn sm", onclick: () => chart.reset(), title: "Or double-click the chart" }, "Reset view");
  const srcLbl = h("span", { class: "mono muted", style: { fontSize: "11px" } });
  const tools = h("div", { class: "tools" }, TOOLS.map(([k, title, svg]) =>
    h("button", { "data-k": k, title, onclick: (e) => { const on_ = chart.tool !== k; chart.setTool(on_ ? k : null); markTools(); } , html: `<svg width="18" height="18" viewBox="0 0 18 18">${svg}</svg>` })),
    h("button", { title: "Clear all drawings", onclick: () => { if (confirm("Clear all drawings for this instrument?")) { chart.setDrawings([]); saveDrawings([]); } }, html: '<svg width="18" height="18" viewBox="0 0 18 18"><line x1="4" y1="4" x2="14" y2="14"/><line x1="14" y1="4" x2="4" y2="14"/></svg>' }));
  const host = h("div", { class: "chart-host" });

  // ---------------------------------------------------------------- chain
  const chainInst = h("div", { class: "seg" }, state.instruments.map((i) => h("button", { "data-k": i.key, class: i.key === chainKey ? "on" : "", onclick: () => { chainKey = i.key; expiry = ""; markSeg(chainInst, "k", chainKey); loadChain(); } }, i.label)));
  const expSel = h("select", { onchange: () => { expiry = expSel.value; loadChain(); } });
  const mirrorBtn = h("button", { class: "btn sm", onclick: () => { mirror = !mirror; mirrorBtn.textContent = mirror ? "Layout: mirrored" : "Layout: strict"; loadChain(); } }, "Layout: strict");
  const chainHead = h("thead");
  const chainBody = h("tbody");
  const chainNote = h("span", { class: "note" });
  const chainWrap = h("div", { class: "tablewrap", style: { maxHeight: "560px" } }, h("table", { class: "t chain" }, chainHead, chainBody));

  root.append(h("div", { class: "page" },
    h("div", { class: "ptitle" }, h("h1", {}, "Market"), h("span", { class: "sub" }, "charting · API routing · option chain")),
    h("div", { class: "sect" }, h("h2", {}, "API connection & dual-IP routing")),
    h("div", { class: "row" }, apiSel, connectBtn, restartBtn, logoutBtn),
    h("div", { class: "tablewrap", style: { marginTop: "10px" } }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, ...["API", "App ID", "Bound IP", "Status", "Token expires", "Requests", "HTTP 429", "Coalesced", "p50", "Last error"].map((x, i) => h("th", { class: [0, 1, 2, 3, 9].includes(i) ? "l" : "" }, x)))),
      apiTable)),
    feedLine, bindLine,
    h("div", { class: "sect" }, h("h2", {}, "Chart"), h("span", { class: "note" }, "drag pans X+Y · wheel zooms X · shift+wheel or drag price axis zooms Y · drag time axis scales X · double-click resets"), h("span", { class: "spacer" }), srcLbl),
    h("div", { class: "chart-bar" }, instSeg, tfSeg, h("span", { class: "spacer" }), autoBtn, resetBtn),
    h("div", { class: "chart-wrap" }, tools, host),
    h("div", { class: "sect" }, h("h2", {}, "Option chain"), chainNote, h("span", { class: "spacer" }), mirrorBtn),
    h("div", { class: "row", style: { marginBottom: "8px" } }, chainInst, h("label", { class: "fld", style: { display: "flex", gap: "6px", alignItems: "center" } }, "Expiry", expSel)),
    chainWrap));

  const chart = new ViperChart(host, {
    onDrawingsChange: (d) => saveDrawings(d),
    onViewChange: (v) => { autoBtn.textContent = `Auto-fit Y: ${v.autoY ? "on" : "off"}`; },
  });
  chart.onToolDone = markTools;
  function markTools() { [...tools.children].forEach((b) => b.classList.toggle("on", b.dataset.k && b.dataset.k === chart.tool)); }
  const saveDrawings = debounce((d) => api("/api/drawings", { method: "PUT", json: { key, drawings: d } }).catch((e) => toast(e.message, true)), 400);

  let loadSeq = 0;
  async function loadChart() {
    const seq = ++loadSeq;
    try {
      const [c, d] = await Promise.all([api(`/api/candles?key=${key}&tf=${tf}&n=600`), api(`/api/drawings?key=${key}`)]);
      if (seq !== loadSeq) return;
      chart.setData(c.candles, c.tf_s);
      chart.setDrawings(d.drawings);
      srcLbl.textContent = `${c.candles.length} bars · ${c.source}`;
    } catch (e) { toast(e.message, true); }
  }

  // chain rendering
  let chainSnap = null;
  async function loadChain() {
    try {
      chainSnap = await api(`/api/chain?key=${chainKey}&expiry=${expiry}&client=${state.client}`);
    } catch (e) {
      chainHead.textContent = ""; chainBody.textContent = "";
      chainBody.append(h("tr", { class: "empty" }, h("td", { colspan: 17 }, `Option chain unavailable: ${e.message}`)));
      return;
    }
    expiry = chainSnap.expiry_ts || "";
    expSel.textContent = "";
    for (const e of chainSnap.expiries) expSel.append(h("option", { value: e.ts, selected: e.ts === expiry }, e.date));
    const L = chainSnap.labels;
    const callCols = mirror ? [...L].reverse() : L;
    chainHead.textContent = "";
    chainHead.append(
      h("tr", { class: "side" }, h("th", { colspan: 8 }, "CALLS"), h("th", { class: "strike" }, ""), h("th", { colspan: 8 }, "PUTS")),
      h("tr", {}, ...callCols.map((l) => h("th", {}, l)), h("th", { class: "strike" }, "STRIKE"), ...L.map((l) => h("th", {}, l))));
    paintChain();
    // centre ATM
    // Centre ATM inside the chain's own scroll box only (never scroll the page away from the chart).
    requestAnimationFrame(() => {
      const row = chainBody.querySelector("tr.atm");
      if (row) chainWrap.scrollTop = row.offsetTop - chainWrap.clientHeight / 2 + row.offsetHeight;
    });
  }
  const CELL = { open: 2, high: 2, low: 2, close: 2, bid: 2, ask: 2, mark: 2, volume: 0 };
  function paintChain() {
    if (!chainSnap) return;
    const cols = chainSnap.columns;
    const callCols = mirror ? [...cols].reverse() : cols;
    const inst = state.instruments.find((i) => i.key === chainKey);
    const spot = tick(inst.spot)?.ltp ?? chainSnap.spot;
    const atm = spot ? Math.round(spot / inst.step) * inst.step : chainSnap.atm;
    chainNote.textContent = `spot ${fmt(spot)} · ATM ${fmt(atm, 0)} · CLOSE = previous session close · MARK = live LTP`;
    chainBody.textContent = "";
    for (const r of chainSnap.rows) {
      const ce = { ...r.CE, ...cellsFrom(tick(r.CE.symbol)) };
      const pe = { ...r.PE, ...cellsFrom(tick(r.PE.symbol)) };
      const tr = h("tr", { class: [r.strike === atm ? "atm" : "", spot && r.strike < spot ? "itm-c" : "", spot && r.strike > spot ? "itm-p" : ""].join(" ") },
        ...callCols.map((c) => h("td", { class: "c" + (c === "mark" ? " mark" : "") }, fmt(ce[c], CELL[c]))),
        h("td", { class: "strike" }, fmt(r.strike, 0)),
        ...cols.map((c) => h("td", { class: "p" + (c === "mark" ? " mark" : "") }, fmt(pe[c], CELL[c]))));
      chainBody.append(tr);
    }
  }
  function cellsFrom(t) {
    if (!t) return {};
    const o = {};
    for (const [k, src] of [["open", "open"], ["high", "high"], ["low", "low"], ["close", "prev_close"], ["bid", "bid"], ["ask", "ask"], ["mark", "ltp"], ["volume", "vol"]]) if (t[src] != null) o[k] = t[src];
    return o;
  }

  // live updates (shared socket, no REST polling)
  let chainDirty = false;
  offs.push(on("ticks", (ts) => {
    const inst = state.instruments.find((i) => i.key === key);
    for (const t of ts) {
      if (t.symbol === inst.spot) chart.update(t.ts || Date.now() / 1000, t.ltp);
      else if (chainSnap) chainDirty = true;
    }
  }));
  const chainTimer = setInterval(() => { if (chainDirty) { chainDirty = false; paintChain(); } }, 500);
  offs.push(on("network", paintNet));
  offs.push(on("feed", () => api("/api/network").then(paintNet).catch(() => {})));
  if (state.network) paintNet(state.network);
  api("/api/network").then(paintNet).catch(() => {});

  loadChart();
  loadChain();
  return () => { offs.forEach((f) => f()); clearInterval(chainTimer); chart.destroy(); };
}

function markSeg(seg, attr, v) { [...seg.children].forEach((b) => b.classList.toggle("on", b.dataset[attr] === v)); }
function sessionGet(k, d) { try { return sessionStorage.getItem(k) || d; } catch { return d; } }
function sessionSet(k, v) { try { sessionStorage.setItem(k, v); } catch {} }
