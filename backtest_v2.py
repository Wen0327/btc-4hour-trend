#!/usr/bin/env python3
"""
backtest_v2.py — 用「變化量」scoring model 回測

跟 v1 的差別：每個維度看的是「跟前一期比的變化」，不是「絕對值」。

用法:
  python backtest_v2.py
  python backtest_v2.py --symbol ETHUSDT
"""
import time
import argparse
from datetime import datetime, timezone
from btc_4h_signal import (
    get, BINANCE_FAPI, FNG_URL,
    check_veto, ema, atr,
    THRESH_LONG, THRESH_SHORT,
)
from backtest import (
    fetch_all_klines, fetch_all_funding, fetch_all_oi,
    fetch_all_ls, fetch_all_top, fetch_all_fng, find_fng_for_time,
)

# ============================================================
# V2 Scoring: 基於「變化」而非「絕對值」
# ============================================================
COMPONENTS_V2 = [
    ("4h candle",      1.0, "candle"),
    ("Funding Δ",      1.5, "funding_delta"),
    ("OI vs price",    1.2, "oi_price"),
    ("Retail L/S Δ",   0.8, "retail_delta"),
    ("Top trader Δ",   1.0, "top_delta"),
    ("EMA cross",      1.0, "ema_cross"),
    ("F&G Δ",          0.5, "fng_delta"),
]

def score_candle_v2(klines):
    """同 v1：上根 4h 突破前高/低"""
    last = klines[-2]
    prev = klines[-3]
    ret = last["close"] / last["open"] - 1
    if last["close"] > prev["high"] and ret > 0.003:
        return 1.0
    if last["close"] < prev["low"] and ret < -0.003:
        return -1.0
    return 0.0

def score_funding_delta(rates):
    """看 funding 的變化方向和幅度，不看絕對正負"""
    if len(rates) < 4:
        return 0.0
    curr = rates[-1][1]
    prev1 = rates[-2][1]
    prev2 = rates[-3][1]
    # 連續兩期變化方向
    delta1 = curr - prev1
    delta2 = prev1 - prev2
    # 加速轉負（不管現在是正還是負）
    if delta1 < -0.00005 and delta2 < -0.00005:
        return 1.0  # 空頭付費加速 → 擠空壓力增加
    if delta1 > 0.00005 and delta2 > 0.00005:
        return -1.0  # 多頭付費加速 → 過熱
    if delta1 < -0.00003:
        return 0.5
    if delta1 > 0.00003:
        return -0.5
    return 0.0

def score_oi_delta_vs_price(oi_hist, klines):
    """同 v1：OI 變化 vs 價格變化"""
    if len(oi_hist) < 2 or len(klines) < 3:
        return 0.0
    oi_chg = oi_hist[-1][1] / oi_hist[-2][1] - 1
    px_chg = klines[-2]["close"] / klines[-3]["close"] - 1
    if oi_chg > 0.005 and px_chg < -0.003:
        return 1.0
    if oi_chg > 0.005 and px_chg > 0.003:
        return -1.0
    if oi_chg < -0.005 and px_chg > 0.003:
        return -0.5
    if oi_chg < -0.005 and px_chg < -0.003:
        return 0.5
    return 0.0

def score_retail_delta(ls_hist):
    """看散戶多空比的變化，不看絕對值"""
    if len(ls_hist) < 3:
        return 0.0
    curr = ls_hist[-1][1]
    prev = ls_hist[-2][1]
    prev2 = ls_hist[-3][1]
    delta = curr - prev
    delta2 = prev - prev2
    # 散戶突然大量做多（反向指標）
    if delta > 0.15 and delta2 > 0.1:
        return -1.0  # 散戶加速追多 → 反向偏空
    if delta < -0.15 and delta2 < -0.1:
        return 1.0   # 散戶加速追空 → 反向偏多
    if delta > 0.1:
        return -0.5
    if delta < -0.1:
        return 0.5
    return 0.0

def score_top_delta(tt_hist):
    """看大戶多空比的變化"""
    if len(tt_hist) < 3:
        return 0.0
    curr = tt_hist[-1][1]
    prev = tt_hist[-2][1]
    prev2 = tt_hist[-3][1]
    delta = curr - prev
    delta2 = prev - prev2
    # 大戶加速做多 → 跟單
    if delta > 0.15 and delta2 > 0.1:
        return 1.0
    if delta < -0.15 and delta2 < -0.1:
        return -1.0
    if delta > 0.1:
        return 0.5
    if delta < -0.1:
        return -0.5
    return 0.0

def score_ema_cross(klines):
    """看價格是否剛穿越 EMA50，不是看在上面還下面"""
    closes = [k["close"] for k in klines]
    if len(closes) < 51:
        return 0.0
    ema_now = ema(closes, 50)
    ema_prev = ema(closes[:-1], 50)
    if ema_now is None or ema_prev is None:
        return 0.0
    price_now = closes[-1]
    price_prev = closes[-2]
    # 剛從下穿上
    if price_prev < ema_prev and price_now > ema_now:
        return 1.0
    # 剛從上穿下
    if price_prev > ema_prev and price_now < ema_now:
        return -1.0
    return 0.0

def score_fng_delta(fng_now, fng_prev):
    """看 F&G 的變化，不看絕對值"""
    delta = fng_now - fng_prev
    # 恐懼加劇（反向 → 偏多）
    if delta < -10:
        return 1.0
    if delta > 10:
        return -1.0
    if delta < -5:
        return 0.5
    if delta > 5:
        return -0.5
    return 0.0

# ============================================================
# Backtest
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

    warmup = 62
    lookahead = 7
    signals = []

    for i in range(warmup, len(klines) - lookahead):
        ts = klines[i]["time"]
        k_slice = klines[:i+1]

        f_slice = [(t, r) for t, r in funding if t <= ts][-10:]
        oi_slice = [(t, o, v) for t, o, v in oi_hist if t <= ts][-10:]
        ls_slice = [(t, r) for t, r in ls_hist if t <= ts][-5:]
        tt_slice = [(t, r) for t, r in tt_hist if t <= ts][-5:]
        fng_now = find_fng_for_time(fng_dict, ts)
        fng_prev = find_fng_for_time(fng_dict, ts.replace(hour=0) - __import__('datetime').timedelta(days=1))

        veto = check_veto(k_slice[-60:])
        if veto:
            continue

        scored = []
        for comp_name, weight, key in COMPONENTS_V2:
            if   key == "candle":        s = score_candle_v2(k_slice[-4:] if len(k_slice) >= 4 else k_slice)
            elif key == "funding_delta": s = score_funding_delta(f_slice)
            elif key == "oi_price":      s = score_oi_delta_vs_price(oi_slice, k_slice[-4:])
            elif key == "retail_delta":  s = score_retail_delta(ls_slice)
            elif key == "top_delta":     s = score_top_delta(tt_slice)
            elif key == "ema_cross":     s = score_ema_cross(k_slice[-60:])
            elif key == "fng_delta":     s = score_fng_delta(fng_now, fng_prev)
            else:                        s = 0
            scored.append((comp_name, s, weight))

        total = sum(s * w for _, s, w in scored)
        max_w = sum(w for _, _, w in scored)
        confidence = total / max_w
        decision = "LONG" if confidence > THRESH_LONG else ("SHORT" if confidence < THRESH_SHORT else "WAIT")

        if decision == "WAIT":
            continue

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

    # Results
    print(f"\n{'='*60}")
    print(f"  {name} V2（變化量模型）回測結果")
    print(f"  期間: {klines[warmup]['time'].strftime('%Y-%m-%d')} ~ {klines[-lookahead]['time'].strftime('%Y-%m-%d')}")
    print(f"{'='*60}")

    if not signals:
        print("\n  沒有產生任何 LONG/SHORT 信號。")
        print("  （「變化量」模型門檻較高，大部分時間為 WAIT）\n")
        return

    longs  = [s for s in signals if s["decision"] == "LONG"]
    shorts = [s for s in signals if s["decision"] == "SHORT"]

    print(f"\n  總信號: {len(signals)}（LONG {len(longs)} / SHORT {len(shorts)}）")

    for timeframe, key in [("4h", "ret_4h"), ("12h", "ret_12h"), ("24h", "ret_24h")]:
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

    bh_ret = (klines[-lookahead]["close"] / klines[warmup]["close"] - 1) * 100
    print(f"\n  ── 對照 ──")
    print(f"  Buy & Hold: {bh_ret:+.2f}%")

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
    ap.add_argument("--symbol", default="BTCUSDT")
    args = ap.parse_args()
    run_backtest(args.symbol)

if __name__ == "__main__":
    main()
