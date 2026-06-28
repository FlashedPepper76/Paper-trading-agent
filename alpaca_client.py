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
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

# paper=True forces the paper-trading base URL (paper-api.alpaca.markets).
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def is_market_open() -> bool:
    clock = trading_client.get_clock()
    return clock.is_open


def get_account():
    return trading_client.get_account()


def get_open_positions() -> dict:
    """Returns {symbol: position} for all currently held positions."""
    return {p.symbol: p for p in trading_client.get_all_positions()}


def get_pending_buy_symbols() -> set:
    """
    Symbols with a still-open (unfilled) buy order. A symbol can have a
    pending order without yet showing up in get_open_positions() — e.g. an
    order submitted while the market is technically open per the clock but
    not actually trading (a force-run outside real session hours, a slow
    fill, etc). Without this check, a duplicate buy for the same symbol can
    slip through on a later run before the first order ever fills.
    """
    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    return {o.symbol for o in open_orders if o.side == OrderSide.BUY}


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
