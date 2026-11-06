from __future__ import annotations

import logging
import logging.handlers
import multiprocessing as mp
import time

from .config import TerminalConfig
from .models import FEED_DISCONNECTED, FEED_FATAL, FEED_OK, ReferenceQuote

_BID, _ASK, _CHANGED_AT, _HEARTBEAT, _STATUS = range(5)
_SIZE = 5

_CONNECTION_CHECK_EVERY = 1.0
_RECONNECT_EVERY = 5.0


class ReferenceFeed:
    def __init__(self, terminal: TerminalConfig, poll_interval_ms: float, log_queue, log_level: int):
        self._terminal = terminal
        self._shared = mp.Array("d", _SIZE)
        self._stop = mp.Event()
        self._proc = mp.Process(
            target=_feed_worker,
            name="MT2-feed",
            args=(terminal, poll_interval_ms, self._shared, self._stop, log_queue, log_level),
            daemon=True,
        )

    def start(self, timeout_s: float) -> None:
        self._proc.start()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            quote = self.read()
            if quote.status == FEED_OK and quote.bid > 0 and quote.ask > 0:
                return
            if quote.status == FEED_FATAL or not self._proc.is_alive():
                raise RuntimeError("MT2 reference feed failed to start - see the log above")
            time.sleep(0.1)
        raise RuntimeError(f"timed out after {timeout_s:.0f}s waiting for the first MT2 quote "
                           f"of {self._terminal.symbol}")

    def read(self) -> ReferenceQuote:
        with self._shared.get_lock():
            bid, ask, changed_at, heartbeat, status = self._shared[:]
        return ReferenceQuote(bid, ask, changed_at, heartbeat, int(status))

    def is_alive(self) -> bool:
        return self._proc.is_alive()

    def stop(self) -> None:
        self._stop.set()
        if self._proc.pid is None:
            return
        self._proc.join(timeout=5)
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=2)


def _setup_child_logging(log_queue, log_level: int) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(logging.handlers.QueueHandler(log_queue))
    root.setLevel(log_level)


def _feed_worker(terminal: TerminalConfig, poll_interval_ms: float, shared, stop_event,
                 log_queue, log_level: int) -> None:
    _setup_child_logging(log_queue, log_level)
    log = logging.getLogger("mt2")

    def set_status(status: int) -> None:
        with shared.get_lock():
            shared[_STATUS] = status

    try:
        import MetaTrader5 as mt5
    except ImportError:
        log.error("The MetaTrader5 package is not installed: pip install MetaTrader5")
        set_status(FEED_FATAL)
        return

    from .mt5_utils import initialize_terminal, prepare_symbol

    symbol = terminal.symbol

    def connect() -> bool:
        nonlocal symbol
        if not initialize_terminal(mt5, terminal, log):
            return False
        info = prepare_symbol(mt5, terminal.symbol, terminal.name, log)
        if info is None:
            return False
        symbol = info.name
        return True

    if not connect():
        set_status(FEED_FATAL)
        mt5.shutdown()
        return

    poll_s = poll_interval_ms / 1000.0
    status = FEED_OK
    last_bid = last_ask = 0.0
    next_check = 0.0
    next_reconnect = 0.0
    try:
        while not stop_event.is_set():
            now = time.time()

            if now >= next_check:
                next_check = now + _CONNECTION_CHECK_EVERY
                info = mt5.terminal_info()
                connected = info is not None and bool(info.connected)
                if info is None and now >= next_reconnect:
                    next_reconnect = now + _RECONNECT_EVERY
                    log.warning("MT2: lost the link to the terminal, re-initialising ...")
                    mt5.shutdown()
                    connected = connect()
                new_status = FEED_OK if connected else FEED_DISCONNECTED
                if new_status != status:
                    if connected:
                        log.info("MT2: connection restored")
                    else:
                        log.warning("MT2: terminal is not connected to the broker - "
                                    "no new trades until it is back")
                    status = new_status

            tick = mt5.symbol_info_tick(symbol)
            with shared.get_lock():
                if tick is not None and tick.bid > 0 and tick.ask > 0:
                    if tick.bid != last_bid or tick.ask != last_ask:
                        last_bid, last_ask = tick.bid, tick.ask
                        shared[_BID] = tick.bid
                        shared[_ASK] = tick.ask
                        shared[_CHANGED_AT] = now
                shared[_HEARTBEAT] = now
                shared[_STATUS] = status

            if poll_s > 0:
                time.sleep(poll_s)
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("MT2 feed crashed")
        set_status(FEED_FATAL)
    finally:
        mt5.shutdown()
