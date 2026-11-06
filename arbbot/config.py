from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_KEYS = {"meta1": "Meta1", "meta2": "Meta2", "symbol": "Symbol", "lot": "Lot", "pip": "Pip",
         "hedging": "Hedging", "tp": "TP", "sl": "SL", "time": "Time"}
_REQUIRED = ("meta1", "meta2", "symbol", "lot", "pip", "hedging")
DEFAULT_HOLD_MINUTES = 10.0


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TerminalConfig:
    name: str
    path: str
    symbol: str
    timeout_ms: int = 60000


@dataclass(frozen=True)
class BotConfig:
    mt1: TerminalConfig
    mt2: TerminalConfig
    lot: float
    divergence_pips: float
    hedging: bool
    take_profit_pips: float = 0.0
    stop_loss_pips: float = 0.0
    max_hold_seconds: float = DEFAULT_HOLD_MINUTES * 60

    exit_fraction: float = 0.2
    cooldown_seconds: float = 2.0
    max_reference_age_ms: float = 5000.0
    deviation_points: int = 20
    magic: int = 20261106
    comment: str = "ArbBot"
    poll_interval_ms: float = 10.0
    feed_timeout_seconds: float = 3.0
    status_interval_seconds: float = 10.0
    log_file: str = "arbitrage_bot.log"

    @property
    def exit_divergence_pips(self) -> float:
        return self.divergence_pips * self.exit_fraction

    @property
    def max_spread_pips(self) -> float:
        return self.divergence_pips


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1256", errors="replace")


def _terminal_path(key: str, value: str) -> str:
    path = os.path.expandvars(value.strip().strip('"').strip("'"))
    if os.path.isdir(path):
        path = os.path.join(path, "terminal64.exe")
    if not os.path.isfile(path):
        raise ConfigError(f"{key}: file not found: {path}")
    return os.path.abspath(path)


def _number(key: str, value: str, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got '{value}'") from None
    if number < 0 or (number == 0 and not allow_zero):
        raise ConfigError(f"{key} must be {'0 or more' if allow_zero else 'greater than 0'}, got {value}")
    return number


def load_config(path: str | os.PathLike) -> BotConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    values: dict[str, str] = {}
    for number, line in enumerate(_read_text(path).splitlines(), 1):
        line = line.strip()
        if not line or line[0] in ";#":
            continue
        key, sep, value = line.partition("=")
        key = key.strip().lower()
        if not sep or key not in _KEYS:
            raise ConfigError(f"line {number}: unknown setting '{line}' "
                              f"(expected {', '.join(_KEYS.values())})")
        if key in values:
            raise ConfigError(f"{_KEYS[key]} is set twice (line {number})")
        values[key] = value.strip()

    missing = [_KEYS[key] for key in _REQUIRED if not values.get(key)]
    if missing:
        raise ConfigError(f"missing value for: {', '.join(missing)}")

    meta1 = _terminal_path("Meta1", values["meta1"])
    meta2 = _terminal_path("Meta2", values["meta2"])
    if os.path.normcase(meta1) == os.path.normcase(meta2):
        raise ConfigError("Meta1 and Meta2 point to the same terminal64.exe - use two separate "
                          "MetaTrader 5 installations (two different folders)")

    if values["hedging"] not in ("0", "1"):
        raise ConfigError(f"Hedging must be 1 or 0, got '{values['hedging']}'")

    symbol = values["symbol"]
    return BotConfig(
        mt1=TerminalConfig("MT1", meta1, symbol),
        mt2=TerminalConfig("MT2", meta2, symbol),
        lot=_number("Lot", values["lot"]),
        divergence_pips=_number("Pip", values["pip"]),
        hedging=values["hedging"] == "1",
        take_profit_pips=_number("TP", values.get("tp") or "0", allow_zero=True),
        stop_loss_pips=_number("SL", values.get("sl") or "0", allow_zero=True),
        max_hold_seconds=60 * _number("Time", values.get("time") or str(DEFAULT_HOLD_MINUTES),
                                      allow_zero=True),
    )
