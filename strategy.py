"""
Placeholder strategy: simple moving-average crossover, long-only.

BUY a symbol when its short MA crosses above its long MA (bullish) and we
don't already hold it, subject to MAX_NEW_BUYS_PER_RUN / MAX_OPEN_POSITIONS
/ cash-buffer caps from config.py.

SELL (close) a symbol we hold when its short MA crosses below its long MA
(bearish). This is exit-only — the bot never shorts a stock it doesn't own.

This is intentionally simple and meant to be replaced. All the
decision-making (which symbols, sizing, risk caps) lives here and in
config.py, so a real strategy can be swapped in without touching
main.py or alpaca_client.py.
"""
from alpaca.trading.enums import OrderSide

import config
import alpaca_client as ac


def _moving_average(closes: list[float], window: int):
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def _signal_for_symbol(bars) -> str | None:
    """Returns 'buy', 'sell', or None for one symbol's bar history."""
    closes = [b.close for b in bars]
    if len(closes) < config.LONG_MA + 1:
        return None

    short_now = _moving_average(closes, config.SHORT_MA)
    long_now = _moving_average(closes, config.LONG_MA)
    short_prev = _moving_average(closes[:-1], config.SHORT_MA)
    long_prev = _moving_average(closes[:-1], config.LONG_MA)

    if None in (short_now, long_now, short_prev, long_prev):
        return None

    crossed_up = short_prev <= long_prev and short_now > long_now
    crossed_down = short_prev >= long_prev and short_now < long_now

    if crossed_up:
        return "buy"
    if crossed_down:
        return "sell"
    return None


def run():
    account = ac.get_account()
    equity = float(account.equity)
    cash = float(account.cash)

    positions = ac.get_open_positions()
    bars_by_symbol = ac.get_recent_bars(
        config.UNIVERSE, lookback_days=config.LONG_MA + 5
    )

    new_buys = 0

    for symbol in config.UNIVERSE:
        bars = bars_by_symbol.get(symbol)
        if not bars:
            continue

        signal = _signal_for_symbol(bars)
        held = symbol in positions

        if signal == "sell" and held:
            qty = positions[symbol].qty
            print(f"SELL {symbol}: closing {qty} shares (bearish crossover)")
            ac.close_position(symbol)

        elif signal == "buy" and not held:
            if len(positions) >= config.MAX_OPEN_POSITIONS:
                print(f"SKIP buy {symbol}: max open positions reached")
                continue
            if new_buys >= config.MAX_NEW_BUYS_PER_RUN:
                print(f"SKIP buy {symbol}: max new buys per run reached")
                continue

            notional = equity * config.POSITION_SIZE_PCT
            free_cash = cash * (1 - config.MIN_CASH_BUFFER_PCT)
            if notional > free_cash:
                print(f"SKIP buy {symbol}: not enough free cash after buffer")
                continue

            print(f"BUY {symbol}: ${notional:.2f} notional (bullish crossover)")
            ac.submit_market_order(symbol, notional, OrderSide.BUY)
            new_buys += 1
            cash -= notional
        else:
            print(f"HOLD {symbol}: no actionable signal")
