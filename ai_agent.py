"""
AI-driven trading agent.

Each run:
  1. Gathers account state, open positions, and recent price stats for the
     universe in config.py.
  2. Sends that context + instructions to the configured AI backend (Gemini
     for Plutus/Helios, xAI Grok for Hermes) and asks for buy/sell/hold
     decisions with reasoning, as JSON.
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
import time as time_module
from datetime import datetime, timedelta, timezone

import requests
from alpaca.trading.enums import OrderSide

import config
import alpaca_client as ac
from schedule import market_phase

# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_KEY_2 = os.environ.get("GEMINI_API_KEY_2", "")
GEMINI_KEYS = [k for k in (GEMINI_API_KEY, GEMINI_API_KEY_2) if k]
GEMINI_KEY_NAMES = ["GEMINI_API_KEY", "GEMINI_API_KEY_2"][: len(GEMINI_KEYS)]

# Groq inference API keys (api.groq.com) — used as fallback for Gemini-backed
# agents AND as the primary backend for Hermes (ai_backend == "groq").
# Two keys provide failover when one hits a rate limit.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_KEY_2 = os.environ.get("GROQ_API_KEY_2", "")
GROQ_KEYS = [k for k in (GROQ_API_KEY, GROQ_API_KEY_2) if k]
GROQ_KEY_NAMES = ["GROQ_API_KEY", "GROQ_API_KEY_2"][: len(GROQ_KEYS)]

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
NOTIFY_SECRET = os.environ.get("NOTIFY_SECRET", "")
# Set by every workflow as RUN_TRIGGER: ${{ github.event_name }} — "schedule"
# for the cron jobs, "workflow_dispatch" for a manual/forced run. Lets the
# dashboard show plainly whether a given run happened on its own or because
# someone (Carter, or Claude during a chat) explicitly triggered it.
RUN_TRIGGER = os.environ.get("RUN_TRIGGER", "unknown")

_KEY_PARAM_RE = re.compile(r"([?&]key=)[^&\s]+")


def _safe_str(e: Exception) -> str:
    """
    str(exception) for a failed Gemini request includes the full request URL
    — which includes the API key as a `?key=...` query param. GitHub Actions
    auto-masks secrets in its own log viewer, but that masking doesn't apply
    to data this code separately ships off to Supabase (and from there, the
    public dashboard). Always scrub before logging/storing any exception
    text so a key can't end up sitting in plaintext somewhere masking can't
    reach.
    """
    return _KEY_PARAM_RE.sub(r"\1***", str(e))


# --------------------------------------------------------------------------
# Daily schedule phases
# --------------------------------------------------------------------------

def _yesterday_recap() -> str:
    """
    Plain-text summary of this agent's own buy/sell decisions over roughly
    the last trading day, for the pre-market review prompt. Holds aren't
    included — there can be dozens of them and "nothing changed" isn't
    useful recap material; the run-level overall_reasoning history already
    covers that if anyone wants to read it later.
    """
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/trading_agent_decisions",
            headers=_supabase_headers(),
            params={
                "agent_id": f"eq.{config.AGENT_ID}",
                "action": "in.(buy,sell)",
                "created_at": f"gte.{since}",
                "select": "symbol,action,qty,order_status,reasoning,created_at",
                "order": "created_at.asc",
            },
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return "No buy or sell decisions were made in the prior trading day — it was a full hold."
        lines = [
            f"- {r['action'].upper()} {r.get('qty') or ''} {r['symbol']} "
            f"({r.get('order_status', 'unknown')}): {r.get('reasoning', '')}"
            for r in rows
        ]
        return "Buy/sell decisions from the prior trading day:\n" + "\n".join(lines)
    except Exception as e:
        print(f"Could not build yesterday's recap ({e}); proceeding without it.")
        return "(Could not load yesterday's activity — proceed using current portfolio state only.)"


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
    bars_by_symbol = ac.get_recent_bars(config.AGENT["universe"], lookback_days=config.LOOKBACK_DAYS + 5)

    held = {}
    for symbol, pos in positions.items():
        held[symbol] = {
            "qty": float(pos.qty),
            "avg_entry_price": round(float(pos.avg_entry_price), 2),
            "current_price": round(float(pos.current_price), 2),
            "unrealized_pl_pct": round(float(pos.unrealized_plpc) * 100, 2),
            "market_value": round(float(pos.market_value), 2),
        }

    watchlist = {}
    for symbol in config.AGENT["universe"]:
        if symbol in held:
            continue
        stats = _symbol_stats(symbol, bars_by_symbol.get(symbol))
        if stats:
            watchlist[symbol] = stats

    cash = round(float(account.cash), 2)
    equity = round(float(account.equity), 2)
    minutes_since_buy = _minutes_since_last_buy()
    starting_equity = config.AGENT.get("starting_equity", config.STARTING_EQUITY)

    return {
        "account": {
            "equity": equity,
            "cash": cash,
            "cash_pct_of_equity": round(cash / equity * 100, 1) if equity else None,
            "equity_vs_starting_balance": round(equity - starting_equity, 2),
        },
        "held_positions": held,
        "watchlist": watchlist,
        "open_positions_count": len(held),
        "max_open_positions": config.AGENT["max_open_positions"],
        "minutes_since_last_buy": round(minutes_since_buy, 1) if minutes_since_buy is not None else None,
    }


# --------------------------------------------------------------------------
# Groq inference API (primary for Hermes, fallback for Plutus/Helios)
# --------------------------------------------------------------------------

def _groq_generate(system_prompt: str, user_message: str, *,
                    json_mode=True, max_output_tokens=2000) -> str:
    """
    Call Groq's inference API (api.groq.com) with key failover between
    GROQ_API_KEY and GROQ_API_KEY_2.

    Serves two roles:
    - Primary backend for Hermes (ai_backend == "groq"), using
      llama-3.3-70b-versatile as the decision model.
    - Fallback for Gemini-backed agents (Plutus/Helios) when all Gemini
      keys are exhausted or Gemini is returning errors.

    Groq's OpenAI-compatible API means no code changes between the two
    roles — same function, same endpoint, same response shape.
    """
    # Use the agent's configured Groq model when running as Hermes (primary
    # backend). Fall back to llama-3.3-70b-versatile for the Gemini-fallback
    # case where Plutus/Helios hit quota — that path doesn't set a groq_model.
    model = config.AGENT.get("groq_model", "llama-3.3-70b-versatile")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_output_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    last_error = None
    n_keys = len(GROQ_KEYS)
    for i, key in enumerate(GROQ_KEYS):
        key_name = GROQ_KEY_NAMES[i]
        try:
            print(f"Calling {key_name}")
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=60,
            )
            if resp.status_code == 429 and i < n_keys - 1:
                print(f"Groq key #{i + 1} hit a rate/quota limit (429), trying next key...")
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as e:
            last_error = e
            if i < n_keys - 1:
                print(f"Groq key #{i + 1} failed ({e}), trying next key...")
    raise last_error or RuntimeError("No Groq API keys configured")


# --------------------------------------------------------------------------
# Gemini API (primary for Plutus/Helios)
# --------------------------------------------------------------------------

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
    n_keys = len(GEMINI_KEYS)
    for i, key in enumerate(GEMINI_KEYS):
        key_name = GEMINI_KEY_NAMES[i]
        is_last_key = i == n_keys - 1
        rate_limit_attempts = 3 if is_last_key else 1
        for attempt in range(rate_limit_attempts):
            try:
                print(f"Calling {key_name} (attempt {attempt + 1})" if attempt > 0 else f"Calling {key_name}")
                resp = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{config.AGENT['gemini_model']}:generateContent",
                    params={"key": key},
                    headers={"content-type": "application/json"},
                    json=body,
                    timeout=60,
                )
                if resp.status_code == 429:
                    safe_body = _KEY_PARAM_RE.sub(r"\1***", resp.text)[:500]
                    print(f"Gemini 429 response body: {safe_body}")
                    if not is_last_key:
                        print(f"Gemini key #{i + 1} hit a rate/quota limit (429), trying next key...")
                        break
                    quota_exhausted = "RESOURCE_EXHAUSTED" in resp.text and "free_tier_requests" in resp.text
                    if quota_exhausted:
                        print(f"Gemini key #{i + 1}: daily free-tier quota exhausted — not retrying, won't clear within seconds.")
                    elif attempt < rate_limit_attempts - 1:
                        wait = 8 * (attempt + 1)
                        print(
                            f"Gemini key #{i + 1} hit a rate/quota limit (429), no fallback key "
                            f"left — waiting {wait}s and retrying ({attempt + 1}/{rate_limit_attempts - 1})..."
                        )
                        time_module.sleep(wait)
                        continue
                resp.raise_for_status()
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except requests.exceptions.RequestException as e:
                last_error = e
                if not is_last_key:
                    print(f"Gemini key #{i + 1} failed ({e}), trying next key...")
                    break
                if tools is None and GROQ_KEYS:
                    print(f"All Gemini keys failed ({e}), falling back to Groq...")
                    return _groq_generate(
                        system_prompt, user_message,
                        json_mode=json_mode, max_output_tokens=max_output_tokens,
                    )
                raise
    if tools is None and GROQ_KEYS:
        print(f"All Gemini keys failed ({last_error}), falling back to Groq...")
        return _groq_generate(
            system_prompt, user_message,
            json_mode=json_mode, max_output_tokens=max_output_tokens,
        )
    raise last_error or RuntimeError("No Gemini API keys configured")


# --------------------------------------------------------------------------
# News / market research (cached per agent)
# --------------------------------------------------------------------------

def _fetch_reddit_posts() -> str:
    """
    Pulls hot posts from stock/trading subreddits via Reddit's public JSON
    API (no auth required for public subs). Returns a formatted block of the
    most-upvoted, non-stickied posts to be prepended to the news research
    prompt as real-time retail sentiment context.

    Best-effort — a Reddit outage or rate-limit should never block a run.
    """
    subreddits = config.AGENT.get("reddit_subreddits", [])
    if not subreddits:
        return ""

    headers = {"User-Agent": "trading-agent-hermes/1.0 (paper trading research bot)"}
    lines = []
    for sub in subreddits:
        try:
            resp = requests.get(
                f"https://www.reddit.com/r/{sub}/hot.json",
                params={"limit": 8},
                headers=headers,
                timeout=8,
            )
            if not resp.ok:
                print(f"Reddit r/{sub}: HTTP {resp.status_code}, skipping.")
                continue
            posts = resp.json().get("data", {}).get("children", [])
            for post in posts:
                p = post.get("data", {})
                if p.get("stickied"):   # skip mod announcements
                    continue
                title = p.get("title", "").strip()
                score = p.get("score", 0)
                n_comments = p.get("num_comments", 0)
                if title:
                    lines.append(f"r/{sub} | ↑{score} | {n_comments} comments | {title}")
        except Exception as e:
            print(f"Reddit r/{sub} fetch failed ({e}), skipping.")

    if not lines:
        return ""
    return (
        "## Reddit retail sentiment (hot posts right now)\n"
        + "\n".join(lines)
        + "\n"
    )


def _research_news_gemini() -> str:
    """News research via Gemini with google_search grounding (all Gemini-backed agents).
    For agents with reddit_subreddits configured (Hermes), Reddit hot posts are
    prepended so Gemini synthesizes retail sentiment alongside live web news."""
    reddit_block = _fetch_reddit_posts()

    symbols = ", ".join(config.AGENT["universe"])
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

    user_message = "Research current market-relevant news, politics, and societal trends now."
    if reddit_block:
        user_message = (
            "Here is real-time retail sentiment from Reddit trading communities "
            "to factor into your research:\n\n"
            + reddit_block
            + "\nNow research current market-relevant news, politics, and societal trends."
        )

    text = _gemini_generate(
        system_prompt,
        user_message,
        tools=[{"google_search": {}}],
        json_mode=False,
        max_output_tokens=800,
    )
    return text.strip()


def _research_news_groq() -> str:
    """
    News research via Groq (Hermes). Groq doesn't offer live web search,
    so this is a pure text call asking the model to surface relevant market
    context from its training data. The price-history data in the decision
    prompt already provides the quantitative picture; this adds qualitative
    sector/company context the model knows about.
    """
    symbols = ", ".join(config.AGENT["universe"])
    system_prompt = (
        "You are a markets research assistant. Based on your training knowledge, "
        "summarize what is broadly relevant to trading decisions for a portfolio "
        "focused on these tickers: " + symbols + ".\n\n"
        "Cover briefly:\n"
        "- Key macro themes currently affecting markets (rates, inflation, growth)\n"
        "- Sector-level tailwinds or headwinds relevant to the tickers above\n"
        "- Any well-known catalysts or risk events (earnings seasons, regulatory "
        "cycles, product launches) for companies in this universe\n"
        "- General sentiment context for high-beta growth vs. defensive names\n\n"
        "Be concise — 150-200 words, plain-text bullet-style lines. "
        "Note that your knowledge has a training cutoff, so flag if something "
        "may have evolved since then."
    )
    text = _groq_generate(
        system_prompt,
        "Provide a brief market context briefing for the tickers in my universe.",
        json_mode=False,
        max_output_tokens=500,
    )
    return text.strip()


def _research_news() -> str:
    """Dispatch news research to the right backend based on agent config."""
    if config.AGENT.get("ai_backend") == "groq":
        return _research_news_groq()
    return _research_news_gemini()


def _get_news_context() -> str | None:
    """
    Returns a cached news/politics/society briefing, refreshing it via
    _research_news() if the cache is missing or older than this agent's
    news_refresh_minutes setting. Cached in Supabase (agent_news_context,
    one row per agent_id) so a fast cron doesn't re-search every run, and so
    each agent's refresh cadence doesn't collide with the other's.

    Never blocks a trading run: any failure here falls back to the stale
    cache, or None, rather than raising — a bad news search is not a reason
    to skip a whole trade decision.
    """
    cached = None
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/agent_news_context",
            headers=_supabase_headers(),
            params={"agent_id": f"eq.{config.AGENT_ID}", "select": "content,updated_at"},
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
            if age_minutes < config.AGENT["news_refresh_minutes"]:
                return cached.get("content")
        except Exception as e:
            print(f"Could not parse cached news context timestamp ({e}), refreshing.")

    try:
        fresh = _research_news()
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/agent_news_context",
            headers=_supabase_headers(),
            params={"agent_id": f"eq.{config.AGENT_ID}"},
            json={"content": fresh, "updated_at": datetime.now(timezone.utc).isoformat()},
            timeout=30,
        ).raise_for_status()
        return fresh
    except Exception as e:
        print(f"Could not refresh news context ({e}), falling back to stale cache if any.")
        return cached.get("content") if cached else None


# --------------------------------------------------------------------------
# Decision call (dispatches to Grok or Gemini based on agent config)
# --------------------------------------------------------------------------

def _load_instructions() -> str:
    """
    Instructions live in Supabase (agent_instructions table, keyed by
    agent_id) so they can be edited from the dashboard per-agent. Falls
    back to the active agent's local instructions file if the Supabase read
    fails for any reason, so a bad network blip never takes the run down —
    except for dynamically-added agents (instructions_file is None for
    those), which have no local file to fall back to and so require the
    Supabase read to succeed.
    """
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/agent_instructions",
            headers=_supabase_headers(),
            params={"agent_id": f"eq.{config.AGENT_ID}", "select": "content"},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        if rows and rows[0].get("content"):
            return rows[0]["content"]
    except Exception as e:
        print(f"Could not load instructions from Supabase ({e}), falling back to local file.")

    instructions_file = config.AGENT.get("instructions_file")
    if not instructions_file:
        raise RuntimeError(
            f"No instructions available for agent '{config.AGENT_ID}': the Supabase "
            "agent_instructions read failed or returned nothing, and this agent has "
            "no local instructions file to fall back to (dynamically-added agents "
            "depend on the Supabase row existing)."
        )
    with open(instructions_file, "r") as f:
        return f.read()


def _build_decision_prompt(context: dict, news_context: str | None,
                            extra_framing: str = "") -> tuple[str, str]:
    """Builds (system_prompt, user_message) for the decision call."""
    starting_equity = config.AGENT.get("starting_equity", config.STARTING_EQUITY)
    minutes_since_buy = context.get("minutes_since_last_buy")
    if minutes_since_buy is None:
        pacing_line = "No prior buy on record — this would be the first."
    elif minutes_since_buy < 60:
        pacing_line = f"{minutes_since_buy:.0f} minutes since your last buy."
    else:
        pacing_line = f"{minutes_since_buy / 60:.1f} hours since your last buy."

    equity_delta = context["account"]["equity_vs_starting_balance"]
    if equity_delta >= 0:
        equity_line = f"${equity_delta:,.2f} ABOVE the ${starting_equity:,.0f} starting balance."
    else:
        equity_line = f"${abs(equity_delta):,.2f} BELOW the ${starting_equity:,.0f} starting balance."

    system_prompt = _load_instructions() + extra_framing + f"""

## Your current portfolio status (weigh this yourself — nothing here is a rule, it's information)
- Equity is currently {equity_line} Try to stay above the starting balance
  as much as possible — that's a real goal to weigh in your own risk-taking
  and sizing decisions, not a hard rule the code enforces.
- Cash: {context['account']['cash_pct_of_equity']}% of equity. There is no
  hard cash-floor cap — you can deploy all of it if you have a real reason
  to, but spending it down to (or near) zero is itself a risk to the equity
  goal above, not a neutral act.
- Open positions: {context['open_positions_count']}{f" of {context['max_open_positions']} max" if context['max_open_positions'] is not None else " (no hard cap on count)"}
- {pacing_line}
None of the above blocks you — the code will stop you from exceeding the
hard limits below, but everything in this section is judgment, not
enforcement.

## Hard limits enforced in code (for your awareness — you don't need to do this math)
- {f"Max open positions at once: {config.AGENT['max_open_positions']}" if config.AGENT['max_open_positions'] is not None else "No cap on number of open positions — sizing and pacing are the controls"}
- Max new positions opened per run: {config.AGENT['max_new_buys_per_run']}
- Position sizing: {config.AGENT['position_size_pct_min'] * 100:.0f}%–{config.AGENT['position_size_pct_max'] * 100:.0f}%
  of equity per buy. This is a range, not a fixed amount — size each buy
  yourself within it based on how strongly you believe in it. A high-
  conviction idea can sit near the top of the range; something you're
  taking a smaller swing on can sit near the bottom. The code only clamps
  you to the range if you go outside it — it does not pick the number for
  you, and there's no requirement that positions end up similar sizes.

## Output format
Respond with ONLY valid JSON, no markdown fences, no other text, matching
exactly this shape:

{{
  "overall_reasoning": "1-3 sentences on your overall read of the portfolio/market this run",
  "decisions": [
    {{"symbol": "AAPL", "action": "buy|sell|hold", "order_type": "market|limit", "limit_price": 0.00, "size_pct": 0.10, "confidence": "low|medium|high", "reasoning": "why"}}
  ]
}}

Only include symbols you want to take action on (buy or sell). Omitting a
symbol means hold/no action. Field notes:

- **order_type**: "market" (default, fills immediately) or "limit" (GTC order,
  fills when price reaches limit_price). Omit to default to "market".
- **limit_price**: Required when order_type is "limit". For a limit buy, set
  this below the current price (buy the dip). For a limit sell, set this above
  the current price (take-profit target). The order stays live until it fills
  or is cancelled by a future run.
- **size_pct**: Fraction of equity per buy (see range above). Ignored for sells.
- Sells always close the full position. A limit sell closes the full position
  at the target price instead of immediately at market.
"""

    news_block = (
        "\n\n## Current news / market catalyst context (refreshed at most every "
        f"{config.AGENT['news_refresh_minutes']} min via live web search)\n" + news_context
        if news_context else
        "\n\n## Current news / market catalyst context\n"
        "(unavailable this run — reason about price action alone)\n"
    )

    user_message = (
        "Here is the current account state and market context for this run:\n\n"
        + json.dumps(context, indent=2)
        + news_block
    )
    return system_prompt, user_message


def _call_model(context: dict, news_context: str | None, extra_framing: str = "") -> dict:
    """
    Routes the decision call to either xAI Grok (Hermes) or Gemini
    (Plutus/Helios) based on the active agent's ai_backend config key.
    Both paths build the same prompt and return the same dict shape so the
    rest of run() doesn't need to know which backend was used.
    """
    system_prompt, user_message = _build_decision_prompt(context, news_context, extra_framing)
    backend = config.AGENT.get("ai_backend", "gemini")

    last_exc = None
    for attempt in range(2):
        prompt = user_message
        if attempt > 0:
            prompt += (
                "\n\n## Your previous response was invalid JSON\n"
                f"Parse error: {last_exc}\n"
                "Keep every string value on a single line with no literal line "
                "breaks, make sure any quotes inside text are properly escaped, "
                "and keep 'reasoning' to one short sentence per symbol."
            )
        if backend == "groq":
            text = _groq_generate(system_prompt, prompt, json_mode=True, max_output_tokens=3500)
        else:
            text = _gemini_generate(system_prompt, prompt, json_mode=True, max_output_tokens=3500)

        cleaned = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            last_exc = e
            print(f"AI JSON parse failed on attempt {attempt + 1}/2 ({e}).")
    raise last_exc


# --------------------------------------------------------------------------
# Risk enforcement (hard caps — independent of what the AI decided)
# --------------------------------------------------------------------------

def _minutes_since_last_buy() -> float | None:
    """
    Minutes since this agent's most recent *executed* buy, or None if it has
    never bought anything. Surfaced to the model as information (see
    _build_decision_prompt's "portfolio status" section) so it can weigh its
    own pacing — this used to be a hard code-level cooldown that blocked any
    buy outright regardless of the AI's reasoning, but that meant the model
    couldn't actually reason about it at all (it was never even told the
    cooldown existed). Removed in favor of giving it the real numbers and
    trusting its judgment, same as the rest of the portfolio context.
    """
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/trading_agent_decisions",
            headers=_supabase_headers(),
            params={
                "agent_id": f"eq.{config.AGENT_ID}",
                "action": "eq.buy",
                "order_id": "not.is.null",
                "select": "created_at",
                "order": "created_at.desc",
                "limit": "1",
            },
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        last_at = datetime.fromisoformat(rows[0]["created_at"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last_at).total_seconds() / 60
    except Exception as e:
        print(f"Could not check last-buy cooldown ({e}); not enforcing it this run.")
        return None


_FILL_PRICE_BUFFER = 1.02  # budget 2% above last_close for actual market fill price


def _enforce_caps(decisions: list, context: dict, pending_buy_info: dict) -> list:
    """Returns the list of decisions actually allowed to execute, each
    annotated with an 'allowed' bool and a 'cap_note' if rejected/clipped."""
    held = set(context["held_positions"].keys())
    watchlist = context["watchlist"]
    equity = context["account"]["equity"]
    cash = context["account"]["cash"]
    pending_buy_symbols = set(pending_buy_info.keys())

    pending_committed = 0.0
    for sym, qty in pending_buy_info.items():
        if sym in context["held_positions"]:
            p = context["held_positions"][sym]["current_price"]
        else:
            p = watchlist.get(sym, {}).get("last_close", 0.0)
        pending_committed += qty * p

    # Hard cash floor: running available cash after pending orders.
    # This is the strict ceiling we never let buys push through.
    available_cash = max(0.0, cash - pending_committed)

    open_count = len(held)
    new_buys = 0
    new_buy_symbols_this_run = set()
    approved = []

    for d in decisions:
        symbol = d.get("symbol", "").upper()
        action = d.get("action", "").lower()
        order_type = d.get("order_type", "market").lower()
        limit_price = d.get("limit_price")
        d["symbol"] = symbol
        d["action"] = action
        d["order_type"] = order_type

        if symbol not in config.AGENT["universe"]:
            d["allowed"] = False
            d["cap_note"] = "symbol not in approved universe"
        elif action == "sell":
            if symbol not in held:
                d["allowed"] = False
                d["cap_note"] = "no position held to sell"
            else:
                pos = context["held_positions"][symbol]
                d["allowed"] = True
                d["qty"] = int(pos["qty"])  # needed for limit sell execution
                d["exit_price"] = pos["current_price"]
                d["realized_pnl_pct"] = pos["unrealized_pl_pct"]
                if order_type == "limit":
                    if not isinstance(limit_price, (int, float)) or limit_price <= 0:
                        d["allowed"] = False
                        d["cap_note"] = "limit sell requires a valid limit_price"
                    elif limit_price <= pos["current_price"]:
                        # Warn but still allow — agent might intend a stop-loss
                        print(
                            f"Note: limit sell for {symbol} at {limit_price} is at or "
                            f"below current price {pos['current_price']} — will fill immediately or act as a floor."
                        )
        elif action == "buy":
            if available_cash <= 0:
                d["allowed"] = False
                d["cap_note"] = "no available cash remaining after pending orders"
            elif symbol in held:
                d["allowed"] = False
                d["cap_note"] = "already held, ignoring duplicate buy"
            elif symbol in pending_buy_symbols:
                d["allowed"] = False
                d["cap_note"] = "a buy order for this symbol is already pending/unfilled, ignoring duplicate"
            elif symbol in new_buy_symbols_this_run:
                d["allowed"] = False
                d["cap_note"] = "duplicate buy for this symbol already approved this run, ignoring"
            elif config.AGENT["max_open_positions"] is not None and open_count >= config.AGENT["max_open_positions"]:
                d["allowed"] = False
                d["cap_note"] = "max open positions reached"
            elif new_buys >= config.AGENT["max_new_buys_per_run"]:
                d["allowed"] = False
                d["cap_note"] = "max new buys per run reached"
            else:
                price = (limit_price if order_type == "limit" and isinstance(limit_price, (int, float)) and limit_price > 0
                         else watchlist.get(symbol, {}).get("last_close"))
                if not price:
                    d["allowed"] = False
                    d["cap_note"] = "no price data available for this symbol this run"
                elif order_type == "limit" and (not isinstance(limit_price, (int, float)) or limit_price <= 0):
                    d["allowed"] = False
                    d["cap_note"] = "limit buy requires a valid limit_price"
                else:
                    min_pct = config.AGENT["position_size_pct_min"]
                    max_pct = config.AGENT["position_size_pct_max"]
                    requested_pct = d.get("size_pct")
                    if isinstance(requested_pct, (int, float)) and requested_pct > 0:
                        size_pct = max(min_pct, min(max_pct, requested_pct))
                    else:
                        size_pct = (min_pct + max_pct) / 2
                    d["size_pct_used"] = round(size_pct, 4)
                    target_notional = equity * size_pct

                    use_notional = config.AGENT.get("use_notional", False) and order_type == "market"

                    if use_notional:
                        # Notional (fractional) buy: send Alpaca a dollar amount
                        # and let it fill the exact fractional qty. No integer
                        # rounding or fill-price buffer — Alpaca handles it.
                        notional_to_spend = round(min(target_notional, available_cash), 2)
                        if notional_to_spend < 1.0:
                            d["allowed"] = False
                            d["cap_note"] = "insufficient cash for $1 minimum notional order"
                        else:
                            d["notional_order"] = notional_to_spend
                            d["entry_price"] = round(price, 2)  # estimated, for logging
                            d["allowed"] = True
                            open_count += 1
                            new_buys += 1
                            new_buy_symbols_this_run.add(symbol)
                            available_cash -= notional_to_spend
                    else:
                        # Integer-qty buy (Plutus, Helios, Hermes limit buys)
                        fill_buffer = 1.0 if order_type == "limit" else _FILL_PRICE_BUFFER
                        notional = min(target_notional, available_cash)
                        qty = int(notional // (price * fill_buffer))

                        if qty < 1:
                            d["allowed"] = False
                            d["cap_note"] = "insufficient cash for even 1 share at the sized notional"
                        else:
                            committed = qty * price * fill_buffer
                            if committed > available_cash:
                                qty = int(available_cash // (price * fill_buffer))
                            if qty < 1:
                                d["allowed"] = False
                                d["cap_note"] = "insufficient cash for even 1 share (hard cash floor enforced)"
                            else:
                                d["qty"] = qty
                                d["entry_price"] = price
                                d["allowed"] = True
                                open_count += 1
                                new_buys += 1
                                new_buy_symbols_this_run.add(symbol)
                                available_cash -= qty * price * fill_buffer
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
            order_type = d.get("order_type", "market").lower()
            limit_price = d.get("limit_price")
            notional_order = d.get("notional_order")
            if d["action"] == "buy":
                qty = d.get("qty", 1)
                if notional_order:
                    # Fractional/notional market buy (Hermes)
                    result = ac.submit_market_order(d["symbol"], notional_order, OrderSide.BUY)
                elif order_type == "limit" and limit_price:
                    result = ac.submit_limit_order(d["symbol"], qty, OrderSide.BUY, limit_price)
                else:
                    result = ac.submit_qty_order(d["symbol"], qty, OrderSide.BUY)
            else:  # sell
                qty = d.get("qty", 0)
                if order_type == "limit" and limit_price and qty:
                    result = ac.submit_limit_order(d["symbol"], qty, OrderSide.SELL, limit_price)
                else:
                    result = ac.close_position(d["symbol"])
            d["order_id"] = str(result.id) if hasattr(result, "id") else None
            d["order_status"] = str(getattr(result, "status", "submitted"))
        except Exception as e:
            d["order_id"] = None
            d["order_status"] = f"error: {_safe_str(e)}"


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
    """Pushes a notification to the dashboard's subscribed devices.
    Best-effort only — a notify failure (no NOTIFY_SECRET, dashboard down,
    no subscribers yet, etc.) must never break or delay a trading run."""
    if not NOTIFY_SECRET:
        return
    try:
        requests.post(
            config.NOTIFY_URL,
            headers={"Content-Type": "application/json", "x-notify-key": NOTIFY_SECRET},
            json={"title": f"{config.AGENT['label']}: {title}", "body": body, "agent_id": config.AGENT_ID},
            timeout=10,
        )
    except Exception as e:
        print(f"Notify failed (non-fatal): {e}")


def _active_model_name() -> str:
    """Returns a human-readable model name for logging."""
    backend = config.AGENT.get("ai_backend", "gemini")
    if backend == "groq":
        return config.AGENT.get("groq_model", "llama-3.3-70b-versatile")
    return config.AGENT.get("gemini_model", "unknown")


def _log_run(market_open: bool, context: dict | None, overall_reasoning: str,
             error: str | None, news_context: str | None = None) -> int | None:
    payload = {
        "agent_id": config.AGENT_ID,
        "trigger": RUN_TRIGGER,
        "market_open": market_open,
        "account_equity": context["account"]["equity"] if context else None,
        "account_cash": context["account"]["cash"] if context else None,
        "num_open_positions": len(context["held_positions"]) if context else None,
        "overall_reasoning": overall_reasoning,
        "model_used": _active_model_name(),
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
            "agent_id": config.AGENT_ID,
            "symbol": d.get("symbol"),
            "action": d.get("action"),
            "qty": d.get("qty"),
            "entry_price": d.get("entry_price"),
            "exit_price": d.get("exit_price"),
            "realized_pnl_pct": d.get("realized_pnl_pct"),
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


def _refresh_account_state() -> dict:
    """
    Re-fetches account + position state fresh from Alpaca. Called after this
    run's own orders have been submitted, so what gets logged reflects what
    actually happened this run instead of the pre-trade snapshot that was
    only meant to feed the decision prompt above.
    """
    account = ac.get_account()
    positions = ac.get_open_positions()
    held = {}
    for symbol, pos in positions.items():
        held[symbol] = {
            "qty": float(pos.qty),
            "avg_entry_price": round(float(pos.avg_entry_price), 2),
            "current_price": round(float(pos.current_price), 2),
            "unrealized_pl_pct": round(float(pos.unrealized_plpc) * 100, 2),
            "market_value": round(float(pos.market_value), 2),
        }
    return {
        "account": {
            "equity": round(float(account.equity), 2),
            "cash": round(float(account.cash), 2),
        },
        "held_positions": held,
    }


def log_idle(market_open: bool):
    """
    Lightweight check-in for cron ticks that don't run the full agent (market
    closed). Deliberately skips the AI entirely so it doesn't burn API quota
    every minute outside trading hours — just records a current account
    snapshot and a plain "checked in, no action" note.
    """
    try:
        state = _refresh_account_state()
        equity = state["account"]["equity"]
        cash = state["account"]["cash"]
        num_positions = len(state["held_positions"])
    except Exception as e:
        print(f"Could not fetch account snapshot for idle check-in ({e}).")
        equity = cash = num_positions = None

    payload = {
        "agent_id": config.AGENT_ID,
        "trigger": RUN_TRIGGER,
        "market_open": market_open,
        "account_equity": equity,
        "account_cash": cash,
        "num_open_positions": num_positions,
        "overall_reasoning": "Market is closed — checked in, no action taken.",
        "model_used": None,
        "error": None,
        "news_context": None,
    }
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/trading_agent_runs",
            headers=_supabase_headers(),
            json=payload,
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f"Could not log idle check-in ({e}) — non-fatal.")


def _log_positions(context: dict):
    """
    Replaces this agent's stored 'current positions' snapshot.
    Upsert-then-prune rather than delete-then-insert to avoid race windows
    with snapshot.py which writes to the same table every minute.
    """
    try:
        held = context["held_positions"] if context else {}
        if held:
            rows = [
                {
                    "agent_id": config.AGENT_ID,
                    "symbol": symbol,
                    "qty": pos["qty"],
                    "avg_entry_price": pos["avg_entry_price"],
                    "current_price": pos["current_price"],
                    "unrealized_pl_pct": pos["unrealized_pl_pct"],
                    "market_value": pos["market_value"],
                }
                for symbol, pos in held.items()
            ]
            requests.post(
                f"{SUPABASE_URL}/rest/v1/trading_agent_positions",
                headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "agent_id,symbol"},
                json=rows,
                timeout=30,
            ).raise_for_status()

        held_list = ",".join(held.keys()) if held else ""
        symbol_filter = f"not.in.({held_list})" if held_list else "not.is.null"
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/trading_agent_positions",
            headers=_supabase_headers(),
            params={"agent_id": f"eq.{config.AGENT_ID}", "symbol": symbol_filter},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f"Could not update positions snapshot ({e}) — non-fatal.")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run(extra_context: str = ""):
    context = None
    news_context = None
    overall_reasoning = ""
    market_open = True
    try:
        market_open = ac.is_market_open()
        context = _build_context()
        missing = [
            s for s in config.AGENT["universe"]
            if s not in context["held_positions"] and s not in context["watchlist"]
        ]
        print(
            f"Context built: {len(context['watchlist'])} watchlist symbols, "
            f"{len(context['held_positions'])} held."
            + (f" No price data this run for: {', '.join(missing)}" if missing else "")
        )
        if not market_open:
            print(
                "Note: market is actually closed right now (this is a force-run via "
                "manual_agent_run.py, which intentionally bypasses the market-hours "
                "gate). Any orders submitted will sit unfilled until the next open."
            )
        news_context = _get_news_context()

        # Extra framing injected when the run was triggered manually with a prompt
        manual_framing = (
            f"\n\n## Manual context from Carter (injected for this run only)\n{extra_context}\n"
            if extra_context else ""
        )

        ai_response = _call_model(context, news_context, extra_framing=manual_framing)
        overall_reasoning = ai_response.get("overall_reasoning", "")
        decisions = ai_response.get("decisions", [])

        decisions = _enforce_caps(decisions, context, ac.get_pending_buy_info())
        _execute(decisions)

        try:
            post_context = _refresh_account_state()
        except Exception as e:
            print(f"Could not refresh post-trade account state ({e}); logging pre-trade snapshot instead.")
            post_context = context
        _log_positions(post_context)

        run_id = _log_run(market_open, post_context, overall_reasoning, error=None, news_context=news_context)
        _log_decisions(run_id, decisions)

        print(f"Run complete. Reasoning: {overall_reasoning}")
        for d in decisions:
            print(f"  {d['symbol']}: {d['action']} -> {d.get('order_status')} | {d.get('reasoning', '')}")

        executed = [d for d in decisions if d.get("order_id")]
        if executed:
            summary = ", ".join(f"{d['action'].upper()} {d.get('qty')} {d['symbol']}" for d in executed)
            _notify("made a move", summary)

    except Exception as e:
        safe_msg = _safe_str(e)
        print(f"Agent run failed: {safe_msg}")
        _log_run(market_open, context, overall_reasoning=overall_reasoning, error=safe_msg, news_context=news_context)
        raise


_PREMARKET_FRAMING = """

## This is your once-daily pre-market review
Markets are still closed — they open in about an hour. This is a dedicated
moment to look back at the prior trading day (decisions and outcomes below)
and your current equity/cash/positions, and decide whether there's a
genuine reason to queue a buy ahead of the open. You do not have to buy —
if nothing here changes your thesis on anything, holding is the right
answer, exactly like any other run. A buy placed now queues as a normal
day order and fills at or shortly after the open; it does not fill
immediately.
"""


def run_premarket_review(extra_context: str = ""):
    """
    Once per trading day, ~1 hour before open (see market_phase()): reviews
    the prior trading day's buy/sell activity plus current equity/cash/
    positions, and gets one chance to queue a buy ahead of the open.
    """
    context = None
    news_context = None
    overall_reasoning = ""
    try:
        context = _build_context()
        recap = _yesterday_recap()
        print(f"Pre-market review. Yesterday recap: {recap}")

        news_context = _get_news_context()

        manual_framing = (
            f"\n\n## Manual context from Carter (injected for this run only)\n{extra_context}\n"
            if extra_context else ""
        )

        ai_response = _call_model(
            context, news_context,
            extra_framing=_PREMARKET_FRAMING + "\n" + recap + manual_framing,
        )
        overall_reasoning = ai_response.get("overall_reasoning", "")
        decisions = ai_response.get("decisions", [])

        decisions = _enforce_caps(decisions, context, ac.get_pending_buy_info())
        _execute(decisions)

        try:
            post_context = _refresh_account_state()
        except Exception as e:
            print(f"Could not refresh post-trade account state ({e}); logging pre-trade snapshot instead.")
            post_context = context
        _log_positions(post_context)

        run_id = _log_run(market_open=False, context=post_context, overall_reasoning=overall_reasoning,
                           error=None, news_context=news_context)
        _log_decisions(run_id, decisions)

        print(f"Pre-market review complete. Reasoning: {overall_reasoning}")
        for d in decisions:
            print(f"  {d['symbol']}: {d['action']} -> {d.get('order_status')} | {d.get('reasoning', '')}")

        executed = [d for d in decisions if d.get("order_id")]
        if executed:
            summary = ", ".join(f"{d['action'].upper()} {d.get('qty')} {d['symbol']}" for d in executed)
            _notify("queued a pre-market move", f"{summary} (will fill at/after the open)")

    except Exception as e:
        safe_msg = _safe_str(e)
        print(f"Pre-market review failed: {safe_msg}")
        _log_run(market_open=False, context=context, overall_reasoning=overall_reasoning,
                 error=safe_msg, news_context=news_context)
        raise
