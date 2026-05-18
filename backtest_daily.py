#!/usr/bin/env python3
"""
backtest_daily.py — 日線級別 regime 判讀模型回測

指標全部改用日線級別，回測看 24h/48h 後的表現。
每 4h 跑一次，但判讀的是「大方向」而不是短線進場。

用法:
  python backtest_daily.py
  python backtest_daily.py --symbol ETHUSDT
"""
import time
import argparse
from datetime import datetime, timezone, timedelta
from btc_4h_signal import (
    get, BINANCE_FAPI, FNG_URL,
    ema, THRESH_LONG, THRESH_SHORT,
)
from backtest import fetch_all_fng, find_fng_for_time

# ============================================================
# Data fetchers (日線 + 擴大範圍)
# ============================================================
def fetch_daily_klines(symbol, limit=200):
    data = get(f"{BINANCE_FAPI}/fapi/v1/klines",
               {"symbol": symbol, "interval": "1d", "limit": limit})
    return [{
        "time": datetime.fromtimestamp(k[0]/1000, tz=timezone.utc),
        "open": float(k[1]), "high": float(k[2]),
        "low": float(k[3]), "close": float(k[4]),
        "volume": float(k[5]),
    } for k in data]

def fetch_4h_klines(symbol, limit=500):
    data = get(f"{BINANCE_FAPI}/fapi/v1/klines",
               {"symbol": symbol, "interval": "4h", "limit": limit})
    return [{
        "time": datetime.fromtimestamp(k[0]/1000, tz=timezone.utc),
        "open": float(k[1]), "high": float(k[2]),
        "low": float(k[3]), "close": float(k[4]),
        "volume": float(k[5]),
    } for k in data]

def fetch_funding_all(symbol):
    data = get(f"{BINANCE_FAPI}/fapi/v1/fundingRate",
               {"symbol": symbol, "limit": 1000})
    return [(datetime.fromtimestamp(d["fundingTime"]/1000, tz=timezone.utc),
             float(d["fundingRate"])) for d in data]

def fetch_oi_all(symbol):
    data = get(f"{BINANCE_FAPI}/futures/data/openInterestHist",
               {"symbol": symbol, "period": "1d", "limit": 200})
    return [(datetime.fromtimestamp(d["timestamp"]/1000, tz=timezone.utc),
             float(d["sumOpenInterest"])) for d in data]

def fetch_ls_all(symbol):
    data = get(f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
               {"symbol": symbol, "period": "1d", "limit": 200})
    return [(datetime.fromtimestamp(d["timestamp"]/1000, tz=timezone.utc),
             float(d["longShortRatio"])) for d in data]

def fetch_top_all(symbol):
    data = get(f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio",
               {"symbol": symbol, "period": "1d", "limit": 200})
    return [(datetime.fromtimestamp(d["timestamp"]/1000, tz=timezone.utc),
             float(d["longShortRatio"])) for d in data]

# ============================================================
# 日線級別 Scoring
# ============================================================
COMPONENTS = [
    ("日線突破",       1.0, "daily_candle"),
    ("Funding 24h",   1.5, "funding_24h"),
    ("OI vs 價格",    1.2, "oi_price"),
    ("散戶+大戶",     1.0, "ls_combined"),
    ("日線 EMA50",    1.0, "ema_daily"),
    ("F&G",           0.5, "fng"),
]

def score_daily_candle(daily_klines):
    """昨日日線是否突破前日高低"""
    if len(daily_klines) < 3:
        return 0.0
    last = daily_klines[-2]  # 昨天已收盤
    prev = daily_klines[-3]
    ret = last["close"] / last["open"] - 1
    if last["close"] > prev["high"] and ret > 0.005:
        return 1.0
    if last["close"] < prev["low"] and ret < -0.005:
        return -1.0
    return 0.0

def score_funding_24h(funding, ts):
    """過去 24h 的 funding 均值 + 趨勢"""
    recent = [(t, r) for t, r in funding if t <= ts]
    if len(recent) < 6:
        return 0.0
    # 最近 3 筆 (24h，每 8h 一次)
    last3 = [r for _, r in recent[-3:]]
    prev3 = [r for _, r in recent[-6:-3]]
    avg_now = sum(last3) / len(last3)
    avg_prev = sum(prev3) / len(prev3)
    trend = avg_now - avg_prev

    if avg_now < -0.0001 and trend < 0:
        return 1.0   # 負 funding 且加深
    if avg_now > 0.0001 and trend > 0:
        return -1.0  # 正 funding 且加深
    if avg_now < -0.00005:
        return 0.5
    if avg_now > 0.00005:
        return -0.5
    return 0.0

def score_oi_vs_price_daily(oi_hist, daily_klines):
    """日線 OI 變化 vs 日線價格變化"""
    if len(oi_hist) < 2 or len(daily_klines) < 3:
        return 0.0
    oi_chg = oi_hist[-1][1] / oi_hist[-2][1] - 1
    px_chg = daily_klines[-2]["close"] / daily_klines[-3]["close"] - 1
    if oi_chg > 0.01 and px_chg < -0.005:
        return 1.0   # OI 升 + 價跌 = 擠空
    if oi_chg > 0.01 and px_chg > 0.005:
        return -1.0  # OI 升 + 價漲 = 過熱
    if oi_chg < -0.01 and px_chg > 0.005:
        return -0.5  # 空回補
    if oi_chg < -0.01 and px_chg < -0.005:
        return 0.5   # 多去槓桿
    return 0.0

def score_ls_combined(ls_hist, tt_hist):
    """散戶和大戶合併成一個維度，看共識或分歧"""
    if not ls_hist or not tt_hist:
        return 0.0
    retail = ls_hist[-1][1]
    top = tt_hist[-1][1]
    # 散戶做多 + 大戶做空 = 強空信號
    if retail > 1.3 and top < 0.9:
        return -1.0
    # 散戶做空 + 大戶做多 = 強多信號
    if retail < 0.8 and top > 1.2:
        return 1.0
    # 大戶單獨偏多/偏空（跟單）
    if top > 1.3:
        return 0.5
    if top < 0.8:
        return -0.5
    # 散戶單獨（反向，但權重低）
    if retail > 1.5:
        return -0.3
    if retail < 0.7:
        return 0.3
    return 0.0

def score_ema_daily(daily_klines):
    """現價 vs 日線 EMA50"""
    closes = [k["close"] for k in daily_klines]
    e = ema(closes, 50)
    if e is None:
        return 0.0
    price = closes[-1]
    diff = price / e - 1
    if diff > 0.01:
        return 1.0
    if diff < -0.01:
        return -1.0
    if diff > 0.005:
        return 0.5
    if diff < -0.005:
        return -0.5
    return 0.0

def score_fng(value):
    if value < 25:
        return 1.0
    if value > 75:
        return -1.0
    if value < 35:
        return 0.5
    if value > 65:
        return -0.5
    return 0.0

# ============================================================
# Backtest
# ============================================================
def run_backtest(symbol="BTCUSDT"):
    name = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}.get(symbol, symbol)
    print(f"Fetching {name} historical data...", flush=True)

    daily = fetch_daily_klines(symbol, 200)
    time.sleep(0.3)
    klines_4h = fetch_4h_klines(symbol, 500)
    time.sleep(0.3)
    funding = fetch_funding_all(symbol)
    time.sleep(0.3)
    oi_hist = fetch_oi_all(symbol)
    time.sleep(0.3)
    ls_hist = fetch_ls_all(symbol)
    time.sleep(0.3)
    tt_hist = fetch_top_all(symbol)
    time.sleep(0.3)
    fng_dict = fetch_all_fng()

    print(f"  Daily:   {len(daily)} ({daily[0]['time'].strftime('%m/%d')} ~ {daily[-1]['time'].strftime('%m/%d')})")
    print(f"  4h:      {len(klines_4h)} ({klines_4h[0]['time'].strftime('%m/%d')} ~ {klines_4h[-1]['time'].strftime('%m/%d')})")
    print(f"  Funding: {len(funding)}")
    print(f"  OI:      {len(oi_hist)}")
    print(f"  L/S:     {len(ls_hist)}")
    print(f"  Top:     {len(tt_hist)}")

    # 用 4h klines 作為時間軸（每 4h 跑一次），但 scoring 用日線資料
    warmup_daily = 55  # EMA50 需要至少 50 根日線
    lookahead_4h = 13  # 48h = 12 根 4h

    # 找到日線 warmup 對應的時間
    if len(daily) < warmup_daily:
        print("Daily klines not enough!")
        return
    start_time = daily[warmup_daily]["time"]

    signals = []

    for i in range(len(klines_4h) - lookahead_4h):
        ts = klines_4h[i]["time"]
        if ts < start_time:
            continue

        # 日線：到 ts 為止的已收盤日線
        d_slice = [d for d in daily if d["time"] < ts.replace(hour=0, minute=0)]
        if len(d_slice) < 52:
            continue

        # 各指標切片
        f_slice = funding  # 傳全部，score 函式內部會過濾
        oi_slice = [(t, o) for t, o in oi_hist if t < ts]
        ls_slice = [(t, r) for t, r in ls_hist if t < ts]
        tt_slice = [(t, r) for t, r in tt_hist if t < ts]
        fng_val = find_fng_for_time(fng_dict, ts)

        if len(oi_slice) < 2 or len(ls_slice) < 1 or len(tt_slice) < 1:
            continue

        # Score
        scored = []
        for comp_name, weight, key in COMPONENTS:
            if   key == "daily_candle": s = score_daily_candle(d_slice)
            elif key == "funding_24h":  s = score_funding_24h(funding, ts)
            elif key == "oi_price":     s = score_oi_vs_price_daily(oi_slice, d_slice)
            elif key == "ls_combined":  s = score_ls_combined(ls_slice, tt_slice)
            elif key == "ema_daily":    s = score_ema_daily(d_slice)
            elif key == "fng":          s = score_fng(fng_val)
            else:                       s = 0
            scored.append((comp_name, s, weight))

        total = sum(s * w for _, s, w in scored)
        max_w = sum(w for _, _, w in scored)
        confidence = total / max_w
        decision = "LONG" if confidence > THRESH_LONG else ("SHORT" if confidence < THRESH_SHORT else "WAIT")

        if decision == "WAIT":
            continue

        price_now = klines_4h[i]["close"]
        ret_4h  = (klines_4h[i+1]["close"] / price_now - 1) * 100
        ret_24h = (klines_4h[i+6]["close"] / price_now - 1) * 100
        ret_48h = (klines_4h[i+12]["close"] / price_now - 1) * 100

        signals.append({
            "time": ts,
            "price": price_now,
            "decision": decision,
            "confidence": confidence,
            "ret_4h": ret_4h,
            "ret_24h": ret_24h,
            "ret_48h": ret_48h,
        })

    # Results
    print(f"\n{'='*60}")
    print(f"  {name} 日線級別 Regime 模型回測")
    print(f"  每 4h 判讀一次，看 24h/48h 後表現")
    print(f"{'='*60}")

    if not signals:
        print("\n  沒有產生任何 LONG/SHORT 信號。\n")
        return

    longs  = [s for s in signals if s["decision"] == "LONG"]
    shorts = [s for s in signals if s["decision"] == "SHORT"]

    print(f"\n  總信號: {len(signals)}（LONG {len(longs)} / SHORT {len(shorts)}）")

    for timeframe, key in [("4h", "ret_4h"), ("24h", "ret_24h"), ("48h", "ret_48h")]:
        print(f"\n  ── {timeframe} 後表現 ──")

        if longs:
            l_wins = sum(1 for s in longs if s[key] > 0)
            l_avg = sum(s[key] for s in longs) / len(longs)
            l_wr = l_wins / len(longs) * 100
            l_gains = [s[key] for s in longs if s[key] > 0]
            l_losses = [abs(s[key]) for s in longs if s[key] <= 0]
            l_pf = (sum(l_gains) / sum(l_losses)) if l_losses else float('inf')
            print(f"  LONG  ({len(longs)}筆): 勝率 {l_wr:.1f}%  平均報酬 {l_avg:+.2f}%  PF {l_pf:.2f}")

        if shorts:
            s_wins = sum(1 for s in shorts if s[key] < 0)
            s_avg = sum(-s[key] for s in shorts) / len(shorts)
            s_wr = s_wins / len(shorts) * 100
            s_gains = [abs(s[key]) for s in shorts if s[key] < 0]
            s_losses = [s[key] for s in shorts if s[key] >= 0]
            s_pf = (sum(s_gains) / sum(s_losses)) if s_losses else float('inf')
            print(f"  SHORT ({len(shorts)}筆): 勝率 {s_wr:.1f}%  平均報酬 {s_avg:+.2f}%  PF {s_pf:.2f}")

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

    # B&H
    valid_start = next(s for s in signals)
    valid_end = signals[-1]
    idx_start = next(i for i, k in enumerate(klines_4h) if k["time"] == valid_start["time"])
    idx_end = next(i for i, k in enumerate(klines_4h) if k["time"] == valid_end["time"])
    bh_ret = (klines_4h[idx_end]["close"] / klines_4h[idx_start]["close"] - 1) * 100
    print(f"\n  ── 對照 ──")
    print(f"  Buy & Hold: {bh_ret:+.2f}%")
    print(f"  期間: {valid_start['time'].strftime('%Y-%m-%d')} ~ {valid_end['time'].strftime('%Y-%m-%d')}")

    rets = []
    for s in signals:
        if s["decision"] == "LONG":
            rets.append(s["ret_24h"])
        else:
            rets.append(-s["ret_24h"])
    max_consec_loss = 0
    curr_streak = 0
    for r in rets:
        if r <= 0:
            curr_streak += 1
            max_consec_loss = max(max_consec_loss, curr_streak)
        else:
            curr_streak = 0
    print(f"  最大連續虧損 (24h): {max_consec_loss} 筆")
    print(f"\n{'='*60}\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    args = ap.parse_args()
    run_backtest(args.symbol)

if __name__ == "__main__":
    main()
