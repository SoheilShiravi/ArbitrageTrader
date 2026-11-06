from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .config import BotConfig
from .models import BUY, FEED_OK, SELL, Position, Quote, ReferenceQuote

RETRY_DELAY = 2.0
MARKET_CLOSED_RETRY_DELAY = 60.0
LOG_REPEAT = 30.0


class Trader(Protocol):
    pip: float
    market_closed: bool

    def quote(self) -> Quote | None: ...
    def positions(self) -> list[Position] | None: ...
    def open_position(self, direction: str, reason: str = "") -> bool: ...
    def close_position(self, position: Position, reason: str = "") -> bool: ...


class Feed(Protocol):
    def read(self) -> ReferenceQuote: ...


@dataclass(frozen=True)
class Snapshot:
    bid1: float
    ask1: float
    bid2: float
    ask2: float
    spread1: float
    spread2: float
    gap: float


def make_snapshot(q1: Quote, ref: ReferenceQuote, pip: float) -> Snapshot:
    return Snapshot(
        bid1=q1.bid, ask1=q1.ask, bid2=ref.bid, ask2=ref.ask,
        spread1=(q1.ask - q1.bid) / pip, spread2=(ref.ask - ref.bid) / pip,
        gap=((ref.bid + ref.ask) - (q1.bid + q1.ask)) / 2.0 / pip,
    )


class ArbitrageStrategy:
    def __init__(self, trader: Trader, feed: Feed, config: BotConfig,
                 log: logging.Logger | None = None, clock: Callable[[], float] = time.time):
        self.trader = trader
        self.feed = feed
        self.cfg = config
        self.pip = trader.pip
        self.log = log or logging.getLogger("strategy")
        self.clock = clock
        self._decimals = max(0, -math.floor(math.log10(self.pip))) + 1

        self._last_action = -math.inf
        self._first_seen: dict[int, float] = {}
        self._disarmed: set[str] = set()
        self._retry_at: dict[str, float] = {}
        self._logged_at: dict[str, float] = {}
        self._gap_min = math.inf
        self._gap_max = -math.inf
        self._last_status = clock()

    def on_tick(self) -> None:
        now = self.clock()
        q1 = self.trader.quote()
        if q1 is None:
            self._log_once("mt1-quote", now, logging.WARNING, "No price from MT1 - waiting")
            return
        ref = self.feed.read()
        if ref.bid <= 0 or ref.ask <= 0:
            return
        positions = self.trader.positions()
        if positions is None:
            self._log_once("positions", now, logging.WARNING,
                           "Could not read MT1 positions - skipping this tick")
            return

        s = make_snapshot(q1, ref, self.pip)
        self._gap_min = min(self._gap_min, s.gap)
        self._gap_max = max(self._gap_max, s.gap)
        self._track(positions, now)

        ref_problem = self._reference_problem(ref, now)
        signal = self._signal(s) if ref_problem is None else None
        self._disarmed.intersection_update({signal})
        block = self._entry_block(s, ref, ref_problem, now)
        if signal and block and block != "cooldown":
            self._log_once("blocked-" + block.split()[0], now, logging.INFO,
                           "Signal %s ignored: %s (gap %+.2f pips)", signal, block, s.gap)

        if self.cfg.hedging:
            self._hedge(s, positions, signal, block, now)
        else:
            self._directional(s, positions, signal, block, ref_problem is None, now)

        self._status(s, positions, ref_problem, now)

    def _directional(self, s: Snapshot, positions: list[Position], signal: str | None,
                     block: str | None, ref_ok: bool, now: float) -> None:
        still_open = []
        for p in positions:
            reason = self._exit_reason(p, s, ref_ok, now)
            if reason and self._close(p, f"exit-{reason}", s, now):
                if reason != "converged":
                    self._disarmed.add(p.direction)
                continue
            still_open.append(p)

        if signal is None or block is not None or signal in self._disarmed or still_open:
            return
        self._open(signal, "entry", s, now)

    def _hedge(self, s: Snapshot, positions: list[Position], signal: str | None,
               block: str | None, now: float) -> None:
        missing = [d for d in (BUY, SELL) if not any(p.direction == d for p in positions)]
        if missing:
            for direction in missing:
                if not self._open(direction, "hedge", s, now) and self.trader.market_closed:
                    break
            return

        if signal is None or block is not None:
            return
        best = max(positions, key=lambda p: self._profit_pips(p, s))
        if self._profit_pips(best, s) <= 0:
            self._log_once("hedge-noprofit", now, logging.INFO,
                           "Divergence %+.2f pips reached but neither BUY nor SELL is in profit",
                           s.gap)
            return
        if self._close(best, f"hedge-take-profit-{signal.lower()}", s, now):
            self._open(best.direction, "hedge-reopen", s, now)

    def _signal(self, s: Snapshot) -> str | None:
        if s.gap >= self.cfg.divergence_pips:
            return BUY
        if s.gap <= -self.cfg.divergence_pips:
            return SELL
        return None

    def _reference_problem(self, ref: ReferenceQuote, now: float) -> str | None:
        if ref.status != FEED_OK:
            return "MT2 is disconnected"
        if now - ref.heartbeat > self.cfg.feed_timeout_seconds:
            return "MT2 feed is not responding"
        return None

    def _entry_block(self, s: Snapshot, ref: ReferenceQuote, ref_problem: str | None,
                     now: float) -> str | None:
        if ref_problem:
            return ref_problem
        max_age = self.cfg.max_reference_age_ms
        if max_age > 0 and (now - ref.changed_at) * 1000.0 > max_age:
            return f"MT2 price unchanged for {now - ref.changed_at:.1f}s (stale)"
        spread_problem = None if self.cfg.hedging else self._spread_problem(s)
        if spread_problem:
            return spread_problem
        if now - self._last_action < self.cfg.cooldown_seconds:
            return "cooldown"
        return None

    def _spread_problem(self, s: Snapshot) -> str | None:
        limit = self.cfg.max_spread_pips
        if s.spread1 > limit:
            return f"MT1 spread {s.spread1:.1f} > {limit:g} pips"
        if s.spread2 > limit:
            return f"MT2 spread {s.spread2:.1f} > {limit:g} pips"
        return None

    def _exit_reason(self, p: Position, s: Snapshot, ref_ok: bool, now: float) -> str | None:
        cfg = self.cfg
        pips = self._profit_pips(p, s)
        if cfg.take_profit_pips > 0 and pips >= cfg.take_profit_pips:
            return "take-profit"
        if cfg.stop_loss_pips > 0 and pips <= -cfg.stop_loss_pips:
            return "stop-loss"
        if ref_ok:
            remaining = s.gap if p.direction == BUY else -s.gap
            if remaining <= self.cfg.exit_divergence_pips:
                return "converged"
        if cfg.max_hold_seconds > 0 and now - self._first_seen.get(p.ticket, now) >= cfg.max_hold_seconds:
            return "max-hold"
        return None

    def _profit_pips(self, p: Position, s: Snapshot) -> float:
        if p.direction == BUY:
            return (s.bid1 - p.price_open) / self.pip
        return (p.price_open - s.ask1) / self.pip

    def _open(self, direction: str, reason: str, s: Snapshot, now: float) -> bool:
        key = f"open-{direction}"
        if now < self._retry_at.get(key, 0.0):
            return False
        self.log.info(">> OPEN %s [%s] | gap %+.2f pips | MT1 %s/%s  MT2 %s/%s", direction, reason,
                      s.gap, self._px(s.bid1), self._px(s.ask1), self._px(s.bid2), self._px(s.ask2))
        if self.trader.open_position(direction, reason):
            self._last_action = now
            return True
        self._retry_at[key] = now + self._retry_delay()
        return False

    def _close(self, p: Position, reason: str, s: Snapshot, now: float) -> bool:
        key = f"close-{p.ticket}"
        if now < self._retry_at.get(key, 0.0):
            return False
        self.log.info("<< CLOSE %s #%d [%s] | %+.1f pips (%.2f) | gap %+.2f pips", p.direction,
                      p.ticket, reason, self._profit_pips(p, s), p.profit, s.gap)
        if self.trader.close_position(p, reason):
            self._last_action = now
            self._first_seen.pop(p.ticket, None)
            return True
        self._retry_at[key] = now + self._retry_delay()
        return False

    def _retry_delay(self) -> float:
        return MARKET_CLOSED_RETRY_DELAY if self.trader.market_closed else RETRY_DELAY

    def _track(self, positions: list[Position], now: float) -> None:
        tickets = {p.ticket for p in positions}
        for ticket in tickets:
            self._first_seen.setdefault(ticket, now)
        for ticket in list(self._first_seen):
            if ticket not in tickets:
                del self._first_seen[ticket]

    def _status(self, s: Snapshot, positions: list[Position], ref_problem: str | None,
                now: float) -> None:
        if now - self._last_status < self.cfg.status_interval_seconds:
            return
        buys = sum(1 for p in positions if p.direction == BUY)
        self.log.info("STATUS | MT1 %s/%s sp %.1f | MT2 %s/%s sp %.1f | gap %+.2f pips "
                      "[min %+.2f max %+.2f] | BUY %d SELL %d | P/L %.2f%s",
                      self._px(s.bid1), self._px(s.ask1), s.spread1,
                      self._px(s.bid2), self._px(s.ask2), s.spread2,
                      s.gap, self._gap_min, self._gap_max, buys, len(positions) - buys,
                      sum(p.profit for p in positions),
                      f" | {ref_problem}" if ref_problem else "")
        self._last_status = now
        self._gap_min, self._gap_max = math.inf, -math.inf

    def _px(self, price: float) -> str:
        return f"{price:.{self._decimals}f}"

    def _log_once(self, key: str, now: float, level: int, msg: str, *args) -> None:
        if now - self._logged_at.get(key, -math.inf) < LOG_REPEAT:
            return
        self._logged_at[key] = now
        self.log.log(level, msg, *args)
