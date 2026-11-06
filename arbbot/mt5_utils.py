from __future__ import annotations

import logging
import time

from .config import TerminalConfig


def initialize_terminal(mt5, terminal: TerminalConfig, log: logging.Logger) -> bool:
    log.info("%s: connecting to %s", terminal.name, terminal.path)
    if not mt5.initialize(path=terminal.path, timeout=terminal.timeout_ms):
        log.error("%s: initialize() failed: %s", terminal.name, mt5.last_error())
        return False

    account = mt5.account_info()
    info = mt5.terminal_info()
    if account is None or info is None:
        log.error("%s: the terminal started but no trading account is logged in - log in to "
                  "the account inside the terminal (%s)", terminal.name, mt5.last_error())
        mt5.shutdown()
        return False

    log.info("%s: account %s | %s | %s | terminal folder: %s",
             terminal.name, account.login, account.server, account.company, info.path)
    return True


_ALIASES = {"GOLD": ("XAUUSD", "GOLD"), "XAUUSD": ("XAUUSD", "GOLD")}


def resolve_symbol(mt5, symbol: str, name: str, log: logging.Logger) -> str | None:
    candidates: list[str] = []
    for wanted in _ALIASES.get(symbol.upper(), (symbol,)):
        if mt5.symbol_info(wanted) is not None:
            found = [wanted]
        else:
            found = [s.name for s in (mt5.symbols_get(group=f"{wanted}*") or ())
                     if s.name.upper().startswith(wanted.upper())
                     and not s.name[len(wanted):][:1].isalpha()]
        if len(found) == 1:
            if found[0] != symbol:
                log.info("%s: symbol %s is called %s on this broker", name, symbol, found[0])
            return found[0]
        candidates = candidates or found
    if candidates:
        log.error("%s: several symbols match '%s' on this broker: %s", name, symbol,
                  ", ".join(candidates[:15]))
    else:
        similar = mt5.symbols_get(group=f"*{symbol[:6]}*") or ()
        log.error("%s: symbol '%s' does not exist on this broker. Similar symbols: %s",
                  name, symbol, ", ".join(s.name for s in similar[:15]) or "none found")
    return None


def prepare_symbol(mt5, symbol: str, name: str, log: logging.Logger):
    symbol = resolve_symbol(mt5, symbol, name, log)
    if symbol is None:
        return None
    info = mt5.symbol_info(symbol)

    if not info.visible and not mt5.symbol_select(symbol, True):
        log.error("%s: could not add '%s' to Market Watch (%s)", name, symbol, mt5.last_error())
        return None

    for _ in range(50):
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and tick.bid > 0 and tick.ask > 0:
            break
        time.sleep(0.1)
    else:
        log.warning("%s: no price received yet for %s (market closed?)", name, symbol)

    return mt5.symbol_info(symbol)
