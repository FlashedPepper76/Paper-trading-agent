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
        "gemini_model": row.get("gemini_model") or "gemini-3.1-flash-lite",
        # Dynamic agents have no local .md fallback — _load_instructions()
        # requires the Supabase agent_instructions row to exist for these.
        "instructions_file": None,
        "max_open_positions": None,
        "max_new_buys_per_run": int(row.get("max_new_buys_per_run", 2)),
        "position_size_pct_min": float(row.get("position_size_pct_min", 0.04)),
        "position_size_pct_max": float(row.get("position_size_pct_max", 0.15)),
        "min_cash_buffer_pct": float(row.get("min_cash_buffer_pct", 0.0)),
        "news_refresh_minutes": int(row.get("news_refresh_minutes", 60)),
    }


if AGENT_ID in AGENTS:
    AGENT = AGENTS[AGENT_ID]
else:
    AGENT = _load_dynamic_agent(AGENT_ID)
    AGENTS[AGENT_ID] = AGENT

# --------------------------------------------------------------------------
# Settings shared by every agent
# --------------------------------------------------------------------------
LOOKBACK_DAYS = 30          # days of price history shown to the agent per symbol

# Push notifications — fired on executed trades and on run failures.
NOTIFY_URL = "https://trading-agent-dashboard-mu.vercel.app/api/notify"
