import os

# --------------------------------------------------------------------------
# Per-agent definitions
# --------------------------------------------------------------------------
# Two independent agents share this one codebase but always run as separate
# GitHub Actions jobs (each setting AGENT_ID + its own Alpaca/Gemini secrets
# for that job only — see .github/workflows/). They never share a brokerage
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

# Both paper accounts started at this balance — the new equity-floor goal
# ("try to stay above this") is anchored to it, surfaced to the model as
# information rather than enforced as a hard code-level cap, same reasoning
# as removing the buy cooldown: a number to weigh, not a rule to route
# around.
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
    },
    "helios": {
        "label": "Helios",
        "universe": HELIOS_UNIVERSE,
        "gemini_model": "gemini-3.1-flash-lite",
        "instructions_file": "instructions_helios.md",
        "max_open_positions": None,
        "max_new_buys_per_run": 2,
        # Tighter range than Plutus, consistent with capital-preservation —
        # but still a range the model sizes within, not one fixed number.
        "position_size_pct_min": 0.03,
        "position_size_pct_max": 0.10,
        "min_cash_buffer_pct": 0.0,
        "news_refresh_minutes": 180,
    },
}

AGENT_ID = os.environ.get("AGENT_ID", "plutus")
if AGENT_ID not in AGENTS:
    raise ValueError(f"Unknown AGENT_ID '{AGENT_ID}' — expected one of {list(AGENTS)}")
AGENT = AGENTS[AGENT_ID]

# --------------------------------------------------------------------------
# Settings shared by every agent
# --------------------------------------------------------------------------
LOOKBACK_DAYS = 30          # days of price history shown to the agent per symbol

# Push notifications — fired on executed trades and on run failures.
NOTIFY_URL = "https://trading-agent-dashboard-mu.vercel.app/api/notify"
