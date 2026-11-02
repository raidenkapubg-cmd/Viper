// SVG sparkline (area or mini-candles) with a tight, data-driven Y domain.
//
// Flat-line fix: the domain is [min(low) - pad, max(high) + pad] of the bars
// being shown, never anchored at 0, and bars containing 0/null/NaN are dropped
// before scaling. Nifty at 24,000 moving 40 points fills the box instead of
// being a hair above a zero baseline.
import { sanitize, bucketStart } from "./util.js";

const NS = "http://www.w3.org/2000/svg";
const el = (t, a = {}) => { const e = document.createElementNS(NS, t); for (const k in a) e.setAttribute(k, a[k]); return e; };

export class Sparkline {
  constructor(host, { mode = "area", tf = 60, maxBars = 180 } = {}) {
    this.host = host;
    this.mode = mode;
    this.tf = tf;
    this.maxBars = maxBars;
    this.bars = [];
    this.prevClose = null;
    this.svg = el("svg", { class: "spark", preserveAspectRatio: "none" });
    host.append(this.svg);
    this.drawn = false;
    this.ro = new ResizeObserver(() => this.render());
    this.ro.observe(host);
  }
  destroy() { this.ro.disconnect(); this.svg.remove(); }
  setMode(m) { this.mode = m; this.drawn = false; this.render(); }
  setBars(b) { this.bars = sanitize(b).slice(-this.maxBars); this.render(); }
  push(ts, price) {
    if (!(price > 0)) return;
    const t = bucketStart(ts, this.tf);
    const last = this.bars[this.bars.length - 1];
    if (last && last.t === t) { last.h = Math.max(last.h, price); last.l = Math.min(last.l, price); last.c = price; }
    else if (!last || t > last.t) { this.bars.push({ t, o: price, h: price, l: price, c: price, v: 0 }); if (this.bars.length > this.maxBars) this.bars.shift(); }
    this.render();
  }
  domain() {
    let lo = Infinity, hi = -Infinity;
    for (const b of this.bars) { if (b.l < lo) lo = b.l; if (b.h > hi) hi = b.h; }
    if (!Number.isFinite(lo)) return null;
    const span = Math.max(hi - lo, hi * 0.0004); // floor so a dead-flat minute isn't divided by ~0
    const pad = span * 0.1;
    return [lo - pad, hi + pad];
  }
  render() {
    const w = this.host.clientWidth, hgt = this.host.clientHeight;
    if (!w || !hgt || this.bars.length < 2) return;
    const dom = this.domain();
    if (!dom) return;
    const [lo, hi] = dom;
    const n = this.bars.length;
    const X = (i) => (i + 0.5) * (w / n);
    const Y = (p) => ((hi - p) / (hi - lo)) * (hgt - 12) + 2;
    const up = this.bars[n - 1].c >= (this.prevClose ?? this.bars[0].o);
    const color = up ? "var(--up)" : "var(--down)";
    this.svg.setAttribute("viewBox", `0 0 ${w} ${hgt}`);
    this.svg.textContent = "";
    if (this.prevClose && this.prevClose > lo && this.prevClose < hi) {
      this.svg.append(el("line", { class: "ref", x1: 0, x2: w, y1: Y(this.prevClose), y2: Y(this.prevClose) }));
    }
    if (this.mode === "candle") {
      const bw = Math.max(1, (w / n) * 0.6);
      for (let i = 0; i < n; i++) {
        const b = this.bars[i];
        const c = b.c >= b.o ? "var(--candle-up)" : "var(--candle-down)";
        this.svg.append(el("line", { x1: X(i), x2: X(i), y1: Y(b.h), y2: Y(b.l), stroke: c, "stroke-width": 1 }));
        const y0 = Y(Math.max(b.o, b.c)), y1 = Y(Math.min(b.o, b.c));
        this.svg.append(el("rect", { x: X(i) - bw / 2, y: y0, width: bw, height: Math.max(1, y1 - y0), fill: c }));
      }
    } else {
      let d = "";
      this.bars.forEach((b, i) => { d += (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(b.c).toFixed(1); });
      const area = d + `L${X(n - 1).toFixed(1)} ${hgt - 10}L${X(0).toFixed(1)} ${hgt - 10}Z`;
      this.svg.append(el("path", { class: "area", d: area, fill: color }));
      const line = el("path", { class: "line" + (this.drawn ? "" : " draw"), d, stroke: color, pathLength: 1 });
      this.svg.append(line);
      this.drawn = true;
      this.svg.append(el("circle", { cx: X(n - 1), cy: Y(this.bars[n - 1].c), r: 2.2, fill: color }));
    }
    const hiT = el("text", { x: w - 2, y: 9, "text-anchor": "end" }); hiT.textContent = hi.toFixed(0);
    const loT = el("text", { x: w - 2, y: hgt - 1, "text-anchor": "end" }); loT.textContent = lo.toFixed(0);
    this.svg.append(hiT, loT);
  }
}
