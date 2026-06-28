"""One-off diagnostic: why is get_recent_bars returning nothing for every
symbol? Tests feed type (SIP vs IEX) and explicit start/end dates vs
limit-only to isolate the cause."""
import os
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

symbols = ["AAPL", "MSFT", "SPY"]
now = datetime.now(timezone.utc)


def try_request(label, **kwargs):
    print(f"\n--- {label} ---")
    try:
        request = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, **kwargs)
        result = client.get_stock_bars(request)
        data = result.data
        print(f"Keys present: {list(data.keys())}")
        for sym in symbols:
            b = data.get(sym)
            print(f"  {sym}: {len(b) if b else 0} bars" + (f", last={b[-1].close}" if b else ""))
    except Exception as e:
        print(f"Raised: {type(e).__name__}: {e}")


try_request("SIP feed, limit=35 (current production code)", limit=35, feed=DataFeed.SIP)
try_request("IEX feed, limit=35", limit=35, feed=DataFeed.IEX)
try_request(
    "SIP feed, explicit start/end (40 days back to 1 day back)",
    start=now - timedelta(days=40), end=now - timedelta(days=1), feed=DataFeed.SIP,
)
try_request(
    "IEX feed, explicit start/end (40 days back to 1 day back)",
    start=now - timedelta(days=40), end=now - timedelta(days=1), feed=DataFeed.IEX,
)
try_request("No feed specified, limit=35 (SDK default)", limit=35)

