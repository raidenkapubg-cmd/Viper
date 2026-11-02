// Shell: collapsible left rail, IST header clock, dual-IP route heartbeat, hash router.
import { connect, on, state } from "./store.js";
import { $, $$, istDate, istDay, istTime } from "./util.js";
import * as viper from "./pages/viper.js";
import * as learn from "./pages/learn.js";
import * as positions from "./pages/positions.js";
import * as journal from "./pages/journal.js";
import * as market from "./pages/market.js";
import * as shadow from "./pages/shadow.js";

const PAGES = { viper, learn, positions, journal, market, shadow };
const shell = $("#shell");
const view = $("#view");
let unmount = null;
let skew = 0; // server - client clock offset (s)

// ---------------------------------------------------------- theme
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("viper.theme", t); } catch {}
}
try { setTheme(localStorage.getItem("viper.theme") || "light"); } catch { setTheme("light"); }
$("#themeBtn").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));

// ---------------------------------------------------------- sidebar
const OPEN_D = { a: "M10 3 L5 8 L10 13", b: "M2 2 L2 14" };
const CLOSED_D = { a: "M6 3 L11 8 L6 13", b: "M14 2 L14 14" };
function setCollapsed(c) {
  shell.classList.toggle("collapsed", c);
  const d = c ? CLOSED_D : OPEN_D;
  $("#toggleA").setAttribute("d", d.a);
  $("#toggleB").setAttribute("d", d.b);
  $("#railToggle").setAttribute("aria-expanded", String(!c));
  $("#railToggle").title = c ? "Expand sidebar  [  or  Ctrl+B" : "Collapse sidebar  [  or  Ctrl+B";
  try { localStorage.setItem("viper.rail", c ? "1" : "0"); } catch {}
  // Grid column animates for 180ms; charts/tables observe their own containers via
  // ResizeObserver, and this final nudge guarantees a resize at the settled width.
  setTimeout(() => window.dispatchEvent(new Event("resize")), 200);
}
// Narrow screens start collapsed; otherwise remember the user's last choice.
let collapsed = window.matchMedia("(max-width: 720px)").matches;
try { const saved = localStorage.getItem("viper.rail"); if (saved !== null && !collapsed) collapsed = saved === "1"; } catch {}
setCollapsed(collapsed);
$("#railToggle").addEventListener("click", () => setCollapsed((collapsed = !collapsed)));
window.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if ((e.key === "[" && !typing && !e.ctrlKey && !e.metaKey) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b")) {
    e.preventDefault();
    setCollapsed((collapsed = !collapsed));
  }
});

// ---------------------------------------------------------- header
function clock() {
  const now = Date.now() / 1000 + skew;
  $("#hClock").textContent = istTime(now) + " IST";
  $("#hDay").textContent = `${istDay(now)} · ${istDate(now)}`;
}
setInterval(clock, 250);
clock();

function routeSvg(net) {
  const apps = net?.apps || {};
  const feedOk = net?.feed?.connected;
  const row = (y, slot, ip, name, ok) => `
    <text x="0" y="${y + 3}">${slot}</text>
    <text x="30" y="${y + 3}" fill="var(--muted)">${ip || "os-route"}</text>
    <line class="flow ${ok ? "" : "off"}" x1="128" y1="${y}" x2="190" y2="${y}" stroke="var(--ink)" stroke-width="1.2"/>
    <rect x="190" y="${y - 5}" width="10" height="10" fill="${ok ? "var(--up)" : "var(--faint)"}"/>
    <text x="206" y="${y + 3}">${name}</text>`;
  return `<svg width="290" height="34" viewBox="0 0 290 34" aria-label="Dual-IP routing">
    ${row(9, "IP_A", apps.trading?.source_ip, "FYERS TRADE", apps.trading?.authed)}
    ${row(26, "IP_B", apps.data?.source_ip, net?.data_via === "trading" ? "DATA via A" : "FYERS DATA", feedOk && net?.feed?.kind === "live")}
  </svg>`;
}

on("hello", (d) => {
  skew = d.server_time - Date.now() / 1000;
  $("#footEnv").innerHTML = `env ${d.env}<br>live orders ${d.live_trading_enabled ? "<b class='down'>ENABLED</b>" : "disabled"}`;
});
on("network", (net) => {
  $("#hRoutes").innerHTML = routeSvg(net);
  const synth = net?.feed?.synthetic;
  $("#hSynth").classList.toggle("hide", !synth);
  const p = $('[data-badge="feed"]');
  p.className = "pulse " + (net?.feed?.connected ? "ok" : "bad");
});
on("feed", (f) => { if (state.network) { state.network.feed = f; $("#hRoutes").innerHTML = routeSvg(state.network); $("#hSynth").classList.toggle("hide", !f.synthetic); } });
on("conn", (ok) => {
  $("#hWs").className = "pulse " + (ok ? "ok" : "bad");
  $("#hWsTxt").textContent = ok ? "socket live" : "reconnecting";
});

// rail badges
const activity = (badge, on_) => $(`[data-badge="${badge}"]`)?.closest("a")?.classList.toggle("act", !!on_);
on("books", (b) => {
  activity("live", b.live.open.length);
  activity("shadow", b.shadow.open.length);
  $('[data-badge="live"]').textContent = b.live.open.length;
  $('[data-badge="live"]').classList.toggle("hot", b.live.open.length > 0);
  $('[data-badge="shadow"]').textContent = b.shadow.open.length;
  const regs = Object.values(b.regimes || {});
  $('[data-badge="regime"]').textContent = regs.length ? mode(regs).replace("_", " ").slice(0, 7) : "·";
});
let newLessons = 0;
on("lesson", () => { newLessons++; activity("lessons", true); $('[data-badge="lessons"]').textContent = newLessons; $('[data-badge="lessons"]').classList.add("hot"); });
on("shadow_close", () => { $('[data-badge="journal"]').textContent = "new"; });
on("live_close", () => { $('[data-badge="journal"]').textContent = "new"; });
function mode(arr) { const c = {}; let best = arr[0]; for (const a of arr) { c[a] = (c[a] || 0) + 1; if (c[a] > c[best]) best = a; } return best; }

// ---------------------------------------------------------- router
function route() {
  const [name, qs] = (location.hash.slice(1) || "viper").split("?");
  const page = PAGES[name] ? name : "viper";
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  // close the overlay rail after navigating on phones
  if (window.matchMedia("(max-width: 720px)").matches && !collapsed) setCollapsed((collapsed = true));
  if (page === "learn") { activity("lessons", false); newLessons = 0; $('[data-badge="lessons"]').textContent = "0"; $('[data-badge="lessons"]').classList.remove("hot"); }
  if (page === "journal") $('[data-badge="journal"]').textContent = "·";
  if (unmount) { try { unmount(); } catch (e) { console.error(e); } }
  view.textContent = "";
  const params = new URLSearchParams(qs || "");
  unmount = PAGES[page].mount(view, params) || null;
  $("#main").scrollTop = 0;
}
window.addEventListener("hashchange", route);

let routed = false;
on("hello", () => { if (!routed) { routed = true; route(); } });
connect();
