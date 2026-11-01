# ICT Power Of Three → MEXC USDT-M Futures → Telegram (SHORT-only, 4H / 1D)

A **signal-only** scanner. It ports the signal logic of the TradingView indicator *"ICT Power Of Three | Flux
Charts"* (Pine v5, © fluxchart, MPL-2.0) to Python. **v2:** the coins to scan are the **top 250 cryptocurrencies by
GLOBAL futures/perpetual 24h volume** (CoinMarketCap, aggregated over many exchanges), reduced to those with a
**verified MEXC USDT-margined crypto perpetual**. They are scanned on **4H and 1D only** (each from its own
candles, MEXC data) and **SELL / SHORT alerts only** go to Telegram. Runs on GitHub Actions; no computer needed.

> It never places orders. It reads public MEXC Futures market data (no MEXC key), public-tier CoinMarketCap
> derivatives data (free API key) and calls the Telegram Bot API. Not financial advice; no signal is guaranteed.

## Global futures-volume ranking (v2)
**Provider: CoinMarketCap** (free "Basic" plan). Endpoints (documented as available on the free plan; see
`REVIEW_REPORT.md` for what was and was not verified):
* `GET /v5/exchange/derivatives/list` – derivatives exchanges + their 24h derivative volume (1 call per refresh)
* `GET /v5/exchange/derivatives/market-pairs/list/latest` – all derivative markets of ONE exchange (batch; one call
  per exchange per 1000 markets). The per-coin endpoint is deliberately not used (would need one call per coin).

**Volume metric.** For each cryptocurrency: the sum of CMC's USD-normalised `quotes[USD].volume_24h` over all its
perpetual + futures markets on the top-N (default 40) derivatives exchanges. Never mixed in: spot volume, open
interest, market cap, exchange-reported raw volumes. Each market is counted once (`market_id` de-duplication);
markets flagged `outlier_detected` or with `exclusions` are not counted; only `currency_type == "cryptocurrency"`
bases with a CMC `crypto_id` are ranked; stablecoin bases are dropped. **Coverage is partial** (top-N exchanges);
the covered share of CMC's total derivative volume is logged and cached (`coverage.fetched_volume_share_pct`).

**Selection (steps A–G).** (A–C) fetch → aggregate → rank. (D) take the top 250. (E) keep those with a MEXC
contract; in the default `strict` mode CMC itself must list that contract on MEXC as a *perpetual*, *USDT*-quoted,
*cryptocurrency* market (so `TSLA_USDT` can never pass merely because it ends in USDT). (F) MEXC-side filters:
`quoteCoin = settleCoin = USDT`, `futureType = 1` (perpetual), `state = 0`, `type = 1` (not suspended),
`typeLabel` not TradFi/stock, not hidden, no stock/forex/commodity keywords, not stablecoin/leveraged, plus a
ticker denylist as a *second* layer. (G) liquidity: MEXC 24h turnover ≥ `MIN_MEXC_TURNOVER_USDT` (1,000,000),
spread ≤ `MAX_SPREAD_PCT` (0.5 %, only if `bid1`/`ask1` exist), tick-size metadata present, one contract per asset.
The default `GLOBAL_FILL_TO_N=true` walks down the global futures ranking until up to 250 eligible MEXC USDT perpetuals are found. If fewer than 250 valid matches exist in the fetched ranking, the actual count is lower; the scanner never fabricates or weakens filters. Anything unknown or ambiguous is excluded and logged.

**Cache and schedule.** Ranking cache `state/global_ranking.json` (persisted on the `scanner-state` branch).
* Workflow **Global Volume Ranking Refresh** runs every 4 hours (`40 */4` UTC). Hourly scans only *read* the cache.
* The hourly scanner rebuilds it itself only if the cache is ≥ `GLOBAL_RANKING_REFRESH_HOURS` (4) old.
* If a refresh fails: cache ≤ `GLOBAL_RANKING_MAX_AGE_HOURS` (72) old is used and **labelled STALE** in logs and
  alerts; otherwise policy `GLOBAL_FALLBACK_POLICY`: `halt` (default: scan nothing, run fails visibly) or
  `mexc_only` (MEXC-volume ranking; alerts say "not global" and show rank/volume as *unavailable*).
* CoinMarketCap cost: roughly 1 + 40 × (a few) credits per refresh (cap `GLOBAL_MAX_CREDITS_PER_REFRESH`=400). The free Basic plan
  is about **10,000 credits/month and ~333/day**, so a 4-hourly refresh can exhaust it. That is exactly what the
  **provider fallback chain** below is for; you can also cut the cost with `GLOBAL_MAX_EXCHANGES=10`.

**Alert fields** (all real or "unavailable"): cryptocurrency, MEXC contract, global rank, global 24h futures volume,
source, ranking timestamp, timeframe, signal/closed-candle times, entry, SL, TP1 (TP2/TP3 are `n/a`: the original PO3
defines one TP; set `TP_EXTRA_RR=1.5,2.5` to add clearly-labelled R-multiple extensions), R:R, setup evidence, data quality.

## Provider fallback chain (CoinMarketCap -> CoinGecko only)

The active ranking chain uses **only these two providers**, as requested. Both must return global futures/perpetual volume data; spot volume and market-cap ranking are never substitutes. The first provider that returns a usable ranking wins. If CoinMarketCap is rate-limited, out of credits, or fails, CoinGecko is tried automatically. If both fail, the refresh fails safely rather than switching to a different ranking source. Every refresh starts at CoinMarketCap again, so it takes over when available.

| Order | Provider | Ranks by | Key (GitHub secret) |
|---|---|---|---|
| 1 | CoinMarketCap | Futures + perpetual 24h volume across supported exchanges | `COINMARKETCAP_API_KEY` |
| 2 | CoinGecko `/derivatives` | Futures + perpetual 24h ticker volume across venues | `COINGECKO_API_KEY` (Demo key recommended) |

Add these under *Settings -> Secrets and variables -> Actions*. **Do not add a CoinGlass secret**; CoinGlass and public multi-exchange backup providers are not part of the active chain. CoinGecko's free Demo API has request limits, and its derivatives endpoint/returned volume fields must be confirmed against the live API key and current API plan.

When CoinGecko is used, it does not provide CoinMarketCap's MEXC contract listing, so a MEXC contract is matched to the ranked coin by base symbol. MEXC-side filters (USDT perpetual, stock/index/commodity exclusions, liquidity and spread) still apply.

Provider responses and API plan permissions have not been live-verified by this local project test run. Check the first forced ranking refresh log to confirm which provider successfully built the ranking.

## "Why no signal?" - reading the scan log / report
Every scan prints a `SCAN SUMMARY` line ending in `phases: ...` - the PO3 phase of every checked coin right now:
`searching_range` (no tight 40-candle range yet), `in_range` (accumulation box formed), `waiting_manipulation` (price left
the box, sweep not big enough yet), `trade_open` (an earlier entry has not hit TP/SL, so **no new cycle can start**),
`just_exited`. A SHORT can only appear for coins that move from `in_range`/`waiting_manipulation` to an upside sweep.
Many `trade_open` coins (BUY entries also block the cycle) mean few possible signals. Set the repository variable
`TELEGRAM_SUMMARY=true` to receive this as one plain message per run. Synthetic-data estimate (not real prices): about
1 valid SHORT per 100 coin-windows, i.e. a handful of 4H signals and about one 1D signal per day for 250 coins; real
markets can be much quieter, and hours without a signal are normal.

## What changed in this version
| Requirement | Implementation |
|---|---|
| USDT-M crypto futures only | `mexc_data.select_top_contracts`: `quoteCoin=settleCoin=USDT`, perpetual (`futureType=1`), `state=0`, not hidden, `type=1`; excludes stablecoin/leveraged bases and **non-crypto** (stocks/commodities/indices/FX, see below). Symbols come from the live `contract/detail` call only — nothing hard-coded. |
| Top 250 eligible MEXC contracts | Global futures-volume ranking refreshes every 4 hours; ranking is walked in descending volume until up to 250 genuine MEXC USDT-margined crypto perpetuals pass verification and liquidity filters. If fewer valid matches are available in the fetched ranking, fewer are scanned. |
| SHORT only | PO3 state machine unchanged (bullish entries still run, as in Pine, because an open trade blocks the next cycle) but only `SELL` entries are alerted; `format_signal_message` and `validate_short` refuse anything else. |
| 4H / 1D | Own MEXC intervals `Hour4` / `Day1` (never resampled). The **current (forming) candle** is analysed too, so a setup can alert intrabar. |
| Signal window | A SHORT is valid only on the **current candle or the previous 2 candles** (3 candles in total) on both 4H and 1D. A setup on an older candle is skipped, never sent late. The state machine still runs over ~500 candles of history for ATR(50)/accumulation context. |
| Scheduling | GitHub Actions runs every hour at minute 00 UTC, which is also minute 00 in Pakistan (UTC+5); each run rechecks both 1D and 4H. |
| Duplicates | ID `PO3|MEXC|PAIR|TF|SELL|signal-candle-open-ms`, persisted; recorded only after Telegram confirms. |
| Message | New SHORT format (below); prices rounded to the contract tick size (`priceUnit`/`priceScale`). |

## Files
| File | Purpose |
|---|---|
| `po3_strategy.py` | Pine → Python port (logic unchanged; only extra context fields + new signal-id format) |
| `mexc_data.py` | MEXC Futures client, contract filtering, contract checks, closed-candle filter |
| `providers.py` | Fallback ranking providers (CoinGecko, multi-exchange futures) |
| `short_signals.py` | SHORT-only gate: validation, tick rounding, reason text |
| `scanner.py` | CLI + one-timeframe scan (lookback window, duplicates, Telegram) |
| `orchestrator.py` | Hourly sequential 1D → 4H cycle with no extra inter-timeframe gap |
| `telegram_bot.py` | Credentials from env, SHORT message format, safe sending (secrets never logged) |
| `state_store.py` | Atomic, lock-protected, merge-on-save state (sent signals + scan progress) |
| `config.py`, `models.py` | Settings (env-overridable), data classes |
| `scripts/state_branch.sh` | Persists state on the `scanner-state` branch (git plumbing; safe mid-run) |
| `.github/workflows/scanner.yml` | Hourly schedule + manual dispatch |
| `.github/workflows/tests.yml` | Test suite on push / PR |

## Schedule (final)
* **Cron: `0 * * * *`** — every hour at minute 00 UTC; Pakistan is UTC+5, so it is also minute 00 each hour in Pakistan.
* Every hourly run rechecks both configured production timeframes, **1D then 4H**, even if that timeframe's latest candle has not changed. The current forming candle plus the previous two candles are eligible for a new alert; alerts are paced 5 s apart across both timeframes.
* Signal IDs are persisted, so the same signal is not sent again on later hourly runs. A genuinely new setup with a different signal-candle timestamp can alert.
* Multiple Telegram alerts are sent sequentially with a **5-second gap**. The 1D and 4H scans run sequentially with no extra delay between timeframes.
* State is pushed to the `scanner-state` branch after every timeframe scan (and once more at the end).
* GitHub Actions cron is best-effort and may be delayed or skipped; verify actual scheduled runs in the Actions tab.

## Telegram message
```
━━━━━━━━━━━━━━━━━━━━
🔴 ICT PO3 — SHORT SIGNAL · 4H
━━━━━━━━━━━━━━━━━━━━
Pair: BTCUSDT
Market: USDT-Margined Perpetual Futures (MEXC)
Direction: 🔻 SELL / SHORT
Timeframe: ⏱ 4H
Signal Time (UTC): …      Candle Close (UTC): …
Entry / Stop Loss (SL) / Take Profit: TP1 …
Strategy: ICT Power of Three · Setup: Confirmed bearish PO3
Reason: <built from the real accumulation box and sweep values>
Risk/Reward: 1:0.86
```
**Take profit:** the original Pine strategy has exactly **one** TP (`RR × risk`, default 0.86) and one SL
(`5×ATR(50)`). Both are preserved, so the alert shows **TP1 only** — TP2/TP3 are *not* invented. Note the
original default has reward < risk (1:0.86); that is the indicator's default, not a choice made here.

## Setup

### 1. Create the Telegram bot and find your chat ID
1. In Telegram open **@BotFather** → `/newbot` → copy the **token** (treat it like a password; never paste it in chats, issues or code).
2. Send any message to your new bot (or add it to your group/channel and post once).
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read `message.chat.id`
   (groups/channels are negative numbers, e.g. `-100123...`). For a channel the bot must be an admin.

### 2. Put the project in GitHub
Web UI: create a new repository (see the *Public vs private* note below), then **Add file → Upload files**,
drag in the project contents (keep the folder structure; make sure `.github/workflows/` and `scripts/` are included)
and **Commit changes** to `main`.

Or with git:
```bash
cd po3-mexc-scanner
git init -b main
git add .
git commit -m "Add PO3 MEXC scanner"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### 3. Add the repository secrets
Get a free key at <https://pro.coinmarketcap.com> (Basic plan). Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from BotFather |
| `TELEGRAM_CHAT_ID` | chat id from step 1 |
| `COINMARKETCAP_API_KEY` | your CoinMarketCap API key (primary global ranking) |
| `COINGECKO_API_KEY` | free CoinGecko *Demo* key (first fallback; optional but recommended) |

Without the CoinMarketCap key the default policy halts (nothing is scanned). To run without it set
`GLOBAL_RANKING_ENABLED=false` (MEXC-volume ranking, labelled as not global).

### 4. Allow the workflow to push the state branch
Repo → **Settings → Actions → General → Workflow permissions → "Read and write permissions"** → Save.
(The workflow also declares `permissions: contents: write`, but an org/repo default of read-only can override it.)

### 5. Enable and test
1. **Actions** tab → enable workflows if prompted.
2. **PO3 SHORT Scanner → Run workflow** with **telegram_test = true** → test message arrives in Telegram.
3. **Global Volume Ranking Refresh → Run workflow** (force = true). Log must show `Global ranking refreshed: N assets`.
   Then run **PO3 SHORT Scanner** with **dry_run = true**, **timeframes = all** → alerts (if any) are *printed in the log*, nothing sent, no state stored. Check the log for the `Contracts:` line and the `excluded (…)` lines.
4. Run with defaults (`timeframes = auto`) or wait for the hourly schedule.

### Verify SHORT-only and no repeats
* Telegram only ever shows `🔴 ICT PO3 — SHORT SIGNAL`; the log line `SCAN SUMMARY` shows `bullish_ignored=N` (seen, never sent).
* Run the workflow twice with `timeframes = all`: the second run logs `duplicates_suppressed=N` and sends nothing new.
  Auto runs log `… not due (latest closed candle … already scanned)`.
* State: branch `scanner-state` → `sent_signals.json` (`sent` ids end with `|SELL|<candle-ms>`, `scans` = last scanned candle per timeframe).

### Local usage
```bash
pip install -r requirements-dev.txt
python -m pytest -q
python scanner.py --refresh-ranking --force               # needs COINMARKETCAP_API_KEY; builds the cache
python scanner.py --list-contracts                        # final contracts + EVERY exclusion with its reason
python scanner.py --dry-run --timeframes all --gap-seconds 0   # alerts printed, nothing sent or stored
python scanner.py --dry-run --symbols BTC_USDT,ETH_USDT --timeframes all --gap-seconds 0  # skips the ranking
python scanner.py --inspect BTC_USDT --timeframes 4h      # every PO3 entry (both directions) in the fetched window
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python scanner.py --telegram-test
python scanner.py                                         # auto: scans whatever is due
```

## How the port maps to the Pine script
Defaults are identical to the Pine inputs: Algorithm Mode *Small Manipulation* (accumulation length 40, max accumulation
size 5×ATR, min manipulation 0.6×ATR), Breakout *Wick*, TP/SL *Dynamic*, Risk *Normal* (SL = 5×ATR), RR 0.86, ATR length 50,
accumulation expansion 0.5×ATR, Fixed-mode 0.3 % TP / 0.4 % SL. All Pine inputs are available on `PO3Config`.

State machine (kept in Pine order, including **same-bar cascading** — accumulation, breakout, manipulation and entry can all occur in one candle, because the Pine blocks are sequential `if`s):
1. *Waiting For Accumulation*: `highest(40) − lowest(40) ≤ ATR × 5` → box = high/low ± 0.5×ATR.
2. *Accumulation End*: wick (or close) outside the box → *Waiting For Manipulation*.
3. *Manipulation*: price beyond box ± 0.6×ATR. **Sweep of highs → SELL; sweep of lows → BUY.**
4. Entry at that candle's **close**; SL/TP set by ATR (Dynamic) or % (Fixed).
5. Exit simulation on later candles (TP first, then SL; if both hit in one candle **SL wins**, as in Pine). A new PO3 cycle only starts after the previous trade exited — exactly like Pine — which is why exits are simulated even though only entries are alerted.

## Pine-to-Python differences (read this)
**The port has been unit-tested against hand-computed values, but it has NOT been compared with live TradingView output.** Do not assume exact equivalence until you check it (procedure below). Known differences:

1. **History window.** TradingView runs the script on up to ~4800 bars of chart history (`maxDistanceToLastBar`); the scanner fetches the last **500** candles per symbol/timeframe. The state machine is path-dependent (it only resets after a trade exits), so the cycle in progress on the first fetched candle can differ from TradingView's, and a signal can appear/disappear until both sides resynchronise. Also, a 500-candle window slides each run.
2. **ATR seeding.** Pine's `ta.atr` is seeded over the whole chart history; here over the first 50 fetched candles. The difference decays (≈ 0.98ⁿ) and is negligible after a few hundred candles, but not zero.
3. **Data source.** Candles come from MEXC Futures public REST API. TradingView's MEXC feed should match, but candle boundaries/volume quirks or a different exchange selected on your chart will change results.
4. **Alert filter (added, not in Pine).** Pine alerts fire live on bar close. The scanner scans history each hourly run, so it only alerts SELL entries inside the recent-candle window (4H/1D: 3) that have not already hit TP/SL. This is a delivery filter; it does not change PO3 rules.
5. **Not ported:** zones/boxes/labels, the backtest dashboard, and the Pine TP-hit / SL-hit alerts (alerts are SHORT entries only). The exit logic is still simulated internally.
6. **Fixed-mode and floating point.** Same formulas as Pine, evaluated in IEEE double precision; results at exactly-equal boundaries could theoretically differ by 1 ulp.

### Verify against TradingView
```bash
python scanner.py --inspect BTC_USDT --timeframes 4h
```
prints every signal PO3 finds in the fetched window (candle time UTC, entry, SL, TP, outcome). Load the same symbol on
TradingView (MEXC feed, same timeframe, indicator defaults, chart timezone UTC) and compare the Buy/Sell labels
for recent candles. Differences near the *start* of the window are expected (difference 1); differences in the most recent days are worth investigating.

## Closed candles, lookback and look-ahead
A candle counts as closed if `open + interval + 5 s ≤ MEXC server time` (runner clock as fallback). Closed candles are
evaluated on closed data only; the **forming candle is then added** so an intrabar setup on it can alert immediately.
Because that candle is not finished, such a setup can disappear before the candle closes (it is still alerted once and
never re-sent). The *search for a valid entry* is limited to the current candle + previous 2 candles (4H, 1D); entries whose
TP/SL was already reached on later closed candles are skipped. If a run is delayed so long that the signal candle falls
out of the window, that signal is skipped rather than sent late.

## Non-crypto filtering (please verify once)
MEXC lists US-stock and other TradFi perpetuals (e.g. TSLA, NVDA) next to crypto, also USDT-settled. The public
`contract/detail` payload has no documented "asset class" field, so exclusion is layered: contract `type != 1`,
`isHidden`, keywords (stock/equity/forex/commodity/ETF) in `displayNameEn`/`baseCoinName`/`conceptPlate`, and a
denylist of well-known equity/commodity/index tickers. Tokenised gold (XAUT, PAXG) is a crypto token and stays
eligible. **Every exclusion is logged with its reason**; add anything that slips through with the env var
`EXTRA_EXCLUDED_BASES=ABC,XYZ` (or a repo/workflow env entry).

## Configuration (environment variables, all optional)
`TOP_N_SYMBOLS` (250), `TELEGRAM_SUMMARY` (false; true = one plain status message per run), `CANDLE_LIMIT` (500, max 1000), `CANDLE_CLOSE_BUFFER_SECONDS` (5),
`LOOKBACK_CANDLES_1H/4H/1D` (7/3/3 = candles INCLUDING the current one; production scans 4H and 1D only), `SCAN_GAP_SECONDS` (0), `STATE_RETENTION_DAYS` (14), `STATE_PATH`,
`EXTRA_EXCLUDED_BASES`, `STATE_SYNC_COMMAND` (set by the workflow).

## Differences added by this version
See the list in the strategy section above (history window, ATR seeding, data source). Added by this version:
the lookback window is counted in candles (7/3/3) instead of minutes; duplicates use the v2 id (old v1 ids are still honoured).

## Known limitations / not verified
* **Not tested against live CoinMarketCap, MEXC, Telegram or GitHub Actions from the build environment** (network restricted). HTTP
  behaviour is tested against a local fake server that mimics the documented response shapes; the first real
  `dry_run` is the true integration test.
* MEXC's `Hour4`/`Day1` interval names and `priceUnit`/`priceScale`/`type` fields follow MEXC's public docs; confirm in the first dry-run log.
* Which UTC hour MEXC's **daily** candle closes is not hard-coded; the scheduler follows the candle timestamps, so the 1D scan fires whenever the exchange closes the day.
* Equivalence with TradingView is unverified.
* GitHub cron is best-effort (can be delayed/dropped); public repos pause schedules after 60 days of inactivity. The scanner checks 1D and 4H each hourly run; GitHub Actions cron is best-effort and can be delayed.
* No TP/SL-hit notifications, no position tracking, no order execution — by design.

## v2 limitations
* CoinMarketCap free-plan access is *documented*, not live-verified here. Plan limits/field semantics (`outlier_detected`, `exclusions`) can change; the first `--refresh-ranking` run is the real check.
* Global volume is a **partial** aggregate (top-N exchanges, top markets each) – not the entire global market.
* `strict` verification needs CMC to list MEXC's derivatives; if it does not, nothing passes (use `CRYPTO_VERIFICATION=symbol`, weaker).
* CMC could label a *tokenised stock* as a cryptocurrency; MEXC `typeLabel`/keyword/denylist filters are the backstop. Inspect `--list-contracts`.
* MEXC's docs now show `/contract/detail/country`; the client tries `/contract/detail` first and falls back to it.
