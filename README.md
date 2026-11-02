# VIPER

Autonomous intraday options engine and terminal for **FYERS API v3**. It trades
NIFTY 50, NIFTY NEXT 50, SENSEX and BANKNIFTY options. It runs a 4-week Shadow
simulator next to live trading, learns from both, and runs on a dual-IP VPS.

```
browser tabs ──(1 local WS each)──► FastAPI ──┬─► MarketHub (singleton) ──► FyersDataSocket  ── IP_B ─► FYERS
                                              │      ▲ refcounted subs       (data app)
                                              │      └─ tick→candle aggregator (1s…1d)
                                              ├─► Engine: features → setups → rules → learner → risk
                                              │      ├─ ShadowBook (bid/ask + slippage + charges)
                                              │      └─ LiveBook ── FyersRest(trading) ── IP_A ─► FYERS
                                              │                  └─ FyersOrderSocket ── IP_A ─► FYERS
                                              └─► SQLite (WAL): trades, lessons, rules, tokens, drawings
```

## Repository audit

The repository was empty when this work started (no commits, no branches), so
there was no existing SURF chart, FYERS wrapper or rate limiter to patch. Viper
was built from scratch, and each root cause in the brief is designed out:

| Problem | Root cause addressed | Where |
|---|---|---|
| FYERS 1-IP-per-app whitelist on a 2-IP VPS | Every outbound socket is source-bound per app role | `viper/net/binding.py` |
| False "API limit reached" | No client-side rejection; streaming instead of polling; coalescing; backoff only on real 429 | `viper/net/ratelimit.py`, `viper/fyers/rest.py`, `viper/market/hub.py` |
| Flat SURF charts | 0/null/NaN stripped server- and client-side; Y-domain = visible window only | `viper/market/candles.py`, `web/js/util.js`, `web/js/sparkline.js`, `web/js/chart/engine.js` |

## 1. Dual-IP source binding

* **REST**: each app role gets a `requests.Session` with `SourceAddressAdapter`
  (urllib3 `source_address=(ip, 0)`). The trading app is bound to `VPS_TRADING_SOURCE_IP` (IP_A), the data
  app to `VPS_DATA_SOURCE_IP` (IP_B).
* **WebSockets**: the FYERS SDK builds its own `websocket.WebSocketApp` and calls
  `run_forever()` on a thread it owns, so there's no parameter to pass. Viper
  hooks `WebSocketApp.run_forever` and binds the whole loop (reconnects included)
  by URL path: `/hsm/` (data socket) goes to IP_B, `/trade/` (order socket) goes to IP_A.
* **Everything else** (the SDK's internal `requests.post` symbol lookups, OAuth token
  exchange) runs inside `bound_to(ip)`, a thread-local scope enforced by a
  `socket.connect` hook.
* `ENABLE_IP_BINDING=false` turns all of it off for laptops and staging. With binding
  enabled, startup verifies that both IPs are bindable on the host. If either isn't,
  FYERS calls are disabled with a clear error rather than leaving from the wrong IP.
* `tests/test_binding.py` uses loopback aliases (127.0.0.2/.3) to check that HTTP,
  plain sockets and SDK-style WebSockets each leave from their assigned IP.

If you only have one FYERS app, leave the data app empty: market data reuses
the trading token and leaves from IP_A (`DATA_USES_TRADING_TOKEN`).

## 2. No more false "API limit reached"

* The token bucket **waits** a few ms during bursts. It never raises.
* Identical concurrent GETs are **single-flighted**, and quotes, history and option
  chain have short TTL caches.
* Retries with exponential backoff (honouring `Retry-After`) happen **only** when
  FYERS returns HTTP 429 or a 429 code. Expired tokens surface as auth errors,
  never as limit errors.
* **Nothing in the UI polls FYERS.** One `FyersDataSocket` and one `FyersOrderSocket`
  per process. Subscriptions are reference-counted across tabs, chain views
  and the engine. Ticks are coalesced and pushed to every tab over one local
  WebSocket, about 4 batches per second.
* The Market page shows per-app request counts, real 429s, coalesced hits and p50 latency.

## 3. Charts that can't flatline, with full 2D navigation

* Bars with any 0/null/NaN price are dropped before scaling (server `sanitize`
  and client `sanitize`).
* Auto-fit Y = `[min(low) − pad, max(high) + pad]` of the **visible** candles only.
* Navigation: drag pans X **and** Y. Wheel zooms X. Shift-wheel or dragging the price
  axis zooms Y. Dragging the time axis scales X. Trackpad horizontal scroll pans.
  Pinch works on touch. Double-click or **Reset view** restores auto-fit. The
  **Auto-fit Y** toggle is also available.
* Sub-minute frames (1s/5s/30s) are aggregated live from the tick stream and
  stitched to REST history with no seam. Frames of 1m and up anchor to the 09:15
  IST session open, as FYERS does.
* Drawing tools: trend, horizontal, vertical, Fibonacci, rectangle and an adjustable
  circle. They have drag handles, drag-to-move and Delete to remove, and are stored
  in data space (time, price) in SQLite so they survive reloads and timeframe changes.

## Engine

* **Setups**: momentum scalp (5s impulse z-score aligned with the 1m EMA trend);
  ATM order-book imbalance; breakout pullback (20-bar breakout retest); and
  TWAP/EMA mean reversion. Index spot has no volume on FYERS, so a session TWAP
  stands in for VWAP on the underlying.
* **Regimes**: trending, chop, high-IV (VIX / realised vol) and expiry gamma.
* **Execution**: long CE/PE only, so per-trade loss is bounded by the premium.
  * Strike moneyness is chosen by regime in AUTO.
  * Sizing obeys Max Capital %, Max SL %, Max Hold, the daily loss limit, max
    concurrent positions and the kill switch.
  * Live entries are marketable IOC limits, with a protective **SL-M at FYERS**
    that is modified as the trail ratchets.
* **Automation matrix** (per instrument):
  * `OFF`: never touch.
  * `ON`: execute setups at fixed sizing.
  * `AUTO`: learner-weighted selection, poor strategy×regime pairs switched off,
    size scaled 0.5–1.5× by edge.
* **Live orders additionally require** `LIVE_TRADING_ENABLED=true`, a live
  (non-synthetic) feed, an authenticated trading app and market hours. The Positions
  page lists the exact blockers per instrument.
* **Reconciliation**:
  * Fills and position changes arrive from the order socket.
  * A REST sweep every 30 s catches anything missed.
  * Positions closed by hand in the FYERS app are closed in Viper as `manual_fyers`.
  * Positions opened by hand are adopted and shown, but never auto-exited.

## Shadow and learning

* Shadow runs on every instrument against live ticks. It fills at ask + slippage
  and exits at bid − slippage (wider spread or larger size means more ticks), and
  charges Indian F&O fees.
* Learning uses rolling 4-week Beta posteriors per (strategy, regime, instrument).
  Live trades count double. Shadow explores via Thompson sampling; live uses the
  posterior mean.
* **Post-trade audit** runs five minutes after each exit, so post-exit drift is
  known. It uses MFE/MAE, slippage and hold time to classify:
  * late exit (gave back ≥1R)
  * stopped by noise
  * premature exit
  * dead trade / theta decay on expiry day
  * entry into resistance or support (with fading momentum)
  * excess slippage

  Each finding writes a ledger row (mistake, where, patch) and applies a
  **bounded parameter patch** that the next bar reads. The Learnings page shows the
  ledger, the strategy×regime posterior grid and the active patches.

## Teaching Viper

Plain text, PDF or chart screenshots, scoped to one instrument or all. A
deterministic parser turns sentences into enforceable rules: time windows,
open/close buffers, call/put-only or bans, RSI/VIX/VWAP filters, regime or
strategy bans, max trades/day, max hold and stop %. Unrecognised text is stored as
a **note** and labelled "note only", never claimed as enforced.

Screenshots and scanned PDFs are read with Claude vision when `ANTHROPIC_API_KEY`
is set (structured JSON output). Claude's sentences are then re-parsed by the same
deterministic parser. Without a key, tesseract OCR is used if it's installed.

## Run locally (staging)

```bash
cp .env.example .env
```

```bash
scripts/start.sh
```

Open http://127.0.0.1:8710. With no FYERS tokens, `FEED_MODE=auto` runs the
**synthetic feed**. A yellow SYNTHETIC FEED banner shows, and live orders are
hard-blocked. Everything else (charts, chain, shadow, learning) works, so the
stack can be exercised before VPS deployment.

Tests:

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

## Deploy on the dual-IP VPS

1. **Assign both IPs** to the host (`ip -4 addr` should show IP_A and IP_B).
2. **Whitelist** IP_A on the FYERS trading app and IP_B on the data app. Set each
   app's redirect URI to `https://<your-domain>/api/auth/callback`.
3. Fill in `.env`:
   * `ENABLE_IP_BINDING=true`, `VPS_TRADING_SOURCE_IP`, `VPS_DATA_SOURCE_IP`
   * both apps' IDs and secrets
   * `VIPER_PUBLIC_BASE_URL`
   * `VIPER_UI_PASSWORD` and `VIPER_SESSION_SECRET`
4. **Verify egress.** This must print `OK` for both IPs:

   ```bash
   deploy/check_ips.sh
   ```

   On some providers a secondary IP is inbound-only until you add policy routing.
   For example, with IP_B on eth0 and gateway GW:

   ```bash
   sudo ip route add default via GW dev eth0 src IP_B table 102
   ```

   ```bash
   sudo ip rule add from IP_B table 102
   ```

   Make these persistent through netplan or your network config.

   Floating IPs that are NAT'd from a private anchor address (for example
   DigitalOcean reserved IPs) can't be used as a bind source. Use two real
   public interface addresses.
5. Install the service:

   ```bash
   sudo deploy/install_service.sh
   ```

   ```bash
   sudo systemctl start viper
   ```

   The service runs `scripts/start.sh --check` before every start.
6. Put `deploy/nginx.conf` in front (WebSocket upgrade included) and add TLS with certbot.
7. **Each morning**: on the Market page, pick **Non-Trading API** and click
   *Authenticate / reconnect*, then the same for **Trading API**. Tokens are
   exchanged from each app's own IP, sockets restart, and every open tab updates live.
8. Leave `LIVE_TRADING_ENABLED=false` until shadow results justify it. Then
   enable it and set instruments to `ON` or `AUTO`.

## Known limitations

* The live FYERS paths (OAuth, sockets, order placement, reconciliation) follow
  the fyers-apiv3 3.2.1 SDK and the v3 REST contract. They could not be run against
  a real account from the build environment, so do a supervised first session with
  1 lot before enabling `AUTO`. The synthetic feed and the test suite exercise
  everything else.
* Daily login is the official OAuth redirect (one click per app). Fully headless
  TOTP login is not included because it depends on undocumented FYERS endpoints.
* The kill switch sends FYERS exit-all and books trades at the last bid. Exact
  fill prices are visible in the FYERS tradebook.
* Charges and the shadow slippage model are approximations. Lot sizes come from
  the public FYERS symbol master, with `LOT_SIZE_*` env fallbacks.
* Second-resolution history (1s/5s/30s) is requested from FYERS. When it isn't
  served, those charts build from live ticks only.
