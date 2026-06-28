"""
Read-only diagnostic: dumps current positions and open (unfilled) orders for
the active agent's Alpaca account. NOT part of the trading pipeline. Used to
sanity-check duplicate-looking buys in the decision log against the actual
account state before deciding whether anything needs to be cancelled.

Writes to last_positions_check.json so it can be read back via the GitHub
Contents API.
"""
import json
import os

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
AGENT_ID = os.environ.get("AGENT_ID", "plutus")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

positions = trading_client.get_all_positions()
open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
account = trading_client.get_account()

output = {
    "agent_id": AGENT_ID,
    "account": {
        "equity": str(account.equity),
        "cash": str(account.cash),
    },
    "positions": [
        {
            "symbol": p.symbol,
            "qty": str(p.qty),
            "avg_entry_price": str(p.avg_entry_price),
            "current_price": str(p.current_price),
            "market_value": str(p.market_value),
        }
        for p in positions
    ],
    "open_orders": [
        {
            "id": str(o.id),
            "symbol": o.symbol,
            "side": str(o.side),
            "qty": str(o.qty),
            "status": str(o.status),
            "submitted_at": str(o.submitted_at),
        }
        for o in open_orders
    ],
}

with open("last_positions_check.json", "w") as f:
    json.dump(output, f, indent=2)

print(json.dumps(output, indent=2))
