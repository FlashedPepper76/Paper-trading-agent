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
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# 5-minute VTI timeframe — matches the granularity the user wants on the chart.
# 6.5 h × 12 bars/h = 78 bars per trading day, giving real shape to the line.
_VTI_TIMEFRAME = TimeFrame(5, TimeFrameUnit.Minute)


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
    """Keep VTI 5-minute bars current in Supabase (Plutus only).

    Runs every 5 minutes (when now.minute % 5 == 0) to match the requested
    VTI granularity. Each call fetches ALL bars for the last 5 trading days
    and upserts them — idempotent against the (symbol, price_time) PK, so
    already-stored bars are just no-ops and new bars are inserted.

    Uses a 15-minute lag (bar_end = now - 15 min) to stay within Alpaca's
    free-tier SIP latency window on paper accounts.

    Also refreshes the daily history backfill once per hour (minute == 0)
    for the last 90 days, so the baseline is always available even if the
    intraday table is fresh.
    """
    if config.AGENT_ID != "plutus":
        return

    now = datetime.now(timezone.utc)

    # ── 5-minute intraday bars (every 5 minutes) ──────────────────────────────
    if now.minute % 5 == 0:
        try:
            bar_end   = now - timedelta(minutes=15)   # free-tier SIP lag
            bar_start = now - timedelta(days=5)        # covers last ~3 trading days
            req = StockBarsRequest(
                symbol_or_symbols=["VTI"],
                timeframe=_VTI_TIMEFRAME,
                start=bar_start,
                end=bar_end,
                feed=DataFeed.SIP,
            )
            bars = ac.data_client.get_stock_bars(req).data
            vti_bars = bars.get("VTI", [])
            if vti_bars:
                rows = [
                    {
                        "symbol": "VTI",
                        "price_date": bar.timestamp.strftime("%Y-%m-%d"),
                        "price_time": bar.timestamp.isoformat(),
                        "close": round(float(bar.close), 4),
                    }
                    for bar in vti_bars
                ]
                requests.post(
                    f"{SUPABASE_URL}/rest/v1/benchmark_prices",
                    headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                    params={"on_conflict": "symbol,price_time"},
                    json=rows,
                    timeout=15,
                ).raise_for_status()
                print(f"VTI 5-min bars upserted: {len(rows)}")
        except Exception as exc:
            print(f"VTI 5-min snapshot failed (non-fatal): {exc}")

    # ── Daily history back-fill (once per hour) ───────────────────────────────
    if now.minute == 0:
        try:
            daily_bars = ac.get_recent_bars(["VTI"], lookback_days=90).get("VTI", [])
            if daily_bars:
                rows = [
                    {
                        "symbol": "VTI",
                        "price_date": bar.timestamp.strftime("%Y-%m-%d"),
                        "price_time": bar.timestamp.strftime("%Y-%m-%d") + "T20:00:00+00:00",
                        "close": round(float(bar.close), 4),
                        "updated_at": now.isoformat(),
                    }
                    for bar in daily_bars
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
