"""
One-off manual order tool, used for testing only — this is NOT part of the
autonomous strategy (see strategy.py for that). Triggered by the
"Manual Test Order" GitHub Actions workflow (workflow_dispatch).

Equities/ETFs only, Alpaca PAPER trading only (paper=True below).

Writes the outcome (success or error) to last_order_result.json so it can
be read back via the GitHub Contents API, since GitHub Actions' raw job
logs aren't reachable from this assistant's sandbox.
"""
import json
import os
import sys

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

try:
    result = trading_client.submit_order(order_request)
    output = {
        "success": True,
        "id": str(result.id),
        "symbol": result.symbol,
        "side": str(result.side),
        "qty": str(result.qty),
        "status": str(result.status),
    }
except Exception as e:
    output = {"success": False, "symbol": SYMBOL, "side": SIDE, "qty": QTY, "error": str(e)}

with open("last_order_result.json", "w") as f:
    json.dump(output, f, indent=2)

print(json.dumps(output, indent=2))

if not output["success"]:
    sys.exit(1)
