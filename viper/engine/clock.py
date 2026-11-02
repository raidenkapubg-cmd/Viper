from __future__ import annotations

import time
from datetime import datetime

from viper.config import settings
from viper.market.candles import IST


def now_ist() -> datetime:
    return datetime.now(IST)


def day_start(ts: float | None = None) -> float:
    d = datetime.fromtimestamp(ts or time.time(), IST)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


OPEN_HM = _hm(settings.market_open)
CLOSE_HM = _hm(settings.market_close)


def market_open(ts: float | None = None) -> bool:
    d = datetime.fromtimestamp(ts or time.time(), IST)
    if d.weekday() >= 5:
        return False
    o = d.replace(hour=OPEN_HM[0], minute=OPEN_HM[1], second=0, microsecond=0)
    c = d.replace(hour=CLOSE_HM[0], minute=CLOSE_HM[1], second=0, microsecond=0)
    return o <= d < c


def minutes_to_close(ts: float | None = None) -> float:
    d = datetime.fromtimestamp(ts or time.time(), IST)
    c = d.replace(hour=CLOSE_HM[0], minute=CLOSE_HM[1], second=0, microsecond=0)
    return (c - d).total_seconds() / 60
