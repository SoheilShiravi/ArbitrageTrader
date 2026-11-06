from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace

from arbbot.models import auto_pip_size
from arbbot.mt5_utils import resolve_symbol

LOG = logging.getLogger("test")
LOG.disabled = True

IC_MARKETS = ["EURUSD", "XAUUSD", "XAUEUR", "GOLD.NYSE", "GOLD.NYSE-24", "GOLD.TO"]
OPO_GROUP = ["EURUSD!", "XAUUSD!", "XAUEUR!", "GOLD.247!"]


class FakeMt5:
    def __init__(self, names):
        self.names = names

    def symbol_info(self, name):
        return SimpleNamespace(name=name) if name in self.names else None

    def symbols_get(self, group):
        prefix = group.strip("*")
        if group.startswith("*"):
            return [SimpleNamespace(name=n) for n in self.names if prefix in n]
        return [SimpleNamespace(name=n) for n in self.names if n.startswith(prefix)]


class ResolveSymbolTests(unittest.TestCase):
    def check(self, broker, symbol, expected):
        self.assertEqual(resolve_symbol(FakeMt5(broker), symbol, "MT", LOG), expected)

    def test_exact_name(self):
        self.check(IC_MARKETS, "EURUSD", "EURUSD")
        self.check(IC_MARKETS, "XAUUSD", "XAUUSD")

    def test_broker_suffix(self):
        self.check(OPO_GROUP, "EURUSD", "EURUSD!")
        self.check(OPO_GROUP, "XAUUSD", "XAUUSD!")

    def test_gold_name_means_xauusd(self):
        self.check(IC_MARKETS, "GOLD", "XAUUSD")
        self.check(OPO_GROUP, "GOLD", "XAUUSD!")

    def test_other_symbol_is_not_a_suffix(self):
        self.check(["EURUSDX"], "EURUSD", None)

    def test_ambiguous_and_missing(self):
        self.check(["EURUSD.a", "EURUSD.b"], "EURUSD", None)
        self.check(IC_MARKETS, "BTCUSD", None)


class PipSizeTests(unittest.TestCase):
    def test_pip_sizes(self):
        self.assertAlmostEqual(auto_pip_size("EURUSD", 5, 0.00001), 0.0001)
        self.assertAlmostEqual(auto_pip_size("USDJPY", 3, 0.001), 0.01)
        self.assertAlmostEqual(auto_pip_size("XAUUSD", 2, 0.01), 0.1)
        self.assertAlmostEqual(auto_pip_size("XAUUSD!", 3, 0.001), 0.1)


if __name__ == "__main__":
    unittest.main()
