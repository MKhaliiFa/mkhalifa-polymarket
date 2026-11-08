"""Entry points.
  python3 -m pmbot.main            run the paper bot forever (systemd does this)
  python3 -m pmbot.main --check    one live round: prints what the server sees, then exits
  python3 -m pmbot.main --report   print today's report text (does not send it)
  python3 -m pmbot.main --summary  print the dashboard numbers as JSON (read only)
"""
from __future__ import annotations

import os
import sys
import time

from .config import Config, SYMBOLS
from .engine import Engine, run_forever
from .feeds import FeedError, Feeds
from .report import daily_report
from .store import Store
from .telegram import Telegram


def build(cfg: Config) -> Engine:
    return Engine(cfg, Feeds(cfg), Store(os.path.join(cfg.data_dir, "pm.db")), Telegram(cfg.tg_token, cfg.tg_chat))


def check(cfg: Config) -> int:
    f = Feeds(cfg)
    ok = True

    def line(name, fn):
        nonlocal ok
        try:
            print(f"OK   {name}: {fn()}")
        except (FeedError, KeyError, TypeError, ValueError) as e:
            ok = False
            print(f"FAIL {name}: {e}")

    now = time.time()
    start = int(now // cfg.window_s * cfg.window_s)
    line("Binance prices", lambda: f.prices([SYMBOLS[a] for a in cfg.assets]))
    line("Polymarket geoblock", f.geoblock)
    for a in cfg.assets:
        m = None

        def market(a=a):
            nonlocal m
            m = f.window_market(a, start, cfg.window_s)
            if not m:
                raise ValueError(f"no market for {a}-updown-15m-{start}")
            return m["slug"]
        line(f"{a.upper()} market", market)
        if m:
            line(f"{a.upper()} book", lambda m=m: {k: f.books([t]).get(t) for k, t in m["tokens"].items()})
    print("Telegram: " + ("set" if cfg.tg_token and cfg.tg_chat else "MISSING"))
    print("ALL GOOD" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    cfg = Config()
    if "--check" in argv:
        return check(cfg)
    if "--summary" in argv:
        import json
        from .report import summary
        st = Store(os.path.join(cfg.data_dir, "pm.db"))
        print(json.dumps(summary(st, cfg, time.time()), default=str))
        return 0
    if "--report" in argv:
        e = build(cfg)
        print(daily_report(e, time.time()))
        return 0
    run_forever(build(cfg), cfg.loop_s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
