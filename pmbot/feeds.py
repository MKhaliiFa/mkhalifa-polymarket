"""Read-only market data: Binance public market data, Polymarket Gamma (markets) and CLOB (order books).
Nothing here can place an order: there are no keys and no signing in this project."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

UA = "mkhalifa-pm/0.1"


class FeedError(Exception):
    pass


def http_json(url: str, body=None, timeout: float = 8.0):
    data = None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise FeedError(f"HTTP {e.code} {url.split('?')[0]}") from e
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        raise FeedError(f"{type(e).__name__} {url.split('?')[0]}") from e


def _maybe_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


class Feeds:
    def __init__(self, cfg, get=http_json):
        self.cfg = cfg
        self.get = get

    # ---- Binance (public market data host, separate from the trading API the live bot uses)
    def prices(self, symbols: list[str]) -> dict[str, float]:
        q = urllib.parse.quote(json.dumps(symbols, separators=(",", ":")))
        rows = self.get(f"{self.cfg.binance}/api/v3/ticker/price?symbols={q}") or []
        return {r["symbol"]: float(r["price"]) for r in rows}

    def closes_1m(self, symbol: str, n: int = 61) -> list[float]:
        rows = self.get(f"{self.cfg.binance}/api/v3/klines?symbol={symbol}&interval=1m&limit={n}") or []
        return [float(r[4]) for r in rows[:-1]]   # drop the candle still open

    def window_candle(self, symbol: str, start_s: int, window_s: int) -> dict | None:
        """The Binance candle that matches the market window: open, close and whether it is finished."""
        interval = {300: "5m", 900: "15m", 3600: "1h"}[window_s]
        rows = self.get(f"{self.cfg.binance}/api/v3/klines?symbol={symbol}&interval={interval}"
                        f"&startTime={start_s * 1000}&limit=1") or []
        if not rows or int(rows[0][0]) != start_s * 1000:
            return None
        r = rows[0]
        return {"open": float(r[1]), "close": float(r[4]), "close_time_ms": int(r[6])}

    # ---- Polymarket Gamma: find the market of one window
    def window_market(self, asset: str, start_s: int, window_s: int) -> dict | None:
        tag = {300: "5m", 900: "15m"}.get(window_s, "15m")
        slug = f"{asset}-updown-{tag}-{start_s}"
        ev = self.get(f"{self.cfg.gamma}/events/slug/{slug}")
        if not ev:
            return None
        markets = ev.get("markets") or []
        if not markets:
            return None
        return parse_market(markets[0], slug)

    # ---- Polymarket CLOB order books
    def books(self, token_ids: list[str]) -> dict[str, dict]:
        if not token_ids:
            return {}
        rows = self.get(f"{self.cfg.clob}/books", [{"token_id": t} for t in token_ids]) or []
        return {str(b.get("asset_id")): parse_book(b) for b in rows if isinstance(b, dict)}

    def geoblock(self) -> dict:
        g = self.get(self.cfg.geoblock) or {}
        return {"blocked": bool(g.get("blocked")), "country": g.get("country") or "?"}


def parse_market(m: dict, slug: str) -> dict | None:
    outcomes = _maybe_json(m.get("outcomes")) or []
    tokens = _maybe_json(m.get("clobTokenIds")) or []
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    by_name = {str(o).strip().capitalize(): str(t) for o, t in zip(outcomes, tokens)}
    if set(by_name) != {"Up", "Down"}:
        return None
    prices = _maybe_json(m.get("outcomePrices")) or []
    winner = None
    if m.get("closed") and len(prices) == 2:
        try:
            p = [float(x) for x in prices]
            if max(p) >= 0.99:
                winner = str(outcomes[p.index(max(p))]).strip().capitalize()
        except (TypeError, ValueError):
            pass
    return {"slug": slug, "tokens": by_name, "closed": bool(m.get("closed")), "winner": winner}


def parse_book(b: dict) -> dict:
    def lv(side):
        out = []
        for x in b.get(side) or []:
            try:
                out.append((float(x["price"]), float(x["size"])))
            except (KeyError, TypeError, ValueError):
                pass
        return out
    bids, asks = lv("bids"), lv("asks")
    bb = max(bids, key=lambda x: x[0]) if bids else None
    ba = min(asks, key=lambda x: x[0]) if asks else None
    try:
        min_size = float(b.get("min_order_size") or 5)
    except (TypeError, ValueError):
        min_size = 5.0
    return {"bid": bb[0] if bb else None, "bid_size": bb[1] if bb else 0.0,
            "ask": ba[0] if ba else None, "ask_size": ba[1] if ba else 0.0, "min_size": min_size}
