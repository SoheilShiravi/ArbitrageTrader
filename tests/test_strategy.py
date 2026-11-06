from __future__ import annotations

import logging
import unittest

from arbbot.config import BotConfig, TerminalConfig
from arbbot.models import BUY, FEED_DISCONNECTED, FEED_OK, SELL, Position, Quote, ReferenceQuote
from arbbot.strategy import MARKET_CLOSED_RETRY_DELAY, RETRY_DELAY, ArbitrageStrategy, make_snapshot

PIP = 0.0001


def make_config(hedging: bool = False, tp: float = 0.0, sl: float = 9.0,
                hold_minutes: float = 1.0) -> BotConfig:
    return BotConfig(TerminalConfig("MT1", "a.exe", "EURUSD"), TerminalConfig("MT2", "b.exe", "EURUSD"),
                     lot=0.01, divergence_pips=3.0, hedging=hedging, take_profit_pips=tp,
                     stop_loss_pips=sl, max_hold_seconds=hold_minutes * 60)


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTrader:
    pip = PIP

    def __init__(self):
        self.bid, self.ask = 1.10000, 1.10002
        self._positions: list[Position] = []
        self._next_ticket = 1
        self.orders: list[tuple[str, str, str]] = []
        self.fail_open = False
        self.market_closed = False
        self.closed_market = False

    def set(self, bid: float, ask: float) -> None:
        self.bid, self.ask = bid, ask

    def quote(self):
        return Quote(self.bid, self.ask)

    def positions(self):
        return list(self._positions)

    def open_position(self, direction, reason=""):
        self.orders.append(("open", direction, reason))
        self.market_closed = self.closed_market
        if self.fail_open or self.closed_market:
            return False
        price = self.ask if direction == BUY else self.bid
        self._positions.append(Position(self._next_ticket, direction, 0.01, price, 0.0))
        self._next_ticket += 1
        return True

    def close_position(self, position, reason=""):
        self.orders.append(("close", position.direction, reason))
        self._positions = [p for p in self._positions if p.ticket != position.ticket]
        return True

    def directions(self):
        return sorted(p.direction for p in self._positions)


class FakeFeed:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.status = FEED_OK
        self.set(1.10000, 1.10002)

    def set(self, bid: float, ask: float) -> None:
        self.bid, self.ask = bid, ask
        self.changed_at = self.clock.now

    def read(self):
        return ReferenceQuote(self.bid, self.ask, self.changed_at, self.clock.now, self.status)


class StrategyTestCase(unittest.TestCase):
    def build(self, hedging: bool = False, **config):
        self.clock = Clock()
        self.trader = FakeTrader()
        self.feed = FakeFeed(self.clock)
        log = logging.getLogger("test")
        log.disabled = True
        self.strategy = ArbitrageStrategy(self.trader, self.feed, make_config(hedging, **config), log, self.clock)

    def tick(self, seconds: float = 0.1) -> None:
        self.clock.advance(seconds)
        self.strategy.on_tick()

    def order_kinds(self):
        return [o[:2] for o in self.trader.orders]


class SnapshotTests(unittest.TestCase):
    def test_gap_and_spreads(self):
        ref = ReferenceQuote(1.10030, 1.10032, 0, 0, FEED_OK)
        s = make_snapshot(Quote(1.10000, 1.10002), ref, PIP)
        self.assertAlmostEqual(s.gap, 3.0)
        self.assertAlmostEqual(s.spread1, 0.2)


class NoHedgingTests(StrategyTestCase):
    def test_no_trade_without_divergence(self):
        self.build()
        self.feed.set(1.10020, 1.10022)
        self.tick()
        self.assertEqual(self.trader.orders, [])

    def test_buy_on_divergence_then_exit_on_convergence(self):
        self.build()
        self.feed.set(1.10035, 1.10037)
        self.tick()
        self.assertEqual(self.trader.directions(), [BUY])
        self.tick()
        self.assertEqual(len(self.trader.orders), 1)
        self.trader.set(1.10034, 1.10036)
        self.tick()
        self.assertEqual(self.trader.orders[-1], ("close", BUY, "exit-converged"))
        self.assertEqual(self.trader.directions(), [])

    def test_sell_on_negative_divergence(self):
        self.build()
        self.feed.set(1.09965, 1.09967)
        self.tick()
        self.assertEqual(self.trader.directions(), [SELL])

    def test_only_one_position_at_a_time(self):
        self.build()
        self.feed.set(1.10035, 1.10037)
        self.tick()
        self.feed.set(1.10036, 1.10038)
        self.tick(3.0)
        self.assertEqual(self.trader.directions(), [BUY])

    def test_wide_spread_blocks_entry(self):
        self.build()
        self.trader.set(1.10000, 1.10040)
        self.feed.set(1.10060, 1.10062)
        self.tick()
        self.assertEqual(self.trader.orders, [])

    def test_stale_reference_blocks_entry(self):
        self.build()
        self.feed.set(1.10035, 1.10037)
        self.tick(6.0)
        self.assertEqual(self.trader.orders, [])

    def test_disconnected_reference_blocks_entry(self):
        self.build()
        self.feed.status = FEED_DISCONNECTED
        self.feed.set(1.10035, 1.10037)
        self.tick()
        self.assertEqual(self.trader.orders, [])

    def test_stop_loss_and_no_reentry_until_divergence_ends(self):
        self.build()
        self.feed.set(1.10035, 1.10037)
        self.tick()
        self.trader.set(1.09880, 1.09882)
        self.feed.set(1.09920, 1.09922)
        self.tick()
        self.assertEqual(self.trader.orders[-1], ("close", BUY, "exit-stop-loss"))
        self.feed.set(1.09921, 1.09923)
        self.tick(3.0)
        self.assertEqual(self.trader.directions(), [])
        self.feed.set(1.09882, 1.09884)
        self.tick()
        self.feed.set(1.09920, 1.09922)
        self.tick(3.0)
        self.assertEqual(self.trader.orders[-1], ("open", BUY, "entry"))

    def test_max_hold(self):
        self.build()
        self.feed.set(1.10035, 1.10037)
        self.tick()
        self.tick(0.01)
        for _ in range(4):
            self.feed.set(self.feed.bid + PIP / 10, self.feed.ask + PIP / 10)
            self.tick(14.0)
        self.assertEqual(self.trader.orders[-1][0], "open")
        self.feed.set(self.feed.bid + PIP / 10, self.feed.ask + PIP / 10)
        self.tick(4.0)
        self.assertEqual(self.trader.orders[-1], ("close", BUY, "exit-max-hold"))
        self.assertEqual(self.trader.directions(), [])

    def test_take_profit(self):
        self.build(tp=2.0)
        self.feed.set(1.10035, 1.10037)
        self.tick()
        self.trader.set(1.10025, 1.10027)
        self.feed.set(1.10061, 1.10063)
        self.tick()
        self.assertEqual(self.trader.orders[-1], ("close", BUY, "exit-take-profit"))

    def test_tp_sl_hold_zero_means_off(self):
        self.build(tp=0.0, sl=0.0, hold_minutes=0.0)
        self.feed.set(1.10035, 1.10037)
        self.tick()
        self.trader.set(1.09500, 1.09502)
        self.feed.set(1.09540, 1.09542)
        self.tick(3600.0)
        self.feed.set(1.09541, 1.09543)
        self.tick()
        self.assertEqual(self.trader.directions(), [BUY])

    def test_failed_order_is_retried_after_delay(self):
        self.build()
        self.trader.fail_open = True
        self.feed.set(1.10035, 1.10037)
        self.tick()
        self.feed.set(1.10036, 1.10038)
        self.tick()
        self.assertEqual(len(self.trader.orders), 1)
        self.tick(RETRY_DELAY)
        self.assertEqual(len(self.trader.orders), 2)


class HedgingTests(StrategyTestCase):
    def build_hedged(self):
        self.build(hedging=True)
        self.tick()
        self.assertEqual(self.order_kinds(), [("open", BUY), ("open", SELL)])
        self.trader.orders.clear()
        self.clock.advance(3.0)

    def test_opens_buy_and_sell_immediately_without_divergence(self):
        self.build(hedging=True)
        self.tick()
        self.assertEqual(self.trader.directions(), [BUY, SELL])

    def test_divergence_closes_profitable_sell_and_reopens_it(self):
        self.build_hedged()
        self.trader.set(1.09980, 1.09982)
        self.feed.set(1.10015, 1.10017)
        self.tick()
        self.assertEqual(self.order_kinds(), [("close", SELL), ("open", SELL)])
        self.assertEqual(self.trader.directions(), [BUY, SELL])

    def test_divergence_closes_profitable_buy_and_reopens_it(self):
        self.build_hedged()
        self.trader.set(1.10025, 1.10027)
        self.feed.set(1.09990, 1.09992)
        self.tick()
        self.assertEqual(self.order_kinds(), [("close", BUY), ("open", BUY)])

    def test_profitable_side_is_closed_whatever_the_gap_direction(self):
        self.build_hedged()
        self.trader.set(1.09980, 1.09982)
        self.feed.set(1.09945, 1.09947)
        self.tick()
        self.assertEqual(self.order_kinds(), [("close", SELL), ("open", SELL)])

    def test_nothing_closed_when_no_side_in_profit(self):
        self.build_hedged()
        self.trader.set(1.10001, 1.10003)
        self.feed.set(1.10036, 1.10038)
        self.tick()
        self.assertEqual(self.trader.orders, [])

    def test_no_action_without_divergence(self):
        self.build_hedged()
        self.trader.set(1.09980, 1.09982)
        self.feed.set(1.09990, 1.09992)
        self.tick()
        self.assertEqual(self.trader.orders, [])

    def test_tp_sl_hold_ignored_with_hedging(self):
        self.build(hedging=True, tp=1.0, sl=1.0, hold_minutes=0.01)
        self.tick()
        self.trader.orders.clear()
        self.trader.set(1.09950, 1.09952)
        self.feed.set(1.09960, 1.09962)
        self.tick(120.0)
        self.feed.set(1.09961, 1.09963)
        self.tick()
        self.assertEqual(self.trader.orders, [])

    def test_wide_spread_does_not_block_hedging(self):
        self.build(hedging=True)
        self.trader.set(1.10000, 1.10040)
        self.feed.set(1.10000, 1.10060)
        self.tick()
        self.assertEqual(self.trader.directions(), [BUY, SELL])
        self.trader.orders.clear()
        self.clock.advance(3.0)
        self.trader.set(1.09950, 1.09990)
        self.feed.set(1.09976, 1.10036)
        self.tick()
        self.assertEqual(self.order_kinds(), [("close", SELL), ("open", SELL)])

    def test_closed_market_is_retried_after_a_minute(self):
        self.build(hedging=True)
        self.trader.closed_market = True
        self.tick()
        self.assertEqual(self.order_kinds(), [("open", BUY)])
        self.tick(RETRY_DELAY + 1)
        self.assertEqual(len(self.trader.orders), 1)
        self.trader.closed_market = False
        self.tick(MARKET_CLOSED_RETRY_DELAY)
        self.assertEqual(self.trader.directions(), [BUY, SELL])

    def test_missing_side_is_restored(self):
        self.build_hedged()
        sell = next(p for p in self.trader.positions() if p.direction == SELL)
        self.trader.close_position(sell)
        self.tick()
        self.assertEqual(self.trader.directions(), [BUY, SELL])


if __name__ == "__main__":
    unittest.main()
