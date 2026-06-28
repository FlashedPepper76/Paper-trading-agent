"""
AI-driven trading agent.

Each run:
  1. Gathers account state, open positions, and recent price stats for the
     universe in config.py.
  2. Sends that context + instructions.md to Gemini and asks for buy/sell/
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
from datetime import datetime, timezone

import requests
from alpaca.trading.enums import OrderSide

import config
import alpaca_client as ac

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_API_KEY_2 = os.environ.get("GEMINI_API_KEY_2", "")
GEMINI_KEYS = [k for k in (GEMINI_API_KEY, GEMINI_API_KEY_2) if k]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
NOTIFY_SECRET = os.environ.get("NOTIFY_SECRET", "")


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
# Gemini call
# --------------------------------------------------------------------------

def _load_instructions() -> str:
    """
    Instructions now live in Supabase (agent_instructions table) so they can
    be edited from the dashboard. Falls back to the local instructions.md
    if the Supabase read fails for any reason, so a bad network blip never
    takes the whole run down.
    """
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/agent_instructions",
            headers=_supabase_headers(),
            params={"id": "eq.1", "select": "content"},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        if rows and rows[0].get("content"):
            return rows[0]["content"]
    except Exception as e:
        print(f"Could not load instructions from Supabase ({e}), falling back to local file.")

    with open(config.INSTRUCTIONS_FILE, "r") as f:
        return f.read()


def _gemini_generate(system_prompt: str, user_message: str, *, tools=None,
                      json_mode=True, max_output_tokens=2000) -> str:
    """
    Shared Gemini call with key failover. Returns the raw text of the first
    candidate's first part — callers decide whether/how to parse it.

    Note: Gemini 2.5 models reject combining a tool (like google_search) with
    responseMimeType="application/json" in the same call (400 INVALID_ARGUMENT,
    "Function calling with a response mime type... is unsupported"). So
    json_mode and tools are mutually exclusive here by construction — the news
    research call uses tools with json_mode=False, the decision call uses
    json_mode=True with no tools, and their outputs are stitched together by
    the caller instead.
    """
    generation_config = {"maxOutputTokens": max_output_tokens}
    if json_mode:
        generation_config["responseMimeType"] = "application/json"

    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": generation_config,
    }
    if tools:
        body["tools"] = tools

    last_error = None
    for i, key in enumerate(GEMINI_KEYS):
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent",
                params={"key": key},
                headers={"content-type": "application/json"},
                json=body,
                timeout=60,
            )
            if resp.status_code == 429 and i < len(GEMINI_KEYS) - 1:
                print(f"Gemini key #{i + 1} hit a rate/quota limit (429), trying next key...")
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except requests.exceptions.RequestException as e:
            last_error = e
            if i < len(GEMINI_KEYS) - 1:
                print(f"Gemini key #{i + 1} failed ({e}), trying next key...")
                continue
            raise
    raise last_error or RuntimeError("No Gemini API keys configured")


# --------------------------------------------------------------------------
# News / politics / society research (Google Search grounding, cached)
# --------------------------------------------------------------------------

def _research_news() -> str:
    """One grounded Gemini call that researches what's currently moving (or
    could move) markets: macro headlines, politics/policy, and broader
    societal trends, plus anything specific to the trading universe."""
    symbols = ", ".join(config.UNIVERSE)
    system_prompt = (
        "You are a markets research assistant. Use Google Search to find what is "
        "actually relevant to trading decisions right now. Cover, briefly:\n"
        "- Major market-wide headlines from the last 24-48 hours (Fed/rate "
        "decisions, inflation or jobs data, other major economic releases)\n"
        "- Political developments with plausible market impact: legislation, "
        "elections, regulatory or policy actions, geopolitical events or "
        "conflicts, trade policy\n"
        "- Broader societal or cultural trends that could shift consumer "
        "behavior, sentiment, or demand in specific sectors\n"
        f"- Recent company-specific news for any of these tickers, if there is "
        f"any: {symbols}\n\n"
        "Stay strictly factual and neutral — describe events and their "
        "plausible market relevance only. Never state your own political "
        "opinion or take a side on a political issue. Write 200-300 words as "
        "short plain-text bullet-style lines (no markdown headers, no asterisks). "
        "If nothing notable turned up in a section, say so in one line."
    )
    text = _gemini_generate(
        system_prompt,
        "Research current market-relevant news, politics, and societal trends now.",
        tools=[{"google_search": {}}],
        json_mode=False,
        max_output_tokens=700,
    )
    return text.strip()


def _get_news_context() -> str | None:
    """
    Returns a cached news/politics/society briefing, refreshing it via
    _research_news() if the cache is missing or older than
    config.NEWS_REFRESH_MINUTES. Cached in Supabase (agent_news_context,
    single row, id=1) so a 1-minute cron doesn't re-search every run.

    Never blocks a trading run: any failure here falls back to the stale
    cache, or None, rather than raising — a bad news search is not a reason
    to skip a whole trade decision.
    """
    cached = None
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/agent_news_context",
            headers=_supabase_headers(),
            params={"id": "eq.1", "select": "content,updated_at"},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        if rows:
            cached = rows[0]
    except Exception as e:
        print(f"Could not load cached news context from Supabase ({e}).")

    if cached and cached.get("updated_at"):
        try:
            updated_at = datetime.fromisoformat(cached["updated_at"].replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - updated_at).total_seconds() / 60
            if age_minutes < config.NEWS_REFRESH_MINUTES:
                return cached.get("content")
        except Exception as e:
            print(f"Could not parse cached news context timestamp ({e}), refreshing.")

    try:
        fresh = _research_news()
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/agent_news_context",
            headers=_supabase_headers(),
            params={"id": "eq.1"},
            json={"content": fresh, "updated_at": datetime.now(timezone.utc).isoformat()},
            timeout=30,
        ).raise_for_status()
        return fresh
    except Exception as e:
        print(f"Could not refresh news context ({e}), falling back to stale cache if any.")
        return cached.get("content") if cached else None


# --------------------------------------------------------------------------
# Decision call
# --------------------------------------------------------------------------

def _call_gemini(context: dict, news_context: str | None) -> dict:
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

    news_block = (
        "\n\n## Current news / politics / society context (researched via web "
        f"search, refreshed at most every {config.NEWS_REFRESH_MINUTES} min — "
        "may be incomplete or slightly stale)\n" + news_context
        if news_context else
        "\n\n## Current news / politics / society context\n"
        "(unavailable this run — reason about price action alone)\n"
    )

    user_message = (
        "Here is the current account state and market context for this run:\n\n"
        + json.dumps(context, indent=2)
        + news_block
    )

    text = _gemini_generate(system_prompt, user_message, json_mode=True, max_output_tokens=3500)
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
                if not price:
                    d["allowed"] = False
                    d["cap_note"] = "no price data available for this symbol this run"
                else:
                    target_notional = equity * config.POSITION_SIZE_PCT
                    free_cash = cash * (1 - config.MIN_CASH_BUFFER_PCT)
                    if target_notional > free_cash:
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


def _notify(title: str, body: str):
    """Pushes a notification to the Plutus dashboard's subscribed devices.
    Best-effort only — a notify failure (no NOTIFY_SECRET, dashboard down,
    no subscribers yet, etc.) must never break or delay a trading run."""
    if not NOTIFY_SECRET:
        return
    try:
        requests.post(
            config.NOTIFY_URL,
            headers={"Content-Type": "application/json", "x-notify-key": NOTIFY_SECRET},
            json={"title": title, "body": body},
            timeout=10,
        )
    except Exception as e:
        print(f"Notify failed (non-fatal): {e}")


def _log_run(market_open: bool, context: dict | None, overall_reasoning: str,
             error: str | None, news_context: str | None = None) -> int | None:
    payload = {
        "market_open": market_open,
        "account_equity": context["account"]["equity"] if context else None,
        "account_cash": context["account"]["cash"] if context else None,
        "num_open_positions": len(context["held_positions"]) if context else None,
        "overall_reasoning": overall_reasoning,
        "model_used": config.GEMINI_MODEL,
        "error": error,
        "news_context": news_context,
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
    news_context = None
    overall_reasoning = ""
    try:
        context = _build_context()
        missing = [
            s for s in config.UNIVERSE
            if s not in context["held_positions"] and s not in context["watchlist"]
        ]
        print(
            f"Context built: {len(context['watchlist'])} watchlist symbols, "
            f"{len(context['held_positions'])} held."
            + (f" No price data this run for: {', '.join(missing)}" if missing else "")
        )
        news_context = _get_news_context()
        ai_response = _call_gemini(context, news_context)
        overall_reasoning = ai_response.get("overall_reasoning", "")
        decisions = ai_response.get("decisions", [])

        decisions = _enforce_caps(decisions, context)
        _execute(decisions)

        run_id = _log_run(True, context, overall_reasoning, error=None, news_context=news_context)
        _log_decisions(run_id, decisions)

        print(f"Run complete. Reasoning: {overall_reasoning}")
        for d in decisions:
            print(f"  {d['symbol']}: {d['action']} -> {d.get('order_status')} | {d.get('reasoning', '')}")

        executed = [d for d in decisions if d.get("order_id")]
        if executed:
            summary = ", ".join(f"{d['action'].upper()} {d.get('qty')} {d['symbol']}" for d in executed)
            _notify("Plutus made a move", summary)

    except Exception as e:
        print(f"Agent run failed: {e}")
        _log_run(True, context, overall_reasoning=overall_reasoning, error=str(e), news_context=news_context)
        _notify("Plutus run failed", str(e)[:200])
        raise
