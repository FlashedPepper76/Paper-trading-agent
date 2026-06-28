"""
Weekly head-to-head digest: compares Plutus vs Helios since each agent's
first run and pushes one push notification via the same /api/notify
endpoint the trading agents already use for trade/failure alerts.

Run via .github/workflows/agent-review.yml.

Env vars required: SUPABASE_URL, SUPABASE_KEY, NOTIFY_SECRET
"""
import os
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
NOTIFY_SECRET = os.environ["NOTIFY_SECRET"]
NOTIFY_URL = "https://trading-agent-dashboard-mu.vercel.app/api/notify"

AGENTS = ["plutus", "helios"]
LOOKBACK_DAYS = 7


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def _agent_summary(agent_id: str) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    runs = requests.get(
        f"{SUPABASE_URL}/rest/v1/trading_agent_runs",
        headers=_headers(),
        params={
            "agent_id": f"eq.{agent_id}",
            "select": "id,run_at,account_equity",
            "order": "run_at.asc",
        },
        timeout=30,
    ).json()

    equities = [r["account_equity"] for r in runs if r.get("account_equity") is not None]
    start, end = (equities[0], equities[-1]) if equities else (None, None)
    pct = ((end - start) / start * 100) if start and end else None

    recent_ids = [r["id"] for r in runs if r["run_at"] >= since]
    trades = 0
    if recent_ids:
        decisions = requests.get(
            f"{SUPABASE_URL}/rest/v1/trading_agent_decisions",
            headers=_headers(),
            params={
                "run_id": f"in.({','.join(map(str, recent_ids))})",
                "action": "in.(buy,sell)",
                "order_id": "not.is.null",
                "select": "id",
            },
            timeout=30,
        ).json()
        trades = len(decisions)

    return {"return_pct": pct, "trades_this_week": trades}


def _format_line(label: str, s: dict) -> str:
    if s["return_pct"] is None:
        return f"{label}: no data yet"
    sign = "+" if s["return_pct"] >= 0 else ""
    return f"{label} {sign}{s['return_pct']:.2f}% all-time ({s['trades_this_week']} trades this week)"


def main():
    summaries = {a: _agent_summary(a) for a in AGENTS}
    body = " | ".join(_format_line(a.capitalize(), s) for a, s in summaries.items())

    requests.post(
        NOTIFY_URL,
        headers={"Content-Type": "application/json", "x-notify-key": NOTIFY_SECRET},
        json={"title": "Weekly agent digest", "body": body, "agent_id": "digest"},
        timeout=10,
    ).raise_for_status()
    print(f"Digest sent: {body}")


if __name__ == "__main__":
    main()
