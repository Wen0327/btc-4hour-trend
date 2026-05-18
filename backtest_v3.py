#!/usr/bin/env python3
"""
backtest_v3.py — 混合模型：條件 + 觸發

每個維度要「方向對」+「剛發生變化」才給分。

用法:
  python backtest_v3.py
  python backtest_v3.py --symbol ETHUSDT
"""
import time
import argparse
from datetime import datetime, timezone, timedelta
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
# V3 Scoring: 條件 + 觸發
# ============================================================
COMPONENTS_V3 = [
    ("4h candle",      1.0, "candle"),
    ("Funding",        1.5, "funding"),
    ("OI vs price",    1.2, "oi_price"),
    ("Retail L/S",     0.8, "retail"),
    ("Top trader L/S", 1.0, "top"),
    ("EMA50",          1.0, "ema"),
    ("F&G",            0.5, "fng"),
]

def score_candle_v3(klines):
    """同 v1，本身就是看變化"""
    last = klines[-2]
    prev = klines[-3]
    ret = last["close"] / last["open"] - 1
    if last["close"] > prev["high"] and ret > 0.003:
        return 1.0
    if last["close"] < prev["low"] and ret < -0.003:
        return -1.0
    return 0.0

def score_funding_v3(rates):
    """條件：funding 正或負。觸發：剛變得更正/更負"""
    if len(rates) < 3:
        return 0.0
    curr = rates[-1][1]
    prev = rates[-2][1]
    delta = curr - prev
    # 條件：負 + 觸發：剛變更負
    if curr < 0 and delta < -0.00003:
        return 1.0
    # 條件：正 + 觸發：剛變更正
    if curr > 0 and delta > 0.00003:
        return -1.0
    # 條件成立但沒觸發 → 給小分
    if curr < -0.0001 and delta < 0:
        return 0.3
    if curr > 0.0001 and delta > 0:
        return -0.3
    return 0.0

def score_oi_price_v3(oi_hist, klines):
    """同 v1，本身就是看變化"""
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

def score_retail_v3(ls_hist):
    """條件：散戶偏多/偏空。觸發：剛變得更偏"""
    if len(ls_hist) < 2:
        return 0.0
    curr = ls_hist[-1][1]
    prev = ls_hist[-2][1]
    delta = curr - prev
    # 條件：散戶重多(>1.3) + 觸發：剛變更多
    if curr > 1.3 and delta > 0.05:
        return -1.0  # 反向
    # 條件：散戶重空(<0.9) + 觸發：剛變更空
    if curr < 0.9 and delta < -0.05:
        return 1.0   # 反向
    # 條件成立但觸發弱
    if curr > 1.3 and delta > 0:
        return -0.3
    if curr < 0.9 and delta < 0:
        return 0.3
    return 0.0

def score_top_v3(tt_hist):
    """條件：大戶偏多/偏空。觸發：剛變得更偏"""
    if len(tt_hist) < 2:
        return 0.0
    curr = tt_hist[-1][1]
    prev = tt_hist[-2][1]
    delta = curr - prev
    # 條件：大戶偏多(>1.2) + 觸發：剛加碼
    if curr > 1.2 and delta > 0.05:
        return 1.0
    # 條件：大戶偏空(<0.9) + 觸發：剛加碼空
    if curr < 0.9 and delta < -0.05:
        return -1.0
    # 條件成立但觸發弱
    if curr > 1.2 and delta > 0:
        return 0.3
    if curr < 0.9 and delta < 0:
        return -0.3
    return 0.0

def score_ema_v3(klines):
    """條件：在 EMA50 上/下方。觸發：剛穿越 或 距離剛擴大"""
    closes = [k["close"] for k in klines]
    if len(closes) < 51:
        return 0.0
    ema_now = ema(closes, 50)
    ema_prev = ema(closes[:-1], 50)
    if ema_now is None or ema_prev is None:
        return 0.0
    price_now = closes[-1]
    price_prev = closes[-2]
    diff_now = price_now / ema_now - 1
    diff_prev = price_prev / ema_prev - 1
    # 剛穿越 → 滿分
    if price_prev < ema_prev and price_now > ema_now:
        return 1.0
    if price_prev > ema_prev and price_now < ema_now:
        return -1.0
    # 條件：在上方 + 觸發：距離剛擴大
    if diff_now > 0.005 and diff_now > diff_prev + 0.002:
        return 0.5
    if diff_now < -0.005 and diff_now < diff_prev - 0.002:
        return -0.5
    return 0.0

def score_fng_v3(fng_now, fng_prev):
    """條件：極端恐懼/貪婪。觸發：剛變得更極端"""
    delta = fng_now - fng_prev
    # 條件：恐懼(<35) + 觸發：剛變更恐懼
    if fng_now < 35 and delta < -3:
        return 1.0
    # 條件：貪婪(>65) + 觸發：剛變更貪婪
    if fng_now > 65 and delta > 3:
        return -1.0
    # 條件成立但觸發弱
    if fng_now < 30:
        return 0.3
    if fng_now > 70:
        return -0.3
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
        fng_prev = find_fng_for_time(fng_dict, ts.replace(hour=0) - timedelta(days=1))

        veto = check_veto(k_slice[-60:])
        if veto:
            continue

        scored = []
        for comp_name, weight, key in COMPONENTS_V3:
            if   key == "candle":   s = score_candle_v3(k_slice[-4:] if len(k_slice) >= 4 else k_slice)
            elif key == "funding":  s = score_funding_v3(f_slice)
            elif key == "oi_price": s = score_oi_price_v3(oi_slice, k_slice[-4:])
            elif key == "retail":   s = score_retail_v3(ls_slice)
            elif key == "top":      s = score_top_v3(tt_slice)
            elif key == "ema":      s = score_ema_v3(k_slice[-60:])
            elif key == "fng":      s = score_fng_v3(fng_now, fng_prev)
            else:                   s = 0
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
    print(f"  {name} V3（混合模型：條件+觸發）回測結果")
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
    curr_streak = 0
    for r in rets:
        if r <= 0:
            curr_streak += 1
            max_consec_loss = max(max_consec_loss, curr_streak)
        else:
            curr_streak = 0
    print(f"  最大連續虧損: {max_consec_loss} 筆")
    print(f"\n{'='*60}\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    args = ap.parse_args()
    run_backtest(args.symbol)

if __name__ == "__main__":
    main()
