"""Fallback ranking providers (CoinGecko / multi-exchange futures) and the provider chain. No real network."""
import logging
import pathlib
from datetime import datetime, timezone

import pytest

import scanner
from config import ConfigError, ScannerConfig
from global_volume import GlobalRanking, GlobalVolumeError, RankingCache
from providers import (CoinGeckoProvider, CoinGlassProvider, ExchangesFuturesProvider, MIN_RANKED_ASSETS,
                       build_fallback_providers, normalize_base)
from selection import ContractResolver, select_global_contracts
from tests.test_selection import NOW, StubMexc, mexc, tick

SECRET = "SECRET-GECKO-KEY-777"
ROOT = pathlib.Path(__file__).resolve().parent.parent


class Resp:
    def __init__(self, status=200, body=None, headers=None, bad_json=False):
        self.status_code, self._body, self.headers, self._bad = status, body, headers or {}, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


class Session:
    """Scripted requests.Session: ``responses`` is a list (consumed in order, last one repeats)."""

    def __init__(self, responses):
        self.responses, self.calls, self.headers = list(responses), [], {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def gecko_rows(n=60):
    rows = []
    for i in range(n):
        rows.append({"market": "Binance (Futures)", "symbol": f"C{i}USDT", "index_id": f"C{i}",
                     "contract_type": "perpetual", "volume_24h": 1_000_000.0 * (n - i)})
    return rows


def provider(cls, responses, **kw):
    return cls(session=Session(responses), sleep=lambda s: None, **kw)


# ---- parsing ---------------------------------------------------------------------------------------------
def test_normalize_base_strips_multiplier_prefixes_only():
    assert normalize_base("1000PEPE") == "PEPE" and normalize_base("1MBABYDOGE") == "BABYDOGE"
    assert normalize_base("10000SATS") == "SATS" and normalize_base("1INCH") == "1INCH" and normalize_base("btc") == "BTC"


def test_coingecko_sums_perpetual_and_futures_per_base_and_ignores_the_rest():
    rows = gecko_rows() + [
        {"market": "OKX (Futures)", "symbol": "C0-USDT-SWAP", "index_id": "C0", "contract_type": "perpetual", "volume_24h": 5e6},
        {"market": "Bybit (Futures)", "symbol": "C0USDT-27DEC", "index_id": "C0", "contract_type": "futures", "volume_24h": 1e6},
        {"market": "X", "symbol": "OPT", "index_id": "C0", "contract_type": "option", "volume_24h": 9e12},   # not a future
        {"market": "Binance (Futures)", "symbol": "C0USDT", "index_id": "C0", "contract_type": "perpetual", "volume_24h": 7e9},  # duplicate
        {"market": "Binance (Futures)", "symbol": "USDCUSDT", "index_id": "USDC", "contract_type": "perpetual", "volume_24h": 9e12},
        {"market": "Binance (Futures)", "symbol": "1000PEPEUSDT", "index_id": "1000PEPE", "contract_type": "perpetual", "volume_24h": 3e9},
        {"market": "Binance (Futures)", "symbol": "NOVOL", "index_id": "ZZZ", "contract_type": "perpetual", "volume_24h": None},
        "garbage",
    ]
    p = provider(CoinGeckoProvider, [Resp(body=rows)], api_key=SECRET)
    rk = p.fetch(NOW)
    by = {a["symbol"]: a for a in rk.assets}
    assert by["C0"]["volume_usd"] == 60_000_000.0 + 5e6 + 1e6          # duplicate / option excluded
    assert "USDC" not in by and "ZZZ" not in by and "OPT" not in by     # stablecoin / no volume
    assert by["PEPE"]["volume_usd"] == 3e9 and rk.assets[0]["symbol"] == "PEPE" and rk.assets[0]["rank"] == 1
    assert rk.provider == "CoinGecko" and rk.coverage["verification"] == "symbol" and rk.coverage["volume_scope"] == "futures"
    assert rk.mexc_markets == {} and rk.generated_at == NOW.isoformat()
    assert p.session.calls == [{"url": CoinGeckoProvider.URL, "params": None, "headers": {"x-cg-demo-api-key": SECRET}}]


def test_coingecko_works_without_a_key_and_sends_no_key_header():
    p = provider(CoinGeckoProvider, [Resp(body=gecko_rows())])
    assert p.available() == (True, "") and p.fetch(NOW).assets
    assert p.session.calls[0]["headers"] == {}


def test_coingecko_rejects_wrong_shape_and_too_few_assets():
    with pytest.raises(GlobalVolumeError, match="unexpected response shape"):
        provider(CoinGeckoProvider, [Resp(body={"error": "x"})]).fetch(NOW)
    with pytest.raises(GlobalVolumeError, match="usable assets"):
        provider(CoinGeckoProvider, [Resp(body=gecko_rows(MIN_RANKED_ASSETS - 1))]).fetch(NOW)


class Routed:
    """requests.Session stand-in answering by URL substring (one entry per exchange)."""

    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        for part, r in self.routes.items():
            if part in url:
                if isinstance(r, Exception):
                    raise r
                return r
        return Resp(status=404)


def okx(rows):
    return Resp(body={"code": "0", "data": rows})


def exchange_world(**over):
    """All five venues answering with a few coins each (>= 50 assets via filler)."""
    filler = lambda n, fmt: [fmt(f"F{i}", 1e6 + i) for i in range(n)]
    routes = {
        "okx.com": okx([{"instId": "AAA-USDT-SWAP", "last": "2", "volCcy24h": "1000"},       # 2000 USDT (base coins x price)
                        {"instId": "AAA-USD-SWAP", "last": "2", "volCcy24h": "9e9"},          # coin-margined: ignored
                        {"instId": "AAA-USDT-240628", "last": "2", "volCcy24h": "9e9"}]       # dated future: ignored
                       + filler(60, lambda b, v: {"instId": f"{b}-USDT-SWAP", "last": "1", "volCcy24h": str(v)})),
        "bitget.com": Resp(body={"data": [{"symbol": "AAAUSDT", "usdtVolume": "3000", "quoteVolume": "1"},
                                          {"symbol": "USDCUSDT", "usdtVolume": "9e12"},
                                          {"symbol": "TSLAUSDT", "usdtVolume": "9e12"}]
                                 + filler(60, lambda b, v: {"symbol": f"{b}USDT", "usdtVolume": str(v)})}),
        "gateio.ws": Resp(body=[{"contract": "AAA_USDT", "volume_24h_quote": "4000"},
                                {"contract": "BBB_USDT", "volume_24h_quote": "500"},
                                {"contract": "AAA_USDT_20250101", "volume_24h_quote": "9e9"}]),
        "bybit.com": Resp(status=403),
        "binance.com": Resp(status=451),
    }
    routes.update(over)
    return Routed(routes)


def test_exchanges_provider_sums_usdt_perp_turnover_across_venues_futures_only():
    p = ExchangesFuturesProvider(session=exchange_world(), sleep=lambda s: None)
    rk = p.fetch(NOW)
    by = {a["symbol"]: a for a in rk.assets}
    assert by["AAA"]["volume_usd"] == 2000 + 3000 + 4000          # OKX x price + Bitget + Gate; coin-margined/dated ignored
    assert "USDC" not in by and "TSLA" not in by                  # stablecoin / known stock never ranked
    assert by["BBB"]["volume_usd"] == 500 and by["AAA"]["exchanges"] == 3
    assert rk.coverage["exchanges_ok"] == ["okx", "bitget", "gate"] and rk.coverage["exchanges_failed"] == ["bybit", "binance"]
    assert rk.coverage["volume_scope"] == "futures" and rk.coverage["verification"] == "symbol"
    assert rk.provider == "Multi-exchange futures" and rk.mexc_markets == {}
    assert any("instType=SWAP" in u or "okx.com" in u for u in p.session.calls)


def test_exchanges_provider_needs_two_venues_and_survives_one_garbage_venue():
    only_one = {"gateio.ws": Resp(status=500), "bitget.com": Resp(status=500)}
    with pytest.raises(GlobalVolumeError, match="only 1 of 5 exchanges answered"):
        ExchangesFuturesProvider(session=exchange_world(**only_one), sleep=lambda s: None).fetch(NOW)
    garbage = exchange_world(**{"gateio.ws": Resp(body={"oops": 1}), "bybit.com": Resp(body=["x", 3, None])})
    rk = ExchangesFuturesProvider(session=garbage, sleep=lambda s: None).fetch(NOW)
    assert rk.coverage["exchanges_ok"] == ["okx", "bitget"] and "gate" in rk.coverage["exchanges_failed"]


def test_exchanges_provider_matches_prefix_contracts_and_has_no_key_requirement():
    routes = exchange_world(**{"gateio.ws": Resp(body=[{"contract": "1000PEPE_USDT", "volume_24h_quote": "7000"}])})
    p = ExchangesFuturesProvider(session=routes, sleep=lambda s: None)
    assert p.available() == (True, "")
    by = {a["symbol"]: a for a in p.fetch(NOW).assets}
    assert by["PEPE"]["volume_usd"] == 7000


# ---- HTTP behaviour --------------------------------------------------------------------------------------
def test_http_errors_become_clean_errors_and_never_leak_the_key(caplog):
    for status, text in ((401, "invalid"), (403, "invalid"), (402, "limit")):
        with pytest.raises(GlobalVolumeError, match=text) as ei:
            provider(CoinGeckoProvider, [Resp(status=status)], api_key=SECRET).fetch(NOW)
        assert SECRET not in str(ei.value)
    with pytest.raises(GlobalVolumeError, match="not valid JSON"):
        provider(CoinGeckoProvider, [Resp(bad_json=True)]).fetch(NOW)
    with pytest.raises(GlobalVolumeError, match="HTTP 404"):
        provider(CoinGeckoProvider, [Resp(status=404)]).fetch(NOW)
    assert SECRET not in caplog.text


def test_429_and_5xx_are_retried_with_backoff_then_give_up_and_network_errors_too():
    import requests
    waits = []
    p = CoinGeckoProvider(session=Session([Resp(status=429, headers={"Retry-After": "7"}), Resp(status=503), Resp(body=gecko_rows())]),
                          sleep=waits.append)
    assert p.fetch(NOW).assets and len(p.session.calls) == 3 and waits[0] == 7.0
    q = CoinGeckoProvider(session=Session([Resp(status=429)]), sleep=lambda s: None, max_retries=2)
    with pytest.raises(GlobalVolumeError, match="gave up after 3 attempts"):
        q.fetch(NOW)
    n = CoinGeckoProvider(session=Session([requests.ConnectionError("boom")]), sleep=lambda s: None, max_retries=1)
    with pytest.raises(GlobalVolumeError, match="ConnectionError"):
        n.fetch(NOW)


# ---- provider chain --------------------------------------------------------------------------------------
class Boom(Exception):
    pass


class StubProvider:
    def __init__(self, name, ranking=None, error=None, key_ok=True):
        self.name, self.ranking, self.error, self.key_ok, self.calls = name, ranking, error, key_ok, 0

    def available(self):
        return (True, "") if self.key_ok else (False, f"{self.name.upper()}_KEY is not set")

    def fetch(self, now=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.ranking


def fb_ranking(label="CoinGecko", scope="futures", syms=("BTC", "ETH")):
    assets = [{"crypto_id": 10_000_000 + i, "symbol": s, "volume_usd": 1e9 / (i + 1), "markets": 1, "exchanges": 1, "rank": i + 1}
              for i, s in enumerate(syms)]
    return GlobalRanking(NOW.isoformat(), label, ["e"], assets, {},
                         {"verification": "symbol", "volume_scope": scope, "fallback": True}, len(assets), 0)


def cmc_ranking():
    a = [{"crypto_id": 1, "symbol": "BTC", "volume_usd": 5e9, "markets": 3, "exchanges": 3, "rank": 1}]
    return GlobalRanking(NOW.isoformat(), "CoinMarketCap", ["e"], a,
                         {"BTC_USDT": {"crypto_id": 1, "symbol": "BTC", "category": "perpetual", "quote": "USDT"}},
                         {"exchanges_fetched": 40}, 1, 100)


def chain(tmp_path, monkeypatch, cmc="ok", fallbacks=None, order=None):
    calls = {"cmc": 0}

    def fake_build(client, *a, **k):
        calls["cmc"] += 1
        if cmc == "fail":
            raise GlobalVolumeError("CMC credit limit reached")
        if cmc == "crash":
            raise Boom("weird")
        return cmc_ranking()
    monkeypatch.setattr("selection.build_global_ranking", fake_build)
    cfg = ScannerConfig(min_mexc_turnover_usdt=0, **({"global_providers": tuple(order)} if order else {}))
    cache = RankingCache(str(tmp_path / "rank.json"))
    stub = StubMexc([mexc("BTC"), mexc("ETH")], [tick("BTC_USDT"), tick("ETH_USDT")])
    r = ContractResolver(stub, cfg, cache, (lambda: object()) if cmc != "nokey" else None, lambda: NOW, fallbacks=fallbacks)
    return r, calls, cache


def test_cmc_success_never_touches_the_fallbacks(tmp_path, monkeypatch):
    g = StubProvider("coingecko", fb_ranking())
    r, calls, cache = chain(tmp_path, monkeypatch, fallbacks={"coingecko": g})
    assert r._refresh().provider == "CoinMarketCap" and g.calls == 0 and cache.load().provider == "CoinMarketCap"


@pytest.mark.parametrize("cmc", ["fail", "nokey", "crash"])
def test_cmc_failure_or_missing_key_falls_to_coingecko_and_is_cached(tmp_path, monkeypatch, cmc, caplog):
    g = StubProvider("coingecko", fb_ranking())
    r, _, cache = chain(tmp_path, monkeypatch, cmc=cmc, fallbacks={"coingecko": g})
    with caplog.at_level(logging.INFO):
        assert r._refresh().provider == "CoinGecko"
    assert g.calls == 1 and cache.load().provider == "CoinGecko"
    if cmc != "nokey":
        assert "FALLBACK provider coingecko" in caplog.text


def test_chain_uses_only_coingecko_after_cmc_fails(tmp_path, monkeypatch):
    g = StubProvider("coingecko", error=GlobalVolumeError("HTTP 429"))
    p = StubProvider("exchanges", fb_ranking("Multi-exchange futures"))
    r, _, _ = chain(tmp_path, monkeypatch, cmc="fail", fallbacks={"coingecko": g, "exchanges": p})
    with pytest.raises(GlobalVolumeError):
        r._refresh()
    assert g.calls == 1 and p.calls == 0


def test_provider_without_key_is_skipped_and_no_other_provider_is_used(tmp_path, monkeypatch):
    g = StubProvider("coingecko", fb_ranking(), key_ok=False)
    p = StubProvider("exchanges", fb_ranking("Multi-exchange futures", "futures"))
    r, _, _ = chain(tmp_path, monkeypatch, cmc="fail", fallbacks={"coingecko": g, "exchanges": p})
    with pytest.raises(GlobalVolumeError, match="COINGECKO_KEY is not set"):
        r._refresh()
    assert g.calls == 0 and p.calls == 0


def test_global_providers_env_order_is_respected(tmp_path, monkeypatch):
    g = StubProvider("coingecko", fb_ranking())
    p = StubProvider("exchanges", fb_ranking("Multi-exchange futures", "futures"))
    r, calls, _ = chain(tmp_path, monkeypatch, fallbacks={"coingecko": g, "exchanges": p},
                        order=["exchanges", "coingecko", "coinmarketcap"])
    assert r._refresh().provider == "Multi-exchange futures" and g.calls == 0 and calls["cmc"] == 0


def test_all_providers_failing_raises_one_error_naming_each_and_stale_cache_policy_still_applies(tmp_path, monkeypatch):
    g = StubProvider("coingecko", error=GlobalVolumeError("HTTP 429"))
    p = StubProvider("exchanges", error=GlobalVolumeError("HTTP 500"))
    r, _, _ = chain(tmp_path, monkeypatch, cmc="fail", fallbacks={"coingecko": g, "exchanges": p})
    with pytest.raises(GlobalVolumeError) as ei:
        r._refresh()
    msg = str(ei.value)
    assert "coinmarketcap: CMC credit limit reached" in msg and "coingecko: HTTP 429" in msg and "exchanges: HTTP 500" not in msg
    # an older (but not too old) cache from any provider is still used, labelled STALE, when every provider fails
    r.cache.save(fb_ranking())
    old = GlobalRanking("2026-10-27T10:00:00+00:00", "CoinGecko", ["e"], fb_ranking().assets, {}, fb_ranking().coverage, 2, 0)
    r.cache.save(old)
    rk, mode, note = r.obtain_ranking()
    assert mode == "global-stale-cache" and "STALE" in note


def test_next_refresh_tries_cmc_first_again_so_it_takes_over_when_credits_return(tmp_path, monkeypatch):
    g = StubProvider("coingecko", fb_ranking())
    r, calls, cache = chain(tmp_path, monkeypatch, cmc="fail", fallbacks={"coingecko": g})
    r._refresh()
    assert calls["cmc"] == 1
    monkeypatch.setattr("selection.build_global_ranking", lambda *a, **k: cmc_ranking())
    assert r._refresh().provider == "CoinMarketCap" and cache.load().provider == "CoinMarketCap"


# ---- selection with a fallback ranking -------------------------------------------------------------------
def test_fallback_ranking_selects_by_symbol_labels_the_source_and_keeps_mexc_filters():
    rk = fb_ranking("Multi-exchange futures", "futures", ("BTC", "PEPE", "SCAMX"))
    contracts = [mexc("BTC"), mexc("1000PEPE"), mexc("TSLA"), mexc("DOGE")]
    cfg = ScannerConfig(min_mexc_turnover_usdt=1000)
    from mexc_data import filter_mexc_contracts
    ranked, _ = filter_mexc_contracts(contracts, [tick(c["symbol"]) for c in contracts])
    final, excluded, report = select_global_contracts(rk, ranked, cfg, "cached")
    assert {c.base for c in final} == {"BTC", "1000PEPE"}                          # prefix contract matched via PEPE
    assert all(c.global_source == "Multi-exchange futures (futures 24h volume, fallback)" for c in final)
    assert all("symbol-matched" in c.ranking_note and "futures volume" in c.ranking_note for c in final)
    assert any("not in the global futures-volume ranking" in r for r in excluded)  # TSLA / DOGE absent from ranking
    assert {c.global_rank for c in final} == {1, 2}


def test_cmc_ranking_still_uses_strict_verification_even_after_fallback_code_was_added():
    rk = cmc_ranking()
    from mexc_data import filter_mexc_contracts
    contracts = [mexc("BTC"), mexc("ZZZQ")]
    ranked, _ = filter_mexc_contracts(contracts, [tick("BTC_USDT"), tick("ZZZQ_USDT")])
    final, excluded, _ = select_global_contracts(rk, ranked, ScannerConfig(min_mexc_turnover_usdt=1000))
    assert [c.base for c in final] == [c.base for c in final if c.base == "BTC"] == ["BTC"]
    assert final[0].global_source.startswith("CoinMarketCap derivatives")
    assert any("absent from CoinMarketCap" in r for r in excluded)


def test_fallback_ranking_survives_a_cache_round_trip(tmp_path):
    cache = RankingCache(str(tmp_path / "r.json"))
    cache.save(fb_ranking("CoinGecko"))
    back = cache.load()
    assert back.provider == "CoinGecko" and back.coverage["verification"] == "symbol" and back.mexc_markets == {}


def test_coinglass_provider_ranks_futures_usd_volume_and_requires_key():
    rows = [{"symbol": f"CG{i}", "volume_usd_24h": 1_000_000 * (100-i)} for i in range(60)]
    body = {"code": "0", "msg": "success", "data": rows}
    p = provider(CoinGlassProvider, [Resp(body=body)], api_key="cg-key")
    ranking = p.fetch(NOW)
    assert ranking.provider == "CoinGlass" and ranking.assets[0]["symbol"] == "CG0"
    assert ranking.coverage["volume_scope"] == "futures"
    assert p.session.calls[0]["headers"]["CG-API-KEY"] == "cg-key"


def test_coinglass_provider_rejects_missing_key_or_missing_volume():
    p = provider(CoinGlassProvider, [Resp(body={"code": "0", "data": [{"symbol": "BTC"}]})])
    assert p.available() == (False, "COINGLASS_API_KEY is not set")
    p = provider(CoinGlassProvider, [Resp(body={"code": "0", "data": [{"symbol": "BTC"}]})], api_key="cg-key")
    with pytest.raises(Exception, match="usable assets|usable assets returned"):
        p.fetch(NOW)


# ---- config / wiring -------------------------------------------------------------------------------------
def test_global_providers_config_default_order_custom_order_dedupe_and_validation():
    assert ScannerConfig.from_env({}).global_providers == ("coinmarketcap", "coingecko")
    assert ScannerConfig.from_env({"GLOBAL_PROVIDERS": " Coingecko ,coingecko "}).global_providers == ("coingecko",)
    for spot_based in ("coinpaprika", "coinlore"):          # spot / total-volume rankings were removed on purpose
        with pytest.raises(ConfigError, match="unknown provider"):
            ScannerConfig.from_env({"GLOBAL_PROVIDERS": spot_based})
    with pytest.raises(ConfigError, match="unknown provider"):
        ScannerConfig.from_env({"GLOBAL_PROVIDERS": "coinmarketcap,binance"})


def test_build_fallback_providers_reads_keys_from_the_environment_only():
    fb = build_fallback_providers(("coinmarketcap", "coingecko"), {"COINGECKO_API_KEY": f" {SECRET} ", "COINGLASS_API_KEY": "cg-key"})
    assert set(fb) == {"coingecko"}  # CMC is primary; CoinGecko is the only fallback
    assert fb["coingecko"]._key == SECRET and repr(fb["coingecko"]).count(SECRET) == 0


def test_scanner_builds_the_chain_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("COINGECKO_API_KEY", SECRET)
    monkeypatch.setenv("COINMARKETCAP_API_KEY", "cmc-key")
    r = scanner._build_resolver(object(), ScannerConfig(ranking_cache_path=str(tmp_path / "x.json")))
    assert r.cmc_factory is not None and set(r.fallbacks) == {"coingecko"}
    monkeypatch.delenv("COINMARKETCAP_API_KEY")
    r = scanner._build_resolver(object(), ScannerConfig(ranking_cache_path=str(tmp_path / "x.json")))
    assert r.cmc_factory is None and "coingecko" in r.fallbacks


def test_workflows_pass_only_cmc_and_coingecko_secrets():
    for wf in ("scanner.yml", "ranking-refresh.yml"):
        text = (ROOT / ".github" / "workflows" / wf).read_text()
        assert "secrets.COINGECKO_API_KEY" in text and "secrets.COINMARKETCAP_API_KEY" in text
        assert "COINGLASS_API_KEY" not in text
    env_text = (ROOT / ".env.example").read_text()
    assert "COINGECKO_API_KEY=" in env_text and "COINGLASS_API_KEY" not in env_text


def test_provider_code_never_passes_the_key_to_logging_or_error_text():
    for line in (ROOT / "providers.py").read_text().splitlines():
        if "self._key" in line:
            assert "log." not in line and "raise" not in line and "f\"" not in line, line
