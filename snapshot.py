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
from datetime import datetime, timezone

import requests

import alpaca_client as ac
import config

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
    """Fetch recent VTI daily closes via Alpaca and upsert to Supabase.

    Called from snapshot.py so the compare-page benchmark line reads from
    Supabase instead of calling Yahoo Finance / stooq directly (both are
    blocked from Vercel's egress IPs). Alpaca's SIP feed has daily bars
    available for free (data is always > 15 min old by definition for daily
    bars, so the free tier latency restriction never applies).

    Only runs when AGENT_ID == "plutus" so three concurrent snapshots
    (one per agent) don't triple-fetch the same data.
    """
    if config.AGENT_ID != "plutus":
        return

    try:
        bars_by_symbol = ac.get_recent_bars(["VTI"], lookback_days=90)
        vti_bars = bars_by_symbol.get("VTI", [])
        if not vti_bars:
            print("VTI snapshot: no bars returned")
            return

        rows = [
            {
                "symbol": "VTI",
                "price_date": bar.timestamp.strftime("%Y-%m-%d"),
                "close": round(float(bar.close), 4),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            for bar in vti_bars
        ]

        requests.post(
            f"{SUPABASE_URL}/rest/v1/benchmark_prices",
            headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            params={"on_conflict": "symbol,price_date"},
            json=rows,
            timeout=15,
        ).raise_for_status()

        print(f"VTI benchmark updated: {len(rows)} daily bars")
    except Exception as exc:
        # Non-fatal: benchmark data is cosmetic; don't crash the snapshot
        print(f"VTI snapshot failed (non-fatal): {exc}")


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

    # Upsert the single equity/cash/position-count row for this agent.
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

    # Refresh the per-symbol positions snapshot the dashboard's Positions
    # page reads. Upsert-then-prune (not delete-then-insert) — this runs
    # roughly every minute and ai_agent.py writes to the same table on its
    # own 15-minute cadence, so a wholesale delete from either process could
    # land mid-write from the other. Upserting by the (agent_id, symbol)
    # unique constraint and only deleting symbols no longer held avoids that
    # window entirely.
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

    # Update VTI benchmark data (Plutus only, idempotent upsert)
    _snapshot_vti()


if __name__ == "__main__":
    main()
