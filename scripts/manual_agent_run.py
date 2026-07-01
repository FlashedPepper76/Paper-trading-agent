"""
Force a full agent run regardless of market hours — useful for testing
changes (e.g. the news research step) without waiting for market open.

This calls the exact same ai_agent.run() the scheduled workflow calls, so it
will place real paper orders if the AI decides to buy/sell. That's
intentional: it's meant to be a faithful end-to-end test, not a dry run.

Set RUN_MODE=premarket to test run_premarket_review() instead (the
once-daily pre-open recap-and-maybe-buy) — same real-order behavior.

EXTRA_CONTEXT: optional free-text injected at the top of the AI prompt for
this run only. Set via the workflow dispatch input or the EXTRA_CONTEXT env
var directly. Useful for manual guidance like "focus on semiconductor stocks
today" or "the Fed just cut rates, factor this in".
"""
import os
import ai_agent

if __name__ == "__main__":
    extra_context = os.environ.get("EXTRA_CONTEXT", "").strip()
    mode = os.environ.get("RUN_MODE", "run")

    if extra_context:
        print(f"Extra context injected for this run:\n{extra_context}\n")

    if mode == "premarket":
        ai_agent.run_premarket_review(extra_context=extra_context)
    else:
        ai_agent.run(extra_context=extra_context)
