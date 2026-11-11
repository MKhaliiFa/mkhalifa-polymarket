"""All settings come from environment variables (config.env in the repo, optional override.env on the server)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _s(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


@dataclass
class Config:
    assets: list[str] = field(default_factory=lambda: [a.strip().lower() for a in _s("PM_ASSETS", "btc,eth").split(",") if a.strip()])
    window_s: int = int(_f("PM_WINDOW_S", 900))          # 15-minute markets
    trade: bool = _f("PM_TRADE", 1) > 0                   # 0 = verdict reached: no new paper trades, reports go on
    loop_s: float = _f("PM_LOOP_S", 2)                    # one tick every 2 s
    snap_every_s: float = _f("PM_SNAP_EVERY_S", 5)        # one stored snapshot per asset every 5 s

    start_bankroll: float = _f("PM_START_BANKROLL", 100)  # paper money
    risk_pct: float = _f("PM_RISK_PCT", 0.03)             # stake per trade, share of bankroll
    max_stake: float = _f("PM_MAX_STAKE", 10)
    daily_stop_pct: float = _f("PM_DAILY_STOP_PCT", 0.10) # no new trades for the day after -10 %

    min_edge: float = _f("PM_MIN_EDGE", 0.03)             # 3 cents per share after fees
    min_price: float = _f("PM_MIN_PRICE", 0.15)
    max_price: float = _f("PM_MAX_PRICE", 0.85)
    min_tau_s: float = _f("PM_MIN_TAU_S", 15)
    max_tau_s: float = _f("PM_MAX_TAU_S", 870)
    fee_rate: float = _f("PM_FEE_RATE", 0.07)             # crypto taker fee: rate * p * (1 - p) per share
    basis_bps: float = _f("PM_BASIS_BPS", 2)              # Binance vs Chainlink price gap allowance
    min_sigma_1m: float = _f("PM_MIN_SIGMA_1M", 0.0004)   # floor on 1-minute volatility

    report_hour: int = int(_f("PM_REPORT_HOUR", 9))
    report_minute: int = int(_f("PM_REPORT_MINUTE", 1))   # one minute after the main bot's 09:00 report
    tz: str = _s("PM_TZ", "Africa/Cairo")

    data_dir: str = _s("PM_DATA_DIR", "/opt/mkhalifa-polymarket/data")
    tg_token: str = _s("TELEGRAM_BOT_TOKEN", "")
    tg_chat: str = _s("TELEGRAM_CHAT_ID", "")

    binance: str = _s("PM_BINANCE_URL", "https://data-api.binance.vision")
    gamma: str = _s("PM_GAMMA_URL", "https://gamma-api.polymarket.com")
    clob: str = _s("PM_CLOB_URL", "https://clob.polymarket.com")
    geoblock: str = _s("PM_GEOBLOCK_URL", "https://polymarket.com/api/geoblock")


SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
