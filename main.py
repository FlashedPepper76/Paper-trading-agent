"""
Entry point. Run on a schedule by .github/workflows/trade.yml.

Stocks & ETFs only, Alpaca PAPER trading only (no real money, ever — see
alpaca_client.py). Checks Alpaca's market clock first and exits
immediately if the market is closed, which is what lets the GitHub Action
run on a fixed cron schedule without manual weekend/holiday handling.

Trading decisions are made by ai_agent.py (Claude), with hard risk caps
enforced in code regardless of what the AI decides — see config.py.
"""
import alpaca_client as ac
import ai_agent


def main():
    if not ac.is_market_open():
        print("Market is closed. Exiting.")
        return

    print("Market is open — running AI agent.")
    ai_agent.run()
    print("Run complete.")


if __name__ == "__main__":
    main()
