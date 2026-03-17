"""
BTC 5m OHLC Strategy
- Fetches 24h of 5m BTC/USDT candles from Binance public API
- Identifies swing highs/lows with neighbor filtering
- Simulates long/short entries on consecutive same-color candle breakouts
- Manages exits via profit target and stop loss
- Updates every 60 seconds, logs all trades
"""

import urllib.request
import json
import time
import math
from datetime import datetime, timezone


# ─── CONFIG ──────────────────────────────────────────────────────────────────
SYMBOL        = "BTCUSDT"
INTERVAL      = "1h"
LIMIT         = 288           # 24 h × 12 candles/h
FEE_RATE      = 0.0002        # 0.02% per side
ACCOUNT_USD   = 100.0
UPDATE_SECS   = 60
ENTRY_OFFSET_PCT  = 0.20          # entry at 20% of avg_above_avg_run beyond pivot
STOP_DIST_PCT     = 0.50          # stop at 50% of avg_run in wrong direction from entry

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def fetch_candles(limit=LIMIT):
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={SYMBOL}&interval={INTERVAL}&limit={limit}"
    )
    with urllib.request.urlopen(url, timeout=10) as r:
        raw = json.loads(r.read())
    # [open_time, o, h, l, c, vol, close_time, ...]
    candles = []
    for row in raw:
        candles.append({
            "t": int(row[0]),
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
        })
    return candles


def candle_color(c):
    return "green" if c["c"] >= c["o"] else "red"


def consecutive_run_ranges(candles):
    """Return list of (range, length) for each consecutive same-color run (≥1 candle)."""
    runs = []
    i = 0
    n = len(candles)
    while i < n:
        col = candle_color(candles[i])
        j = i + 1
        while j < n and candle_color(candles[j]) == col:
            j += 1
        run = candles[i:j]
        hi  = max(c["h"] for c in run)
        lo  = min(c["l"] for c in run)
        runs.append(hi - lo)
        i = j
    return runs


def avg_run_range(candles):
    ranges = consecutive_run_ranges(candles)
    if not ranges:
        return 0.0
    return sum(ranges) / len(ranges)


def avg_above_avg_run_range(candles):
    """Average of only the runs whose range exceeds the overall average (profit target threshold)."""
    ranges = consecutive_run_ranges(candles)
    if not ranges:
        return 0.0
    avg = sum(ranges) / len(ranges)
    above = [r for r in ranges if r > avg]
    if not above:
        return avg  # fallback: use avg if nothing is above-average
    return sum(above) / len(above)


def find_swing_highs(candles):
    """Highs with 2 lower highs on each side."""
    result = []
    n = len(candles)
    for i in range(2, n - 2):
        h = candles[i]["h"]
        if (candles[i-1]["h"] < h and candles[i-2]["h"] < h and
                candles[i+1]["h"] < h and candles[i+2]["h"] < h):
            result.append({"idx": i, "price": h, "t": candles[i]["t"]})
    return result


def find_swing_lows(candles):
    """Lows with 2 higher lows on each side."""
    result = []
    n = len(candles)
    for i in range(2, n - 2):
        l = candles[i]["l"]
        if (candles[i-1]["l"] > l and candles[i-2]["l"] > l and
                candles[i+1]["l"] > l and candles[i+2]["l"] > l):
            result.append({"idx": i, "price": l, "t": candles[i]["t"]})
    return result


def pivot_is_valid(candles, pivot_idx, pivot_price, is_high):
    """
    A pivot is invalid if any candle after its formation (excluding the most recent 5)
    trades through its price level.
    For a swing high: invalid if any later candle has a high >= pivot_price.
    For a swing low:  invalid if any later candle has a low  <= pivot_price.
    """
    check_candles = candles[pivot_idx + 1 : len(candles) - 5]
    for c in check_candles:
        if is_high and c["h"] >= pivot_price:
            return False
        if not is_high and c["l"] <= pivot_price:
            return False
    return True


def last_consecutive_run(candles):
    """
    Walk the candle list forward, segmenting into consecutive same-colour runs
    each time the colour changes. Return the final segment — the current move.
    e.g. [G,G,R,G,G,G] → runs: [G,G], [R], [G,G,G] → returns [G,G,G], "green"
    """
    if not candles:
        return [], "none"
    runs = []
    run_start = 0
    for i in range(1, len(candles)):
        if candle_color(candles[i]) != candle_color(candles[run_start]):
            runs.append(candles[run_start:i])
            run_start = i
    runs.append(candles[run_start:])   # final (current) run
    last_run = runs[-1]
    return last_run, candle_color(last_run[-1])


def run_price_range(run):
    if not run:
        return 0.0, 0.0, 0.0
    hi = max(c["h"] for c in run)
    lo = min(c["l"] for c in run)
    return hi - lo, hi, lo


def ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")


def now_str():
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ─── POSITION STATE ───────────────────────────────────────────────────────────

class Position:
    def __init__(self, side, entry_price, size_usd, pivot_price, avg_run, avg_above_avg_run):
        self.side              = side
        self.entry_price       = entry_price
        self.size_usd          = size_usd
        self.pivot_price       = pivot_price
        self.avg_run           = avg_run
        self.avg_above_avg_run = avg_above_avg_run
        self.best_price        = entry_price
        self.entry_fee         = size_usd * FEE_RATE
        self.entry_time        = now_str()
        # stop level: 50% of avg_run in wrong direction from pivot
        stop_dist = avg_run * STOP_DIST_PCT
        self.stop_level = pivot_price - stop_dist if side == "long" else pivot_price + stop_dist

    def update_best(self, current_price):
        if self.side == "long":
            self.best_price = max(self.best_price, current_price)
        else:
            self.best_price = min(self.best_price, current_price)

    def profit_target_hit(self, current_price):
        """Close if best price reached from pivot > avg of above-average run ranges."""
        if self.side == "long":
            return (self.best_price - self.pivot_price) > self.avg_above_avg_run
        else:
            return (self.pivot_price - self.best_price) > self.avg_above_avg_run

    def stop_hit(self, current_price):
        """Close if price moves 50% of avg_run in wrong direction from entry."""
        if self.side == "long":
            return current_price < self.stop_level
        else:
            return current_price > self.stop_level

    def pnl(self, exit_price):
        if self.side == "long":
            gross = self.size_usd * (exit_price - self.entry_price) / self.entry_price
        else:
            gross = self.size_usd * (self.entry_price - exit_price) / self.entry_price
        fees = self.entry_fee + self.size_usd * FEE_RATE
        return gross - fees


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def main():
    account   = ACCOUNT_USD
    position  = None
    trade_log = []
    cycle     = 0

    print("=" * 70)
    print(f"  BTC/USDT 5m Strategy  |  Start balance: ${account:.2f}")
    print("=" * 70)

    while True:
        cycle += 1
        print(f"\n[Cycle {cycle}] {now_str()}")

        try:
            candles = fetch_candles()
        except Exception as e:
            print(f"  ⚠  Fetch error: {e} — retrying in {UPDATE_SECS}s")
            time.sleep(UPDATE_SECS)
            continue

        # Exclude last (unclosed) candle for analysis
        closed   = candles[:-1]
        current  = candles[-1]
        cur_price = current["c"]

        avg_run           = avg_run_range(closed)
        avg_above_avg_run = avg_above_avg_run_range(closed)

        swing_highs = find_swing_highs(closed)
        swing_lows  = find_swing_lows(closed)

        # Last consecutive run: current candle + preceding closed candles of same colour
        run, run_color = last_consecutive_run(candles)
        run_range, run_hi, run_lo = run_price_range(run)

        print(f"  Price: ${cur_price:,.2f}  |  Avg run: ${avg_run:.2f}  |  Avg above-avg run: ${avg_above_avg_run:.2f}")
        print(f"  Swing highs: {len(swing_highs)}  |  Swing lows: {len(swing_lows)}")
        print(f"  Last run: {len(run)} {run_color} candles, range ${run_range:.2f}")

        # ── Next valid pivots: closest untouched high above and low below price ─
        next_up_pivot = None
        for sh in reversed(swing_highs):
            if sh["price"] > cur_price and pivot_is_valid(closed, sh["idx"], sh["price"], is_high=True):
                next_up_pivot = sh
                break

        next_down_pivot = None
        for sl in reversed(swing_lows):
            if sl["price"] < cur_price and pivot_is_valid(closed, sl["idx"], sl["price"], is_high=False):
                next_down_pivot = sl
                break

        up_str   = f"${next_up_pivot['price']:,.2f} (entry≥${next_up_pivot['price'] + avg_above_avg_run * ENTRY_OFFSET_PCT:,.2f})"   if next_up_pivot   else "none"
        down_str = f"${next_down_pivot['price']:,.2f} (entry≤${next_down_pivot['price'] - avg_above_avg_run * ENTRY_OFFSET_PCT:,.2f})" if next_down_pivot else "none"
        print(f"  Next UP pivot:   {up_str}")
        print(f"  Next DOWN pivot: {down_str}")

        # ── Update best price for open position ──────────────────────────────
        if position:
            position.update_best(cur_price)

        # ── Check exits first ────────────────────────────────────────────────
        if position:
            reason = None
            if position.profit_target_hit(cur_price):
                reason = "PROFIT TARGET"
            elif position.stop_hit(cur_price):
                reason = "STOP LOSS"

            if reason:
                pnl   = position.pnl(cur_price)
                account += pnl
                trade = {
                    "action":     f"CLOSE {position.side.upper()} ({reason})",
                    "entry":      position.entry_price,
                    "exit":       cur_price,
                    "pnl":        pnl,
                    "balance":    account,
                    "time":       now_str(),
                }
                trade_log.append(trade)
                print(f"\n  ✖  {trade['action']}")
                print(f"     Entry ${position.entry_price:,.2f} → Exit ${cur_price:,.2f}")
                print(f"     PnL: ${pnl:+.4f}  |  Balance: ${account:.4f}")
                position = None

        # ── Entry logic ──────────────────────────────────────────────────────
        if not position and run_range > avg_run and avg_run > 0:
            run_start_idx = len(closed) - len(run)

            # Green run breaching a swing high pivot → long
            if run_color == "green" and swing_highs:
                candidates = [sh for sh in swing_highs if sh["price"] < run_hi]
                if candidates:
                    pivot = max(candidates, key=lambda x: x["price"])
                    pivot_price = pivot["price"]
                    entry_threshold = pivot_price + avg_above_avg_run * ENTRY_OFFSET_PCT
                    # Breach: run crosses pivot; valid if no candle after pivot touched it before this run
                    if run_hi > pivot_price and pivot_is_valid(closed[:run_start_idx], pivot["idx"], pivot_price, is_high=True):
                        size_usd = account
                        position = Position(
                            side="long",
                            entry_price=entry_threshold,
                            size_usd=size_usd,
                            pivot_price=pivot_price,
                            avg_run=avg_run,
                            avg_above_avg_run=avg_above_avg_run,
                        )
                        trade = {
                            "action":  "OPEN LONG",
                            "price":   entry_threshold,
                            "pivot":   pivot_price,
                            "size":    size_usd,
                            "balance": account,
                            "time":    now_str(),
                        }
                        trade_log.append(trade)
                        print(f"\n  ▲  OPEN LONG @ ${entry_threshold:,.2f}  |  Pivot: ${pivot_price:,.2f}  |  Run hi: ${run_hi:,.2f}")
                        print(f"     Size: ${size_usd:.2f}  |  Stop: ${position.stop_level:,.2f}  |  Target dist: ${avg_above_avg_run:.2f}")

            # Red run breaching a swing low pivot → short
            elif run_color == "red" and swing_lows and not position:
                candidates = [sl for sl in swing_lows if sl["price"] > run_lo]
                if candidates:
                    pivot = min(candidates, key=lambda x: x["price"])
                    pivot_price = pivot["price"]
                    entry_threshold = pivot_price - avg_above_avg_run * ENTRY_OFFSET_PCT
                    # Breach: run crosses pivot; valid if no candle after pivot touched it before this run
                    if run_lo < pivot_price and pivot_is_valid(closed[:run_start_idx], pivot["idx"], pivot_price, is_high=False):
                        size_usd = account
                        position = Position(
                            side="short",
                            entry_price=entry_threshold,
                            size_usd=size_usd,
                            pivot_price=pivot_price,
                            avg_run=avg_run,
                            avg_above_avg_run=avg_above_avg_run,
                        )
                        trade = {
                            "action":  "OPEN SHORT",
                            "price":   entry_threshold,
                            "pivot":   pivot_price,
                            "size":    size_usd,
                            "balance": account,
                            "time":    now_str(),
                        }
                        trade_log.append(trade)
                        print(f"\n  ▼  OPEN SHORT @ ${entry_threshold:,.2f}  |  Pivot: ${pivot_price:,.2f}  |  Run lo: ${run_lo:,.2f}")
                        print(f"     Size: ${size_usd:.2f}  |  Stop: ${position.stop_level:,.2f}  |  Target dist: ${avg_above_avg_run:.2f}")

        # ── Status ───────────────────────────────────────────────────────────
        if position:
            unrealised = position.pnl(cur_price)
            print(f"\n  Position: {position.side.upper()}  Entry: ${position.entry_price:,.2f}")
            print(f"  Best: ${position.best_price:,.2f}  |  Unrealised: ${unrealised:+.4f}")
            print(f"  Stop: ${position.stop_level:,.2f}  |  Pivot: ${position.pivot_price:,.2f}")
        else:
            print(f"\n  No open position  |  Balance: ${account:.4f}")

        # ── Trade log summary ────────────────────────────────────────────────
        if trade_log:
            print(f"\n  ── Trade Log ({len(trade_log)} events) " + "─" * 40)
            for t in trade_log[-5:]:   # show last 5 events
                if "pnl" in t:
                    print(f"  {t['time']}  {t['action']:35s}  PnL: ${t['pnl']:+.4f}  Bal: ${t['balance']:.4f}")
                else:
                    print(f"  {t['time']}  {t['action']:35s}  @ ${t['price']:,.2f}  Bal: ${t['balance']:.4f}")

        print(f"\n  Sleeping {UPDATE_SECS}s …")
        time.sleep(UPDATE_SECS)


if __name__ == "__main__":
    main()
