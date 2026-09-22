# Questrade Integration Plan

Goal: evolve the paper-trading bot into a **real-money equities bot on Questrade**,
starting with a **practice (demo) account**. Real money comes only after the practice
path is proven safe.

## Current state (crypto paper bot)
- Data: Binance via `ccxt` (`data/fetcher.py`) — **discarded** for the equities path.
- Execution: `execution/paper_broker.py` — simulated fills, keep as the safe default.
- Strategy: `sma_crossover`, `llm_strategy`; risk: ATR trailing stop.
- Orchestration: `bot.py` (backtest / live_paper / status). Strategy + risk + state code
  are broker-agnostic and do **not** change.

## Architecture (unchanged shape, new adapters)
- `data/questrade_auth.py` — OAuth: seed refresh token → access token + api_server, with
  **refresh-token rotation** persisted safely. (this phase)
- `data/questrade_fetcher.py` — equity candles/quotes behind the same shape as `fetcher.py`.
- `execution/questrade_broker.py` — real orders behind the same interface as `PaperBroker`.
- `bot.py` — flag to select paper vs questrade execution.

## Equities constraints that break crypto assumptions
- **Market hours** — no 24/7 loop; gate on ~09:30–16:00 ET, weekdays, minus holidays.
- **Integer shares** — Questrade has no fractional shares; position sizing must round down.
- **Settlement** — cash-account proceeds settle T+2; can't instantly redeploy.
- **Order reality** — market vs limit, slippage, partial fills, rejects. Paper broker fills
  instantly at close; real fills differ.

## Auth model (VERIFY against the live practice portal)
Assumed standard Questrade flow — confirm before trusting:
- `GET {token_url}?grant_type=refresh_token&refresh_token=<seed>` →
  `{access_token, token_type, expires_in (~1800s), refresh_token (NEW), api_server}`.
- **Refresh token is one-time-use and rotates every call** — persist the new one *before*
  doing anything else, or you lock yourself out and must regenerate manually.
- All API calls hit `api_server + "v1/..."` with `Authorization: Bearer <access_token>`.
- Practice account returns a practice `api_server`; no code difference expected.

## Phases
1. **Auth** (this phase) — `questrade_auth.py` + read-only balances smoke test. No orders.
2. **Data adapter** — pull equity candles; run backtest on a real ticker.
3. **Shadow mode** — live loop on Questrade data, log would-be orders, still route to PaperBroker.
4. **Real execution** — `QuestradeBroker`, integer shares, market-hours gate, hard caps. 1 share, 1 cheap ticker.
5. **Safety/reconciliation** — kill-switch, max daily loss, reconcile positions from Questrade as source of truth, idempotent orders.

## Deployment (later)
- Separate repo (this one). Own `deploy-bot.yml` copied from personal-website's SSH+compose pattern.
- Own secrets; own host/user (do NOT co-locate broker creds with the public web app).
- Add a test-gate / manual approval — no fire-and-forget auto-deploy for a real-money bot.

## Secrets
- Refresh token stored in gitignored `.questrade_token.json` (practice phase).
- Migrate to OS keychain / secret store before real money.
