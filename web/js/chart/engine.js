// ViperChart — canvas candlestick chart with full 2D navigation and drawings.
//
// Navigation (lightweight-charts terms in brackets):
//   drag in plot            pan X and Y freely           [pressedMouseMove, horz/vertTouchDrag]
//   wheel                   zoom X around the cursor     [mouseWheel scale]
//   shift+wheel / alt+wheel zoom Y around the cursor
//   horizontal wheel        pan X                         [mouseWheel scroll]
//   drag price axis         stretch / squeeze Y          [axisPressedMouseMove.price]
//   drag time axis          stretch / squeeze X          [axisPressedMouseMove.time]
//   pinch (touch)           zoom X (and Y with vertical spread)
//   double-click / Reset    auto-fit Y + snap to latest
// Any manual vertical pan/zoom turns Auto-fit off; the toggle turns it back on.
//
// Y auto-scale uses only the *visible* candles: [min(low) - pad, max(high) + pad],
// after stripping 0/null/NaN bars, so high-priced indices never render flat.
//
// Drawings are stored in data space (unix time, price) so they persist across
// timeframes and reloads. Tools: trend, hline, vline, fib, rect, circle.

import { bucketStart, sanitize, istTime, istShort, fmt } from "../util.js";

const AXIS_W = 74, AXIS_H = 24, PAD_T = 8;
const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

export class ViperChart {
  constructor(host, { onDrawingsChange, onViewChange } = {}) {
    this.host = host;
    this.canvas = document.createElement("canvas");
    host.append(this.canvas);
    this.ctx = this.canvas.getContext("2d");
    this.bars = [];
    this.tf = 60;
    this.barW = 8;
    this.right = 0;        // fractional bar index at the right edge of the plot
    this.autoY = true;
    this.yMin = 0; this.yMax = 1;
    this.mouse = null;
    this.drawings = [];
    this.tool = null;
    this.sel = null;
    this.drag = null;
    this.onDrawingsChange = onDrawingsChange || (() => {});
    this.onViewChange = onViewChange || (() => {});
    this.dirty = true;
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(host);
    this._bind();
    this.resize();
    this._raf = requestAnimationFrame(this._loop);
  }

  destroy() {
    cancelAnimationFrame(this._raf);
    this.ro.disconnect();
    window.removeEventListener("keydown", this._key);
    this.canvas.remove();
  }

  // ---------------------------------------------------------------- data
  setData(bars, tfSec) {
    this.tf = tfSec;
    this.bars = sanitize(bars);
    this.reset();
  }
  update(ts, price) {
    if (!(price > 0) || !this.bars.length) return;
    const t = bucketStart(ts, this.tf);
    const last = this.bars[this.bars.length - 1];
    const following = this.right >= this.bars.length - 2;
    if (last.t === t) { last.h = Math.max(last.h, price); last.l = Math.min(last.l, price); last.c = price; }
    else if (t > last.t) {
      this.bars.push({ t, o: price, h: price, l: price, c: price, v: 0 });
      if (following) this.right += 1;
    }
    this.dirty = true;
  }
  setDrawings(list) { this.drawings = list || []; this.sel = null; this.dirty = true; }
  setTool(t) { this.tool = t; this.sel = null; this.canvas.style.cursor = t ? "crosshair" : "crosshair"; this.dirty = true; }

  reset() {
    this.barW = Math.max(3, Math.min(14, (this.plotW || 800) / 120));
    this.right = this.bars.length - 1 + 4;
    this.autoY = true;
    this.dirty = true;
    this.onViewChange(this.view());
  }
  setAutoY(on) { this.autoY = on; this.dirty = true; this.onViewChange(this.view()); }
  view() { return { autoY: this.autoY }; }

  // --------------------------------------------------------- geometry
  resize() {
    const r = this.host.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.W = Math.max(50, r.width); this.H = Math.max(50, r.height);
    this.canvas.width = Math.round(this.W * dpr);
    this.canvas.height = Math.round(this.H * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.plotW = this.W - AXIS_W;
    this.plotH = this.H - AXIS_H - PAD_T;
    this.dirty = true;
  }
  xOf(i) { return this.plotW - (this.right - i) * this.barW; }
  iOf(x) { return this.right - (this.plotW - x) / this.barW; }
  yOf(p) { return PAD_T + ((this.yMax - p) / (this.yMax - this.yMin)) * this.plotH; }
  pOf(y) { return this.yMax - ((y - PAD_T) / this.plotH) * (this.yMax - this.yMin); }
  // time <-> fractional index (works beyond the data using the timeframe)
  iOfT(t) {
    const b = this.bars;
    if (!b.length) return 0;
    if (t <= b[0].t) return (t - b[0].t) / this.tf;
    if (t >= b[b.length - 1].t) return b.length - 1 + (t - b[b.length - 1].t) / this.tf;
    let lo = 0, hi = b.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (b[m].t <= t) lo = m; else hi = m; }
    return lo + (t - b[lo].t) / Math.max(1, b[hi].t - b[lo].t);
  }
  tOfI(i) {
    const b = this.bars;
    if (!b.length) return 0;
    if (i <= 0) return b[0].t + i * this.tf;
    if (i >= b.length - 1) return b[b.length - 1].t + (i - (b.length - 1)) * this.tf;
    const lo = Math.floor(i), f = i - lo;
    return b[lo].t + f * (b[lo + 1].t - b[lo].t);
  }
  visible() {
    const a = Math.max(0, Math.floor(this.iOf(0))), z = Math.min(this.bars.length - 1, Math.ceil(this.right));
    return [a, z];
  }
  autoscale() {
    const [a, z] = this.visible();
    let lo = Infinity, hi = -Infinity;
    for (let i = a; i <= z; i++) { const c = this.bars[i]; if (!c) continue; if (c.l < lo) lo = c.l; if (c.h > hi) hi = c.h; }
    if (!Number.isFinite(lo)) return;
    const span = Math.max(hi - lo, hi * 0.0005);
    const pad = span * 0.08;
    this.yMin = lo - pad; this.yMax = hi + pad;
  }

  // ------------------------------------------------------- interaction
  _bind() {
    const c = this.canvas;
    const pos = (e) => { const r = c.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    const zone = (p) => (p.x > this.plotW ? "price" : p.y > this.H - AXIS_H ? "time" : "plot");

    c.addEventListener("pointerdown", (e) => {
      c.setPointerCapture(e.pointerId);
      const p = pos(e);
      const z = zone(p);
      this.downAt = p;
      if (z === "plot" && this.tool) { this._startDrawing(p); return; }
      if (z === "plot") {
        const hit = this._hitTest(p);
        if (hit) {
          this.sel = hit.d;
          this.drag = { kind: hit.handle == null ? "move" : "handle", d: hit.d, handle: hit.handle, last: p };
          this.dirty = true;
          return;
        }
        this.sel = null;
      }
      this.drag = { kind: z, start: p, right: this.right, barW: this.barW, yMin: this.yMin, yMax: this.yMax };
    });
    c.addEventListener("pointermove", (e) => {
      const p = pos(e);
      this.mouse = p;
      const d = this.drag;
      if (d) {
        if (d.kind === "plot") {
          this.right = d.right - (p.x - d.start.x) / this.barW;
          const dy = p.y - d.start.y;
          if (Math.abs(dy) > 2 || !this.autoY) {
            if (this.autoY) { this.autoY = false; this.onViewChange(this.view()); }
            const per = (d.yMax - d.yMin) / this.plotH;
            this.yMin = d.yMin + dy * per; this.yMax = d.yMax + dy * per;
          }
        } else if (d.kind === "price") {
          const f = Math.exp((p.y - d.start.y) / 150);
          const mid = (d.yMin + d.yMax) / 2, half = ((d.yMax - d.yMin) / 2) * f;
          this.yMin = mid - half; this.yMax = mid + half;
          if (this.autoY) { this.autoY = false; this.onViewChange(this.view()); }
        } else if (d.kind === "time") {
          this.barW = Math.max(1, Math.min(60, d.barW * Math.exp((p.x - d.start.x) / 200)));
        } else if (d.kind === "draw") {
          d.d.p[d.d.p.length - 1] = this._pt(p);
        } else if (d.kind === "handle") {
          d.d.p[d.handle] = this._pt(p);
        } else if (d.kind === "move") {
          const a = this._pt(d.last), b = this._pt(p);
          d.d.p = d.d.p.map((q) => ({ t: q.t + (b.t - a.t), p: q.p + (b.p - a.p) }));
          d.last = p;
        }
      }
      this.dirty = true;
    });
    const end = () => {
      const d = this.drag;
      this.drag = null;
      if (d && ["draw", "handle", "move"].includes(d.kind)) {
        if (d.kind === "draw") { this.tool = null; this.onToolDone?.(); }
        this.onDrawingsChange(this.drawings);
      }
      this.dirty = true;
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", end);
    c.addEventListener("pointerleave", () => { this.mouse = null; this.dirty = true; });
    c.addEventListener("dblclick", (e) => { if (!this._hitTest(pos(e))) this.reset(); });
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const p = pos(e);
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) { this.right += e.deltaX / this.barW; this.dirty = true; return; }
      const k = Math.exp(-e.deltaY * 0.0015);
      if (e.shiftKey || e.altKey || p.x > this.plotW) {
        const at = this.pOf(p.y);
        this.yMin = at - (at - this.yMin) / k; this.yMax = at + (this.yMax - at) / k;
        if (this.autoY) { this.autoY = false; this.onViewChange(this.view()); }
      } else {
        const at = this.iOf(p.x);
        this.barW = Math.max(1, Math.min(60, this.barW * k));
        this.right = at + (this.plotW - p.x) / this.barW;
      }
      this.dirty = true;
    }, { passive: false });
    // two-finger pinch
    const pts = new Map();
    let pinch = null;
    c.addEventListener("pointerdown", (e) => { if (e.pointerType === "touch") pts.set(e.pointerId, pos(e)); });
    c.addEventListener("pointermove", (e) => {
      if (e.pointerType !== "touch" || !pts.has(e.pointerId)) return;
      pts.set(e.pointerId, pos(e));
      if (pts.size === 2) {
        const [a, b] = [...pts.values()];
        const dx = Math.abs(a.x - b.x) + 1, dy = Math.abs(a.y - b.y) + 1;
        if (!pinch) pinch = { dx, dy, barW: this.barW, yMin: this.yMin, yMax: this.yMax };
        this.drag = null;
        this.barW = Math.max(1, Math.min(60, pinch.barW * (dx / pinch.dx)));
        if (dy > 40) {
          const mid = (pinch.yMin + pinch.yMax) / 2, half = ((pinch.yMax - pinch.yMin) / 2) * (pinch.dy / dy);
          this.yMin = mid - half; this.yMax = mid + half; this.autoY = false;
        }
      }
    });
    const tEnd = (e) => { pts.delete(e.pointerId); if (pts.size < 2) pinch = null; };
    c.addEventListener("pointerup", tEnd);
    c.addEventListener("pointercancel", tEnd);
    this._key = (e) => {
      if (!this.sel || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        this.drawings = this.drawings.filter((d) => d !== this.sel);
        this.sel = null; this.dirty = true;
        this.onDrawingsChange(this.drawings);
      } else if (e.key === "Escape") { this.sel = null; this.dirty = true; }
    };
    window.addEventListener("keydown", this._key);
  }

  _pt(p) { return { t: Math.round(this.tOfI(this.iOf(p.x))), p: +this.pOf(p.y).toFixed(2) }; }
  _px(q) { return { x: this.xOf(this.iOfT(q.t)), y: this.yOf(q.p) }; }

  _startDrawing(p) {
    const q = this._pt(p);
    const single = this.tool === "hline" || this.tool === "vline";
    const d = { id: Math.random().toString(36).slice(2, 10), kind: this.tool, p: single ? [q] : [q, { ...q }] };
    this.drawings.push(d);
    this.sel = d;
    if (single) { this.tool = null; this.onToolDone?.(); this.onDrawingsChange(this.drawings); }
    else this.drag = { kind: "draw", d };
    this.dirty = true;
  }

  _hitTest(p) {
    const near = (a, b, r = 7) => Math.hypot(a.x - b.x, a.y - b.y) <= r;
    for (let k = this.drawings.length - 1; k >= 0; k--) {
      const d = this.drawings[k];
      const P = d.p.map((q) => this._px(q));
      for (let i = 0; i < P.length; i++) if (near(P[i], p)) return { d, handle: i };
      if (d.kind === "hline" && Math.abs(P[0].y - p.y) < 5) return { d };
      if (d.kind === "vline" && Math.abs(P[0].x - p.x) < 5) return { d };
      if (d.kind === "trend" && segDist(p, P[0], P[1]) < 5) return { d };
      if ((d.kind === "rect" || d.kind === "fib") && inBox(p, P[0], P[1], 4)) return { d };
      if (d.kind === "circle") {
        const r = Math.hypot(P[1].x - P[0].x, P[1].y - P[0].y);
        if (Math.abs(Math.hypot(p.x - P[0].x, p.y - P[0].y) - r) < 6) return { d };
      }
    }
    return null;
  }

  // ------------------------------------------------------------ render
  _loop = () => {
    if (this.dirty) { this.dirty = false; this._render(); }
    this._raf = requestAnimationFrame(this._loop);
  };

  _render() {
    const g = this.ctx, W = this.W, H = this.H;
    const C = {
      paper: css("--paper"), ink: css("--ink"), muted: css("--muted"), rule: css("--rule"), grid: css("--grid"),
      up: css("--candle-up"), down: css("--candle-down"), sel: css("--sel"), accent: css("--accent"), paper2: css("--paper-2"),
    };
    g.fillStyle = C.paper; g.fillRect(0, 0, W, H);
    if (!this.bars.length) {
      g.fillStyle = C.muted; g.font = "12px IBM Plex Mono, monospace";
      g.fillText("No candles yet — waiting for data", 16, 28);
      return;
    }
    if (this.autoY) this.autoscale();
    if (!(this.yMax > this.yMin)) { this.yMax = this.yMin + 1; }

    // grid + price axis ticks
    const step = niceStep((this.yMax - this.yMin) / Math.max(3, this.plotH / 56));
    g.font = "11px IBM Plex Mono, monospace";
    g.textBaseline = "middle";
    g.lineWidth = 1;
    for (let p = Math.ceil(this.yMin / step) * step; p <= this.yMax; p += step) {
      const y = Math.round(this.yOf(p)) + 0.5;
      g.strokeStyle = C.grid; g.beginPath(); g.moveTo(0, y); g.lineTo(this.plotW, y); g.stroke();
      g.fillStyle = C.muted; g.fillText(fmt(p, step < 1 ? 2 : step < 10 ? 1 : 0), this.plotW + 8, y);
    }
    // time axis labels
    const [a, z] = this.visible();
    const every = Math.max(1, Math.ceil(90 / this.barW));
    g.textBaseline = "alphabetic";
    for (let i = a - (a % every); i <= z; i += every) {
      const b = this.bars[i];
      if (!b) continue;
      const x = Math.round(this.xOf(i)) + 0.5;
      g.strokeStyle = C.grid; g.beginPath(); g.moveTo(x, PAD_T); g.lineTo(x, PAD_T + this.plotH); g.stroke();
      g.fillStyle = C.muted;
      g.fillText(this.tf >= 86400 ? istShort(b.t) : this.tf >= 3600 ? `${istShort(b.t)} ${istTime(b.t, false)}` : istTime(b.t, this.tf < 60), x - 20, H - 7);
    }
    // candles
    g.save();
    g.beginPath(); g.rect(0, 0, this.plotW, PAD_T + this.plotH); g.clip();
    const bw = Math.max(1, this.barW * 0.7);
    for (let i = a; i <= z; i++) {
      const c = this.bars[i];
      const x = this.xOf(i);
      const col = c.c >= c.o ? C.up : C.down;
      g.strokeStyle = col; g.fillStyle = col;
      g.beginPath(); g.moveTo(Math.round(x) + 0.5, this.yOf(c.h)); g.lineTo(Math.round(x) + 0.5, this.yOf(c.l)); g.stroke();
      const y0 = this.yOf(Math.max(c.o, c.c)), y1 = this.yOf(Math.min(c.o, c.c));
      g.fillRect(Math.round(x - bw / 2), Math.round(y0), Math.max(1, Math.round(bw)), Math.max(1, Math.round(y1 - y0)));
    }
    this._renderDrawings(g, C);
    g.restore();

    // last price marker
    const last = this.bars[this.bars.length - 1];
    const ly = this.yOf(last.c);
    if (ly >= PAD_T && ly <= PAD_T + this.plotH) {
      g.strokeStyle = C.ink; g.setLineDash([2, 3]);
      g.beginPath(); g.moveTo(0, Math.round(ly) + 0.5); g.lineTo(this.plotW, Math.round(ly) + 0.5); g.stroke(); g.setLineDash([]);
      label(g, this.plotW + 1, ly, fmt(last.c), last.c >= last.o ? C.up : C.down, C.paper);
    }
    // axes frame
    g.strokeStyle = C.ink;
    g.beginPath(); g.moveTo(this.plotW + 0.5, 0); g.lineTo(this.plotW + 0.5, H); g.moveTo(0, H - AXIS_H + 0.5); g.lineTo(W, H - AXIS_H + 0.5); g.stroke();

    // crosshair + legend
    const m = this.mouse;
    let hoverBar = last;
    if (m && m.x < this.plotW && m.y < H - AXIS_H) {
      g.strokeStyle = C.muted; g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(Math.round(m.x) + 0.5, 0); g.lineTo(Math.round(m.x) + 0.5, H - AXIS_H);
      g.moveTo(0, Math.round(m.y) + 0.5); g.lineTo(this.plotW, Math.round(m.y) + 0.5); g.stroke(); g.setLineDash([]);
      label(g, this.plotW + 1, m.y, fmt(this.pOf(m.y)), C.ink, C.paper);
      const i = Math.round(this.iOf(m.x));
      if (this.bars[i]) hoverBar = this.bars[i];
      const t = this.tOfI(this.iOf(m.x));
      const txt = this.tf >= 86400 ? istShort(t) : `${istShort(t)} ${istTime(t, this.tf < 60)}`;
      g.fillStyle = C.ink; g.fillRect(m.x - 52, H - AXIS_H + 1, 104, AXIS_H - 1);
      g.fillStyle = C.paper; g.textAlign = "center"; g.fillText(txt, m.x, H - 7); g.textAlign = "left";
    }
    g.font = "11px IBM Plex Mono, monospace"; g.textBaseline = "top";
    const hb = hoverBar, chg = hb.c - hb.o;
    const legend = `O ${fmt(hb.o)}  H ${fmt(hb.h)}  L ${fmt(hb.l)}  C ${fmt(hb.c)}  ${chg >= 0 ? "+" : "−"}${fmt(Math.abs(chg))}`;
    g.fillStyle = C.paper; g.fillRect(6, 4, g.measureText(legend).width + 10, 16);
    g.fillStyle = chg >= 0 ? C.up : C.down; g.fillText(legend, 11, 6);
    if (!this.autoY) {
      g.fillStyle = C.accent; g.fillText("MANUAL Y — double-click to reset", 11, 22);
    }
  }

  _renderDrawings(g, C) {
    g.lineWidth = 1.2;
    for (const d of this.drawings) {
      const P = d.p.map((q) => this._px(q));
      const selected = d === this.sel;
      g.strokeStyle = selected ? C.sel : C.ink;
      g.fillStyle = selected ? C.sel : C.ink;
      g.beginPath();
      if (d.kind === "trend") { g.moveTo(P[0].x, P[0].y); g.lineTo(P[1].x, P[1].y); g.stroke(); }
      else if (d.kind === "hline") {
        g.moveTo(0, P[0].y); g.lineTo(this.plotW, P[0].y); g.stroke();
        g.font = "10px IBM Plex Mono, monospace"; g.textBaseline = "bottom"; g.fillText(fmt(d.p[0].p), 6, P[0].y - 2);
      } else if (d.kind === "vline") { g.moveTo(P[0].x, 0); g.lineTo(P[0].x, PAD_T + this.plotH); g.stroke(); }
      else if (d.kind === "rect") {
        g.globalAlpha = 0.08; g.fillRect(Math.min(P[0].x, P[1].x), Math.min(P[0].y, P[1].y), Math.abs(P[1].x - P[0].x), Math.abs(P[1].y - P[0].y));
        g.globalAlpha = 1; g.strokeRect(Math.min(P[0].x, P[1].x), Math.min(P[0].y, P[1].y), Math.abs(P[1].x - P[0].x), Math.abs(P[1].y - P[0].y));
      } else if (d.kind === "circle") {
        const r = Math.hypot(P[1].x - P[0].x, P[1].y - P[0].y);
        g.arc(P[0].x, P[0].y, r, 0, Math.PI * 2); g.stroke();
      } else if (d.kind === "fib") {
        const x0 = Math.min(P[0].x, P[1].x), x1 = Math.max(P[0].x, P[1].x, this.plotW);
        g.font = "10px IBM Plex Mono, monospace"; g.textBaseline = "bottom";
        for (const f of FIB) {
          const price = d.p[1].p + (d.p[0].p - d.p[1].p) * f;
          const y = this.yOf(price);
          g.globalAlpha = f === 0 || f === 1 ? 1 : 0.7;
          g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
          g.fillText(`${(f * 100).toFixed(1)}%  ${fmt(price)}`, x0 + 4, y - 2);
        }
        g.globalAlpha = 1;
        g.setLineDash([2, 3]); g.beginPath(); g.moveTo(P[0].x, P[0].y); g.lineTo(P[1].x, P[1].y); g.stroke(); g.setLineDash([]);
      }
      if (selected) for (const q of P) { g.fillStyle = C.paper; g.fillRect(q.x - 4, q.y - 4, 8, 8); g.strokeStyle = C.sel; g.strokeRect(q.x - 4, q.y - 4, 8, 8); }
    }
  }
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / p;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * p;
}
function label(g, x, y, txt, bg, fg) {
  g.font = "11px IBM Plex Mono, monospace";
  g.fillStyle = bg; g.fillRect(x, y - 9, AXIS_W - 2, 18);
  g.fillStyle = fg; g.textBaseline = "middle"; g.fillText(txt, x + 6, y);
}
function segDist(p, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y, L = dx * dx + dy * dy || 1;
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}
function inBox(p, a, b, m) {
  return p.x >= Math.min(a.x, b.x) - m && p.x <= Math.max(a.x, b.x) + m && p.y >= Math.min(a.y, b.y) - m && p.y <= Math.max(a.y, b.y) + m;
}
