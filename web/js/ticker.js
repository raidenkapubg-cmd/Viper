// Mechanical tabular ticker: only the digits that changed flip, in the direction of the move.
import { fmt } from "./util.js";

export class Ticker {
  constructor(el, decimals = 2) {
    this.el = el;
    this.el.classList.add("tk");
    this.d = decimals;
    this.v = null;
    this.spans = [];
  }
  set(v) {
    if (v == null || !Number.isFinite(v)) return;
    const txt = fmt(v, this.d);
    const dir = this.v == null ? 0 : Math.sign(v - this.v);
    if (this.spans.length !== txt.length) {
      this.el.textContent = "";
      this.spans = [...txt].map((ch) => {
        const s = document.createElement("span");
        s.textContent = ch;
        this.el.append(s);
        return s;
      });
    } else {
      [...txt].forEach((ch, i) => {
        const s = this.spans[i];
        if (s.textContent !== ch) {
          s.textContent = ch;
          s.classList.remove("fu", "fd");
          void s.offsetWidth; // restart animation
          if (dir) s.classList.add(dir > 0 ? "fu" : "fd");
        }
      });
    }
    this.v = v;
  }
}
