from __future__ import annotations

import logging
import time

import MetaTrader5 as mt5

from .config import BotConfig, TerminalConfig
from .models import BUY, SELL, Position, Quote, auto_pip_size
from .mt5_utils import initialize_terminal, prepare_symbol

_DONE = {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE_PARTIAL}
_PRICE_MOVED = {mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED,
                mt5.TRADE_RETCODE_PRICE_OFF}
_MAX_ATTEMPTS = 3

_SYMBOL_FILLING_FOK = 1
_SYMBOL_FILLING_IOC = 2


class Mt1Trader:
    def __init__(self, terminal: TerminalConfig, config: BotConfig,
                 log: logging.Logger | None = None):
        self.terminal = terminal
        self.trading = config
        self.symbol = terminal.symbol
        self.log = log or logging.getLogger("mt1")
        self.pip = 0.0
        self.digits = 5
        self.volume = config.lot
        self.market_closed = False
        self._fillings = [mt5.ORDER_FILLING_RETURN]
        self._connected = False
        self._next_check = 0.0

    def connect(self) -> None:
        if not initialize_terminal(mt5, self.terminal, self.log):
            raise RuntimeError("could not connect to MT1 - see the log above")
        info = prepare_symbol(mt5, self.terminal.symbol, self.terminal.name, self.log)
        if info is None:
            raise RuntimeError(f"MT1 symbol '{self.terminal.symbol}' is not available")
        self.symbol = info.name

        self.pip = auto_pip_size(info.name, info.digits, info.point)
        self.digits = info.digits
        self.volume = self._normalize_volume(self.trading.lot, info)
        self._fillings = self._filling_candidates(info.filling_mode)
        self._connected = True
        self._next_check = time.monotonic() + 1.0
        self._warn_if_trading_disabled(info)
        self.log.info("MT1: %s | digits %d | pip %g | lot %g", self.symbol, info.digits,
                      self.pip, self.volume)

    def shutdown(self) -> None:
        mt5.shutdown()

    def is_hedging_account(self) -> bool:
        account = mt5.account_info()
        return account is not None and account.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING

    def is_connected(self) -> bool:
        now = time.monotonic()
        if now < self._next_check:
            return self._connected
        self._next_check = now + 1.0

        info = mt5.terminal_info()
        if info is None:
            self.log.warning("MT1: lost the link to the terminal, re-initialising ...")
            mt5.shutdown()
            connected = (initialize_terminal(mt5, self.terminal, self.log)
                         and prepare_symbol(mt5, self.symbol, self.terminal.name, self.log) is not None)
            if not connected:
                self._next_check = now + 5.0
        else:
            connected = bool(info.connected)

        if connected != self._connected:
            if connected:
                self.log.info("MT1: connection restored")
            else:
                self.log.warning("MT1: terminal is not connected to the broker - trading paused")
        self._connected = connected
        return connected

    def quote(self) -> Quote | None:
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            return None
        return Quote(tick.bid, tick.ask)

    def positions(self) -> list[Position] | None:
        raw = mt5.positions_get(symbol=self.symbol)
        if raw is None:
            if mt5.last_error()[0] == mt5.RES_S_OK:
                return []
            return None
        return [
            Position(
                ticket=p.ticket,
                direction=BUY if p.type == mt5.POSITION_TYPE_BUY else SELL,
                volume=p.volume,
                price_open=p.price_open,
                profit=p.profit,
            )
            for p in raw
            if p.magic == self.trading.magic
        ]

    def open_position(self, direction: str, reason: str = "") -> bool:
        order_type = mt5.ORDER_TYPE_BUY if direction == BUY else mt5.ORDER_TYPE_SELL
        return self._send(order_type, self.volume, None, f"OPEN {direction} ({reason})")

    def close_position(self, position: Position, reason: str = "") -> bool:
        order_type = mt5.ORDER_TYPE_SELL if position.direction == BUY else mt5.ORDER_TYPE_BUY
        return self._send(order_type, position.volume, position.ticket,
                          f"CLOSE {position.direction} #{position.ticket} ({reason})")

    def _send(self, order_type: int, volume: float, position_ticket: int | None, label: str) -> bool:
        self.market_closed = False
        result = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                self.log.error("MT1 %s: no price available (%s)", label, mt5.last_error())
                return False
            is_buy = order_type == mt5.ORDER_TYPE_BUY
            price = tick.ask if is_buy else tick.bid
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": float(volume),
                "type": order_type,
                "price": price,
                "deviation": self.trading.deviation_points,
                "magic": self.trading.magic,
                "comment": self.trading.comment,
                "type_time": mt5.ORDER_TIME_GTC,
            }
            if position_ticket is not None:
                request["position"] = position_ticket

            result = self._send_with_filling(request)
            if result is None:
                self.log.error("MT1 %s: order_send failed: %s", label, mt5.last_error())
                return False
            if result.retcode in _DONE:
                fill = result.price or price
                slippage = ((fill - price) if is_buy else (price - fill)) / self.pip
                self.log.info("MT1 %s done: order %s @ %.*f (requested %.*f, slippage %+.1f pips)",
                              label, result.order, self.digits, fill, self.digits, price, slippage)
                return True
            if result.retcode in _PRICE_MOVED and attempt < _MAX_ATTEMPTS:
                self.log.warning("MT1 %s: %s (retcode %s) - retrying", label, result.comment,
                                 result.retcode)
                continue
            break

        if result.retcode == mt5.TRADE_RETCODE_MARKET_CLOSED:
            self.market_closed = True
            self.log.warning("MT1 %s: market is closed - retrying in a minute", label)
            return False
        self.log.error("MT1 %s rejected: retcode %s (%s)", label, result.retcode, result.comment)
        return False

    def _send_with_filling(self, request: dict):
        result = None
        for filling in list(self._fillings):
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
                if result is not None and filling != self._fillings[0]:
                    self._fillings.remove(filling)
                    self._fillings.insert(0, filling)
                return result
        return result

    def _normalize_volume(self, lot: float, info) -> float:
        step = info.volume_step or 0.01
        volume = round(round(lot / step) * step, 8)
        volume = min(max(volume, info.volume_min), info.volume_max)
        if abs(volume - lot) > 1e-9:
            self.log.warning("MT1: lot %g adjusted to %g (min %g, max %g, step %g)",
                             lot, volume, info.volume_min, info.volume_max, step)
        return volume

    @staticmethod
    def _filling_candidates(flags: int) -> list[int]:
        modes = []
        if flags & _SYMBOL_FILLING_FOK:
            modes.append(mt5.ORDER_FILLING_FOK)
        if flags & _SYMBOL_FILLING_IOC:
            modes.append(mt5.ORDER_FILLING_IOC)
        modes.append(mt5.ORDER_FILLING_RETURN)
        return modes

    def _warn_if_trading_disabled(self, info) -> None:
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        if terminal is not None and not terminal.trade_allowed:
            self.log.warning("MT1: 'Algo Trading' is OFF in the terminal - orders will be rejected "
                             "until you press the Algo Trading button")
        if account is not None and not account.trade_allowed:
            self.log.warning("MT1: trading is not allowed on this account (investor password?)")
        if info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            self.log.warning("MT1: trading on %s is restricted by the broker (trade_mode=%s)",
                             self.symbol, info.trade_mode)
