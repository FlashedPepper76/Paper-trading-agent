"""
Lightweight account snapshot — pulls current equity/cash/positions straight
from Alpaca and writes them to Supabase. Deliberately separate from
main.py/ai_agent.py: this never calls Gemini, never makes a trading
decision, and never touches the risk caps. It exists purely so the
dashboard can show near-real-time numbers without that freshness being
coupled to how often the (deliberately slower, Gemini-rate-limited) AI
decision cycle runs.

Triggered every minute during and around market hours by its own Supabase
pg_cron job + GitHub Actions workflow. Deliberately does NOT check
market_phase() — the snapshot has no AI/API cost, so there's no reason to
gate it. Running during pre-market and after-hours is the whole point: it
keeps equity current through extended-hours price moves so the dashboard
always matches what Alpaca shows.
"""
import os
from datetime import datetime, timedelta, timezone

import requests

import alpaca_client as ac
import config
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]


def _headers(extra_prefer: str = "") -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra_prefer:
        h["Prefer"] = extra_prefer
    return h


def _snapshot_vti() -> None:
    """Keep the VTI benchmark current in Supabase.

    Two behaviours in one call (Plutus only):

    1. Daily history back-fill: upserts the last 90 days of daily closes so
       the compare chart always has a long enough baseline. Stored at 20:00 UTC
       (= 4 PM ET close) per day. Run only every ~60 minutes to avoid hammering
       Alpaca — we check whether Supabase already has today's daily close first.

    2. Intraday tick: fetches the most-recent 1-minute bar (using a 15-minute
       lag to stay within Alpaca's free-tier SIP latency window) and upserts it
       under its own price_time. At one call per minute during market hours this
       gives ~390 intraday VTI points per trading day, matching the density of
       Plutus's own run history and giving the benchmark line real definition
       on the compare chart.
    """
    if config.AGENT_ID != "plutus":
        return

    now = datetime.now(timezone.utc)

    # ── Intraday tick (every run) ─────────────────────────────────────────────
    try:
        # Stay 15 min behind to satisfy Alpaca free-tier SIP delay
        bar_end   = now - timedelta(minutes=15)
        bar_start = bar_end - timedelta(minutes=5)
        req = StockBarsRequest(
            symbol_or_symbols=["VTI"],
            timeframe=TimeFrame.Minute,
            start=bar_start,
            end=bar_end,
            feed=DataFeed.SIP,
        )
        bars = ac.data_client.get_stock_bars(req).data
        vti_bars = bars.get("VTI", [])
        if vti_bars:
            bar = vti_bars[-1]
            requests.post(
                f"{SUPABASE_URL}/rest/v1/benchmark_prices",
                headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "symbol,price_time"},
                json=[{
                    "symbol": "VTI",
                    "price_date": bar.timestamp.strftime("%Y-%m-%d"),
                    "price_time": bar.timestamp.isoformat(),
                    "close": round(float(bar.close), 4),
                }],
                timeout=10,
            ).raise_for_status()
    except Exception as exc:
        print(f"VTI intraday tick failed (non-fatal): {exc}")

    # ── Daily history back-fill (once per hour, rough guard) ─────────────────
    # Only run when the minute hand is near 0 (i.e., roughly once an hour) to
    # avoid 90-day Alpaca fetches every minute.
    if now.minute > 2:
        return

    try:
        bars_by_symbol = ac.get_recent_bars(["VTI"], lookback_days=90)
        vti_daily = bars_by_symbol.get("VTI", [])
        if not vti_daily:
            return
        rows = [
            {
                "symbol": "VTI",
                "price_date": bar.timestamp.strftime("%Y-%m-%d"),
                # Daily close stored at 20:00 UTC (4 PM ET)
                "price_time": bar.timestamp.strftime("%Y-%m-%d") + "T20:00:00+00:00",
                "close": round(float(bar.close), 4),
                "updated_at": now.isoformat(),
            }
            for bar in vti_daily
        ]
        requests.post(
            f"{SUPABASE_URL}/rest/v1/benchmark_prices",
            headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            params={"on_conflict": "symbol,price_time"},
            json=rows,
            timeout=15,
        ).raise_for_status()
        print(f"VTI daily history refreshed: {len(rows)} bars")
    except Exception as exc:
        print(f"VTI daily history failed (non-fatal): {exc}")


def main():
    account = ac.get_account()
    positions = ac.get_open_positions()

    held = {
        symbol: {
            "qty": float(pos.qty),
            "avg_entry_price": round(float(pos.avg_entry_price), 2),
            "current_price": round(float(pos.current_price), 2),
            "unrealized_pl_pct": round(float(pos.unrealized_plpc) * 100, 2),
            "market_value": round(float(pos.market_value), 2),
        }
        for symbol, pos in positions.items()
    }

    state_payload = {
        "agent_id": config.AGENT_ID,
        "equity": round(float(account.equity), 2),
        "cash": round(float(account.cash), 2),
        "num_open_positions": len(held),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    requests.post(
        f"{SUPABASE_URL}/rest/v1/agent_account_state",
        headers=_headers("resolution=merge-duplicates,return=minimal"),
        json=state_payload,
        timeout=15,
    ).raise_for_status()

    if held:
        rows = [
            {"agent_id": config.AGENT_ID, "symbol": symbol, **pos}
            for symbol, pos in held.items()
        ]
        requests.post(
            f"{SUPABASE_URL}/rest/v1/trading_agent_positions",
            headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            params={"on_conflict": "agent_id,symbol"},
            json=rows,
            timeout=30,
        ).raise_for_status()

    held_list = ",".join(held.keys()) if held else ""
    symbol_filter = f"not.in.({held_list})" if held_list else "not.is.null"
    requests.delete(
        f"{SUPABASE_URL}/rest/v1/trading_agent_positions",
        headers=_headers(),
        params={"agent_id": f"eq.{config.AGENT_ID}", "symbol": symbol_filter},
        timeout=15,
    ).raise_for_status()

    print(
        f"Snapshot logged for {config.AGENT_ID}: equity={state_payload['equity']} "
        f"cash={state_payload['cash']} positions={state_payload['num_open_positions']}"
    )

    _snapshot_vti()


if __name__ == "__main__":
    main()
