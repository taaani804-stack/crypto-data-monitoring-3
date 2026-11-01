# Requested modifications — 2026-10-30

Applied to the previous audited v2 global-ranking scanner. PO3 strategy formulas, entry/SL/TP calculations, crypto-only USDT perpetual filtering, global-volume ranking provider, liquidity filters, Telegram formatting, and no-auto-order behavior remain unchanged.

- Production signal timeframes: 4H and 1D only; 1H removed from scheduled/normal scans.
- Every hourly GitHub Actions run scans both configured timeframes (1D, then 4H), even if their newest closed candle has not changed.
- Signal freshness window: last 3 CLOSED candles on both 4H and 1D (latest closed candle plus the previous two). Forming candles are never used.
- Telegram message spacing: 5 seconds between successfully delivered alert messages.
- Global volume ranking universe: top 250 cryptocurrency futures/perpetual assets (was 200), then verify eligible MEXC USDT-margined crypto perpetual contracts.
- Schedule: `0 * * * *` UTC, equivalent to every hour at minute 00 in Pakistan (UTC+5).
- Duplicate signal suppression and persisted state are retained.
- Tests: `198 passed` locally. Live CoinMarketCap, MEXC, Telegram and GitHub Actions behavior still needs runtime verification in the user's repository.


## Telegram message compacting update (2026-10-30)
Telegram alerts now contain exactly six concise fields: coin symbol + SHORT, Entry, SL, TP, global rank, and timeframe. Verbose explanations, global volume/source/timestamp, candle times, data quality, risk/reward, and extra TP extensions are omitted from the message. PO3 calculation and validation are unchanged.


## Ranking provider fallback chain (added) - futures only
CoinMarketCap -> CoinGecko (`/derivatives`) -> `exchanges` (OKX/Bitget/Gate/Bybit/Binance public USDT-perp tickers), in `GLOBAL_PROVIDERS`
order. Spot / total-volume providers were removed on purpose. A provider that is out of credits, fails or has no key is skipped and
the next one builds the ranking; CoinMarketCap is retried first at every refresh. Fallback rankings are matched to MEXC by base symbol.
New secret: `COINGECKO_API_KEY` (optional).

## Universe, window, pacing, diagnostics (added)
* `TOP_N_SYMBOLS` default 200 -> **250**; larger stock/index/commodity denylist (futures crypto only).
* Signal window = current (forming) candle + previous 2 candles (4H and 1D). Older setups are invalid.
* The 5-second gap between Telegram alerts is now shared across timeframes (1D -> 4H as well).
* Each scan logs the PO3 phase of every coin (`phases:`); `TELEGRAM_SUMMARY=true` sends one status message per run.
