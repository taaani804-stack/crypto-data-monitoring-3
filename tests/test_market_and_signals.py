"""Market filtering, top-200 global ranking, entry/SL/TP validation and Telegram message format."""
import math

import pytest
import requests

from mexc_data import non_crypto_reason, select_top_contracts, select_top_symbols
from models import ContractInfo, Signal
from short_signals import SignalRejected, risk_reward, round_to_tick, tick_size, validate_short
from telegram_bot import TelegramCredentials, TelegramSender, format_signal_message, display_pair


def _c(base, quote="USDT", settle="USDT", future_type=1, state=0, **extra):
    d = {"symbol": f"{base}_{quote}", "baseCoin": base, "quoteCoin": quote, "settleCoin": settle,
         "futureType": future_type, "state": state, "priceUnit": 0.01, "priceScale": 2}
    d.update(extra)
    return d


def _t(sym, amount, last="1.0"):
    return {"symbol": sym, "lastPrice": last, "amount24": str(amount)}


# ---- A. market filtering -----------------------------------------------------------------------
def test_only_usdt_margined_crypto_perpetuals_selected():
    contracts = [
        _c("BTC"), _c("ETH"),
        _c("COINM", settle="BTC"),                        # coin-margined
        _c("SPOTLIKE", quote="BTC", settle="BTC"),        # not a USDT contract
        _c("QTR", future_type=2),                         # delivery contract
        _c("DEAD", state=3),                              # delisted / offline
        _c("PAUSED", state=4),
        _c("HID", isHidden=True),
        _c("TSLA"), _c("NVDA"),                           # stocks (denylist)
        _c("XYZ", displayNameEn="XYZ STOCK PERPETUAL"),   # stock by metadata keyword
        _c("OPN", conceptPlate=["mc-trade-zone-forex"]),  # forex plate
        _c("HALT", type=2),                               # market-closed class
        _c("USDC"), _c("ETH3L"),                          # stablecoin / leveraged
    ]
    tickers = [_t(c["symbol"], 100) for c in contracts]
    sel, stats = select_top_contracts(contracts, tickers, 100)
    assert sorted(c.symbol for c in sel) == ["BTC_USDT", "ETH_USDT"]
    assert stats.discovered == len(contracts) and stats.eligible == 2 and stats.selected == 2
    assert sum(len(v) for v in stats.excluded.values()) == len(contracts) - 2


def test_spot_or_unknown_ticker_symbols_are_ignored():
    contracts = [_c("AAA")]
    tickers = [_t("AAA_USDT", 10), _t("AAAUSDT", 9e9), _t("GHOST_USDT", 9e9)]
    assert select_top_symbols(contracts, tickers) == ["AAA_USDT"]


def test_top_100_cap_and_volume_order():
    contracts = [_c(f"C{i:03d}") for i in range(150)]
    tickers = [_t(f"C{i:03d}_USDT", 1000 + i) for i in range(150)]
    sel, stats = select_top_contracts(contracts, tickers, 100)
    assert len(sel) == 100 and stats.eligible == 150 and stats.selected == 100
    assert sel[0].symbol == "C149_USDT" and sel[-1].symbol == "C050_USDT"
    assert [c.turnover_24h for c in sel] == sorted((c.turnover_24h for c in sel), reverse=True)


def test_fewer_than_100_available_does_not_crash():
    sel, stats = select_top_contracts([_c("AAA"), _c("BBB")], [_t("AAA_USDT", 5), _t("BBB_USDT", 6)], 100)
    assert [c.symbol for c in sel] == ["BBB_USDT", "AAA_USDT"] and stats.selected == 2


def test_precision_is_carried_from_contract_detail():
    sel, _ = select_top_contracts([_c("AAA", priceUnit=0.0001, priceScale=4)], [_t("AAA_USDT", 5)], 5)
    assert sel[0].price_unit == 0.0001 and sel[0].price_scale == 4


def test_extra_excluded_bases_env_extension():
    sel, _ = select_top_contracts([_c("AAA"), _c("BBB")], [_t("AAA_USDT", 5), _t("BBB_USDT", 6)], 5, extra_excluded=["bbb"])
    assert [c.symbol for c in sel] == ["AAA_USDT"]


def test_gold_token_is_crypto_but_plain_gold_is_not():
    assert non_crypto_reason(_c("XAUT"), "XAUT") is None
    assert non_crypto_reason(_c("GOLD"), "GOLD") is not None


# ---- D. entry / SL / TP ------------------------------------------------------------------------
def _sig(entry=100.0, sl=105.0, tp=95.7, direction="SELL"):
    return Signal(direction, 60, 1_760_000_000_000, entry, sl, tp, 1.0, acc_top=101.5, acc_bottom=98.5,
                  manip_extreme=104.0, manip_atr_mult=0.6, acc_length=40)


CI = ContractInfo("AAA_USDT", "AAA", 1.0, 0.1, 1)


def test_valid_short_is_accepted_and_rounded_to_tick():
    s = validate_short(_sig(100.04, 105.06, 95.74), CI)
    assert (s.entry_price, s.stop_loss, s.take_profit) == (100.0, 105.1, 95.7)
    assert s.stop_loss > s.entry_price > s.take_profit
    assert risk_reward(s) == pytest.approx((100.0 - 95.7) / (105.1 - 100.0))


@pytest.mark.parametrize("entry,sl,tp", [
    (100, 99, 95), (100, 105, 101), (100, 100, 95),  # wrong side / equal
    (math.nan, 105, 95), (100, math.inf, 95), (100, 105, 0), (100, 105, -3), (0, 105, 95),
])
def test_invalid_levels_are_rejected(entry, sl, tp):
    with pytest.raises(SignalRejected):
        validate_short(_sig(entry, sl, tp), CI)


def test_rounding_that_collapses_levels_is_rejected():
    with pytest.raises(SignalRejected):
        validate_short(_sig(100.0, 100.04, 99.0), CI)  # SL rounds onto entry at tick 0.1


def test_tick_helpers():
    assert tick_size(None) is None
    assert round_to_tick(1.23456, tick_size(ContractInfo("A", "A", 1, 0.001, 3))) == 1.235
    assert round_to_tick(5.5, None) == 5.5
    assert float(tick_size(ContractInfo("A", "A", 1, 0.0, 2))) == 0.01


# ---- G. Telegram -------------------------------------------------------------------------------
def test_message_has_only_requested_compact_fields():
    sig = validate_short(_sig(), CI)
    text = format_signal_message(sig, "BTC_USDT", "4h", 4 * 3_600_000, 1_760_003_600_000, CI)
    for needle in ["AAA | SHORT", "Entry:", "<code>100.0</code>", "SL:", "<code>105.0</code>",
                   "TP:", "<code>95.7</code>", "Ranking:", "N/A", "Timeframe:", "4H"]:
        assert needle in text, needle
    assert len(text.splitlines()) == 6
    for unwanted in ["Risk/Reward", "Signal generated", "Latest closed candle", "Data quality", "Liquidity sweep"]:
        assert unwanted not in text


def test_message_shows_global_rank_only_not_extra_ranking_details():
    ci = ContractInfo("BTC_USDT", "BTC", 5e8, 0.1, 1, spread_pct=0.01, name="Bitcoin", crypto_id=1, global_rank=3,
                      global_volume_usd=12_340_000_000.0, global_source="CoinMarketCap derivatives, 40 exchanges",
                      ranking_ts="2026-10-28T00:10:00+00:00", ranking_note="latest successful ranking, 5.0h old")
    text = format_signal_message(validate_short(_sig(), ci), "BTC_USDT", "4h", 4 * 3_600_000, 0, ci)
    assert "Ranking:</b> #3" in text
    assert "Bitcoin" not in text and "$12.34B" not in text and "CoinMarketCap" not in text


def test_optional_extra_take_profits_do_not_change_compact_message():
    sig = validate_short(_sig(), CI)
    text = format_signal_message(sig, "A_USDT", "4h", 4 * 3_600_000, 0, CI, extra_tp_rr=(1.5, 2.0))
    assert "TP:</b>" in text and "Take Profit 2" not in text and "Take Profit 3" not in text


def test_compact_message_stays_short_even_if_old_explanation_would_be_long(monkeypatch):
    import telegram_bot
    monkeypatch.setattr(telegram_bot, "setup_lines", lambda s, f: {"structure": "s", "sweep": "x" * 6000, "confirmation": "c"})
    text = format_signal_message(validate_short(_sig(), CI), "A_USDT", "4h", 4 * 3_600_000, 0, CI)
    assert len(text) < 300 and "…" not in text


def test_prices_use_contract_precision():
    ci = ContractInfo("A_USDT", "A", 1, 0.0001, 4)
    sig = validate_short(_sig(1.5, 1.6, 1.4), ci)
    assert "<code>1.5000</code>" in format_signal_message(sig, "A_USDT", "1h", 3_600_000, 0, ci)


def test_long_sweep_line_is_truncated_not_the_prices(monkeypatch):
    import telegram_bot
    monkeypatch.setattr(telegram_bot, "setup_lines", lambda s, f: {"structure": "s", "sweep": "x" * 6000, "confirmation": "c"})
    text = format_signal_message(validate_short(_sig(), CI), "A_USDT", "1h", 3_600_000, 0, CI)
    assert len(text) < 300 and "<code>105.0</code>" in text and "…" not in text


class _R:
    def __init__(self, status, body=None):
        self.status_code, self._b = status, body or {}

    def json(self):
        return self._b


class _S:
    def __init__(self, results):
        self.results, self.posts = list(results), []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        return self.results.pop(0)


def test_html_parse_error_falls_back_to_plain_text_once():
    s = _S([_R(400, {"description": "Bad Request: can't parse entities"}), _R(200)])
    snd = TelegramSender(TelegramCredentials("t", "1"), session=s, sleep=lambda _x: None)
    assert snd.send_text("<b>hi</b> &amp; you") is True
    assert "parse_mode" not in s.posts[1] and s.posts[1]["text"] == "hi & you"


def test_other_400_is_failure_without_resend():
    s = _S([_R(400, {"description": "chat not found"})])
    snd = TelegramSender(TelegramCredentials("t", "1"), session=s, sleep=lambda _x: None)
    assert snd.send_text("x") is False and len(s.posts) == 1
