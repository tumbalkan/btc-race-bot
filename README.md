# Slow-Trend Regime Bot — BTC Race 24h

[![make.fun](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/tumbalkan/btc-race-bot/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/tumbalkan/btc-race-bot/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)

A trading bot for the make.fun bounty
[Race a BTC trading bot live for 24 hours](https://make.fun/bounty/btc-race-24h-20261118-1444):
it trades cbBTC/USDC on 1-minute bars with $1,000 of paper money, long only,
no leverage, through the `agent-fun/bars-replay/v1` protocol (NDJSON on
stdin/stdout). Pure Python 3.13, standard library only, fully deterministic.

## The idea: trade the hour, not the minute

The dev window's 1-minute bars are nearly a random walk: per-bar returns have
a 6.7 bps standard deviation against a ~36 bps full-size round-trip cost
(4 bps fee + 2 bps slippage + ~12 bps price impact per leg at the dev
window's ~$844k pool depth). Any fast signal churns itself to death — an
EMA 8/21 cross tested during development lost **$148 in 80 trades**, and a
1-minute ADX never rose above 20 all day, so there was never a "trending"
minute to follow. The only moves that pay are the multi-hour drifts. This
bot therefore works on an hourly timescale and holds cash the rest of the
time.

## Strategy

**Trend regime — the only regime it trades.** A slow trend is confirmed when
two signals agree:

1. **EMA(60) crosses above EMA(240)** — a 1-hour average over a 4-hour one.
   Entries are edge-triggered: only the cross bar buys, never a persisting
   condition, so a flat market cannot re-trigger entries.
2. **The 60-bar return is positive** — momentum confirmation. A cross that
   arrives while price is still falling over the last hour is not a trend.

**Ranging markets are deliberately not traded.** Mean reversion was measured
and rejected on cost grounds: the dev window's chop spans ~0.5%, so a
band-to-mid swing pays ~25 bps against ~24–36 bps of round-trip cost, with a
fat left tail when the range breaks (the day ended with a −1.07% hour). The
bot's regime decision is to hold cash through chop rather than pay to trade
noise.

**Exits are as slow as entries** — four of them, whichever fires first:

- EMA(60) crosses back below EMA(240),
- a **1% trailing stop** from the peak since entry,
- a **1.5% hard stop** from the entry fill,
- a **momentum exit** when the 60-bar return falls below −0.2% (the trend
  has broken).

The trailing and momentum exits do the real work: on the dev window they
banked the mid-day recovery at $64,811 instead of riding it back down to
$64,424.

**Risk-based sizing.** Each trade risks 1.5% of equity against a stop of
`max(1.5%, 3× hourly volatility)`, so size is 100% in calm markets and
shrinks as volatility rises (floor 50%). Sizing follows volatility, not
conviction.

**Drawdown guard.** If equity falls 5% below its peak, new entries halt for
120 bars (2 hours); exits keep working during the halt. It never fired on
the dev window (max drawdown 1.07%) — it is insurance for the race.

**Rejected orders are retried.** Fills arrive one bar after the decision, so
if a buy was sent but no position appears on the next batch, it is sent
again while the trend still holds; likewise a sell while the position
remains. Every signal is evaluated on closed bars only.

## Dev-window result

Tested with a local replay harness that runs the real `main.py` over the
frozen 1,440-bar dev window with the official fill model (fee, slippage,
price impact, next-bar-open fills, flatten at the last close):

| Metric | Value |
| --- | --- |
| Net PnL | **+$6.48** |
| Trades | 4 (2 round trips) |
| Max drawdown | 1.07% |
| Protocol errors | 0 over 1,440 bars |
| Determinism | Two runs, byte-identical action logs |

Trade 1 bought the mid-day recovery at $64,052.60 and sold at $64,811.53
(+$11.04). Trade 2 entered a chop wiggle at $64,755.68 and was stopped by
the momentum exit at $64,515.29 (−$4.56). The design accepts losing chop
trades as the cost of catching real trends — the edge is that a winning
trend pays roughly twice what a losing wiggle costs.

Neighbouring parameters (EMA 60/180, 90/240, 60/300; momentum thresholds
−0.15% to −0.40%) all stayed positive on the dev window, so the result is a
plateau, not a fitted spike.

## Files

- `main.py` — the bot (stdlib only, no clock, no network, no files, no
  randomness; the script's directory is off `sys.path` under `-P`, so it
  adds `vendor/` itself).
- `vendor/makefun_bars.py` — the kit's Python SDK shim, byte-for-byte.

## Run it

```sh
python3 -P -s main.py   # speaks agent-fun/bars-replay/v1 on stdin/stdout
```

## License

MIT.
