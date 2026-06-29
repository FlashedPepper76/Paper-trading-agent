"""
Force a full agent run regardless of market hours — useful for testing
changes (e.g. the news research step) without waiting for market open.

This calls the exact same ai_agent.run() the scheduled workflow calls, so it
will place real paper orders if the AI decides to buy/sell. That's
intentional: it's meant to be a faithful end-to-end test, not a dry run.

Set RUN_MODE=premarket to test run_premarket_review() instead (the
once-daily pre-open recap-and-maybe-buy) — same real-order behavior.
"""
import os

import ai_agent

if __name__ == "__main__":
    if os.environ.get("RUN_MODE") == "premarket":
        ai_agent.run_premarket_review()
    else:
        ai_agent.run()
