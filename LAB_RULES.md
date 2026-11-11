# Polymarket strategy lab: rules fixed before any result (2026-11-10)

Four strategies are measured on paper with public data only (no wallet, no keys, no orders).
Every number below is fixed now, before the first result; changing a rule restarts that strategy's clock.

| # | Strategy | Where |
|---|---|---|
| S1 | Multi-outcome arbitrage (negRisk events) | `pmlab/arb.py` |
| S2 | Market making on reward markets | `pmlab/mm.py` |
| S3 | 15-minute crypto model vs market | `pmbot/` (the existing paper bot, its own rules in README) |
| S4 | Copy the month's best wallets, with delay | `pmlab/copy.py` |
| S5 | Information or domain edge | not automatable; out of scope for the lab |

## Costs used everywhere

- Taker fee per share: `rate × p × (1 − p)`, with `rate` from the market's `feeSchedule` (0 when fees are off).
- Makers pay no fee and receive `rebateRate` × the taker fee their fill generated.
- Paper fills never improve on the order book that was read; no partial credit for depth that was not there.

## S1: multi-outcome arbitrage

- Universe: active negRisk events, not `negRiskAugmented`, every outcome accepting orders, 3 to 40 outcomes.
- An event with an outcome that already resolved YES is left out (the remaining outcomes can only lose).
- Buy-all: buy one YES of every outcome when the summed ask (with fees, walking the book) is below 1.
- Sell-all: buy one NO of every outcome when the summed YES bids minus fees are above 1 (pays n − 1).
- A scan every 60 s finds candidates from Gamma; each is confirmed on the CLOB books, then re-checked 5 s later.
- Paper capture: only if still profitable at the 5 s re-check; the smaller of the two profits counts.
  - Spending is capped at $100 per opportunity.
  - One capture per event per hour.
- **Verdict at 14 days.**
  - PASS needs all three:
    - at least 20 captures;
    - at least $1 each on average;
    - total of $50 or more.
  - Otherwise FAIL.
- **Known limit:** the legs are not atomic. A real fill can leave one leg unfilled; the paper log cannot see that.

## S2: market making on reward markets

- Universe: the 10 markets with the highest daily reward rate meeting all three:
  - mid between 0.20 and 0.80;
  - spread at most the reward max spread;
  - more than 3 days to the end date.
- Quotes: bid and ask at the mid ± half the reward max spread, size = the reward minimum size.
  - Quotes are refreshed every 60 s.
  - A side stops quoting when the inventory exceeds 3 × size.
- Fills are conservative: only a public taker trade strictly through the quote fills it.
  - Fill size: the smaller of the trade and the quote.
- Result:
  - spread P&L, marked to mid, with inventory settled at resolution;
  - plus maker rebates;
  - plus **half** of an estimated reward share of the daily rate. Each resting order is weighted with Polymarket's score, ((v − s) ÷ v)² × size, where s is its distance from the mid and v the max spread; our share is our score ÷ everyone's.
- **Verdict at 14 days.**
  - PASS needs all four:
    - net result above 0;
    - net result above 0 on 9 or more of the 14 days;
    - worst drawdown below 20 % of the capital at risk;
    - estimated rewards below 70 % of the result.
  - Otherwise FAIL.

## S4: copy the best wallets

- Leaders: the month's top 20 by P&L, after leaving out two kinds:
  - market makers: P&L ÷ volume below 5 %;
  - wallets with under $5,000 of monthly volume.
  - The list is refreshed every 6 h.
- Each leader's trades are polled every 60 s. A BUY of $100 or more opens a $10 paper copy at the CLOB best ask when we see it, with the taker fee.
- Exit: when the leader sells that token we sell at the best bid; otherwise at resolution.
- The leader's own entry price is logged, to show what the delay costs.
- **Verdict at 14 days.**
  - PASS needs all three:
    - at least 100 closed copies;
    - a positive average result with t ≥ 2;
    - no single market making more than half the profit.
  - Otherwise FAIL.

## From paper to real money

A PASS is necessary, not sufficient. Real money also needs all of these:
1. The owner's decision.
2. A location where Polymarket allows orders. The server is in Germany, where orders are blocked; no VPN.
3. A wallet and keys the owner creates himself, never pasted into chat.
4. A small fixed cap first, with the same measurements running beside it.

## Changes

- 2026-11-11 (rules version 2, one day after the start): S1 leaves out events with an outcome already resolved YES; S2's reward estimate uses the quadratic score instead of a plain size share, and capital at risk is counted in dollars. Both clocks restarted on that day; S4 unchanged.
