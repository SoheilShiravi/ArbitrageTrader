from __future__ import annotations

import argparse
import logging
import logging.handlers
import multiprocessing as mp
import sys
import time
from pathlib import Path

from arbbot.config import BotConfig, ConfigError, load_config
from arbbot.reference_feed import ReferenceFeed
from arbbot.strategy import ArbitrageStrategy, make_snapshot

BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
log = logging.getLogger("bot")


def setup_logging(cfg: BotConfig, log_queue) -> logging.handlers.QueueListener:
    formatter = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(processName)-11s %(message)s",
                                  "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(BASE_DIR / cfg.log_file, maxBytes=10 * 1024 * 1024,
                                             backupCount=5, encoding="utf-8"),
    ]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)

    listener = logging.handlers.QueueListener(log_queue, *handlers, respect_handler_level=True)
    listener.start()
    return listener


def describe(cfg: BotConfig) -> None:
    log.info("=" * 72)
    log.info("MT1 (trades)    : %s", cfg.mt1.path)
    log.info("MT2 (reference) : %s", cfg.mt2.path)
    log.info("Symbol / lot    : %s / %g", cfg.mt1.symbol, cfg.lot)
    log.info("Pip (divergence): %g pips", cfg.divergence_pips)
    if cfg.hedging:
        log.info("Hedging         : ON")
    else:
        log.info("Hedging         : OFF | TP %g pips | SL %g pips | Time %g min (0 = off)",
                 cfg.take_profit_pips, cfg.stop_loss_pips, cfg.max_hold_seconds / 60)
    log.info("=" * 72)


def check_startup_gap(cfg: BotConfig, trader, feed: ReferenceFeed, pip: float) -> None:
    q1 = trader.quote()
    if q1 is None:
        return
    s = make_snapshot(q1, feed.read(), pip)
    log.info("Prices now: MT1 %.*f/%.*f | MT2 %.*f/%.*f | gap %+.2f pips (pip = %g)",
             trader.digits, s.bid1, trader.digits, s.ask1, trader.digits, s.bid2,
             trader.digits, s.ask2, s.gap, pip)
    if abs(s.gap) >= 3 * cfg.divergence_pips:
        log.warning("MT1 and MT2 already differ by %+.1f pips - check that both terminals show "
                    "the same instrument. The bot will act on this gap right away.", s.gap)


def run(cfg: BotConfig, log_queue) -> int:
    from arbbot.meta_trader import Mt1Trader

    describe(cfg)
    feed = ReferenceFeed(cfg.mt2, cfg.poll_interval_ms, log_queue, logging.INFO)
    trader = Mt1Trader(cfg.mt1, cfg)
    try:
        log.info("Starting the MT2 reference feed ...")
        feed.start(timeout_s=cfg.mt2.timeout_ms / 1000.0 + 30)
        log.info("Connecting to MT1 ...")
        trader.connect()
        if cfg.hedging and not trader.is_hedging_account():
            log.error("Hedging = 1 but the MT1 account is a NETTING account, so a BUY and a SELL "
                      "cannot be open together. Use a hedging account or set Hedging = 0.")
            return 1

        strategy = ArbitrageStrategy(trader, feed, cfg)
        check_startup_gap(cfg, trader, feed, strategy.pip)
        log.info("Bot is running - press Ctrl+C to stop.")

        pause = cfg.poll_interval_ms / 1000.0
        while True:
            if not feed.is_alive():
                log.error("The MT2 feed process has stopped - shutting down.")
                return 1
            if trader.is_connected():
                strategy.on_tick()
            time.sleep(pause)
    except KeyboardInterrupt:
        log.info("Stopping (Ctrl+C) - open positions are left as they are.")
        return 0
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    finally:
        feed.stop()
        trader.shutdown()
        log.info("Bot stopped.")


def enable_ctrl_c() -> None:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)


def main() -> int:
    enable_ctrl_c()
    parser = argparse.ArgumentParser(description="MT1/MT2 MetaTrader 5 arbitrage bot")
    parser.add_argument("--config", default=str(BASE_DIR / "config.txt"),
                        help="path of the config file (default: config.txt next to main.py)")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    log_queue = mp.Queue()
    listener = setup_logging(cfg, log_queue)
    try:
        return run(cfg, log_queue)
    finally:
        listener.stop()


if __name__ == "__main__":
    mp.freeze_support()
    exit_code = main()
    if exit_code and getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close ...")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(exit_code)
