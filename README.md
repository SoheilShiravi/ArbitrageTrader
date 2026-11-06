# MetaTrader Arbitrage Bot

## Running

1. Open both MetaTrader 5 terminals (installed in two separate folders) and log in to both. Turn on **Algo Trading** in MetaTrader 1.
2. Edit `config.txt`. It must be in the same folder as `ArbitrageBot.exe`.
3. Run `ArbitrageBot.exe`. Press **Ctrl+C** to stop. The log is saved to `arbitrage_bot.log`.

To run from source: `pip install -r requirements.txt`, then `python main.py`.

## config.txt parameters

| Parameter | Description | Example |
|---|---|---|
| `Meta1` | Path of MetaTrader 1's `terminal64.exe`. Every trade is placed on this terminal | `C:\Program Files\MetaTrader 5\terminal64.exe` |
| `Meta2` | Path of MetaTrader 2's `terminal64.exe`. Used only as the reference price | `C:\Program Files\Opo group MetaTrader 5\terminal64.exe` |
| `Symbol` | Symbol to trade. The broker's own name is found automatically (e.g. `XAUUSD!`). `GOLD` means gold | `EURUSD` / `XAUUSD` |
| `Lot` | Volume of each trade | `0.01` |
| `Pip` | Minimum divergence between the two terminals' prices before the bot acts (in pips). For gold, 1 pip = $0.10. With `Hedging = 0`, no trade is opened while either spread is wider than `Pip` | `3` |
| `Hedging` | `1`: open a Buy and a Sell immediately. When the divergence reaches `Pip`, close whichever position is in profit and open it again right away<br>`0`: buy or sell in the direction of the divergence, and close the position when the prices converge | `0` |
| `TP` | Take profit (pips). Only with `Hedging = 0`. `0` = off | `5` |
| `SL` | Stop loss (pips). Only with `Hedging = 0`. Must be larger than the spread. `0` = off | `10` |
| `Time` | Maximum time a position stays open (minutes). Only with `Hedging = 0`. `0` = off. Default 10 | `10` |
