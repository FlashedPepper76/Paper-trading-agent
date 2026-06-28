"""
One-off manual cancel tool, used for cleanup only — NOT part of the
autonomous strategy. Cancels one or more open (unfilled) orders by ID.
Triggered by the "Cancel Orders" GitHub Actions workflow (workflow_dispatch).

Writes the outcome (per-order success/error) to last_cancel_result.json so
it can be read back via the GitHub Contents API.
"""
import json
import os
import sys

from alpaca.trading.client import TradingClient

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ORDER_IDS = [o.strip() for o in os.environ["ORDER_IDS"].split(",") if o.strip()]

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

results = []
any_failed = False
for order_id in ORDER_IDS:
    try:
        trading_client.cancel_order_by_id(order_id)
        results.append({"order_id": order_id, "success": True})
    except Exception as e:
        any_failed = True
        results.append({"order_id": order_id, "success": False, "error": str(e)})

with open("last_cancel_result.json", "w") as f:
    json.dump(results, f, indent=2)

print(json.dumps(results, indent=2))

if any_failed:
    sys.exit(1)
