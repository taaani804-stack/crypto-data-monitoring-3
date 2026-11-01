"""250-pair default, phase diagnostics, optional Telegram scan report, extended non-crypto denylist."""
import scanner
from config import ScannerConfig
from mexc_data import KNOWN_NON_CRYPTO_BASES, non_crypto_reason
from orchestrator import run_cycle, scan_report_text
from po3_strategy import PHASE_LABELS, po3_phase, run_po3
from state_store import SentSignalStore
from tests.helpers import (BOUNDARY, FakeClient, FakeSender, TIMEFRAME_MS, base_series, buy_series, candle, contract,
                           flat, quiet_after, retime, sell_series)

CFG = ScannerConfig(request_delay_seconds=0, close_buffer_seconds=0)


def test_default_universe_is_250_pairs_and_env_can_change_it():
    assert ScannerConfig.from_env({}).top_n == 250
    assert ScannerConfig.from_env({"TOP_N_SYMBOLS": "120"}).top_n == 120


def test_phase_labels_follow_the_pine_state_machine():
    assert po3_phase(flat(0, 30)) == "searching_range"                    # not enough candles for ATR(50)/40-range yet
    assert po3_phase(base_series()) == "in_range"                         # tight 40-candle box formed, no breakout
    broke_out = base_series() + [candle(60, 100, 101.2, 100, 100.5)]      # leaves the box, but sweep < 0.6 ATR beyond it
    assert po3_phase(broke_out) == "waiting_manipulation"
    assert po3_phase(sell_series(quiet_after(2))) == "trade_open"         # SELL entry taken, TP/SL not reached
    tp_hit = sell_series([candle(61, 100, 100.5, 90, 91)])                # price falls through TP -> exited
    assert po3_phase(tp_hit) == "just_exited"
    assert set(PHASE_LABELS.values()) >= {"in_range", "trade_open", "just_exited", "waiting_manipulation"}


def test_phase_helper_does_not_change_the_signals():
    cs = sell_series(quiet_after(3))
    assert [(s.direction, s.entry_index) for s in run_po3(cs)] == [("SELL", 60)]


def test_scan_summary_counts_phases_per_coin(tmp_path):
    tf_ms = TIMEFRAME_MS["4h"]
    series = {"AAA_USDT": retime(sell_series(quiet_after(5)), tf_ms, BOUNDARY), "BBB_USDT": retime(base_series(), tf_ms, BOUNDARY)}
    client = FakeClient({"4h": lambda sym: series[sym]})
    s = scanner.run_scan(client, FakeSender(), SentSignalStore(str(tmp_path / "s.json")), CFG, scanner.PO3Config(),
                         [contract("AAA_USDT"), contract("BBB_USDT")], BOUNDARY + 10_000, "4h", sleep=lambda _x: None)
    assert s.phases == {"trade_open": 1, "in_range": 1} and "in_range=1" in s.phase_text() and s.checked == 2


def _cycle(tmp_path, summary_on):
    tf_ms = {t: TIMEFRAME_MS[t] for t in ("1d", "4h")}
    client = FakeClient({t: retime(sell_series(), tf_ms[t], BOUNDARY) for t in tf_ms})
    sender = FakeSender()
    cfg = ScannerConfig(request_delay_seconds=0, close_buffer_seconds=0, telegram_summary=summary_on)
    run_cycle(client, sender, SentSignalStore(str(tmp_path / "c.json")), cfg, forced=["1d", "4h"], symbols_override=["AAA_USDT"],
              sleep=lambda _x: None, now_fn=lambda: BOUNDARY + 10_000)
    return sender


def test_scan_report_is_off_by_default_and_one_plain_message_when_enabled(tmp_path):
    assert len(_cycle(tmp_path / "off", False).messages) == 2              # only the two SHORT alerts
    msgs = _cycle(tmp_path / "on", True).messages
    assert len(msgs) == 3
    report = msgs[-1]
    assert report.startswith("PO3 scan report") and "Futures pairs scanned: 1" in report
    assert "4H: checked 1/1" in report and "1D: checked 1/1" in report and "phases:" in report
    assert "<" not in report and "&" not in report                          # safe for Telegram HTML parse mode


def test_scan_report_never_raises_when_sending_fails(tmp_path):
    class Broken(FakeSender):
        def send_text(self, text):
            if text.startswith("PO3 scan report"):
                raise RuntimeError("telegram down")
            return super().send_text(text)
    client = FakeClient({"4h": retime(sell_series(), TIMEFRAME_MS["4h"], BOUNDARY)})
    cfg = ScannerConfig(request_delay_seconds=0, close_buffer_seconds=0, telegram_summary=True, timeframes=("4h",))
    r = run_cycle(client, Broken(), SentSignalStore(str(tmp_path / "b.json")), cfg, forced=["4h"], symbols_override=["AAA_USDT"],
                  sleep=lambda _x: None, now_fn=lambda: BOUNDARY + 10_000)
    assert r.exit_code == 0


def test_more_stock_index_and_commodity_tickers_are_excluded():
    for base in ("TSLA", "BA", "XOM", "NDX", "XAUUSD", "GLD", "MSTR", "SPY"):
        assert base in KNOWN_NON_CRYPTO_BASES and non_crypto_reason({"typeLabel": 0, "type": 1}, base)
    assert non_crypto_reason({"typeLabel": 0, "type": 1}, "BTC") is None
    assert non_crypto_reason({"typeLabel": 2, "type": 1}, "WHATEVER")        # MEXC stock label always wins
