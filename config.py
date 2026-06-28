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

# --- Risk guardrails (hard limits, enforced in code regardless of what the
#     AI decides — this runs fully autonomously, so these caps matter) ---
MAX_OPEN_POSITIONS = 8         # never hold more than this many positions at once
MAX_NEW_BUYS_PER_RUN = 3       # don't open more than this many new positions per run
POSITION_SIZE_PCT = 0.10       # fraction of account equity per new position
MIN_CASH_BUFFER_PCT = 0.10     # always keep at least this fraction of equity as cash

