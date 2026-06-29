"""
Lightweight account snapshot — pulls current equity/cash/positions straight
from Alpaca and writes them to Supabase. Deliberately separate from
main.py/ai_agent.py: this never calls Gemini, never makes a trading
decision, and never touches the risk caps. It exists purely so the
dashboard can show near-real-time numbers without that freshness being
coupled to how often the (deliberately slower, Gemini-rate-limited) AI
decision cycle runs.

Triggered every minute during market hours by its own Supabase pg_cron job
+ GitHub Actions workflow (snapshot_plutus.yml / snapshot_helios.yml) —
independent of trade.yml / trade_helios.yml's 15-minute cadence.
"""
import os
from datetime import datetime, timezone

import requests

import alpaca_client as ac
import config
from schedule import market_phase

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


def main():
    phase = market_phase()
    if phase == "quiet":
        print(f"Phase: {phase} — overnight pause, skipping the snapshot pull entirely.")
        return
    print(f"Phase: {phase}")

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


if __name__ == "__main__":
    main()
