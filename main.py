"""
Entry point. Run on a schedule by .github/workflows/trade.yml (and the
analogous trade_helios.yml).

Stocks & ETFs only, Alpaca PAPER trading only (no real money, ever — see
alpaca_client.py). The day is split into three phases (see
ai_agent.market_phase(), boundaries fixed in ET so they're correct across
the EST/EDT switch with no manual adjustment):

  - "quiet" (roughly 5pm-8:30am ET): does nothing at all. No Gemini call,
    no Alpaca call, no Supabase write — a deliberate overnight pause, not
    just "checked and found nothing to do."
  - "pre_market_review" (8:30-8:45am ET, once a day): reviews yesterday's
    activity and current portfolio, gets one chance to queue a buy ahead
    of the open. Guarded by a real trading-day check (minutes_until_next_
    open) so a holiday/weekend morning that happens to look like 8:30am ET
    doesn't trigger a real review for a session that isn't actually
    starting soon.
  - "active_window" (9:15am-5pm ET): the regular intraday cadence — still
    double-checks Alpaca's real market-open status before spending a
    Gemini call, same as before.
"""
import alpaca_client as ac
import ai_agent
import config


def main():
    print(f"Agent: {config.AGENT['label']} ({config.AGENT_ID})")

    phase = ai_agent.market_phase()
    print(f"Phase: {phase}")

    if phase == "quiet":
        print("Overnight pause — doing nothing this tick.")
        return

    if phase == "pre_market_review":
        minutes_to_open = ac.minutes_until_next_open()
        if minutes_to_open > 120:
            print(f"Looks like 8:30am ET, but the next real session is {minutes_to_open:.0f} min away "
                  "(holiday/weekend) — skipping the pre-market review.")
            return
        print("Running the once-daily pre-market review.")
        ai_agent.run_premarket_review()
        return

    # phase == "active_window"
    if not ac.is_market_open():
        print("Market is closed (holiday/weekend, or just outside actual session hours). Logging a check-in.")
        ai_agent.log_idle(market_open=False)
        return

    print("Market is open — running AI agent.")
    ai_agent.run()
    print("Run complete.")


if __name__ == "__main__":
    main()
