# DesiSamosayQuant

Autonomous Roostoo competition bot for 1× long and short spot exposure. It uses a four-hour rebalance schedule and a price-only trend strategy, with a persistent audit trail for decisions and orders.

## Strategy

The bot ranks configured Roostoo USD pairs by a risk-adjusted blend of 24-hour and 72-hour momentum. It buys assets with positive momentum above their 24-hour EMA and opens shorts on assets with negative momentum below that EMA. BTC's 72-hour trend shifts the budget toward longs in a risk-on regime or shorts in a risk-off regime. Combined long plus short gross exposure is capped at 75% of NAV, with at most 25% per asset; unused capital stays in USD. During cold start, total exposure is capped at 25%.

Live Roostoo ticker responses do not provide OHLCV history or volume. By default, free public Binance hourly closes seed the initial price history; after startup, all new signal observations come from Roostoo. Set `BOOTSTRAP_BINANCE=false` to disable that source. If seeding is unavailable, the bot can start conservatively from Roostoo's 24-hour `Change` field, limits exposure to 25%, and switches to the full multi-horizon model after it collects 73 hourly prices.

The bot uses spot market orders and Roostoo's `/v6/short_open`, `/v6/short_close`, and `/v6/short_positions` endpoints. Short collateral is sized 1:1 with position notional; there is no leverage. It does not make markets, arbitrage, or submit rapid orders. It checks the account once per hour and rebalances at most once every four hours. Longs and shorts are reduced or closed before opposite-side exposure is opened. The bot accounts for the competition's 0.1% taker fee, including short open/close fees. Exchange order precision and minimums are read from `exchangeInfo`.

Risk controls include a 75% maximum combined gross exposure, 25% maximum per asset, a 4% daily loss gate, and a persistent 8% peak-to-trough drawdown halt that closes longs and shorts toward cash. These are configurable in `.env`.

## Setup

Python 3.10 or later is recommended.

```bash
python -m venv .venv
```

Activate the environment, then install dependencies and create a private config:

```bash
pip install -r requirements.txt
cp .env.example .env
```

Fill `ROOSTOO_API_KEY` and `ROOSTOO_API_SECRET` in `.env`. `.env` is ignored by Git; `.env.example` is safe to commit. Never put credentials in source files, shell scripts, logs, or commits.

## Run

```bash
# Read-only API and credential check
python main.py --check

# Read-only account status
python main.py --status

# Run one collection/decision cycle. With DRY_RUN=false, this can submit market orders.
python main.py --once

# Run continuously
python main.py

# Force a no-order cycle regardless of .env
python main.py --once --dry-run
```

`DRY_RUN=false` is the `.env.example` default so that adding valid credentials is sufficient to trade on the Roostoo mock exchange. Use `DRY_RUN=true` until you are ready to submit competition orders. `--check` and `--status` never place orders.

If a market-order POST times out, its status is uncertain. The client deliberately does not retry it automatically; check the account and audit log before taking action.

## Runtime records

The ignored `runtime/` directory is created at first run:

- `state.json` stores hourly prices and risk state across restarts.
- `portfolio.csv` records NAV, cash, combined gross exposure, and daily/peak drawdown.
- `logs/cycles.jsonl` records every decision cycle and its targets.
- `logs/orders.jsonl` records order requests and exchange responses.
- `logs/bot.log` contains operational logs.

Keep these records for the competition's trade-integrity review. They are local runtime evidence and are not committed automatically.

## AWS EC2

On the competition-provided EC2 instance, clone this repository, install Python and the requirements, create `.env` locally, then run the continuous command under `systemd` or another process supervisor. Keep `.env` permissions restricted and never include it in a public repository. Confirm `python main.py --check` succeeds before starting the live loop.

## Competition context

The current APAC event page specifies autonomous trading, open-source code review, traceable strategy commits, no HFT/market making/arbitrage, and spot trading with 1× long/short allowed. It also requires at least 8 active trading days with enough strategy trades each day. The bot rebalances at most every four hours and never forces trades to inflate activity; that means the strategy and live market conditions must still produce enough actual fills. Use `python audit_competition.py` to review successful live order actions by UTC day. The organizers' public page does not define the numeric meaning of “enough trades,” so confirm that threshold with them.

The event page lists the live period as Oct 4–17, 2026, and the open-source repository submission deadline as before Oct 14. Keep each strategy change in a descriptive Git commit and run the bot autonomously; do not manually call trading endpoints on the competition account. `--check` and `--status` are read-only. Do not publish `.env` or runtime credentials.

The event page states 0.1% taker and 0.05% maker fees; this bot uses market orders and the 0.1% taker assumption. Roostoo's short API also documents 0.1% fees on opening and closing short positions. It uses Binance public hourly candles only to seed initial price history; the event page permits external data sources. Recheck event updates and actual account permissions before deployment.

## References

- [APAC Quant Trading Hackathon event page](https://luma.com/coghwiyt)
- [Roostoo API documentation](https://github.com/roostoo/Roostoo-API-Documents)
- [Quant-trading-hackathon-V2 example](https://github.com/Akshay-TP/Quant-trading-hackathon-V2)
- [web3-quant-trading-competition example](https://github.com/Akshay-TP/web3-quant-trading-competition)
