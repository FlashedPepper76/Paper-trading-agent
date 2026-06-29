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

AGENTS = {
    "plutus": {
        "label": "Plutus",
        "universe": PLUTUS_UNIVERSE,
        # Switched from gemini-2.5-flash: that model's free-tier allocation on
        # this project turned out to be a hard 20 requests/day (confirmed via
        # the actual 429 response body — "limit: 20, model: gemini-2.5-flash"
        # — well below the 100-1500/day figures generic docs/guides cite, and
        # nowhere near enough even at a 15-minute decision cadence). Quota is
        # tracked per-model within a project, so a different model name gets
        # its own separate, untouched bucket — Flash-Lite's free tier is
        # documented as meaningfully higher (RPM and RPD) than Flash's, and
        # the task here (synthesize given price/news context into a
        # buy/sell/hold call) doesn't need Flash's extra reasoning depth.
        "gemini_model": "gemini-2.5-flash-lite",
        "instructions_file": "instructions.md",
        "max_open_positions": 12,
        "max_new_buys_per_run": 3,
        "position_size_pct": 0.12,
        "min_cash_buffer_pct": 0.10,
        "news_refresh_minutes": 30,
    },
    "helios": {
        "label": "Helios",
        "universe": HELIOS_UNIVERSE,
        "gemini_model": "gemini-2.5-flash-lite",
        "instructions_file": "instructions_helios.md",
        "max_open_positions": 10,
        "max_new_buys_per_run": 2,
        "position_size_pct": 0.08,
        "min_cash_buffer_pct": 0.15,
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
