"""Strategy lab: several simple, rule-based strategies (both directions) that run side by side.

The same code produces backtest trades (Binance archive + Hyperliquid history, 2021 onward) and live
paper trades, so a bot's paper results can be compared with its own backtest. No parameter tuning:
each strategy uses one fixed, conventional setting so the backtest is not curve-fitted.
"""
