# Paper Trading Agent

An autonomous **AI-driven paper trading agent** for stocks and ETFs only,
powered by Gemini and run for free on GitHub Actions. It trades with
simulated money — no real funds are ever at risk, and crypto is never
touched by design.

## How it works

1. **`.github/workflows/trade.yml`** (and the Helios/Hermes equivalents) are
   triggered by a Supabase pg_cron job and can also be triggered manually
   from the Actions tab. The intended decision cadence is every 15 minutes
   during market hours; `main.py` self-throttles to that cadence in code
   (`ai_agent.should_throttle_cadence()`) regardless of how often the
   external trigger actually fires, so a faster or misconfigured trigger
   can't cause more-frequent-than-intended trading or burn through API quota.
2. **`main.py`** checks Alpaca's market clock and exits immediately if the
   market is closed.
3. **`ai_agent.py`** gathers account state, open positions, and recent
   price stats for the universe in `config.py`, and cancels any open orders
   that never resolved (see `alpaca_client.cancel_stale_open_orders`) so
   they stop tying up cash indefinitely. It also researches current news,
   politics, and societal trends via a Gemini call with Google Search
   grounding, cached in Supabase and refreshed at most every
   `NEWS_REFRESH_MINUTES` (so a fast cron doesn't re-search every run). Both
   feed into a separate Gemini call (alongside `instructions.md`) that
   returns buy/sell/hold decisions with reasoning.
4. **Hard risk caps in `config.py` are enforced in code** — the AI's
   judgment never overrides them. Max new buys per run and position sizing
   are checked before any order is placed (see the table below for current
   per-agent values — some caps, like max open positions and the cash
   floor, have deliberately been removed in favor of the AI weighing that
   context itself; see `instructions.md`'s "Lessons learned" section).
5. **Every run is logged to Supabase** (`trading_agent_runs` and
   `trading_agent_decisions` tables in the life-dashboard project) — that's
   your window into what the agent decided and why.
6. **`alpaca_client.py`** is the only file that talks to Alpaca, pinned to
   `paper=True` so it can never place a live order.
7. **Executed trades and run failures push a notification** to the Plutus
   dashboard (trading-agent-dashboard repo) — enable alerts from the bell in
   its nav once it's added to your home screen.

## Steering the agent

Edit **`instructions.md`** — that's the agent's system prompt and trading
philosophy. Tell the assistant what you want changed (risk tolerance,
things to avoid/prioritize, lessons learned) and it'll update and push the
file. The agent reads it fresh on every run, so changes take effect
immediately on the next scheduled run.

## Reviewing what it's done

Query the Supabase tables directly, or build a view into them from your
Command Deck dashboard:

- `trading_agent_runs` — one row per run: account state, overall reasoning,
  the news/politics/society briefing in play that run, whether the market
  was open, any errors.
- `trading_agent_decisions` — one row per symbol the agent acted on: the
  action, quantity, confidence, full reasoning text, and the resulting
  order ID/status (including why something was skipped if a risk cap
  blocked it).
- `agent_news_context` — single cached row holding the most recent news/
  politics/society briefing and when it was last researched.

## Setup

Repo secrets required (**Settings → Secrets and variables → Actions**):

- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — your Alpaca **paper** trading keys
- `GEMINI_API_KEY` — from Google AI Studio (aistudio.google.com). Free tier
  covers this comfortably: the decision call runs every minute during market
  hours, and the news-research call (with Google Search grounding) only runs
  once every `NEWS_REFRESH_MINUTES` thanks to the Supabase cache, not every
  run. One caveat: on the free tier, Google's terms allow your
  prompts/responses to be used to improve their models. That's the
  trade-off for $0 — fine for paper-trading test data, just worth knowing.
- `SUPABASE_URL` / `SUPABASE_KEY` — already set, pointing at the
  life-dashboard project

You can trigger a run immediately from the **Actions** tab via
"Run workflow" (workflow_dispatch), without waiting for the schedule.

## Manual one-off orders

`.github/workflows/manual-order.yml` lets you fire a single buy/sell from
the Actions tab outside the AI agent's normal decision loop — useful for
testing. Results get written to `last_order_result.json` in the repo.

## Risk guardrails (config.py)

Per-agent, set in the `AGENTS` dict in `config.py`. `max_open_positions` and
the cash floor are intentionally `None`/`0` for every agent right now — see
`instructions.md`'s "Lessons learned" section for why (position sizing and
the AI's own equity-floor judgment are the real guardrails there instead).

| Setting | Plutus | Helios | Hermes | Meaning |
|---|---|---|---|---|
| `max_open_positions` | none | none | none | Cap on simultaneous open positions |
| `max_new_buys_per_run` | 3 | 2 | 2 | Cap on new positions opened in a single run |
| `position_size_pct_min`–`max` | 4%–18% | 3%–10% | 5%–15% | Position size range as a fraction of equity; the AI picks within it, code clamps if it doesn't |
| `min_cash_buffer_pct` | 0% | 0% | 0% | Cash always kept in reserve |
