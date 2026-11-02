"""Environment-driven configuration.

Every deploy-specific value comes from the process environment (optionally
seeded from a ``.env`` file in the project root or the path in ``VIPER_ENV_FILE``).
Nothing here hardcodes hosts, paths or IPs, so the same tree runs in the cloud
sandbox, on a staging laptop and on the dual-IP VPS.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    path = Path(os.environ.get("VIPER_ENV_FILE", ROOT / ".env"))
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        os.environ.setdefault(key, val)  # real env always wins over the file


_load_dotenv()


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _f(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _i(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    p = Path(v).expanduser() if v else default
    return p if p.is_absolute() else (ROOT / p)


@dataclass(frozen=True)
class FyersApp:
    """One Fyers API app (client id + secret) and the source IP it is whitelisted on."""

    role: str  # "trading" | "data"
    app_id: str
    secret: str
    redirect_uri: str
    source_ip: str  # "" => OS default route

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.secret)


@dataclass(frozen=True)
class Settings:
    env: str
    host: str
    port: int
    public_base_url: str
    data_dir: Path
    db_path: Path
    log_level: str
    ui_password: str
    session_secret: str

    enable_ip_binding: bool
    trading: FyersApp
    data: FyersApp
    # When only one Fyers app is available, the data role can reuse the trading
    # app's token (still bound to its own IP only if that IP is whitelisted).
    data_uses_trading_token: bool

    feed_mode: str  # auto | live | synthetic
    live_trading_enabled: bool
    market_open: str
    market_close: str
    timezone: str

    rest_rate_per_sec: float
    rest_burst: int
    rest_max_retries: int
    quote_cache_ms: int

    shadow_start_capital: float
    learning_window_days: int

    anthropic_api_key: str
    vision_model: str

    lot_overrides: dict[str, int] = field(default_factory=dict)


def load_settings() -> Settings:
    data_dir = _path("VIPER_DATA_DIR", ROOT / "data")
    base_url = _s("VIPER_PUBLIC_BASE_URL", f"http://127.0.0.1:{_i('VIPER_PORT', 8710)}")
    default_redirect = base_url.rstrip("/") + "/api/auth/callback"
    trading = FyersApp(
        role="trading",
        app_id=_s("FYERS_TRADING_APP_ID"),
        secret=_s("FYERS_TRADING_SECRET"),
        redirect_uri=_s("FYERS_TRADING_REDIRECT_URI", default_redirect),
        source_ip=_s("VPS_TRADING_SOURCE_IP"),
    )
    data = FyersApp(
        role="data",
        app_id=_s("FYERS_DATA_APP_ID"),
        secret=_s("FYERS_DATA_SECRET"),
        redirect_uri=_s("FYERS_DATA_REDIRECT_URI", default_redirect),
        source_ip=_s("VPS_DATA_SOURCE_IP"),
    )
    lots: dict[str, int] = {}
    for key in ("NIFTY50", "NIFTYNXT50", "SENSEX", "BANKNIFTY"):
        v = os.environ.get(f"LOT_SIZE_{key}")
        if v:
            lots[key] = int(v)
    return Settings(
        env=_s("VIPER_ENV", "staging"),
        host=_s("VIPER_HOST", "127.0.0.1"),
        port=_i("VIPER_PORT", 8710),
        public_base_url=base_url,
        data_dir=data_dir,
        db_path=_path("VIPER_DB_PATH", data_dir / "viper.sqlite3"),
        log_level=_s("VIPER_LOG_LEVEL", "INFO"),
        ui_password=_s("VIPER_UI_PASSWORD"),
        session_secret=_s("VIPER_SESSION_SECRET"),
        enable_ip_binding=_b("ENABLE_IP_BINDING", False),
        trading=trading,
        data=data,
        data_uses_trading_token=_b("DATA_USES_TRADING_TOKEN", not data.configured),
        feed_mode=_s("FEED_MODE", "auto").lower(),
        live_trading_enabled=_b("LIVE_TRADING_ENABLED", False),
        market_open=_s("MARKET_OPEN", "09:15"),
        market_close=_s("MARKET_CLOSE", "15:30"),
        timezone=_s("MARKET_TZ", "Asia/Kolkata"),
        rest_rate_per_sec=_f("FYERS_REST_RATE_PER_SEC", 8.0),
        rest_burst=_i("FYERS_REST_BURST", 10),
        rest_max_retries=_i("FYERS_REST_MAX_RETRIES", 5),
        quote_cache_ms=_i("FYERS_QUOTE_CACHE_MS", 750),
        shadow_start_capital=_f("SHADOW_START_CAPITAL", 500000.0),
        learning_window_days=_i("LEARNING_WINDOW_DAYS", 28),
        anthropic_api_key=_s("ANTHROPIC_API_KEY"),
        vision_model=_s("VISION_MODEL", "claude-opus-5-5"),
        lot_overrides=lots,
    )


settings = load_settings()
