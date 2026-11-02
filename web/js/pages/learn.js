// Page 2 — Learnings & Teachings: multi-modal instruction input + mistakes ledger.
import { on, state } from "../store.js";
import { api, h, istDateTime, toast, STRAT, REGIME } from "../util.js";

export function mount(root, params) {
  const offs = [];
  let tab = params.get("tab") || "teach";
  const tabs = h("div", { class: "tabs" });
  const pane = h("div");
  root.append(h("div", { class: "page" },
    h("div", { class: "ptitle" }, h("h1", {}, "Learnings & Teachings"), h("span", { class: "sub" }, "what you teach Viper · what Viper taught itself")),
    tabs, pane));

  const TABS = [["teach", "Instruction input"], ["mistakes", "Mistakes & self-improvement"], ["state", "Strategy × regime"]];
  function paintTabs() {
    tabs.textContent = "";
    TABS.forEach(([k, l]) => tabs.append(h("button", { class: k === tab ? "on" : "", onclick: () => { tab = k; paintTabs(); render(); } }, l)));
  }

  // ----------------------------------------------------------- teach
  function teach() {
    let method = "text", file = null;
    const scope = h("select", {}, h("option", { value: "ALL" }, "Multi / All instruments"), ...state.instruments.map((i) => h("option", { value: i.key }, i.label)));
    const methodSeg = h("div", { class: "seg" });
    const text = h("textarea", { class: "instr", placeholder: "One rule per line, e.g.\nAvoid the first 15 minutes.\nNo calls when RSI above 72.\nOnly buy puts below VWAP on BankNifty.\nNo trades between 12:30 and 13:30.\nMax 6 trades per day.\nAvoid chop." });
    const fileIn = h("input", { type: "file", class: "hide" });
    const drop = h("div", { class: "drop" }, "Drop a file here or click to choose");
    const out = h("div");
    const inputArea = h("div");
    function setMethod(m) {
      method = m; file = null;
      [...methodSeg.children].forEach((b) => b.classList.toggle("on", b.dataset.m === m));
      fileIn.accept = m === "pdf" ? "application/pdf" : "image/png,image/jpeg,image/webp,image/gif";
      drop.textContent = m === "pdf" ? "Drop a PDF (playbook, notes) or click to choose" : "Drop a chart screenshot or click to choose";
      inputArea.textContent = "";
      inputArea.append(m === "text" ? text : h("div", {}, drop, fileIn));
    }
    [["text", "Plain text"], ["pdf", "PDF upload"], ["image", "Image / chart"]].forEach(([m, l]) => methodSeg.append(h("button", { "data-m": m, onclick: () => setMethod(m) }, l)));
    const pick = (f) => { file = f; drop.textContent = `${f.name} · ${(f.size / 1024).toFixed(0)} KB`; };
    drop.addEventListener("click", () => fileIn.click());
    fileIn.addEventListener("change", () => fileIn.files[0] && pick(fileIn.files[0]));
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); e.dataTransfer.files[0] && pick(e.dataTransfer.files[0]); });

    const submit = h("button", { class: "btn primary", onclick: async () => {
      const fd = new FormData();
      fd.set("scope", scope.value); fd.set("method", method);
      if (method === "text") fd.set("text", text.value); else if (file) fd.set("file", file); else { toast("Choose a file first", true); return; }
      submit.disabled = true; submit.textContent = "Parsing…";
      try {
        const r = await api("/api/learn/instruction", { method: "POST", body: fd });
        out.textContent = "";
        out.append(...[h("div", { class: "sect" }, h("h2", {}, "Extracted"), h("span", { class: "note" }, `engine: ${r.engine} · ${r.rules.length} rule(s)`)),
          r.error ? h("div", { class: "down mono" }, r.error) : null,
          ruleTable(r.rules, false),
          r.setups?.length ? h("div", {}, h("div", { class: "kicker", style: { margin: "12px 0 6px" } }, "Setups described"), h("ul", {}, r.setups.map((s) => h("li", {}, s)))) : null,
          r.annotations?.length ? h("div", {}, h("div", { class: "kicker", style: { margin: "12px 0 6px" } }, "Annotations"), h("ul", {}, r.annotations.map((s) => h("li", {}, s)))) : null].filter(Boolean));
        if (method === "text") text.value = "";
        loadRules();
      } catch (e) { toast(e.message, true); }
      submit.disabled = false; submit.textContent = "Teach Viper";
    } }, "Teach Viper");

    const rulesBox = h("div");
    async function loadRules() {
      const r = await api("/api/learn/rules");
      rulesBox.textContent = "";
      rulesBox.append(ruleTable(r.rules, true));
    }
    function ruleTable(rules, manage) {
      if (!rules.length) return h("div", { class: "muted", style: { padding: "8px 0" } }, "No rules yet.");
      return h("div", { class: "tablewrap" }, h("table", { class: "t" },
        h("thead", {}, h("tr", {}, h("th", { class: "l" }, "Rule"), h("th", { class: "l" }, "Kind"), h("th", { class: "l" }, "Scope"), h("th", { class: "l" }, "Status"), manage ? h("th", {}, "") : null)),
        h("tbody", {}, rules.map((r) => h("tr", {},
          h("td", { class: "l", style: { whiteSpace: "normal" } }, r.text),
          h("td", { class: "l" }, h("span", { class: "rule-kind" + (r.kind === "note" ? " note" : "") }, r.kind.replace(/_/g, " "))),
          h("td", { class: "l" }, r.scope || scope.value),
          h("td", { class: "l " + (r.enforced ? "up" : "muted") }, r.enforced ? (r.enabled === 0 ? "disabled" : "enforced") : "note only"),
          manage ? h("td", {}, h("div", { class: "row", style: { justifyContent: "flex-end", gap: "6px" } },
            r.enforced ? h("button", { class: "btn sm", onclick: async () => { await api(`/api/learn/rules/${r.id}`, { method: "PATCH", json: { enabled: !r.enabled } }); loadRules(); } }, r.enabled ? "Disable" : "Enable") : null,
            h("button", { class: "btn sm danger", onclick: async () => { await api(`/api/learn/rules/${r.id}`, { method: "DELETE" }); loadRules(); } }, "Delete"))) : null)))));
    }
    setMethod("text");
    loadRules();
    pane.append(h("div", { class: "ingest" },
      h("div", {},
        h("div", { class: "row", style: { marginBottom: "10px" } }, h("label", { class: "fld" }, "Target scope", scope), h("label", { class: "fld" }, "Input method", methodSeg)),
        inputArea, h("div", { class: "row", style: { marginTop: "10px" } }, submit), out),
      h("aside", {},
        h("div", { class: "kicker", style: { marginBottom: "8px" } }, "How rules are applied"),
        h("p", { style: { fontSize: "12px", margin: "0 0 8px" } }, "Recognised rules (time windows, open/close buffers, call/put restrictions, RSI/VIX/VWAP filters, regime or strategy bans, max trades, max hold, stop %) are enforced by the engine on both Shadow and Live before any entry."),
        h("p", { style: { fontSize: "12px", margin: 0 }, class: "muted" }, "Anything else is kept as a note: visible, never silently claimed as enforced. Chart screenshots and scanned PDFs are read with Claude vision when ANTHROPIC_API_KEY is set; otherwise tesseract OCR, if installed."))),
      h("div", { class: "sect" }, h("h2", {}, "Rule database")), rulesBox);
  }

  // -------------------------------------------------------- mistakes
  function mistakes() {
    const modeSel = h("select", {}, h("option", { value: "both" }, "Live + Shadow"), h("option", { value: "live" }, "Live only"), h("option", { value: "shadow" }, "Shadow only"));
    const body = h("tbody");
    async function load() {
      const r = await api(`/api/learn/lessons?mode=${modeSel.value}`);
      body.textContent = "";
      if (!r.lessons.length) { body.append(h("tr", { class: "empty" }, h("td", { colspan: 4 }, "No post-mortems yet. Each closed trade is audited about five minutes after exit (post-exit drift is part of the check)."))); return; }
      for (const l of r.lessons) {
        body.append(h("tr", {},
          h("td", { class: "l" }, h("span", { class: "rule-kind" + (l.kind === "good_trade" ? " note" : "") }, l.kind.replace(/_/g, " ")), h("div", { style: { marginTop: "4px" } }, l.mistake)),
          h("td", { class: "l" }, `${l.instrument} · ${REGIME[l.regime] || l.regime} · ${l.interval}`, h("div", { class: "muted" }, `${istDateTime(l.ts)} · ${l.mode} #${l.trade_id}`)),
          h("td", { class: "l patch" }, l.patch),
          h("td", {}, l.mode.toUpperCase())));
      }
    }
    modeSel.addEventListener("change", load);
    load();
    offs.push(on("lesson", () => tab === "mistakes" && load()));
    pane.append(h("div", { class: "row", style: { marginBottom: "10px" } }, h("label", { class: "fld" }, "Source", modeSel)),
      h("div", { class: "tablewrap" }, h("table", { class: "t ledger" },
        h("thead", {}, h("tr", {}, h("th", { class: "l" }, "Mistake identified"), h("th", { class: "l" }, "Where it happened"), h("th", { class: "l" }, "How Viper improved (rule patch)"), h("th", {}, "Book"))), body)));
  }

  // ------------------------------------------------------------ state
  async function stateTab() {
    const r = await api("/api/learn/state");
    const regs = ["trending", "chop", "high_iv", "expiry_gamma"];
    const strats = [...new Set(r.matrix.map((m) => m.strategy))];
    const cell = (s, g) => r.matrix.find((m) => m.strategy === s && m.regime === g);
    pane.append(
      h("div", { class: "kicker", style: { marginBottom: "8px" } }, `4-week posterior win probability (live counts double) · cycle ${r.cycle.cycle}, ${r.cycle.pct}% through`),
      h("table", { class: "t heat" },
        h("thead", {}, h("tr", {}, h("th", { class: "l" }, "Strategy"), ...regs.map((g) => h("th", {}, REGIME[g])))),
        h("tbody", {}, strats.map((s) => h("tr", {}, h("td", { class: "l", style: { textAlign: "left" } }, STRAT[s] || s),
          ...regs.map((g) => { const c = cell(s, g); const p = c.win_p; return h("td", { style: { background: `color-mix(in srgb, ${p >= 0.5 ? "var(--up)" : "var(--down)"} ${Math.round(Math.abs(p - 0.5) * 120)}%, transparent)` } }, `${(p * 100).toFixed(0)}% · n${c.n}`); }))))),
      h("div", { class: "sect" }, h("h2", {}, "Active parameter patches")),
      h("pre", { class: "mono", style: { fontSize: "12px", border: "1px solid var(--rule)", padding: "10px", overflow: "auto" } }, JSON.stringify(r.params, null, 2) || "{}"));
  }

  function render() {
    pane.textContent = "";
    if (tab === "teach") teach(); else if (tab === "mistakes") mistakes(); else stateTab();
  }
  paintTabs();
  render();
  return () => offs.forEach((f) => f());
}
