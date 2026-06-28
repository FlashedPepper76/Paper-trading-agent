# Universe: liquid large/mid-cap US equities + popular broad-market ETFs.
# The agent autonomously decides which of these (if any) to buy or sell on
# each run — nothing here is a standing instruction to trade a specific
# name. No crypto symbols are ever included, by design.
UNIVERSE = [
    # Broad-market / sector ETFs
    "SPY", "QQQ", "DIA", "IWM", "VTI", "VOO", "XLK", "XLF", "XLE", "XLV",
    # Large-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "ORCL", "CRM",
    # Other large-cap / liquid names across sectors
    "JPM", "V", "MA", "UNH", "HD", "PG", "KO", "PEP", "WMT", "DIS",
    "BAC", "XOM", "CVX", "JNJ", "ABBV", "COST", "MCD", "NKE", "ADBE", "NFLX",
]

# --- AI agent settings ---
GEMINI_MODEL = "gemini-2.5-flash"   # free-tier model, plenty for 1 call every 15 min
LOOKBACK_DAYS = 30          # days of price history shown to the agent per symbol
INSTRUCTIONS_FILE = "instructions.md"

# How long a researched news/politics/society briefing is reused before the
# agent re-searches (cached in Supabase). The trade loop can run as often as
# once a minute; re-searching every run would be wasteful and slow, and
# market-moving news doesn't change minute to minute anyway.
NEWS_REFRESH_MINUTES = 20

# Push notifications (Plutus dashboard) — fired on executed trades and on
# run failures. The dashboard's /api/notify checks NOTIFY_SECRET (a GitHub
# Actions secret here, hardcoded on the dashboard side since we don't have
# Vercel env var access — see that repo's lib/push-server.ts for the caveat).
NOTIFY_URL = "https://trading-agent-dashboard-mu.vercel.app/api/notify"

# --- Risk guardrails (hard limits, enforced in code regardless of what the
#     AI decides — this runs fully autonomously, so these caps matter) ---
# Raised across the board (was 8 / 3 / 10% / 10%) to back up the
# maximize-returns goal — paper money only, so more aggressive sizing is an
# acceptable trade-off here. The cash-buffer check in ai_agent.py still
# blocks any buy that would actually overcommit capital, so these are
# ceilings, not guarantees every run hits them.
MAX_OPEN_POSITIONS = 12        # never hold more than this many positions at once
MAX_NEW_BUYS_PER_RUN = 5       # don't open more than this many new positions per run
POSITION_SIZE_PCT = 0.15       # fraction of account equity per new position
MIN_CASH_BUFFER_PCT = 0.05     # always keep at least this fraction of equity as cash

