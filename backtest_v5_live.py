#!/usr/bin/env python3
"""
backtest_v5_live.py — V5 模型 live data 回測

讀 signals.csv 的 V5 信號，對比實際後續 24h 報酬。
結果輸出到 terminal + 推 Discord。

用法:
  python backtest_v5_live.py
"""
import os
import sys
import csv
import json
import requests
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btc_4h_signal import get, BINANCE_FAPI, load_dotenv, SYMBOL_DISPLAY

load_dotenv()

SIGNALS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.csv")

def fetch_4h_klines(symbol, limit=500):
    data = get(f"{BINANCE_FAPI}/fapi/v1/klines",
               {"symbol": symbol, "interval": "4h", "limit": limit})
    return [{
        "time": datetime.fromtimestamp(k[0]/1000, tz=timezone.utc),
        "close": float(k[4]),
    } for k in data]

def find_price_at(klines, ts, offset_hours):
    target = ts + timedelta(hours=offset_hours)
    closest = min(klines, key=lambda k: abs((k["time"] - target).total_seconds()))
    if abs((closest["time"] - target).total_seconds()) < 3600 * 3:
        return closest["close"]
    return None

def run_backtest():
    if not os.path.exists(SIGNALS_CSV):
        print("signals.csv not found!")
        return None

    # Read signals
    signals = []
    with open(SIGNALS_CSV, "r") as f:
        reader = csv.reader(f)
        header = next(reader)
        for row in reader:
            if len(row) != 7:
                continue
            v5 = row[4]
            if v5 not in ("LONG", "SHORT"):
                continue
            signals.append({
                "symbol": row[0],
                "timestamp": datetime.fromisoformat(row[1]),
                "price": float(row[2]),
                "v5": v5,
                "market_score": float(row[5]),
                "macro_score": float(row[6]),
            })

    if not signals:
        print("No LONG/SHORT signals found in CSV.")
        return None

    # Fetch historical klines for price comparison
    klines_cache = {}
    for sym in set(s["symbol"] for s in signals):
        print(f"Fetching {sym} klines...", flush=True)
        klines_cache[sym] = fetch_4h_klines(sym, 500)

    # Calculate returns
    results = {"BTCUSDT": [], "ETHUSDT": []}
    for s in signals:
        klines = klines_cache.get(s["symbol"], [])
        price_24h = find_price_at(klines, s["timestamp"], 24)
        if price_24h is None:
            continue
        ret_24h = (price_24h / s["price"] - 1) * 100
        pnl = ret_24h if s["v5"] == "LONG" else -ret_24h
        s["ret_24h"] = ret_24h
        s["pnl_24h"] = pnl
        results[s["symbol"]].append(s)

    # Build report
    lines = []
    lines.append("📋 **V5 模型 Live 回測報告**")
    lines.append(f"期間: {signals[0]['timestamp'].strftime('%Y-%m-%d')} ~ {signals[-1]['timestamp'].strftime('%Y-%m-%d')}")
    lines.append("")

    all_pnl = []
    for sym in ["BTCUSDT", "ETHUSDT"]:
        group = results[sym]
        if not group:
            continue
        name = SYMBOL_DISPLAY.get(sym, sym)
        longs = [s for s in group if s["v5"] == "LONG"]
        shorts = [s for s in group if s["v5"] == "SHORT"]

        pnls = [s["pnl_24h"] for s in group]
        all_pnl.extend(pnls)
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(pnls) * 100
        avg = sum(pnls) / len(pnls)
        gains = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p <= 0]
        pf = sum(gains) / sum(losses) if losses else float("inf")

        # Max consecutive loss
        max_consec = 0
        curr = 0
        for p in pnls:
            if p <= 0:
                curr += 1
                max_consec = max(max_consec, curr)
            else:
                curr = 0

        # Price change
        first_price = group[0]["price"]
        last_price = group[-1]["price"]
        bh = (last_price / first_price - 1) * 100

        lines.append(f"**{name}**")
        lines.append(f"```")
        lines.append(f"信號: {len(group)} (L:{len(longs)} S:{len(shorts)})")
        lines.append(f"24h 勝率: {wr:.1f}%")
        lines.append(f"24h PF: {pf:.2f}")
        lines.append(f"平均報酬: {avg:+.2f}%")
        lines.append(f"最大連續虧損: {max_consec} 筆")
        lines.append(f"Buy&Hold: {bh:+.1f}%")
        lines.append(f"```")

        # LONG / SHORT breakdown
        if longs:
            l_pnl = [s["pnl_24h"] for s in longs]
            l_wr = sum(1 for p in l_pnl if p > 0) / len(l_pnl) * 100
            lines.append(f"  LONG {len(longs)}筆 勝率 {l_wr:.0f}%")
        if shorts:
            s_pnl = [s["pnl_24h"] for s in shorts]
            s_wr = sum(1 for p in s_pnl if p > 0) / len(s_pnl) * 100
            lines.append(f"  SHORT {len(shorts)}筆 勝率 {s_wr:.0f}%")
        lines.append("")

    # Overall
    if all_pnl:
        total_wins = sum(1 for p in all_pnl if p > 0)
        total_wr = total_wins / len(all_pnl) * 100
        total_avg = sum(all_pnl) / len(all_pnl)
        total_gains = [p for p in all_pnl if p > 0]
        total_losses = [abs(p) for p in all_pnl if p <= 0]
        total_pf = sum(total_gains) / sum(total_losses) if total_losses else float("inf")
        lines.append(f"**合計: {len(all_pnl)}筆 勝率 {total_wr:.1f}% PF {total_pf:.2f} 平均 {total_avg:+.2f}%**")

    # Phase 2 pass/fail
    lines.append("")
    passed = True
    checks = []
    if len(all_pnl) < 50:
        checks.append(f"❌ 信號數 {len(all_pnl)} < 50")
        passed = False
    else:
        checks.append(f"✅ 信號數 {len(all_pnl)} >= 50")
    if total_wr >= 50 or total_pf >= 1.3:
        checks.append(f"✅ 勝率 {total_wr:.1f}% 或 PF {total_pf:.2f}")
    else:
        checks.append(f"❌ 勝率 {total_wr:.1f}% 且 PF {total_pf:.2f}")
        passed = False
    lines.extend(checks)
    lines.append(f"\n{'✅ Phase 2 通過' if passed else '❌ Phase 2 未通過'}")

    report = "\n".join(lines)

    # Print
    print()
    print(report)
    print()

    # Send Discord
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if webhook:
        try:
            requests.post(webhook, json={"content": report}, timeout=10)
            print("Report sent to Discord.")
        except Exception as e:
            print(f"Discord send failed: {e}")

    return report

if __name__ == "__main__":
    run_backtest()
