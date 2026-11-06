from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from arbbot.config import ConfigError, load_config

SHIPPED_CONFIG = Path(__file__).resolve().parent.parent / "config.txt"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        for name in ("a", "b"):
            (self.dir / name).mkdir()
            (self.dir / name / "terminal64.exe").write_bytes(b"")
        self.a = str(self.dir / "a" / "terminal64.exe")
        self.b = str(self.dir / "b")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, meta1: str, meta2: str, edit=lambda text: text) -> Path:
        text = SHIPPED_CONFIG.read_text(encoding="utf-8-sig")
        text = re.sub(r"(?m)^Meta1 = .*$", lambda _: f"Meta1 = {meta1}", text)
        text = re.sub(r"(?m)^Meta2 = .*$", lambda _: f"Meta2 = {meta2}", text)
        path = self.dir / "config.txt"
        path.write_text(edit(text), encoding="utf-8")
        return path

    def test_shipped_config_loads(self):
        cfg = load_config(self.write(self.a, self.b))
        self.assertTrue(cfg.mt2.path.endswith("terminal64.exe"))
        self.assertEqual(cfg.mt1.symbol, "EURUSD")
        self.assertEqual(cfg.lot, 0.01)
        self.assertEqual(cfg.divergence_pips, 3.0)
        self.assertFalse(cfg.hedging)
        self.assertEqual((cfg.take_profit_pips, cfg.stop_loss_pips), (5.0, 10.0))
        self.assertEqual(cfg.max_hold_seconds, 600.0)

    def test_optional_settings_default(self):
        cfg = load_config(self.write(self.a, self.b,
                                     lambda t: re.sub(r"(?m)^(TP|SL|Time) = .*$", "", t)))
        self.assertEqual((cfg.take_profit_pips, cfg.stop_loss_pips), (0.0, 0.0))
        self.assertEqual(cfg.max_hold_seconds, 600.0)

    def test_hedging_one_turns_it_on(self):
        cfg = load_config(self.write(self.a, self.b, lambda t: t.replace("Hedging = 0", "Hedging = 1")))
        self.assertTrue(cfg.hedging)

    def test_hedging_must_be_zero_or_one(self):
        path = self.write(self.a, self.b, lambda t: t.replace("Hedging = 0", "Hedging = yes"))
        with self.assertRaisesRegex(ConfigError, "Hedging must be 1 or 0"):
            load_config(path)

    def test_same_terminal_rejected(self):
        with self.assertRaisesRegex(ConfigError, "same terminal"):
            load_config(self.write(self.a, self.a))

    def test_missing_terminal_rejected(self):
        with self.assertRaisesRegex(ConfigError, "Meta1: file not found"):
            load_config(self.write(str(self.dir / "nope.exe"), self.b))

    def test_unknown_setting_rejected(self):
        path = self.write(self.a, self.b, lambda t: t + "dry_run = true\n")
        with self.assertRaisesRegex(ConfigError, "unknown setting"):
            load_config(path)

    def test_missing_setting_rejected(self):
        path = self.write(self.a, self.b, lambda t: t.replace("Lot = 0.01", "Lot ="))
        with self.assertRaisesRegex(ConfigError, "missing value for: Lot"):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
