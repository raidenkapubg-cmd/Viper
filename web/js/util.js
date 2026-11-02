// Small shared helpers: DOM builder, formatting, candle maths shared with the server.

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const inr0 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const inr2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function fmt(n, d = 2) {
  if (n == null || !Number.isFinite(+n)) return "—";
  return d === 0 ? inr0.format(n) : d === 2 ? inr2.format(n) : (+n).toFixed(d);
}
export function money(n) {
  if (n == null || !Number.isFinite(+n)) return "—";
  return (n < 0 ? "−₹" : "₹") + inr0.format(Math.abs(n));
}
export function signed(n, d = 2) {
  if (n == null || !Number.isFinite(+n)) return "—";
  return (n > 0 ? "+" : n < 0 ? "−" : "") + fmt(Math.abs(n), d);
}
export function pct(n, d = 2) {
  if (n == null || !Number.isFinite(+n)) return "—";
  return signed(n, d) + "%";
}
export const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");

const IST_OFF = 330 * 60; // seconds
export function istParts(ts) {
  const d = new Date((ts + IST_OFF) * 1000);
  return { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(), wd: d.getUTCDay(), h: d.getUTCHours(), m: d.getUTCMinutes(), s: d.getUTCSeconds() };
}
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const p2 = (n) => String(n).padStart(2, "0");
export function istTime(ts, secs = true) {
  const p = istParts(ts);
  return `${p2(p.h)}:${p2(p.m)}${secs ? ":" + p2(p.s) : ""}`;
}
export function istDate(ts) {
  const p = istParts(ts);
  return `${p2(p.d)} ${MON[p.mo]} ${p.y}`;
}
export function istDay(ts) { return DOW[istParts(ts).wd]; }
export function istDateTime(ts) { return ts ? `${istDate(ts)} ${istTime(ts)}` : "—"; }
export function istShort(ts) { const p = istParts(ts); return `${p2(p.d)} ${MON[p.mo]}`; }
export function dur(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${p2(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${p2(Math.floor((s % 3600) / 60))}m`;
}

// Mirror of viper/market/candles.py bucket_start: frames >= 1m anchor to 09:15 IST.
export function bucketStart(ts, tf) {
  const t = Math.floor(ts);
  if (tf < 60) return t - (t % tf);
  const local = t + IST_OFF;
  const midnight = local - (local % 86400) - IST_OFF;
  if (tf >= 86400) return midnight;
  const anchor = midnight + 9 * 3600 + 15 * 60;
  if (t < anchor) return t - ((t - midnight) % tf);
  return anchor + Math.floor((t - anchor) / tf) * tf;
}

// Client half of the flat-chart fix: never let 0/null/NaN reach a scale.
export function sanitize(candles) {
  const ok = (v) => typeof v === "number" && Number.isFinite(v) && v > 0;
  const out = [];
  for (const c of candles || []) {
    if (!c || !c.t || !ok(c.o) || !ok(c.h) || !ok(c.l) || !ok(c.c)) continue;
    out.push({ t: c.t, o: c.o, h: Math.max(c.o, c.h, c.l, c.c), l: Math.min(c.o, c.h, c.l, c.c), c: c.c, v: ok(c.v) ? c.v : 0 });
  }
  out.sort((a, b) => a.t - b.t);
  return out.filter((c, i) => i === out.length - 1 || out[i + 1].t !== c.t);
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function toast(msg, err = false) {
  const el = h("div", { class: "toast" + (err ? " err" : "") }, msg);
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), err ? 7000 : 3500);
}

export async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.body = JSON.stringify(opts.json);
    init.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, init);
  if (r.status === 401 && !path.startsWith("/api/chain")) { location.href = "/login"; throw new Error("login required"); }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.detail) || (body && body.error) || `HTTP ${r.status}`);
  return body;
}

export const DIR = (d) => (d > 0 ? "CE ▲" : "PE ▼");
export const STRAT = {
  momentum_scalp: "Momentum scalp", ofi_imbalance: "Order-flow imbalance", breakout_pullback: "Breakout pullback",
  mean_reversion: "TWAP mean reversion", manual: "Manual",
};
export const REGIME = { trending: "Trending", chop: "Chop", high_iv: "High IV", expiry_gamma: "Expiry gamma" };
