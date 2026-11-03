"""Fair value of an Up/Down market and the trade decision. Pure functions, no network.

Up wins when the price at the end of the window is >= the price at its start (Chainlink BTC/USD stream).
We use Binance as a stand-in for Chainlink and widen the uncertainty a little (basis) for the gap.

P(Up) = Phi( ln(S/K) / sqrt(sigma_s^2 * tau + basis^2) )
  S = price now, K = price at window start, tau = seconds left, sigma_s = per-second volatility.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def sigma_per_second(closes_1m: list[float], floor_1m: float = 0.0004) -> float:
    """Per-second volatility from 1-minute closes (log returns), with a floor so a calm hour does not
    make the model overconfident."""
    rets = [math.log(b / a) for a, b in zip(closes_1m, closes_1m[1:]) if a > 0 and b > 0]
    if len(rets) >= 10:
        m = sum(rets) / len(rets)
        s1m = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    else:
        s1m = 0.0
    return max(s1m, floor_1m) / math.sqrt(60.0)


def fair_up(price: float, strike: float, tau_s: float, sigma_s: float, basis_bps: float = 2.0) -> float:
    if price <= 0 or strike <= 0:
        return 0.5
    var = sigma_s ** 2 * max(tau_s, 0.0) + (basis_bps / 1e4) ** 2
    if var <= 0:
        return 1.0 if price >= strike else 0.0
    return phi(math.log(price / strike) / math.sqrt(var))


def taker_fee(price: float, rate: float = 0.07) -> float:
    """Fee per share in USDC: rate * p * (1 - p)."""
    return rate * price * (1.0 - price)


@dataclass
class Signal:
    side: str          # "Up" or "Down"
    ask: float
    fair: float
    fee: float
    edge: float        # fair - ask - fee, per share


def best_signal(p_up: float, asks: dict[str, float | None], tau_s: float, cfg) -> Signal | None:
    """Pick the side with the biggest edge that passes every rule, or None."""
    if not (cfg.min_tau_s <= tau_s <= cfg.max_tau_s):
        return None
    best = None
    for side, fair in (("Up", p_up), ("Down", 1.0 - p_up)):
        a = asks.get(side)
        if a is None or not (cfg.min_price <= a <= cfg.max_price):
            continue
        fee = taker_fee(a, cfg.fee_rate)
        edge = fair - a - fee
        if edge >= cfg.min_edge and (best is None or edge > best.edge):
            best = Signal(side, a, fair, fee, edge)
    return best


def stake_for(bankroll: float, ask: float, ask_size: float, min_shares: float, cfg) -> tuple[float, float]:
    """Return (shares, stake_usdc). (0, 0) when the trade is too small for the book or the minimum size."""
    stake = min(bankroll * cfg.risk_pct, cfg.max_stake, ask_size * ask)
    if stake <= 0 or ask <= 0:
        return 0.0, 0.0
    shares = math.floor(stake / ask * 100) / 100
    if shares < max(min_shares, 0.01):
        return 0.0, 0.0
    return shares, round(shares * ask, 6)
