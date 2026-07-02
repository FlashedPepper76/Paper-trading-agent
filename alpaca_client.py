"""
Thin wrapper around Alpaca's PAPER trading and market-data APIs.

This module is hard-restricted to equities/ETFs:
- TradingClient is constructed with paper=True, which pins it to Alpaca's
  paper-trading base URL. No code path here can place a live order.
- The trading universe is defined in config.py and only ever contains
  stock/ETF tickers. This module never imports or calls Alpaca's crypto
  endpoints.
"""
import os
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderType
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

# paper=True forces the paper-trading base URL (paper-api.alpaca.markets).
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def fetch_news_headlines(symbols: list[str], limit: int = 18) -> list[dict]:
    """
    Real headlines from Alpaca's News API (Benzinga-sourced) for the given
    symbols, most recent first. Uses the same API keys as trading. Returns a
    list of {headline, source, symbols, created_at} dicts; empty list on any
    failure — news is a nice-to-have, never a reason to stop a run.
    """
    import requests
    try:
        resp = requests.get(
            "https://data.alpaca.markets/v1beta1/news",
            headers={
                "APCA-API-KEY-ID": API_KEY,
                "APCA-API-SECRET-KEY": SECRET_KEY,
            },
            params={
                "symbols": ",".join(symbols),
                "limit": limit,
                "include_content": "false",
                "exclude_contentless": "true",
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("news", [])
        return [
            {
                "headline": n.get("headline", ""),
                "source": n.get("source", ""),
                "symbols": n.get("symbols", []),
                "created_at": n.get("created_at", ""),
            }
            for n in items
            if n.get("headline")
        ]
    except Exception as e:
        print(f"Alpaca news fetch failed ({e}) — continuing without headlines.")
        return []


def is_market_open() -> bool:
    clock = trading_client.get_clock()
    return clock.is_open


def minutes_until_next_open() -> float:
    """
    How many minutes from right now until the next real trading session
    opens. Large (multi-hour/day) on a weekend or holiday morning even if
    the wall-clock time looks like a normal pre-market hour — used to tell
    "today is genuinely about to open" apart from "today just isn't a
    trading day" before running anything tied to a specific clock time.
    """
    clock = trading_client.get_clock()
    if clock.is_open:
        return 0.0
    return (clock.next_open - clock.timestamp).total_seconds() / 60


def get_account():
    return trading_client.get_account()


def get_open_positions() -> dict:
    """Returns {symbol: position} for all currently held positions."""
    return {p.symbol: p for p in trading_client.get_all_positions()}


def get_pending_buy_info() -> dict[str, float]:
    """
    Returns {symbol: unfilled_qty} for open buy orders.

    Used in two ways by _enforce_caps:
    1. Block duplicate buys for the same symbol (same as the old
       get_pending_buy_symbols() check).
    2. Estimate how much cash is already committed to those pending orders
       so that a new run doesn't double-spend the same dollars — the most
       common cause of negative cash is a pre-market run queuing orders and
       a market-open run seeing the same account.cash (fills haven't landed
       yet) and committing it again to different symbols.
    """
    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    result = {}
    for o in open_orders:
        if o.side != OrderSide.BUY or not o.qty:
            continue
        unfilled = float(o.qty) - float(o.filled_qty or 0)
        if unfilled > 0:
            result[o.symbol] = unfilled
    return result


def get_pending_buy_symbols() -> set:
    return set(get_pending_buy_info().keys())


def get_recent_bars(symbols: list[str], lookback_days: int = 60) -> dict:
    """
    Daily bars for a list of equity/ETF symbols (no crypto).

    Uses an explicit start/end date range. Confirmed via direct testing that
    Alpaca's bars endpoint returns nothing at all for multi-symbol requests
    when only `limit` is given (regardless of feed) — a date range is
    required to actually get data back.

    Explicitly requests the SIP feed (full consolidated tape across all US
    exchanges) rather than the IEX-only default. This is free on the Basic
    plan as long as the data is more than 15 minutes old, which daily bars
    always are by definition.
    """
    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=lookback_days * 2)  # generous buffer for weekends/holidays
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.SIP,
    )
    return data_client.get_stock_bars(request).data


def submit_market_order(symbol: str, notional: float, side: OrderSide):
    order = MarketOrderRequest(
        symbol=symbol,
        notional=round(notional, 2),
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    return trading_client.submit_order(order)


def submit_qty_order(symbol: str, qty: int, side: OrderSide):
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    return trading_client.submit_order(order)


def close_position(symbol: str):
    return trading_client.close_position(symbol)


def submit_limit_order(symbol: str, qty: int, side: OrderSide, limit_price: float):
    """
    Submits a GTC limit order. Used for both limit buys (buy-the-dip price
    targets) and limit sells (take-profit / stop-gain targets).

    GTC (Good Till Cancelled) keeps the order live until it fills or the agent
    explicitly cancels it — appropriate for price targets that may take
    multiple sessions to hit. The agent should track these open orders and
    cancel/replace them if its thesis changes.
    """
    order = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price=round(limit_price, 2),
    )
    return trading_client.submit_order(order)
