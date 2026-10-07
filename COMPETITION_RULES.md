# Competition rules and bot compliance

This summary is based on the public [APAC Quant Trading Hackathon event page](https://luma.com/coghwiyt) and [Roostoo API documentation](https://github.com/roostoo/Roostoo-API-Documents), reviewed on 2026-10-08. Event dates and organizer instructions can change; follow any newer direct instructions from the organizers.

## Verified event requirements

- Teams must build a strategy that trades autonomously on Roostoo's mock exchange through its API. The event page says competition account access through the frontend is disabled to prevent manual trades.
- At least 8 active trading days are required during the 14-day live period, with “enough trades” made from the strategy each day. The public page does not state a numeric daily trade threshold.
- No high-frequency trading, market making, or arbitrage. Excessive API requests can fail.
- Spot trading is allowed, including 1× long and short. Leverage is prohibited.
- Each team receives a $100,000 mock portfolio. The bot reads live account balances rather than assuming a starting balance.
- Fees are listed as 0.1% for market/taker orders and 0.05% for limit/maker orders. Roostoo's short endpoint documentation lists a 0.1% fee for short opens and closes.
- A public/open-source GitHub repository is required for code validation. Strategy changes must have traceable commit history. The event page says there must be no traces of manually called APIs.
- The event page lists the live window as Oct 4–17, 2026, repository submission before Oct 14, and allows strategy iteration and redeployment during live trading.
- Performance screening includes portfolio return, then a composite risk-adjusted score of 0.4 Sortino + 0.3 Sharpe + 0.3 Calmar, followed by code and strategy review. The page also names clear strategy logic, clean repository maintenance, and continuous Roostoo compatibility as review criteria.
- All submissions must be original; the page states a strict anti-plagiarism policy. Teams are 1–4 students, with eligibility limited to the listed universities.
- The event page permits any data sources and says Roostoo covers cloud server costs, while additional data-source costs are not covered.
- Deployment is required on an AWS VM. The event page says teams will receive an AWS sub-account/EC2 instance.

## How this bot addresses the rules

- Live mode places strategy-generated orders through Roostoo APIs. `--check` and `--status` are read-only. Keep the competition key dedicated to the autonomous bot and do not make manual trade API calls.
- Rebalancing is limited to once every four hours, with one polling cycle per hour by default. It does not use market making, arbitrage, leverage, or rapid order placement.
- Long and short target weights share a 75% gross exposure cap by default; each asset is capped at 25% of NAV. Short collateral is sized 1:1 with target notional.
- Market orders use the taker fee assumption. Exchange pair enablement, precision, and minimum order size are read from `exchangeInfo`.
- Strategy decisions, order intents/results, portfolio snapshots, and price history are saved under ignored `runtime/` for review. Keep these logs for judging and operational review.
- Public Binance hourly closes seed the initial history; the strategy uses Roostoo prices for subsequent observations. This is a public data source, not a trade execution venue.
- `python audit_competition.py` counts successful live order actions by UTC date and excludes dry-run records. This is a local activity check, not an official determination that the organizer's undefined “enough trades” threshold has been met.
- Create a descriptive Git commit for each strategy update before deploying it and submitting the repository, so the review history matches the live bot. Do not rewrite history to hide a deployed change.
- The README documents EC2 launch, but AWS account-specific sign-in, instance, and security steps must follow the organizers' AWS guide.

## Items not verified from linked materials

The linked Notion FAQ, Data Sources Pack, and AWS deployment guide, and the Pitch info-session page could not be fetched in this review. Their additional answers, approved data integrations, security steps, or rule updates are not assumed here. The event page links to those materials; confirm their contents in a browser or with the organizers before final deployment. In particular, confirm the daily trade-count threshold, the competition's date/time zone for active days, and any restrictions specific to external data or API polling.

## Pre-submission checklist

- Confirm the exact “enough trades” threshold and active-day timezone with organizers; inspect `python audit_competition.py` daily.
- Run only the autonomous strategy against the competition API. Do not make manual trades or manually call trade endpoints.
- Keep traceable commits for every strategy change and submit the public repository before the stated deadline.
- Use the provided AWS EC2 account, keep `.env` private, and run `python main.py --check` before starting the service.
- Check the competition account actually permits short positions; the API documents that short permission can be disabled per competition/account.
