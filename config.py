import os

# --------------------------------------------------------------------------
# Per-agent definitions
# --------------------------------------------------------------------------
# Three independent agents share this one codebase but always run as separate
# GitHub Actions jobs (each setting AGENT_ID + its own Alpaca/AI secrets for
# that job only — see .github/workflows/). They never share a brokerage
# account, a risk budget, or a Supabase cache row. AGENT_ID picks which
# entry in AGENTS below is active for a given run.

# Plutus: the original agent. Maximize total returns, willing to take real
# risk to do it (paper money only).
PLUTUS_UNIVERSE = [
    # Broad-market / sector ETFs
    "SPY", "QQQ", "DIA", "IWM", "VTI", "VOO", "XLK", "XLF", "XLE", "XLV",
    # Large-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "ORCL", "CRM",
    # Other large-cap / liquid names across sectors
    "JPM", "V", "MA", "UNH", "HD", "PG", "KO", "PEP", "WMT", "DIS",
    "BAC", "XOM", "CVX", "JNJ", "ABBV", "COST", "MCD", "NKE", "ADBE", "NFLX",
]

# Helios: long-horizon, capital-preservation focused. Broad index and bond/
# treasury exposure plus lower-volatility dividend blue chips. Deliberately
# excludes the higher-beta growth/momentum names Plutus trades.
HELIOS_UNIVERSE = [
    # Broad market
    "SPY", "VOO", "VTI", "DIA",
    # Bonds / treasuries (capital preservation ballast)
    "BND", "AGG", "TLT", "SHY",
    # Defensive sectors
    "XLP", "XLV", "XLU",
    # Dividend-paying blue chips
    "JNJ", "PG", "KO", "PEP", "MCD", "WMT", "JPM", "V", "MA", "HD", "UNH", "COST",
]

# Hermes: news/catalyst-driven event trader. Focuses on liquid large-cap stocks
# across sectors that react meaningfully to earnings, regulatory decisions,
# macro data releases, and other discrete news catalysts. Takes short-to-medium
# term positions (1-5 days typically) when fresh events create mis-pricing.
# Uses Gemini with Google Search grounding for news research (Groq-hosted
# Llama-3.3-70b as fallback) — news context is the primary input, price
# history is secondary. Starts at $10k (displayed as $100k equivalent).
HERMES_UNIVERSE = [
    # Mega-cap tech (frequent earnings/product/regulatory news)
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    # Semiconductors (supply chain, export controls, earnings-driven)
    "AMD", "INTC", "AVGO", "QCOM", "MU",
    # Financials (rate decisions, regulatory, earnings)
    "JPM", "GS", "BAC", "MS", "C", "V", "MA",
    # Healthcare / pharma (FDA catalysts, trial results, drug pricing)
    "JNJ", "PFE", "MRNA", "ABBV", "LLY", "BMY",
    # Energy (oil price, geopolitical, supply/demand news)
    "XOM", "CVX", "OXY", "SLB",
    # Retail / consumer (CPI, consumer confidence, earnings)
    "WMT", "COST", "TGT", "HD", "NKE", "MCD",
    # High-beta growth (heavily news-driven price swings)
    "COIN", "PLTR", "SQ", "SHOP", "RBLX",
    # Sector ETFs (macro or sector-wide news plays)
    "SPY", "QQQ", "XLK", "XLF", "XLE", "XLV",
]

# Both Plutus and Helios paper accounts started at this balance.
STARTING_EQUITY = 100_000.0

AGENTS = {
    "plutus": {
        "label": "Plutus",
        "universe": PLUTUS_UNIVERSE,
        # Switched from gemini-2.5-flash: that model's free-tier allocation on
        # this project turned out to be a hard 20 requests/day (confirmed via
        # the actual 429 response body — "limit: 20, model: gemini-2.5-flash"
        # — well below the 100-1500/day figures generic docs/guides cite, and
        # nowhere near enough even at a 15-minute decision cadence). Then
        # moved off gemini-2.5-flash-lite too, once AI Studio's Rate Limit
        # page (confirmed visually, not just from a 429 body) showed it was
        # ALSO capped at 20 RPD on this project — same ceiling, different
        # model name. gemini-3.1-flash-lite carries a 500 RPD / 15 RPM quota
        # on the same project, confirmed via the same Rate Limit page, which
        # comfortably covers even 15-minute polling across market hours.
        "gemini_model": "gemini-3.1-flash-lite",
        "ai_backend": "gemini",
        "instructions_file": "instructions.md",
        # No hard cap on open positions — removed per Carter's request
        # (2026-06-30). Position sizing (position_size_pct_min/_max) and
        # max new buys per run remain the real risk controls; an open-
        # position count limit was redundant with those and just blocked
        # good opportunities once the portfolio happened to be diversified.
        "max_open_positions": None,
        "max_new_buys_per_run": 3,
        # A range, not a fixed number — the model picks where in this band
        # to size each buy based on its own conviction (see size_pct in the
        # decision schema in ai_agent.py). Code only clamps to the range;
        # it doesn't pick the number. Replaces the old single
        # position_size_pct, which forced every buy to the same fraction
        # of equity regardless of how strongly the model felt about it,
        # producing artificially even position sizes across the portfolio.
        "position_size_pct_min": 0.04,
        "position_size_pct_max": 0.18,
        # 0 = no hard cash-floor cap. It can deploy all of its cash if it has
        # a real reason to — the equity-floor goal (stay above
        # STARTING_EQUITY) is the actual guardrail now, weighed by the model
        # itself rather than blocked by code.
        "min_cash_buffer_pct": 0.0,
        "news_refresh_minutes": 30,
        "starting_equity": 100_000.0,
    },
    "helios": {
        "label": "Helios",
        "universe": HELIOS_UNIVERSE,
        "gemini_model": "gemini-3.1-flash-lite",
        "ai_backend": "gemini",
        "instructions_file": "instructions_helios.md",
        "max_open_positions": None,
        "max_new_buys_per_run": 2,
        # Tighter range than Plutus, consistent with capital-preservation —
        # but still a range the model sizes within, not one fixed number.
        "position_size_pct_min": 0.03,
        "position_size_pct_max": 0.10,
        "min_cash_buffer_pct": 0.0,
        "news_refresh_minutes": 180,
        "starting_equity": 100_000.0,
    },
    "hermes": {
        "label": "Hermes",
        "universe": HERMES_UNIVERSE,
        # Uses Gemini on its own isolated Google AI Studio project so its
        # quota is completely separate from Plutus/Helios. google_search
        # grounding gives Hermes genuine live web search on every news
        # research call — critical for the news-catalyst strategy.
        # Groq (GROQ_API_KEY in the workflow) is the fallback if Gemini
        # quota is exhausted.
        "ai_backend": "gemini",
        "gemini_model": "gemini-3.1-flash-lite",
        "instructions_file": "instructions_hermes.md",
        "max_open_positions": None,
        "max_new_buys_per_run": 2,
        # Moderate sizing — bigger than Helios, smaller than Plutus's max.
        # Hermes bets on specific catalysts, so medium conviction = medium size.
        "position_size_pct_min": 0.05,
        "position_size_pct_max": 0.15,
        "min_cash_buffer_pct": 0.0,
        # Fractional/notional orders: Alpaca supports notional market orders
        # (e.g. "buy $500 of NVDA") which fills fractional shares automatically.
        # Enabled for Hermes because its $10k equity makes whole-share rounding
        # waste a meaningful % of each position. Only applies to market buys —
        # limit orders still use integer qty to preserve GTC capability.
        "use_notional": True,
        "news_refresh_minutes": 15,
        # Reddit subreddits to scrape for retail sentiment on each news refresh.
        # Hot posts are prepended to the Gemini news research prompt so the model
        # can factor in what retail traders are actually talking about right now.
        "reddit_subreddits": ["daytrading", "wallstreetbets", "stocks", "StockMarket", "options"],
        # Hermes paper account started at $10k (not $100k like Plutus/Helios).
        # The dashboard displays this as ×10 for apples-to-apples comparison.
        "starting_equity": 10_000.0,
        # Multiplier applied by the dashboard when showing absolute dollar
        # amounts (equity, cash, P/L) — makes $10k look like $100k so the
        # three agents are visually on the same scale. Return percentages
        # are naturally unaffected.
        "display_scale": 10,
    },
}

AGENT_ID = os.environ.get("AGENT_ID", "plutus")


def _load_dynamic_agent(agent_id: str) -> dict:
    """
    Agents created through the dashboard's "Add agent" flow aren't hardcoded
    above — they live in the `agents` Supabase table instead, so adding one
    doesn't require touching this file or redeploying. Only called when
    AGENT_ID isn't one of the hardcoded entries in AGENTS.

    Unlike instructions (which fall back to a local file if Supabase is
    unreachable), there's no local fallback here — a dynamic agent only
    exists in Supabase, so a failed lookup is a real failure, not something
    to silently paper over.
    """
    import requests  # local import: keep this off the hot path for the two hardcoded agents

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        raise ValueError(
            f"Unknown AGENT_ID '{agent_id}' (not one of {list(AGENTS)}), and "
            "SUPABASE_URL/SUPABASE_KEY aren't set to look it up dynamically."
        )

    resp = requests.get(
        f"{supabase_url}/rest/v1/agents",
        headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
        params={"id": f"eq.{agent_id}", "active": "eq.true", "select": "*"},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        raise ValueError(
            f"Unknown AGENT_ID '{agent_id}' — not hardcoded in config.py and no "
            "active row for it in the agents table."
        )
    row = rows[0]
    return {
        "label": row["label"],
        "universe": row["universe"],
        "ai_backend": row.get("ai_backend") or "gemini",
        "gemini_model": row.get("gemini_model") or "gemini-3.1-flash-lite",
        "groq_model": row.get("groq_model") or "llama-3.3-70b-versatile",
        # Dynamic agents have no local .md fallback — _load_instructions()
        # requires the Supabase agent_instructions row to exist for these.
        "instructions_file": None,
        "max_open_positions": None,
        "max_new_buys_per_run": int(row.get("max_new_buys_per_run", 2)),
        "position_size_pct_min": float(row.get("position_size_pct_min", 0.04)),
        "position_size_pct_max": float(row.get("position_size_pct_max", 0.15)),
        "min_cash_buffer_pct": float(row.get("min_cash_buffer_pct", 0.0)),
        "news_refresh_minutes": int(row.get("news_refresh_minutes", 60)),
        "starting_equity": float(row.get("starting_equity", 100_000.0)),
        "display_scale": int(row.get("display_scale", 1)),
    }


if AGENT_ID in AGENTS:
    AGENT = AGENTS[AGENT_ID]
else:
    AGENT = _load_dynamic_agent(AGENT_ID)
    AGENTS[AGENT_ID] = AGENT

# --------------------------------------------------------------------------
# Settings shared by every agent
# --------------------------------------------------------------------------
LOOKBACK_DAYS = 60          # days of price history shown to the agent per symbol (60 needed for SMA-50)

# Push notifications — fired on executed trades and on run failures.
NOTIFY_URL = "https://trading-agent-dashboard-mu.vercel.app/api/notify"

