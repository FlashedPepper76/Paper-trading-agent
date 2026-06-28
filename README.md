# Paper Trading Agent

An autonomous **AI-driven paper trading agent** for stocks and ETFs only,
powered by Gemini and run for free on GitHub Actions. It trades with
simulated money — no real funds are ever at risk, and crypto is never
touched by design.

## How it works

1. **`.github/workflows/trade.yml`** fires every 15 min on weekdays (with a
   buffer window around US market hours) and can also be triggered
   manually from the Actions tab.
2. **`main.py`** checks Alpaca's market clock and exits immediately if the
   market is closed.
3. **`ai_agent.py`** gathers account state, open positions, and recent
   price stats for the universe in `config.py`, sends that context plus
   `instructions.md` to Gemini, and asks for buy/sell/hold decisions with
   reasoning.
4. **Hard risk caps in `config.py` are enforced in code** — the AI's
   judgment never overrides them. Max positions, max new buys per run,
   position sizing, and a cash buffer are all checked before any order is
   placed.
5. **Every run is logged to Supabase** (`trading_agent_runs` and
   `trading_agent_decisions` tables in the life-dashboard project) — that's
   your window into what the agent decided and why.
6. **`alpaca_client.py`** is the only file that talks to Alpaca, pinned to
   `paper=True` so it can never place a live order.

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
  whether the market was open, any errors.
- `trading_agent_decisions` — one row per symbol the agent acted on: the
  action, quantity, confidence, full reasoning text, and the resulting
  order ID/status (including why something was skipped if a risk cap
  blocked it).

## Setup

Repo secrets required (**Settings → Secrets and variables → Actions**):

- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — your Alpaca **paper** trading keys
- `GEMINI_API_KEY` — from Google AI Studio (aistudio.google.com). Free tier
  is plenty for this run frequency (~26 calls/day, well under free-tier
  rate limits). One caveat: on the free tier, Google's terms allow your
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

| Setting | Default | Meaning |
|---|---|---|
| `MAX_OPEN_POSITIONS` | 8 | Never hold more than this many positions at once |
| `MAX_NEW_BUYS_PER_RUN` | 3 | Cap on new positions opened in a single run |
| `POSITION_SIZE_PCT` | 10% | Position size as a fraction of account equity |
| `MIN_CASH_BUFFER_PCT` | 10% | Cash that's always kept in reserve |
