"""Signal window = CURRENT (forming) candle + previous 2 candles. Older setups are invalid. 5 s gap between ANY alerts."""
import pytest

import orchestrator
import scanner
from config import DEFAULT_LOOKBACK_CANDLES, ScannerConfig
from models import Candle
from po3_strategy import PO3Config
from state_store import SentSignalStore
from tests.helpers import BOUNDARY, FakeClient, FakeSender, TIMEFRAME_MS, contract, quiet_after, retime, sell_series

CFG = ScannerConfig(request_delay_seconds=0, close_buffer_seconds=0)


def run(tmp_path, k, tf, live, store=None):
    """SELL setup that is ``k`` closed candles old (k=0: the latest closed candle). ``live`` adds a quiet forming candle."""
    tf_ms = TIMEFRAME_MS[tf]
    candles = list(retime(sell_series(quiet_after(k)), tf_ms, BOUNDARY))
    now = BOUNDARY + 10_000
    if live:  # the current candle: opened at BOUNDARY, half-way through now, quiet (no new signal, nothing resolved)
        candles.append(Candle(BOUNDARY, 100.0, 100.4, 99.6, 100.0, 1.0))
        now = BOUNDARY + tf_ms // 2
    sender = FakeSender()
    store = store if store is not None else SentSignalStore(str(tmp_path / "sent.json"))
    summary = scanner.run_scan(FakeClient({tf: candles}), sender, store, CFG, PO3Config(), [contract()], now, tf,
                               sleep=lambda _s: None)
    return summary, sender


def test_default_window_is_three_candles_for_4h_and_1d():
    assert DEFAULT_LOOKBACK_CANDLES["4h"] == 3 and DEFAULT_LOOKBACK_CANDLES["1d"] == 3


@pytest.mark.parametrize("tf", ["4h", "1d"])
@pytest.mark.parametrize("k,expected", [(0, 1), (1, 1), (2, 0), (3, 0), (10, 0)])
def test_with_a_forming_candle_only_current_and_previous_two_are_valid(tmp_path, tf, k, expected):
    """Candles in the window: current (forming), latest closed (k=0), the one before it (k=1). k>=2 is too old."""
    summary, sender = run(tmp_path, k, tf, live=True)
    assert len(sender.messages) == expected and summary.sent == expected
    if expected == 0:
        assert summary.skipped_stale == 1 and summary.setups_found == 0     # seen, but invalid because it is too old


@pytest.mark.parametrize("tf", ["4h", "1d"])
def test_signal_on_the_forming_candle_itself_is_valid(tmp_path, tf):
    tf_ms = TIMEFRAME_MS[tf]
    series = retime(sell_series(), tf_ms, BOUNDARY + tf_ms)          # the sweep candle is the one still forming
    sender = FakeSender()
    summary = scanner.run_scan(FakeClient({tf: series}), sender, SentSignalStore(str(tmp_path / "s.json")), CFG, PO3Config(),
                               [contract()], BOUNDARY + tf_ms // 2, tf, sleep=lambda _s: None)
    assert len(sender.messages) == 1 and "SHORT" in sender.messages[0]


@pytest.mark.parametrize("k,expected", [(0, 1), (1, 1), (2, 1), (3, 0)])
def test_without_any_forming_candle_the_last_three_closed_candles_are_used(tmp_path, k, expected):
    summary, sender = run(tmp_path, k, "4h", live=False)
    assert len(sender.messages) == expected


def test_an_old_setup_is_never_sent_later_even_if_it_was_valid_earlier_and_not_recorded(tmp_path):
    """Valid when it was 1 candle back; two candles later it is outside the window and must not alert any more."""
    summary, sender = run(tmp_path, 1, "4h", live=True)
    assert len(sender.messages) == 1
    summary, sender = run(tmp_path / "later", 3, "4h", live=True)       # same setup, now 3 closed candles old
    assert sender.messages == []


def test_a_signal_is_still_sent_only_once_inside_the_window(tmp_path):
    store = SentSignalStore(str(tmp_path / "once.json"))
    _, first = run(tmp_path, 1, "4h", live=True, store=store)
    summary, second = run(tmp_path, 1, "4h", live=True, store=store)
    assert len(first.messages) == 1 and second.messages == [] and summary.duplicates == 1


# ---- 5-second gap between ANY two alerts, also from the 1D scan to the 4H scan ------------------------
def _both_tf_client():
    return FakeClient({tf: retime(sell_series(), TIMEFRAME_MS[tf], BOUNDARY) for tf in ("1d", "4h")})


def test_alert_gap_is_shared_between_timeframe_scans(tmp_path):
    sleeps, pace, sender = [], {"sent": False}, FakeSender()
    store = SentSignalStore(str(tmp_path / "s.json"))
    client = _both_tf_client()
    for tf in ("1d", "4h"):
        scanner.run_scan(client, sender, store, CFG, PO3Config(), [contract()], BOUNDARY + 10_000, tf,
                         sleep=sleeps.append, pacer=pace)
    assert len(sender.messages) == 2 and sleeps == [5]          # no wait before the first alert, 5 s before the second


def test_run_cycle_paces_the_first_4h_alert_after_the_last_1d_alert(tmp_path):
    sleeps, sender = [], FakeSender()
    store = SentSignalStore(str(tmp_path / "c.json"))
    result = orchestrator.run_cycle(_both_tf_client(), sender, store, CFG, PO3Config(), forced=["1d", "4h"],
                                    symbols_override=["AAA_USDT"], dry_run=False, sleep=sleeps.append,
                                    now_fn=lambda: BOUNDARY + 10_000)
    assert len(sender.messages) == 2 and 5 in sleeps
    assert [s.sent for s in result.summaries] == [1, 1]


def test_first_alert_of_a_run_is_not_delayed(tmp_path):
    sleeps = []
    store = SentSignalStore(str(tmp_path / "f.json"))
    scanner.run_scan(_both_tf_client(), FakeSender(), store, CFG, PO3Config(), [contract()], BOUNDARY + 10_000, "1d",
                     sleep=sleeps.append)
    assert sleeps == []
