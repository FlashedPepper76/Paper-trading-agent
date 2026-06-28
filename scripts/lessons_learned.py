"""
Weekly job: reviews one agent's last 7 days of runs/decisions, asks Gemini
to draft 1-3 concrete "lessons learned" bullets, and appends them under the
"## Lessons learned / standing notes" heading in that agent's
Supabase-stored instructions (agent_instructions.content). ai_agent.py
reads instructions from Supabase first (see _load_instructions), so this
takes effect on the agent's very next scheduled run — no redeploy needed.

Run once per agent via the AGENT_ID env var — see
.github/workflows/agent-review.yml, which matrices over both plutus and
helios.

Env vars required: AGENT_ID, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests

AGENT_ID = os.environ["AGENT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

LESSONS_HEADING = "## Lessons learned / standing notes"
LOOKBACK_DAYS = 7
MAX_BULLETS_KEPT = 15


def _headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _fetch_recent_activity():
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    runs = requests.get(
        f"{SUPABASE_URL}/rest/v1/trading_agent_runs",
        headers=_headers(),
        params={
            "agent_id": f"eq.{AGENT_ID}",
            "run_at": f"gte.{since}",
            "select": "id,run_at,account_equity,num_open_positions,overall_reasoning,error",
            "order": "run_at.asc",
        },
        timeout=30,
    ).json()

    run_ids = [r["id"] for r in runs]
    decisions = []
    if run_ids:
        decisions = requests.get(
            f"{SUPABASE_URL}/rest/v1/trading_agent_decisions",
            headers=_headers(),
            params={
                "run_id": f"in.({','.join(map(str, run_ids))})",
                "select": "symbol,action,order_status,reasoning,realized_pnl_pct,order_id",
            },
            timeout=30,
        ).json()
    return runs, decisions


def _summarize(runs: list, decisions: list) -> dict:
    equities = [r["account_equity"] for r in runs if r.get("account_equity") is not None]
    errors = [r["error"] for r in runs if r.get("error")]
    acted_on = [d for d in decisions if d.get("action") in ("buy", "sell")]
    executed = [d for d in acted_on if d.get("order_id")]
    losers = [d for d in decisions if d.get("realized_pnl_pct") is not None and d["realized_pnl_pct"] < 0]
    return {
        "runs": len(runs),
        "errors": len(errors),
        "equity_start": equities[0] if equities else None,
        "equity_end": equities[-1] if equities else None,
        "buy_sell_attempts": len(acted_on),
        "executed_trades": len(executed),
        "losing_closes": len(losers),
    }


def _draft_lessons(summary: dict, decisions: list) -> list:
    sample_reasoning = [d["reasoning"] for d in decisions if d.get("reasoning")][:20]

    prompt = f"""You are reviewing one week of an autonomous paper-trading agent's
activity to draft standing notes for its own future self.

Weekly summary: {json.dumps(summary, indent=2)}

Sample of this week's decision reasoning:
{json.dumps(sample_reasoning, indent=2)}

Write 1-3 short, concrete bullet points (no more) capturing any real pattern,
mistake, or lesson worth remembering going forward. Skip generic advice. If
nothing notable happened this week, return an empty list. Respond with ONLY a
JSON array of strings, e.g. ["...", "..."]."""

    resp = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        params={"key": GEMINI_API_KEY},
        headers={"content-type": "application/json"},
        json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 400},
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    bullets = json.loads(text)
    return [b.strip() for b in bullets if isinstance(b, str) and b.strip()]


def _update_instructions(new_bullets: list):
    if not new_bullets:
        print(f"[{AGENT_ID}] No new lessons this week.")
        return

    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/agent_instructions",
        headers=_headers(),
        params={"agent_id": f"eq.{AGENT_ID}", "select": "content"},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        print(f"[{AGENT_ID}] No agent_instructions row found, skipping.")
        return
    content = rows[0]["content"]

    if LESSONS_HEADING not in content:
        print(f"[{AGENT_ID}] Heading '{LESSONS_HEADING}' not found in instructions, skipping.")
        return

    before, after = content.split(LESSONS_HEADING, 1)
    existing_bullets = re.findall(r"^- (.+)$", after, flags=re.MULTILINE)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dated_new = [f"({today}) {b}" for b in new_bullets]
    combined = (existing_bullets + dated_new)[-MAX_BULLETS_KEPT:]

    new_section = "\n\n" + "\n".join(f"- {b}" for b in combined) + "\n"
    new_content = before + LESSONS_HEADING + new_section

    requests.patch(
        f"{SUPABASE_URL}/rest/v1/agent_instructions",
        headers=_headers(),
        params={"agent_id": f"eq.{AGENT_ID}"},
        json={"content": new_content, "updated_at": datetime.now(timezone.utc).isoformat()},
        timeout=30,
    ).raise_for_status()
    print(f"[{AGENT_ID}] Added {len(new_bullets)} lesson(s): {new_bullets}")


def main():
    runs, decisions = _fetch_recent_activity()
    if not runs:
        print(f"[{AGENT_ID}] No runs in the last {LOOKBACK_DAYS} days, skipping.")
        return
    summary = _summarize(runs, decisions)
    bullets = _draft_lessons(summary, decisions)
    _update_instructions(bullets)


if __name__ == "__main__":
    main()
