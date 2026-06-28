# Paper Trading Agent

A fully autonomous **paper trading** bot for stocks and ETFs only, built on
Alpaca's API and run for free on GitHub Actions. It trades with simulated
money — no real funds are ever at risk, and crypto is never touched by
design (the universe in `config.py` only contains stock/ETF tickers, and
`alpaca_client.py` is pinned to `paper=True`).

## How it works

1. **`.github/workflows/trade.yml`** fires on a cron schedule (every 15 min,
   weekdays, with a buffer window around US market hours) and can also be
   triggered manually from the Actions tab.
2. **`main.py`** checks Alpaca's market clock and exits immediately if the
   market is closed — this is what lets the cron schedule stay simple
   without manually accounting for weekends/holidays/DST.
3. **`strategy.py`** runs a placeholder long-only moving-average crossover
   strategy across the universe in `config.py`, autonomously deciding what
   (if anything) to buy or sell, subject to the risk caps below.
4. **`alpaca_client.py`** is the only file that talks to Alpaca. It's a thin
   wrapper, intentionally kept separate so swapping in a real strategy
   later doesn't require touching the Alpaca integration.

## Current strategy (placeholder — replace anytime)

- Long-only moving-average crossover (10-day vs 30-day).
- Buys on a bullish crossover, sells (closes) on a bearish crossover.
- Universe: a fixed list of liquid large/mid-cap US stocks + popular
  broad-market ETFs (see `config.py`). The bot picks autonomously among
  these each run — nothing is hand-picked per trade.

## Risk guardrails (config.py)

Because this runs unattended, there are hard caps so a single run (or a
buggy signal) can't blow up the paper account:

| Setting | Default | Meaning |
|---|---|---|
| `MAX_OPEN_POSITIONS` | 8 | Never hold more than this many positions at once |
| `MAX_NEW_BUYS_PER_RUN` | 3 | Cap on new positions opened in a single run |
| `POSITION_SIZE_PCT` | 10% | Position size as a fraction of account equity |
| `MIN_CASH_BUFFER_PCT` | 10% | Cash that's always kept in reserve |

## Setup

1. Create (or use an existing) Alpaca account and grab your **paper**
   trading API key ID + secret key from the Alpaca dashboard.
2. In this repo: **Settings → Secrets and variables → Actions → New
   repository secret**, and add:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
3. That's it — the workflow will start running on its schedule. You can
   also trigger a run immediately from the **Actions** tab via
   "Run workflow" (workflow_dispatch).

## Changing the schedule

Edit the `cron` line in `.github/workflows/trade.yml`. Cron is in UTC.

## Replacing the strategy

Everything decision-related lives in `strategy.py` + `config.py`.
`main.py` and `alpaca_client.py` don't need to change.
