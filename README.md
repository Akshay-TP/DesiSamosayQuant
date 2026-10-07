# DesiSamosayQuant

Autonomous, long-only Roostoo competition bot. It uses a four-hour rebalance schedule and a price-only trend strategy, with a persistent audit trail for decisions and orders.

## Strategy

The bot ranks the configured Roostoo USD pairs by a risk-adjusted blend of 24-hour and 72-hour momentum. It only holds assets with positive short-term momentum and price above their 24-hour exponential moving average. BTC below its 72-hour EMA reduces the portfolio exposure cap. The bot allocates to at most four leaders, caps each asset at 25% of NAV, and holds the remaining portfolio in USD.

Live Roostoo ticker responses do not provide OHLCV history or volume. By default, free public Binance hourly closes seed the initial price history; after startup, all new signal observations come from Roostoo. Set `BOOTSTRAP_BINANCE=false` to disable that source. If seeding is unavailable, the bot can start conservatively from Roostoo's 24-hour `Change` field, limits exposure to 25%, and switches to the full multi-horizon model after it collects 73 hourly prices.

The bot uses only spot market orders. It does not short, use leverage, make markets, arbitrage, or submit rapid orders. It checks the account once per hour and rebalances at most once every four hours. It accounts for the competition's 0.1% taker fee when sizing buys. Exchange order precision and minimums are read from `exchangeInfo`.

Risk controls include a 75% maximum gross exposure, 25% maximum per asset, a 4% daily loss gate, and a persistent 8% peak-to-trough drawdown halt that sells toward cash. These are configurable in `.env`.

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
- `portfolio.csv` records NAV, cash, exposure, and daily/peak drawdown.
- `logs/cycles.jsonl` records every decision cycle and its targets.
- `logs/orders.jsonl` records order requests and exchange responses.
- `logs/bot.log` contains operational logs.

Keep these records for the competition's trade-integrity review. They are local runtime evidence and are not committed automatically.

## AWS EC2

On the competition-provided EC2 instance, clone this repository, install Python and the requirements, create `.env` locally, then run the continuous command under `systemd` or another process supervisor. Keep `.env` permissions restricted and never include it in a public repository. Confirm `python main.py --check` succeeds before starting the live loop.

## Competition context

The current APAC event page specifies autonomous trading, open-source code review, traceable strategy commits, no HFT/market making/arbitrage, and spot trading with 1x long/short allowed. This implementation chooses long-only spot. The page states 0.1% taker and 0.05% maker fees; the bot uses market orders and the 0.1% taker assumption. Recheck event updates and actual account configuration before deployment.

## References

- [APAC Quant Trading Hackathon event page](https://luma.com/coghwiyt)
- [Roostoo API documentation](https://github.com/roostoo/Roostoo-API-Documents)
- [Quant-trading-hackathon-V2 example](https://github.com/Akshay-TP/Quant-trading-hackathon-V2)
- [web3-quant-trading-competition example](https://github.com/Akshay-TP/web3-quant-trading-competition)
