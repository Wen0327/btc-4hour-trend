#!/usr/bin/env python3
"""
backtest.py — 用歷史資料回測 btc_4h_signal 的 scoring model

從 Binance 抓過去 ~2.5 個月的 4h 資料，模擬每根 4h K 線結束時
跑一次 scoring，然後看實際後續 4h/12h/24h 的報酬。

用法:
  python backtest.py              # 跑 BTCUSDT 回測
  python backtest.py --symbol ETHUSDT
"""
import time
import argparse
from datetime import datetime, timezone
from btc_4h_signal import (
    get, BINANCE_FAPI, FNG_URL,
    score_candle, score_funding, score_oi_vs_price,
    score_ls_retail, score_top_trader, score_ema, score_fng,
    check_veto, COMPONENTS, THRESH_LONG, THRESH_SHORT, ema, atr,
)

# ============================================================
# Bulk historical data fetchers
# ============================================================
def fetch_all_klines(symbol, limit=500):
    data = get(f"{BINANCE_FAPI}/fapi/v1/klines",
               {"symbol": symbol, "interval": "4h", "limit": limit})
    return [{
        "time": datetime.fromtimestamp(k[0]/1000, tz=timezone.utc),
        "open": float(k[1]), "high": float(k[2]),
        "low": float(k[3]), "close": float(k[4]),
        "volume": float(k[5]),
    } for k in data]

def fetch_all_funding(symbol):
    # Funding is every 8h, fetch max
    data = get(f"{BINANCE_FAPI}/fapi/v1/fundingRate",
               {"symbol": symbol, "limit": 1000})
    return [(datetime.fromtimestamp(d["fundingTime"]/1000, tz=timezone.utc),
             float(d["fundingRate"])) for d in data]

def fetch_all_oi(symbol):
    data = get(f"{BINANCE_FAPI}/futures/data/openInterestHist",
               {"symbol": symbol, "period": "4h", "limit": 500})
    return [(datetime.fromtimestamp(d["timestamp"]/1000, tz=timezone.utc),
             float(d["sumOpenInterest"]),
             float(d["sumOpenInterestValue"])) for d in data]

def fetch_all_ls(symbol):
    data = get(f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
               {"symbol": symbol, "period": "4h", "limit": 500})
    return [(datetime.fromtimestamp(d["timestamp"]/1000, tz=timezone.utc),
             float(d["longShortRatio"])) for d in data]

def fetch_all_top(symbol):
    data = get(f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio",
               {"symbol": symbol, "period": "4h", "limit": 500})
    return [(datetime.fromtimestamp(d["timestamp"]/1000, tz=timezone.utc),
             float(d["longShortRatio"])) for d in data]

def fetch_all_fng():
    data = get(FNG_URL, {"limit": 90})  # ~3 months daily
    return {d["timestamp"]: int(d["value"]) for d in data["data"]}

def find_fng_for_time(fng_dict, ts):
    """Find the F&G value for a given timestamp (daily granularity)."""
    day_start = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    day_ts = str(int(day_start.timestamp()))
    if day_ts in fng_dict:
        return fng_dict[day_ts]
    # Try nearby days
    for offset in range(1, 3):
        for direction in [-1, 1]:
            key = str(int(day_start.timestamp()) + direction * offset * 86400)
            if key in fng_dict:
                return fng_dict[key]
    return 50  # default neutral

# ============================================================
# Backtest engine
# ============================================================
def run_backtest(symbol="BTCUSDT"):
    name = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}.get(symbol, symbol)
    print(f"Fetching {name} historical data...", flush=True)

    klines = fetch_all_klines(symbol, 500)
    time.sleep(0.5)
    funding = fetch_all_funding(symbol)
    time.sleep(0.5)
    oi_hist = fetch_all_oi(symbol)
    time.sleep(0.5)
    ls_hist = fetch_all_ls(symbol)
    time.sleep(0.5)
    tt_hist = fetch_all_top(symbol)
    time.sleep(0.5)
    fng_dict = fetch_all_fng()

    print(f"  Klines:  {len(klines)} ({klines[0]['time'].strftime('%m/%d')} ~ {klines[-1]['time'].strftime('%m/%d')})")
    print(f"  Funding: {len(funding)}")
    print(f"  OI:      {len(oi_hist)}")
    print(f"  L/S:     {len(ls_hist)}")
    print(f"  Top:     {len(tt_hist)}")
    print(f"  F&G:     {len(fng_dict)} days")

    # Need 60 klines for EMA50 warmup, and look-ahead for return calculation
    # Test from index 62 to len-7 (need 6 candles ahead for 24h return)
    warmup = 62
    lookahead = 7  # 6 candles = 24h, +1 buffer

    if len(klines) < warmup + lookahead + 10:
        print("Not enough data!")
        return

    signals = []

    for i in range(warmup, len(klines) - lookahead):
        ts = klines[i]["time"]
        k_slice = klines[:i+1]  # all klines up to this point

        # Find matching funding rates (before this timestamp)
        f_slice = [(t, r) for t, r in funding if t <= ts][-10:]
        oi_slice = [(t, o, v) for t, o, v in oi_hist if t <= ts][-10:]
        ls_slice = [(t, r) for t, r in ls_hist if t <= ts][-5:]
        tt_slice = [(t, r) for t, r in tt_hist if t <= ts][-5:]
        fng = find_fng_for_time(fng_dict, ts)

        # Check veto
        veto = check_veto(k_slice[-60:])
        if veto:
            continue

        # Score
        scored = []
        for comp_name, weight, key in COMPONENTS:
            if   key == "candle":   s, _ = score_candle(k_slice[-4:] if len(k_slice) >= 4 else k_slice)
            elif key == "funding":  s, _ = score_funding(f_slice)
            elif key == "oi_price": s, _ = score_oi_vs_price(oi_slice, k_slice[-4:])
            elif key == "retail":   s, _ = score_ls_retail(ls_slice)
            elif key == "top":      s, _ = score_top_trader(tt_slice)
            elif key == "ema":      s, _ = score_ema(k_slice[-60:])
            elif key == "fng":      s, _ = score_fng(fng)
            else:                   s = 0
            scored.append((comp_name, s, weight))

        total = sum(s * w for _, s, w in scored)
        max_w = sum(w for _, _, w in scored)
        confidence = total / max_w
        decision = "LONG" if confidence > THRESH_LONG else ("SHORT" if confidence < THRESH_SHORT else "WAIT")

        if decision == "WAIT":
            continue

        # Calculate future returns
        price_now = klines[i]["close"]
        ret_4h  = (klines[i+1]["close"] / price_now - 1) * 100
        ret_12h = (klines[i+3]["close"] / price_now - 1) * 100
        ret_24h = (klines[i+6]["close"] / price_now - 1) * 100

        signals.append({
            "time": ts,
            "price": price_now,
            "decision": decision,
            "confidence": confidence,
            "ret_4h": ret_4h,
            "ret_12h": ret_12h,
            "ret_24h": ret_24h,
        })

    # ============================================================
    # Results
    # ============================================================
    print(f"\n{'='*60}")
    print(f"  {name} 歷史回測結果")
    print(f"  期間: {klines[warmup]['time'].strftime('%Y-%m-%d')} ~ {klines[-lookahead]['time'].strftime('%Y-%m-%d')}")
    print(f"{'='*60}")

    if not signals:
        print("\n  沒有產生任何 LONG/SHORT 信號。\n")
        return

    longs  = [s for s in signals if s["decision"] == "LONG"]
    shorts = [s for s in signals if s["decision"] == "SHORT"]

    print(f"\n  總信號: {len(signals)}（LONG {len(longs)} / SHORT {len(shorts)}）")

    for timeframe, key in [("4h", "ret_4h"), ("12h", "ret_12h"), ("24h", "ret_24h")]:
        print(f"\n  ── {timeframe} 後表現 ──")

        # LONG: win = positive return
        if longs:
            l_wins = sum(1 for s in longs if s[key] > 0)
            l_avg = sum(s[key] for s in longs) / len(longs)
            l_wr = l_wins / len(longs) * 100
            l_gains = [s[key] for s in longs if s[key] > 0]
            l_losses = [abs(s[key]) for s in longs if s[key] <= 0]
            l_pf = (sum(l_gains) / sum(l_losses)) if l_losses else float('inf')
            print(f"  LONG  ({len(longs)}筆): 勝率 {l_wr:.1f}%  平均報酬 {l_avg:+.2f}%  PF {l_pf:.2f}")

        # SHORT: win = negative return (price went down)
        if shorts:
            s_wins = sum(1 for s in shorts if s[key] < 0)
            s_avg = sum(-s[key] for s in shorts) / len(shorts)  # flip sign for SHORT P&L
            s_wr = s_wins / len(shorts) * 100
            s_gains = [abs(s[key]) for s in shorts if s[key] < 0]
            s_losses = [s[key] for s in shorts if s[key] >= 0]
            s_pf = (sum(s_gains) / sum(s_losses)) if s_losses else float('inf')
            print(f"  SHORT ({len(shorts)}筆): 勝率 {s_wr:.1f}%  平均報酬 {s_avg:+.2f}%  PF {s_pf:.2f}")

        # Combined
        all_ret = []
        for s in signals:
            if s["decision"] == "LONG":
                all_ret.append(s[key])
            else:
                all_ret.append(-s[key])
        wins = sum(1 for r in all_ret if r > 0)
        avg = sum(all_ret) / len(all_ret)
        wr = wins / len(all_ret) * 100
        gains = [r for r in all_ret if r > 0]
        losses = [abs(r) for r in all_ret if r <= 0]
        pf = (sum(gains) / sum(losses)) if losses else float('inf')
        print(f"  合計  ({len(signals)}筆): 勝率 {wr:.1f}%  平均報酬 {avg:+.2f}%  PF {pf:.2f}")

    # Buy and hold comparison
    bh_ret = (klines[-lookahead]["close"] / klines[warmup]["close"] - 1) * 100
    print(f"\n  ── 對照 ──")
    print(f"  Buy & Hold: {bh_ret:+.2f}%")

    # Max consecutive losses
    rets = []
    for s in signals:
        if s["decision"] == "LONG":
            rets.append(s["ret_4h"])
        else:
            rets.append(-s["ret_4h"])
    max_consec_loss = 0
    curr = 0
    for r in rets:
        if r <= 0:
            curr += 1
            max_consec_loss = max(max_consec_loss, curr)
        else:
            curr = 0
    print(f"  最大連續虧損: {max_consec_loss} 筆")

    print(f"\n{'='*60}\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT", help="BTCUSDT or ETHUSDT")
    args = ap.parse_args()
    run_backtest(args.symbol)

if __name__ == "__main__":
    main()
