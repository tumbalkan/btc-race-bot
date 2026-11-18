"""Slow-trend regime bot for make.fun btc-race-24h-20261118-1444.

Trades cbBTC/USDC on 1-minute bars, long only, $1,000 paper money, speaking
agent-fun/bars-replay/v1 through the vendored SDK shim (vendor/makefun_bars.py).

Design (measured on the dev window, not fitted to it):

  The dev window's 1-minute bars are nearly a random walk: per-bar returns have
  a 6.7 bps standard deviation against a ~36 bps full-size round-trip cost, so
  any fast signal churns itself to death (an EMA 8/21 cross lost $148 in 80
  trades in testing). The only moves that pay are the multi-hour drifts. This
  bot therefore trades on an hourly timescale and holds cash the rest of the
  time:

  * TREND REGIME, the only regime it trades. A slow trend is confirmed when
    EMA(60) crosses above EMA(240) (a 1-hour average over a 4-hour one) AND
    the 60-bar return is positive (two signals must agree). Entries are
    edge-triggered: only the cross bar buys, never a persisting condition, so
    a flat market cannot re-trigger entries.
  * RANGING MARKETS are deliberately not traded. Mean reversion was measured
    and rejected: the dev window's chop spans ~0.5%, so a band-to-mid swing
    pays ~25 bps against ~24-36 bps of round-trip cost, with a fat left tail
    when the range breaks. Holding cash through chop is the regime decision.
  * EXITS are as slow as entries: EMA cross back down, a 1% trailing stop
    from the peak, a 1.5% hard stop from the entry, and a momentum exit when
    the 60-bar return falls below -0.2% (the trend has broken). The trailing
    and momentum exits do the real work: they banked the dev window's
    recovery at $64,811 instead of riding it back down to $64,424.
  * RISK-BASED SIZING: each trade risks 1.5% of equity against a stop of
    max(1.5%, 3x hourly volatility), so size is 100% in calm markets and
    shrinks as volatility rises (floor 50%). Sizing by volatility, not by
    conviction: no entry signal here is strong enough to justify leverage,
    and there is none available anyway.
  * DRAWDOWN GUARD: if equity falls 5% below its peak, new entries halt for
    120 bars (2 hours). Exits keep working during the halt. It never fired
    on the dev window (max drawdown 1.07%); it is insurance for the race.

  Rejected orders are retried: if a buy was sent but no position appears on
  the next batch, it is sent again while the trend still holds; likewise a
  sell while the position remains. Fills arrive one bar after the decision,
  so every signal is evaluated on closed bars only.

  Deterministic: floats only, no clock, no network, no files, no randomness.
  Two runs on the same bars produce the same orders byte for byte.
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- Strategy parameters (round numbers, chosen for the hourly timescale) ---
FAST_SPAN = 60          # 1-hour EMA
SLOW_SPAN = 240         # 4-hour EMA
MOM_BARS = 60           # momentum window, bars
MOM_ENTRY_MIN = 0.0     # entry needs a positive 60-bar return
MOM_EXIT_MAX = -0.002   # exit when the 60-bar return falls below -0.2%
TRAIL_PCT = 0.01        # trailing stop: 1% below the peak since entry
HARD_STOP_PCT = 0.015   # hard stop: 1.5% below the entry fill
RISK_BUDGET = 0.015     # risk 1.5% of equity per trade
MIN_STOP_PCT = 0.015    # stop is never tighter than 1.5%
STOP_ATR_MULT = 3.0     # ... or 3x hourly volatility, whichever is wider
MIN_SIZE_BPS = 5000     # sizing floor: 50% of equity
MAX_SLIPPAGE_BPS = 100  # 1% past the close the order was decided on
GUARD_DD = 0.05         # halt entries after a 5% drawdown from peak equity
GUARD_BARS = 120        # ... for 120 bars


class Ema:
    """An exponential moving average, seeded as the mean of its first span."""

    def __init__(self, span):
        self.span = span
        self.k = 2.0 / (span + 1)
        self.value = None
        self._warm = []

    def push(self, close):
        if self.value is not None:
            self.value += self.k * (close - self.value)
            return
        self._warm.append(close)
        if len(self._warm) == self.span:
            self.value = sum(self._warm) / self.span


class Atr:
    """Wilder's ATR over true ranges, seeded as the mean of its first span."""

    def __init__(self, span=14):
        self.span = span
        self.value = None
        self._warm = []
        self._prev_close = None

    def push(self, high, low, close):
        if self._prev_close is not None:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
            if self.value is None:
                self._warm.append(tr)
                if len(self._warm) == self.span:
                    self.value = sum(self._warm) / self.span
            else:
                self.value += (tr - self.value) / self.span
        self._prev_close = close


class Bot:
    def __init__(self):
        self.fast = Ema(FAST_SPAN)
        self.slow = Ema(SLOW_SPAN)
        self.atr = Atr(14)
        self.closes = []
        self.prev_gap = None
        self.entry_px = None
        self.peak_px = None
        self.peak_equity = None
        self.guard_until = -1
        self.pending_buy = False
        self.pending_sell = False

    def _size_bps(self, close):
        """Risk-based position size in bps of equity."""
        if self.atr.value is None or close <= 0:
            return 10000
        hourly_vol = self.atr.value * math.sqrt(60) / close
        stop = max(MIN_STOP_PCT, STOP_ATR_MULT * hourly_vol)
        size = int(RISK_BUDGET / stop * 10000)
        return max(MIN_SIZE_BPS, min(10000, size))

    def on_batch(self, state, batch):
        # The fills of the previous batch are already folded into state by
        # the shim. A pending order that did not produce the expected
        # position was rejected; retry it while its reason still holds.
        held = state.positions.get(SYMBOL)
        holding = held is not None and held.qty > 0
        if self.pending_buy and holding:
            self.pending_buy = False
            # The entry price is the actual fill price, not the next close:
            # the hard stop measures from what was really paid.
            self.entry_px = state.close(SYMBOL)
            for fill in batch["fills"]:
                if (fill["symbol"] == SYMBOL and fill["side"] == "buy"
                        and fill["status"] == "filled"):
                    self.entry_px = int(fill["price"])
            self.peak_px = self.entry_px
        if self.pending_sell and not holding:
            self.pending_sell = False
            self.entry_px = None
            self.peak_px = None

        # Only a fresh bar for our symbol moves the indicators. A batch with
        # no CBBTC bar (a gap minute) carries no new information.
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b
                break
        if bar is None:
            return []

        close = int(bar["c"])
        high = int(bar["h"])
        low = int(bar["l"])
        self.closes.append(close)
        self.fast.push(close)
        self.slow.push(close)
        self.atr.push(high, low, close)

        if holding and self.peak_px is not None and close > self.peak_px:
            self.peak_px = close

        # Drawdown guard: track peak equity, halt entries after a 5% fall.
        if self.peak_equity is None or state.equity > self.peak_equity:
            self.peak_equity = state.equity
        if state.equity < self.peak_equity * (1 - GUARD_DD):
            self.guard_until = state.seq + GUARD_BARS

        if state.warming_up:
            return []

        if self.fast.value is None or self.slow.value is None:
            return []

        gap = self.fast.value - self.slow.value
        crossed_up = self.prev_gap is not None and gap > 0 and self.prev_gap <= 0
        crossed_down = self.prev_gap is not None and gap < 0 and self.prev_gap >= 0
        self.prev_gap = gap

        mom60 = None
        if len(self.closes) > MOM_BARS:
            mom60 = close / self.closes[-MOM_BARS - 1] - 1

        if not holding:
            # Entry: a fresh cross up, confirmed by positive momentum. The
            # retry path re-sends a rejected buy while the trend still holds.
            retry = self.pending_buy and gap > 0 and (mom60 is None or mom60 > MOM_ENTRY_MIN)
            fresh = crossed_up and (mom60 is None or mom60 > MOM_ENTRY_MIN)
            if (fresh or retry) and state.seq >= self.guard_until:
                size = self._size_bps(close)
                amount = min(size * state.equity // 10000, state.cash,
                             state.weight_room(SYMBOL))
                if amount >= int(state.hello["fees"]["min_notional_quote_units"]):
                    self.pending_buy = True
                    return [{"side": "buy", "symbol": SYMBOL,
                             "notional_quote_units": amount,
                             "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            return []

        # Holding: exit on trend reversal, trailing stop, hard stop, or
        # momentum breakdown. A rejected sell is retried while held.
        should_exit = (
            crossed_down
            or self.pending_sell
            or (self.peak_px is not None and close <= self.peak_px * (1 - TRAIL_PCT))
            or (self.entry_px is not None and close <= self.entry_px * (1 - HARD_STOP_PCT))
            or (mom60 is not None and mom60 < MOM_EXIT_MAX)
        )
        if should_exit:
            self.pending_sell = True
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []


def main():
    bot = Bot()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
