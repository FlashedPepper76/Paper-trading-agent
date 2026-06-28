"""
One-off manual order tool, used for testing only — this is NOT part of the
autonomous strategy (see strategy.py for that). Triggered by the
"Manual Test Order" GitHub Actions workflow (workflow_dispatch).

Equities/ETFs only, Alpaca PAPER trading only (paper=True below).
"""
import os

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
SYMBOL = os.environ["SYMBOL"].upper()
SIDE = os.environ["SIDE"].lower()
QTY = os.environ["QTY"]

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

order_request = MarketOrderRequest(
    symbol=SYMBOL,
    qty=QTY,
    side=OrderSide.BUY if SIDE == "buy" else OrderSide.SELL,
    time_in_force=TimeInForce.DAY,
)

result = trading_client.submit_order(order_request)

print(
    f"Order submitted: id={result.id} symbol={result.symbol} "
    f"side={result.side} qty={result.qty} status={result.status}"
)
