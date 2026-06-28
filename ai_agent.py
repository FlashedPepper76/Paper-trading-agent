"""
AI-driven trading agent.

Each run:
  1. Gathers account state, open positions, and recent price stats for the
     universe in config.py.
  2. Sends that context + instructions.md to Claude and asks for buy/sell/
     hold decisions with reasoning, as JSON.
  3. Validates every decision against the hard risk caps in config.py —
     the AI's judgment never overrides these; they're enforced in code.
  4. Executes approved orders via alpaca_client.
  5. Logs the full run (reasoning, decisions, outcomes) to Supabase so it
     can be reviewed later.

This module deliberately keeps risk enforcement and logging independent of
whether the AI call succeeds — a bad/failed AI response should never leave
silent gaps in the log, and should never bypass the risk caps.
"""
import json
import os
import re

import requests
from alpaca.trading.enums import OrderSide

import config
import alpaca_client as ac

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]


# --------------------------------------------------------------------------
# Context gathering
# --------------------------------------------------------------------------

def _symbol_stats(symbol: str, bars) -> dict | None:
    if not bars:
        return None
    closes = [b.close for b in bars]
    if len(closes) < 2:
        return None

    last = closes[-1]

    def pct_change(n):
        if len(closes) <= n:
            return None
        prior = closes[-1 - n]
        return round((last - prior) / prior * 100, 2)

    return {
        "last_close": round(last, 2),
        "pct_change_1d": pct_change(1),
        "pct_change_5d": pct_change(5),
        "pct_change_10d": pct_change(10),
        "pct_change_30d": pct_change(min(30, len(closes) - 1)),
    }


def _build_context():
    account = ac.get_account()
    positions = ac.get_open_positions()
    bars_by_symbol = ac.get_recent_bars(config.UNIVERSE, lookback_days=config.LOOKBACK_DAYS + 5)

    held = {}
    for symbol, pos in positions.items():
        held[symbol] = {
            "qty": float(pos.qty),
            "avg_entry_price": round(float(pos.avg_entry_price), 2),
            "current_price": round(float(pos.current_price), 2),
            "unrealized_pl_pct": round(float(pos.unrealized_plpc) * 100, 2),
        }

    watchlist = {}
    for symbol in config.UNIVERSE:
        if symbol in held:
            continue
        stats = _symbol_stats(symbol, bars_by_symbol.get(symbol))
        if stats:
            watchlist[symbol] = stats

    return {
        "account": {
            "equity": round(float(account.equity), 2),
            "cash": round(float(account.cash), 2),
        },
        "held_positions": held,
        "watchlist": watchlist,
    }


# --------------------------------------------------------------------------
# Claude call
# --------------------------------------------------------------------------

def _load_instructions() -> str:
    with open(config.INSTRUCTIONS_FILE, "r") as f:
        return f.read()


def _call_claude(context: dict) -> dict:
    system_prompt = _load_instructions() + f"""

## Hard limits enforced in code (for your awareness — you don't need to do this math)
- Max open positions at once: {config.MAX_OPEN_POSITIONS}
- Max new positions opened per run: {config.MAX_NEW_BUYS_PER_RUN}
- Position size target: {config.POSITION_SIZE_PCT * 100:.0f}% of equity
- Minimum cash buffer: {config.MIN_CASH_BUFFER_PCT * 100:.0f}% of equity

## Output format
Respond with ONLY valid JSON, no markdown fences, no other text, matching
exactly this shape:

{{
  "overall_reasoning": "1-3 sentences on your overall read of the portfolio/market this run",
  "decisions": [
    {{"symbol": "AAPL", "action": "buy|sell|hold", "qty": 1, "confidence": "low|medium|high", "reasoning": "why"}}
  ]
}}

Only include symbols you want to take action on (buy or sell). You don't
need to list every "hold" — omitting a symbol means hold/no action. qty is
only required for buy/sell.
"""

    user_message = (
        "Here is the current account state and market context for this run:\n\n"
        + json.dumps(context, indent=2)
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": config.ANTHROPIC_MODEL,
            "max_tokens": 2000,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(block["text"] for block in data["content"] if block["type"] == "text")
    cleaned = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(cleaned)


# --------------------------------------------------------------------------
# Risk enforcement (hard caps — independent of what the AI decided)
# --------------------------------------------------------------------------

def _enforce_caps(decisions: list, context: dict) -> list:
    """Returns the list of decisions actually allowed to execute, each
    annotated with an 'allowed' bool and a 'cap_note' if rejected/clipped."""
    held = set(context["held_positions"].keys())
    watchlist = context["watchlist"]
    equity = context["account"]["equity"]
    cash = context["account"]["cash"]

    open_count = len(held)
    new_buys = 0
    approved = []

    for d in decisions:
        symbol = d.get("symbol", "").upper()
        action = d.get("action", "").lower()
        d["symbol"] = symbol
        d["action"] = action

        if symbol not in config.UNIVERSE:
            d["allowed"] = False
            d["cap_note"] = "symbol not in approved universe"
        elif action == "sell":
            if symbol not in held:
                d["allowed"] = False
                d["cap_note"] = "no position held to sell"
            else:
                d["allowed"] = True
        elif action == "buy":
            if symbol in held:
                d["allowed"] = False
                d["cap_note"] = "already held, ignoring duplicate buy"
            elif open_count >= config.MAX_OPEN_POSITIONS:
                d["allowed"] = False
                d["cap_note"] = "max open positions reached"
            elif new_buys >= config.MAX_NEW_BUYS_PER_RUN:
                d["allowed"] = False
                d["cap_note"] = "max new buys per run reached"
            else:
                price = watchlist.get(symbol, {}).get("last_close")
                target_notional = equity * config.POSITION_SIZE_PCT
                free_cash = cash * (1 - config.MIN_CASH_BUFFER_PCT)
                if not price or target_notional > free_cash:
                    d["allowed"] = False
                    d["cap_note"] = "insufficient free cash after buffer"
                else:
                    qty = max(1, int(target_notional // price))
                    d["qty"] = qty
                    d["allowed"] = True
                    open_count += 1
                    new_buys += 1
                    cash -= qty * price
        elif action == "hold":
            d["allowed"] = False
            d["cap_note"] = "hold (no action taken)"
        else:
            d["allowed"] = False
            d["cap_note"] = f"unrecognized action '{action}'"

        approved.append(d)

    return approved


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def _execute(decisions: list):
    for d in decisions:
        if not d.get("allowed"):
            d["order_id"] = None
            d["order_status"] = f"skipped: {d.get('cap_note', 'not allowed')}"
            continue
        try:
            if d["action"] == "buy":
                qty = d.get("qty", 1)
                result = ac.submit_qty_order(d["symbol"], qty, OrderSide.BUY)
            else:  # sell
                result = ac.close_position(d["symbol"])
            d["order_id"] = str(result.id) if hasattr(result, "id") else None
            d["order_status"] = str(getattr(result, "status", "submitted"))
        except Exception as e:
            d["order_id"] = None
            d["order_status"] = f"error: {e}"


# --------------------------------------------------------------------------
# Supabase logging
# --------------------------------------------------------------------------

def _supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _log_run(market_open: bool, context: dict | None, overall_reasoning: str, error: str | None) -> int | None:
    payload = {
        "market_open": market_open,
        "account_equity": context["account"]["equity"] if context else None,
        "account_cash": context["account"]["cash"] if context else None,
        "num_open_positions": len(context["held_positions"]) if context else None,
        "overall_reasoning": overall_reasoning,
        "model_used": config.ANTHROPIC_MODEL,
        "error": error,
    }
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/trading_agent_runs",
        headers=_supabase_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0]["id"] if rows else None


def _log_decisions(run_id: int, decisions: list):
    if not decisions:
        return
    rows = [
        {
            "run_id": run_id,
            "symbol": d.get("symbol"),
            "action": d.get("action"),
            "qty": d.get("qty"),
            "confidence": d.get("confidence"),
            "reasoning": d.get("reasoning"),
            "order_id": d.get("order_id"),
            "order_status": d.get("order_status") or d.get("cap_note"),
        }
        for d in decisions
    ]
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/trading_agent_decisions",
        headers=_supabase_headers(),
        json=rows,
        timeout=30,
    )
    resp.raise_for_status()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run():
    context = None
    try:
        context = _build_context()
        ai_response = _call_claude(context)
        overall_reasoning = ai_response.get("overall_reasoning", "")
        decisions = ai_response.get("decisions", [])

        decisions = _enforce_caps(decisions, context)
        _execute(decisions)

        run_id = _log_run(True, context, overall_reasoning, error=None)
        _log_decisions(run_id, decisions)

        print(f"Run complete. Reasoning: {overall_reasoning}")
        for d in decisions:
            print(f"  {d['symbol']}: {d['action']} -> {d.get('order_status')} | {d.get('reasoning', '')}")

    except Exception as e:
        print(f"Agent run failed: {e}")
        _log_run(True, context, overall_reasoning="", error=str(e))
        raise
