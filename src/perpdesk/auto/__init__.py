"""Phase 2 auto-trader. Strategy: 4h breakout, LONG only, on coins where it tested positive out-of-sample.

Only paper mode exists: trades are simulated against live Hyperliquid 1m candles with the same scorer
as the backtest. Nothing here holds keys or places orders.
"""
