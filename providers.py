"""Fallback providers for the global volume ranking (used when CoinMarketCap is unavailable / out of credits).

FUTURES ONLY: every provider here ranks coins by perpetual/futures 24h volume. No spot volume, no total (spot +
derivatives) volume, no stocks are ever mixed in.

The active provider chain is intentionally limited to two providers: CoinMarketCap first, then CoinGecko.
They are tried strictly in that order; the first one that returns a usable futures-volume ranking wins and is cached.
If CoinMarketCap has a credit/rate limit or API failure, the scanner tries CoinGecko. If both fail, the ranking
refresh fails safely rather than silently substituting spot volume or an unrelated ranking.

Differences from the CoinMarketCap ranking (important):
* Fallback providers do not know MEXC's own derivative listing, so a contract is matched to a ranked coin by its BASE
  SYMBOL (``CRYPTO_VERIFICATION=symbol`` behaviour for these rankings). The MEXC-side filters (TradFi/stock labels,
  keywords, denylist, liquidity, spread) still apply in full, and known stock/commodity tickers are also dropped from
  the ranking itself.
* The "exchanges" provider covers only five venues (a partial view of global futures volume). Binance and Bybit block
  some cloud IP ranges (e.g. US-based GitHub runners); that is tolerated, as long as at least two venues answer.
* If several listings share a ticker, the volumes are summed (futures providers).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import requests

from config import KNOWN_PROVIDERS
from global_volume import GlobalRanking, GlobalVolumeError, _num, rank_assets
from mexc_data import KNOWN_NON_CRYPTO_BASES, is_unsuitable_base

log = logging.getLogger("providers")

MIN_RANKED_ASSETS = 50  # a ranking with fewer assets than this is treated as a failed / broken response
_SYMBOL_OK = re.compile(r"^[A-Z0-9]{2,20}$")
_MULTIPLIER_PREFIX = re.compile(r"^(1000000|10000|1000|1M)(?=[A-Z])")  # 1000PEPE -> PEPE


def skip_base(symbol: str) -> bool:
    """Stablecoin / leveraged token / known stock-commodity-index-FX ticker: never ranked."""
    return is_unsuitable_base(symbol) or symbol.upper() in KNOWN_NON_CRYPTO_BASES


def normalize_base(symbol: str) -> str:
    """Upper-case ticker with exchange 'multiplier' prefixes removed (``1000PEPE`` -> ``PEPE``)."""
    s = (symbol or "").strip().upper()
    return _MULTIPLIER_PREFIX.sub("", s) or s


def make_ranking(
    label: str,
    volumes: Mapping[str, float],
    venues: Mapping[str, Set[str]],
    endpoints: List[str],
    scope: str,
    now: Optional[datetime] = None,
    extra_coverage: Optional[Dict[str, Any]] = None,
    assets_received: Optional[int] = None,
) -> GlobalRanking:
    """Turn ``{base symbol: USD volume}`` into the common cache object (symbol-verified, no MEXC listing)."""
    now = now or datetime.now(timezone.utc)
    ordered = sorted(((s, v) for s, v in volumes.items() if v > 0), key=lambda kv: (-kv[1], kv[0]))
    if len(ordered) < MIN_RANKED_ASSETS:
        raise GlobalVolumeError(f"{label}: only {len(ordered)} usable assets returned (need >= {MIN_RANKED_ASSETS})")
    assets = {}
    for i, (sym, vol) in enumerate(ordered):
        cid = 10_000_000 + i  # synthetic, unique within this ranking (these providers have no CMC ids)
        assets[cid] = {"crypto_id": cid, "symbol": sym, "volume_usd": vol,
                       "markets": len(venues.get(sym, ())) or 1, "exchanges": set(venues.get(sym, ()))}
    ranked = rank_assets(assets)
    coverage: Dict[str, Any] = {
        "verification": "symbol", "volume_scope": scope, "fallback": True, "provider_label": label,
        "exchanges_fetched": None, "exchanges_failed": [], "fetched_volume_share_pct": None,
    }
    coverage.update(extra_coverage or {})
    return GlobalRanking(now.isoformat(), label, list(endpoints), ranked[:600], {}, coverage,
                         assets_received if assets_received is not None else len(ranked), 0)


class RankingProvider:
    """Base class: HTTP with bounded retries; subclasses implement :meth:`fetch`. Keys are never logged."""

    name = ""  # id used in GLOBAL_PROVIDERS
    label = ""  # shown in logs / cache
    env_key: Optional[str] = None  # environment variable holding the key
    key_required = False

    def __init__(self, api_key: str = "", session: Optional[requests.Session] = None, timeout: float = 30.0,
                 max_retries: int = 2, backoff_seconds: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._key = (api_key or "").strip()
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "po3-signal-scanner/2.0"})
        self.timeout, self.max_retries, self.backoff, self._sleep = timeout, max_retries, backoff_seconds, sleep
        self.calls = 0

    def available(self) -> Tuple[bool, str]:
        if self.key_required and not self._key:
            return False, f"{self.env_key} is not set"
        return True, ""

    def fetch(self, now: Optional[datetime] = None) -> GlobalRanking:  # pragma: no cover - interface
        raise NotImplementedError

    def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
        last = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last = type(exc).__name__
                if attempt < self.max_retries:
                    self._sleep(self.backoff * (2 ** attempt))
                continue
            self.calls += 1
            code = resp.status_code
            if code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise GlobalVolumeError(f"{self.label}: response was not valid JSON")
            if code in (401, 403):
                raise GlobalVolumeError(f"{self.label}: HTTP {code} - API key invalid or not allowed")
            if code == 402:
                raise GlobalVolumeError(f"{self.label}: HTTP 402 - plan / credit limit reached")
            if code == 429 or code >= 500:
                last = f"HTTP {code}"
                delay = self.backoff * (2 ** attempt)
                try:
                    delay = max(delay, float(resp.headers.get("Retry-After", 0)))
                except (ValueError, AttributeError, TypeError):
                    pass
                if attempt < self.max_retries:
                    self._sleep(min(delay, 30.0))
                continue
            raise GlobalVolumeError(f"{self.label}: HTTP {code}")
        raise GlobalVolumeError(f"{self.label}: gave up after {self.max_retries + 1} attempts ({last})")


class CoinGlassProvider(RankingProvider):
    """CoinGlass V4 futures coin-market ranking. Requires a CoinGlass API key and a plan
    that exposes /api/futures/coins-markets (official docs currently list Standard+).

    This endpoint is futures-market data, not spot volume. If the plan or response does not
    expose a usable USD-volume field, the provider fails cleanly and the chain moves to CoinGecko.
    """

    name, label, env_key = "coinglass", "CoinGlass", "COINGLASS_API_KEY"
    key_required = True
    URL = "https://open-api-v4.coinglass.com/api/futures/coins-markets"

    def fetch(self, now: Optional[datetime] = None) -> GlobalRanking:
        rows = self._get_json(self.URL, headers={"CG-API-KEY": self._key})
        if not isinstance(rows, dict) or str(rows.get("code", "0")) != "0":
            msg = rows.get("msg", "unexpected response") if isinstance(rows, dict) else "unexpected response shape"
            raise GlobalVolumeError(f"{self.label}: API error: {str(msg)[:120]}")
        data = rows.get("data")
        if not isinstance(data, list):
            raise GlobalVolumeError(f"{self.label}: data field is not a list")
        volumes: Dict[str, float] = {}
        venues: Dict[str, Set[str]] = {}
        skipped: Dict[str, int] = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            base = normalize_base(str(row.get("symbol") or row.get("coin") or ""))
            if not _SYMBOL_OK.match(base) or skip_base(base):
                skipped["unsuitable symbol"] = skipped.get("unsuitable symbol", 0) + 1
                continue
            # CoinGlass V4 field names may vary by endpoint/plan; accept only explicit USD 24h volume fields.
            vol = next((_num(row.get(k)) for k in ("volume_usd_24h", "volume_24h_usd", "volume_usd", "volume_24h")
                        if _num(row.get(k)) is not None), None)
            if vol is None or vol <= 0:
                skipped["missing USD volume"] = skipped.get("missing USD volume", 0) + 1
                continue
            volumes[base] = max(volumes.get(base, 0.0), vol)
            venues.setdefault(base, set()).add("CoinGlass futures aggregate")
        ranking = make_ranking(self.label, volumes, venues, ["GET /api/futures/coins-markets"], "futures", now,
                               {"endpoint": self.URL, "skipped": skipped, "verification": "symbol"},
                               assets_received=len(data))
        log.info("%s futures ranking: %d rows -> %d usable assets", self.label, len(data), len(volumes))
        return ranking


class CoinGeckoProvider(RankingProvider):
    """CoinGecko ``GET /derivatives``: every derivative ticker on every venue in ONE call, ``volume_24h`` in USD.

    Futures-only ranking (like CoinMarketCap): perpetual + futures tickers summed per base asset (``index_id``).
    Free "Demo" plan: header ``x-cg-demo-api-key`` (10k calls/month, 30/min). Works without a key too, but shared
    public limits are tight - set COINGECKO_API_KEY.
    """

    name, label, env_key = "coingecko", "CoinGecko", "COINGECKO_API_KEY"
    URL = "https://api.coingecko.com/api/v3/derivatives"
    CONTRACT_TYPES = ("perpetual", "futures")

    def fetch(self, now: Optional[datetime] = None) -> GlobalRanking:
        headers = {"x-cg-demo-api-key": self._key} if self._key else {}
        rows = self._get_json(self.URL, headers=headers)
        if not isinstance(rows, list):
            raise GlobalVolumeError(f"{self.label}: unexpected response shape (expected a list)")
        volumes: Dict[str, float] = {}
        venues: Dict[str, Set[str]] = {}
        seen: Set[Tuple[str, str]] = set()
        skipped: Dict[str, int] = {}

        def skip(why: str) -> None:
            skipped[why] = skipped.get(why, 0) + 1

        for r in rows:
            if not isinstance(r, dict):
                skip("malformed")
                continue
            if str(r.get("contract_type", "")).lower() not in self.CONTRACT_TYPES:
                skip("contract_type")
                continue
            key = (str(r.get("market", "")), str(r.get("symbol", "")))
            if key in seen:
                skip("duplicate")
                continue
            seen.add(key)
            base = normalize_base(str(r.get("index_id") or ""))
            if not _SYMBOL_OK.match(base) or skip_base(base):
                skip("base unsuitable / empty")
                continue
            vol = _num(r.get("volume_24h"))
            if vol is None or vol <= 0:
                skip("no volume")
                continue
            volumes[base] = volumes.get(base, 0.0) + vol
            venues.setdefault(base, set()).add(key[0])
        ranking = make_ranking(self.label, volumes, venues, ["GET /api/v3/derivatives"], "futures", now,
                               {"tickers_seen": len(rows), "skipped": skipped}, assets_received=len(volumes))
        log.info("%s ranking: %d tickers -> %d assets (skipped %s)", self.label, len(rows), len(volumes), skipped)
        return ranking


class ExchangesFuturesProvider(RankingProvider):
    """Sum of USDT-perpetual 24h turnover over five big derivatives exchanges' PUBLIC ticker endpoints (futures only).

    OKX ``/api/v5/market/tickers?instType=SWAP`` (``volCcy24h`` is in BASE coin for derivatives -> x last price),
    Bitget ``/api/v2/mix/market/tickers?productType=USDT-FUTURES`` (``usdtVolume``),
    Gate ``/api/v4/futures/usdt/tickers`` (``volume_24h_quote``), Bybit ``/v5/market/tickers?category=linear``
    (``turnover24h``), Binance ``/fapi/v1/ticker/24hr`` (``quoteVolume``). Each exchange is optional: one that is
    blocked or failing is skipped, but at least ``MIN_EXCHANGES`` must answer, otherwise the provider fails.
    """

    name, label, env_key = "exchanges", "Multi-exchange futures", None
    MIN_EXCHANGES = 2

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.max_retries = min(self.max_retries, 1)  # a blocked exchange should fail fast

    @staticmethod
    def _rows(body: Any, *path: str) -> List[Any]:
        cur = body
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if not isinstance(cur, list):
            raise GlobalVolumeError("unexpected response shape")
        return cur

    def _okx(self) -> Dict[str, float]:
        rows = self._rows(self._get_json("https://www.okx.com/api/v5/market/tickers", params={"instType": "SWAP"}), "data")
        out: Dict[str, float] = {}
        for r in rows:
            inst = str(r.get("instId", "")) if isinstance(r, dict) else ""
            if not inst.endswith("-USDT-SWAP"):
                continue
            coins, last = _num(r.get("volCcy24h")), _num(r.get("last"))
            if coins and last:
                out[inst.split("-")[0]] = coins * last
        return out

    def _bitget(self) -> Dict[str, float]:
        rows = self._rows(self._get_json("https://api.bitget.com/api/v2/mix/market/tickers",
                                         params={"productType": "USDT-FUTURES"}), "data")
        out: Dict[str, float] = {}
        for r in rows:
            sym = str(r.get("symbol", "")) if isinstance(r, dict) else ""
            vol = _num(r.get("usdtVolume")) if isinstance(r, dict) else None
            if sym.endswith("USDT") and vol:
                out[sym[:-4]] = vol
        return out

    def _gate(self) -> Dict[str, float]:
        rows = self._rows(self._get_json("https://api.gateio.ws/api/v4/futures/usdt/tickers"))
        out: Dict[str, float] = {}
        for r in rows:
            c = str(r.get("contract", "")) if isinstance(r, dict) else ""
            vol = _num(r.get("volume_24h_quote")) if isinstance(r, dict) else None
            if c.endswith("_USDT") and c.count("_") == 1 and vol:
                out[c[:-5]] = vol
        return out

    def _bybit(self) -> Dict[str, float]:
        rows = self._rows(self._get_json("https://api.bybit.com/v5/market/tickers", params={"category": "linear"}), "result", "list")
        out: Dict[str, float] = {}
        for r in rows:
            sym = str(r.get("symbol", "")) if isinstance(r, dict) else ""
            vol = _num(r.get("turnover24h")) if isinstance(r, dict) else None
            if sym.endswith("USDT") and "-" not in sym and vol:
                out[sym[:-4]] = vol
        return out

    def _binance(self) -> Dict[str, float]:
        rows = self._rows(self._get_json("https://fapi.binance.com/fapi/v1/ticker/24hr"))
        out: Dict[str, float] = {}
        for r in rows:
            sym = str(r.get("symbol", "")) if isinstance(r, dict) else ""
            vol = _num(r.get("quoteVolume")) if isinstance(r, dict) else None
            if sym.endswith("USDT") and "_" not in sym and vol:
                out[sym[:-4]] = vol
        return out

    def fetch(self, now: Optional[datetime] = None) -> GlobalRanking:
        sources = (("okx", self._okx), ("bitget", self._bitget), ("gate", self._gate),
                   ("bybit", self._bybit), ("binance", self._binance))
        volumes: Dict[str, float] = {}
        venues: Dict[str, Set[str]] = {}
        ok, failed = [], []
        for name, fn in sources:
            try:
                per_base = fn()
            except GlobalVolumeError as exc:
                failed.append(name)
                log.info("%s: %s unavailable (%s)", self.label, name, exc)
                continue
            except Exception as exc:  # parsing surprise in one venue must not lose the others
                failed.append(name)
                log.info("%s: %s unavailable (unexpected %s)", self.label, name, type(exc).__name__)
                continue
            if not per_base:
                failed.append(name)
                log.info("%s: %s returned no USDT perpetual volume", self.label, name)
                continue
            ok.append(name)
            for raw, vol in per_base.items():
                base = normalize_base(raw)
                if not _SYMBOL_OK.match(base) or skip_base(base):
                    continue
                volumes[base] = volumes.get(base, 0.0) + vol
                venues.setdefault(base, set()).add(name)
        if len(ok) < self.MIN_EXCHANGES:
            raise GlobalVolumeError(f"{self.label}: only {len(ok)} of {len(sources)} exchanges answered "
                                    f"(need >= {self.MIN_EXCHANGES}; failed: {', '.join(failed) or 'none'})")
        log.info("%s ranking: exchanges ok=%s failed=%s -> %d assets", self.label, ok, failed, len(volumes))
        return make_ranking(self.label, volumes, venues, [f"GET {n} public futures tickers" for n in ok], "futures", now,
                            {"exchanges_fetched": len(ok), "exchanges_ok": ok, "exchanges_failed": failed},
                            assets_received=len(volumes))


_CLASSES = {CoinGeckoProvider.name: CoinGeckoProvider}
assert set(_CLASSES) <= set(KNOWN_PROVIDERS)


def build_fallback_providers(order: Iterable[str], env: Mapping[str, str],
                             session_factory: Optional[Callable[[], requests.Session]] = None) -> Dict[str, RankingProvider]:
    """Instantiate the non-CoinMarketCap providers named in ``order`` (keys read from ``env`` only)."""
    out: Dict[str, RankingProvider] = {}
    for name in order:
        cls = _CLASSES.get(name)
        if cls is None:
            continue
        key = (env.get(cls.env_key) or "").strip() if cls.env_key else ""
        out[name] = cls(api_key=key, session=session_factory() if session_factory else None)
    return out
