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

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

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


def get_recent_bars(symbols: list[str], lookback_days: int = 60) -> dict:
    """Daily bars for a list of equity/ETF symbols (no crypto)."""
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        limit=lookback_days,
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


def close_position(symbol: str):
    return trading_client.close_position(symbol)
