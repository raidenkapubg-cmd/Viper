"""The four tradable underlyings and their derivative metadata."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    key: str            # stable id used across API/DB/UI
    label: str
    spot: str           # Fyers index symbol
    exchange: str       # NSE | BSE
    fo_prefix: str      # derivative symbol root, e.g. NSE:NIFTY
    strike_step: int
    default_lot: int    # fallback; overridden by symbol master / LOT_SIZE_* env
    synthetic_seed: float  # starting level for the synthetic staging feed only


INSTRUMENTS: dict[str, Instrument] = {
    "NIFTY50": Instrument("NIFTY50", "NIFTY 50", "NSE:NIFTY50-INDEX", "NSE", "NSE:NIFTY", 50, 75, 24350.0),
    "NIFTYNXT50": Instrument("NIFTYNXT50", "NIFTY NEXT 50", "NSE:NIFTYNXT50-INDEX", "NSE", "NSE:NIFTYNXT50", 100, 25, 67400.0),
    "SENSEX": Instrument("SENSEX", "SENSEX", "BSE:SENSEX-INDEX", "BSE", "BSE:SENSEX", 100, 20, 80150.0),
    "BANKNIFTY": Instrument("BANKNIFTY", "BANKNIFTY", "NSE:NIFTYBANK-INDEX", "NSE", "NSE:BANKNIFTY", 100, 35, 52800.0),
}
VIX_SYMBOL = "NSE:INDIAVIX-INDEX"
SPOT_TO_KEY = {i.spot: k for k, i in INSTRUMENTS.items()}


def lot_size(key: str, overrides: dict[str, int], master: dict[str, int] | None = None) -> int:
    if key in overrides:
        return overrides[key]
    if master and key in master:
        return master[key]
    return INSTRUMENTS[key].default_lot


def atm_strike(key: str, spot: float) -> float:
    step = INSTRUMENTS[key].strike_step
    return round(spot / step) * step


def instrument_for_symbol(symbol: str) -> str | None:
    """Map a spot or derivative Fyers symbol back to an instrument key."""
    if symbol in SPOT_TO_KEY:
        return SPOT_TO_KEY[symbol]
    # Order matters: NIFTYNXT50 and BANKNIFTY before NIFTY.
    for key in ("NIFTYNXT50", "BANKNIFTY", "SENSEX", "NIFTY50"):
        if symbol.startswith(INSTRUMENTS[key].fo_prefix):
            return key
    return None
