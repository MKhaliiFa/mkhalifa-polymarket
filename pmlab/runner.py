"""The strategy lab loop (its own systemd service). Paper only: no wallet, no keys, no orders.
  python3 -m pmlab.runner            run forever
  python3 -m pmlab.runner --once     one pass of every strategy, then print the summary
  python3 -m pmlab.runner --summary  print the summary as JSON
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

from . import arb, copy, mm
from .common import FeedError, LabConfig, LabStore

STRATS = ("arb", "mm", "copy")


def db_path(cfg: LabConfig) -> str:
    return os.path.join(cfg.data_dir, "lab.db")


class Lab:
    def __init__(self, cfg: LabConfig, store: LabStore, get=None, clock=time.time, sleep=time.sleep):
        kw = {"get": get} if get else {}
        self.cfg, self.store, self.clock = cfg, store, clock
        self.arb = arb.Arb(cfg, store, sleep=sleep, clock=clock, **kw)
        self.mm = mm.MM(cfg, store, clock=clock, **kw)
        self.copy = copy.Copy(cfg, store, clock=clock, **kw)
        for s in STRATS:
            if store.meta(f"started_{s}") is None:
                store.set_meta(f"started_{s}", clock())
        self.due = {s: 0.0 for s in STRATS}

    def _safe(self, name: str, fn) -> None:
        try:
            fn()
        except FeedError as e:
            self.store.error(self.clock(), name, str(e))
        except Exception as e:  # keep running; errors are counted in the report
            self.store.error(self.clock(), name, f"{type(e).__name__}: {e} {traceback.format_exc(limit=2)[-150:]}")

    def tick(self, force: bool = False) -> None:
        now = self.clock()
        every = {"arb": self.cfg.arb_every_s, "mm": self.cfg.mm_every_s, "copy": self.cfg.copy_every_s}
        for s, fn in (("copy", lambda: (self.copy.poll(), self.copy.resolve())), ("mm", self.mm.step), ("arb", self.arb.scan)):
            if force or now >= self.due[s]:
                self.due[s] = now + every[s]
                self._safe(s, fn)


def summary(store: LabStore, now: float) -> dict:
    st = {s: float(store.meta(f"started_{s}", now)) for s in STRATS}
    day = now - 86400
    errs = store.one("SELECT COUNT(*) n FROM errors WHERE ts>=?", day)["n"]
    return {
        "ts": now,
        "arb": {**arb.summary(store, st["arb"]), "day": arb.summary(store, day), "verdict": arb.verdict(store, st["arb"], now)},
        "mm": {**mm.summary(store, st["mm"]), "verdict": mm.verdict(store, st["mm"], now)},
        "copy": {**copy.summary(store, st["copy"]), "verdict": copy.verdict(store, st["copy"], now)},
        "errors_24h": errs,
    }


def report_lines(s: dict) -> list[str]:
    """Four short lines for the 09:01 Polymarket report."""
    def usd(x):
        return f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"
    a, m, c = s["arb"], s["mm"], s["copy"]
    out = ["🧪 <b>Lab</b> (paper)",
           f"S1 arbitrage: {a['captured']} captured {usd(a['profit'])} · 24 h {a['day']['captured']} · {a['verdict']}",
           f"S2 market making: {usd(m['net'])} · {m['fills']} fills · {m['verdict']}",
           f"S4 copy: {c['closed']} closed {usd(c['pnl'])} · open {c['open']} · {c['verdict']}"]
    if s.get("errors_24h"):
        out.append(f"⚠️ lab errors 24 h: {s['errors_24h']}")
    return out


def main(argv: list[str]) -> int:
    cfg = LabConfig()
    store = LabStore(db_path(cfg))
    if "--summary" in argv:
        print(json.dumps(summary(store, time.time()), default=str))
        return 0
    lab = Lab(cfg, store)
    if "--once" in argv:
        lab.tick(force=True)
        print(json.dumps(summary(store, time.time()), default=str, indent=1))
        return 0
    while True:
        lab.tick()
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
