// One WebSocket per tab to the Viper server. The server multiplexes a single
// Fyers socket to every tab, so nothing in the UI ever polls Fyers REST.

const listeners = new Map();
export const state = {
  client: null,
  instruments: [],
  bySpot: {},
  ticks: new Map(),
  risk: null,
  network: null,
  books: null,
  connected: false,
  boot: null,
};

export function on(topic, fn) {
  if (!listeners.has(topic)) listeners.set(topic, new Set());
  listeners.get(topic).add(fn);
  return () => listeners.get(topic)?.delete(fn);
}
function emit(topic, data) {
  for (const fn of listeners.get(topic) || []) {
    try { fn(data); } catch (e) { console.error(topic, e); }
  }
}

let ws, backoff = 500, pingTimer;

export function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    backoff = 500;
    state.connected = true;
    emit("conn", true);
    clearInterval(pingTimer);
    pingTimer = setInterval(() => ws.readyState === 1 && ws.send("ping"), 15000);
  };
  ws.onclose = (e) => {
    state.connected = false;
    emit("conn", false);
    clearInterval(pingTimer);
    if (e.code === 4401) { location.href = "/login"; return; }
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 8000);
  };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    switch (m.t) {
      case "hello":
        state.client = m.d.client;
        state.boot = m.d;
        state.instruments = m.d.instruments;
        state.bySpot = Object.fromEntries(m.d.instruments.map((i) => [i.spot, i]));
        state.risk = m.d.risk;
        state.network = m.d.network;
        emit("hello", m.d);
        emit("network", m.d.network);
        break;
      case "ticks":
        for (const t of m.d) state.ticks.set(t.symbol, t);
        emit("ticks", m.d);
        break;
      case "books": state.books = m.d; emit("books", m.d); break;
      case "network": state.network = m.d; emit("network", m.d); break;
      case "feed": emit("feed", m.d); break;
      case "risk": state.risk = m.d; emit("risk", m.d); break;
      default: emit(m.t, m.d);
    }
  };
}

export const tick = (symbol) => state.ticks.get(symbol);
