from __future__ import annotations

from dataclasses import dataclass

BUY = "BUY"
SELL = "SELL"

FEED_STARTING = 0
FEED_OK = 1
FEED_DISCONNECTED = 2
FEED_FATAL = -1


def opposite(direction: str) -> str:
    return SELL if direction == BUY else BUY


def is_gold(symbol: str) -> bool:
    return symbol.upper().startswith(("XAU", "GOLD"))


def auto_pip_size(symbol: str, digits: int, point: float) -> float:
    if is_gold(symbol):
        return 0.1
    return point * 10 if digits in (3, 5) else point


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float


@dataclass(frozen=True)
class ReferenceQuote:
    bid: float
    ask: float
    changed_at: float
    heartbeat: float
    status: int


@dataclass(frozen=True)
class Position:
    ticket: int
    direction: str
    volume: float
    price_open: float
    profit: float
