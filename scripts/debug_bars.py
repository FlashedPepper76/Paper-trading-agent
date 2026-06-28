"""One-off diagnostic: why is get_recent_bars returning nothing for every
symbol? Prints the raw shape of what comes back so we can see exactly
where it's failing."""
import alpaca_client as ac

symbols = ["AAPL", "MSFT", "SPY"]
print(f"Requesting bars for {symbols}...")
try:
    bars = ac.get_recent_bars(symbols, lookback_days=35)
    print(f"Type of result: {type(bars)}")
    try:
        keys = list(bars.keys())
        print(f"Keys present: {keys}")
    except Exception as e:
        print(f"No .keys() on result ({e}); repr: {bars!r}")
    for sym in symbols:
        b = bars.get(sym) if hasattr(bars, "get") else None
        print(f"  {sym}: {len(b) if b else 0} bars" + (f", last={b[-1]}" if b else ""))
except Exception as e:
    print(f"get_recent_bars raised: {type(e).__name__}: {e}")
