"""
Which part of the trading day it is right now, by fixed ET wall-clock
boundaries. Deliberately has zero dependency on Gemini/Supabase config so
both ai_agent.py (the AI decision cycle) and snapshot.py (the Gemini-free
Alpaca-only balance puller) can share it without snapshot.py needing to
import anything that requires a GEMINI_API_KEY to even load.

9:30am-4pm market hours are always 9:30am-4pm ET regardless of the time of
year — converting through zoneinfo handles the EST/EDT switch automatically
rather than needing two cron schedules or a manual seasonal adjustment.
"""
from datetime import datetime, time as time_of_day, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def market_phase(now_utc: datetime | None = None) -> str:
    """
    Returns one of:
      - "pre_market_review": 8:30-8:45am ET — the once-daily pre-open
        review window. A 15-minute slot, not "anytime before open," so a
        15-minute-grid cron produces exactly one tick here per day.
      - "active_window": 8:45am-5pm ET — the regular intraday cadence,
        starting right when the pre-market review window closes, covering
        the pre-open gap, market hours, and a 1-hour post-close buffer.
      - "quiet": everything else — true overnight pause, nothing should
        run at all during this phase.

    This only looks at wall-clock time, not whether today is actually a
    trading day — callers still need Alpaca's real clock/calendar (weekends
    and holidays look identical to a quiet Tuesday night from here).
    """
    now_et = (now_utc or datetime.now(timezone.utc)).astimezone(ET)
    t = now_et.time()
    if time_of_day(8, 30) <= t < time_of_day(8, 45):
        return "pre_market_review"
    if time_of_day(8, 45) <= t < time_of_day(17, 0):
        return "active_window"
    return "quiet"
