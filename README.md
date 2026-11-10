# MKhaliFa Polymarket (paper)

A paper-trading bot for Polymarket's 15-minute crypto **Up or Down** markets (BTC, ETH).
**No real money, no wallet, no keys**: it only reads public prices and keeps score.

## What it does

1. Every 2 seconds it reads the Binance price and the Polymarket order book of the current 15-minute window.
2. It works out the fair chance of **Up** from the price now, the price at the window start, the time left
   and recent volatility: `P(Up) = Φ( ln(S/K) / √(σ²·τ + basis²) )`.
3. When the cheapest ask is below fair value by at least **3¢ per share after Polymarket's taker fee**
   (`0.07 × p × (1−p)` for crypto), it paper-buys that side.
4. At the end of the window it settles with Binance's candle, then re-scores with Polymarket's own
   (Chainlink) result when it is published.
5. Every snapshot, window and trade is stored in SQLite (`/opt/mkhalifa-polymarket/data/pm.db`).

## Rules (`config.env`)

| Rule | Value |
|---|---|
| Paper bankroll | $100 |
| Stake per trade | 3 % of bankroll, max $10 |
| Minimum edge after fees | 3¢ per share |
| Price range | 0.15 to 0.85 |
| Time left | 15 s to 14.5 min |
| One trade per window | yes |
| Daily stop | no new trades after −10 % in a Cairo day |

## Daily report

Sent to the main bot's Telegram chat at **09:01 Cairo**, one minute after the main bot's own 09:00 report.
It shows trades, win rate, P&L after fees, **model vs market** (Brier score), whether Binance matched
Polymarket's result, and a verdict. The verdict needs at least 100 trades and 3 days before it says
anything other than "too early". The bot only sends messages; it never reads Telegram updates, so the
main bot's commands are untouched.

## Separate from the live bot

- Own folder `/opt/mkhalifa-polymarket`, own user `pmbot`, own systemd units, no Docker.
- Capped at 30 % of one CPU and 300 MB of memory, lower priority than everything else.
- Reads Binance from the public market-data host (`data-api.binance.vision`), about 35 requests a minute.
- The live bot's folder, container and timers are never changed.
- Polymarket blocks new orders from Germany; that does not matter for paper trading.

## Strategy lab (paper, since 2026-11-10)

A second service (`mkhalifa-pm-lab`, package `pmlab/`) measures three more ways people make money on
Polymarket, with public data only. Rules and verdicts are fixed in advance in [LAB_RULES.md](LAB_RULES.md).

| # | Strategy | What is measured |
|---|---|---|
| S1 | Multi-outcome arbitrage | negRisk events whose outcomes cost less than they pay, re-checked 5 s later |
| S2 | Market making on reward markets | quotes at mid ± half the reward spread, filled only by public trades through them |
| S4 | Copy the best wallets | the month's top directional wallets, copied at our own price after the delay |

S3 is the bot above. Each strategy gets a PASS or FAIL after 14 days; its line is in the 09:01 report.

```bash
cd /opt/mkhalifa-polymarket/repo && sudo -u pmbot bash -c 'set -a; . ./config.env; python3 -m pmlab.runner --summary'
journalctl -u mkhalifa-pm-lab -n 50
```

## Server

```bash
# install (once, as root)
curl -fsSL https://raw.githubusercontent.com/MKhaliiFa/mkhalifa-polymarket/main/install.sh | bash
# what the server sees right now
cd /opt/mkhalifa-polymarket/repo && sudo -u pmbot bash -c 'set -a; . ./config.env; . ../.env; python3 -m pmbot.main --check'
# today's report without sending it
cd /opt/mkhalifa-polymarket/repo && sudo -u pmbot bash -c 'set -a; . ./config.env; . ../.env; python3 -m pmbot.main --report'
# logs / stop
journalctl -u mkhalifa-polymarket -n 50
systemctl disable --now mkhalifa-polymarket mkhalifa-pm-update.timer
```

Updates: a push to `main` is installed within 10 minutes (only if the tests pass).

## Tests

```bash
python3 -m unittest -v
```
