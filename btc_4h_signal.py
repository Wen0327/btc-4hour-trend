#!/usr/bin/env python3
"""
btc_4h_signal.py — BTC 4 小時順向判讀腳本

每次執行抓 Binance Futures 公開 API + Fear & Greed,根據 7 個維度
加權打分,輸出 LONG / SHORT / WAIT。

不需要任何 API key。

用法:
  python btc_4h_signal.py                       # 跑一次,印 stdout
  python btc_4h_signal.py --json                # 輸出 JSON
  python btc_4h_signal.py --loop                # 持續每 4h 自動跑(置於 4h 邊界)
  python btc_4h_signal.py --discord <webhook>   # 結果送 Discord
  python btc_4h_signal.py --log signals.csv     # 每次結果 append 到 CSV

依賴: requests (pip install requests)

排程建議:
  crontab -e
  5 0,4,8,12,16,20 * * * cd /path && python btc_4h_signal.py --log signals.csv --discord <webhook> >> run.log 2>&1
"""
import os
import sys
import csv
import json
import time
import math
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple, Dict, Any

import requests

# ============================================================
# .env loader
# ============================================================
def load_dotenv(path: str = None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

load_dotenv()

# ============================================================
# Config
# ============================================================
BINANCE_FAPI = "https://fapi.binance.com"
FNG_URL = "https://api.alternative.me/fng/"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
SYMBOL_DISPLAY = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
HEADERS = {"User-Agent": "Mozilla/5.0 (btc-4h-signal/1.0)"}

# Scoring thresholds (tune after paper trading)
THRESH_LONG = 0.30      # confidence > +30% → LONG
THRESH_SHORT = -0.30    # confidence < -30% → SHORT
VETO_ATR_PCT = 0.005    # 4h ATR < 0.5% of price → too quiet, veto

# 2026 FOMC statement release times (ET 14:00 → UTC 18:00)
# 2026 CPI release times (ET 08:30 → UTC 12:30)
MACRO_EVENTS_2026 = [
    # FOMC (remaining 2026)
    ("FOMC", datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc)),
    ("FOMC", datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)),
    ("FOMC", datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)),
    ("FOMC", datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)),
    ("FOMC", datetime(2026, 12, 9, 18, 0, tzinfo=timezone.utc)),
    # CPI (remaining 2026)
    ("CPI", datetime(2026, 6, 10, 12, 30, tzinfo=timezone.utc)),
    ("CPI", datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)),
    ("CPI", datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)),
    ("CPI", datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)),
    ("CPI", datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc)),
    ("CPI", datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc)),  # DST ends, ET+5
    ("CPI", datetime(2026, 12, 10, 13, 30, tzinfo=timezone.utc)),  # DST ends, ET+5
]
MACRO_WINDOW_H = 6  # hours before/after event

def check_macro_event(ts: datetime) -> Optional[str]:
    for name, event_time in MACRO_EVENTS_2026:
        diff_h = abs((ts - event_time).total_seconds()) / 3600
        if diff_h <= MACRO_WINDOW_H:
            direction = "後" if ts > event_time else "前"
            return f"⚠️ {name} 公布{direction} {diff_h:.1f}h — 波動劇烈，技術面指標可能失效"
    return None

# ============================================================
# HTTP helper with retry
# ============================================================
def get(url: str, params: Optional[dict] = None, retries: int = 3, timeout: int = 10):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(2 ** i)

# ============================================================
# Data fetchers
# ============================================================
def fetch_klines_4h(symbol: str, limit: int = 60) -> List[Dict[str, Any]]:
    """Last `limit` 4h candles. Last item may be in-progress."""
    data = get(f"{BINANCE_FAPI}/fapi/v1/klines",
               {"symbol": symbol, "interval": "4h", "limit": limit})
    return [{
        "time":   datetime.fromtimestamp(k[0]/1000, tz=timezone.utc),
        "open":   float(k[1]),
        "high":   float(k[2]),
        "low":    float(k[3]),
        "close":  float(k[4]),
        "volume": float(k[5]),
    } for k in data]

def fetch_funding_rate(symbol: str) -> List[Tuple[datetime, float]]:
    data = get(f"{BINANCE_FAPI}/fapi/v1/fundingRate",
               {"symbol": symbol, "limit": 10})
    return [(datetime.fromtimestamp(d["fundingTime"]/1000, tz=timezone.utc),
             float(d["fundingRate"])) for d in data]

def fetch_open_interest_hist(symbol: str) -> List[Tuple[datetime, float, float]]:
    """Returns [(time, sumOI, sumOI_value), ...]"""
    data = get(f"{BINANCE_FAPI}/futures/data/openInterestHist",
               {"symbol": symbol, "period": "4h", "limit": 10})
    return [(datetime.fromtimestamp(d["timestamp"]/1000, tz=timezone.utc),
             float(d["sumOpenInterest"]),
             float(d["sumOpenInterestValue"])) for d in data]

def fetch_long_short_ratio(symbol: str) -> List[Tuple[datetime, float]]:
    """Retail (global) account-based long/short ratio."""
    data = get(f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
               {"symbol": symbol, "period": "4h", "limit": 5})
    return [(datetime.fromtimestamp(d["timestamp"]/1000, tz=timezone.utc),
             float(d["longShortRatio"])) for d in data]

def fetch_top_trader_ratio(symbol: str) -> List[Tuple[datetime, float]]:
    """Top trader (by position size) long/short."""
    data = get(f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio",
               {"symbol": symbol, "period": "4h", "limit": 5})
    return [(datetime.fromtimestamp(d["timestamp"]/1000, tz=timezone.utc),
             float(d["longShortRatio"])) for d in data]

def fetch_fear_greed() -> int:
    data = get(FNG_URL, {"limit": 1})
    return int(data["data"][0]["value"])

# ============================================================
# Technical helpers
# ============================================================
def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def atr(klines: List[dict], period: int = 14) -> Optional[float]:
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        tr = max(
            klines[i]["high"] - klines[i]["low"],
            abs(klines[i]["high"] - klines[i-1]["close"]),
            abs(klines[i]["low"]  - klines[i-1]["close"])
        )
        trs.append(tr)
    return sum(trs[-period:]) / period

# ============================================================
# Scoring (returns score in [-1, +1] and a reason string)
# ============================================================
def score_candle(klines):
    # Use last CLOSED candle (klines[-2]) for "上一根 4h"
    last = klines[-2]
    prev = klines[-3]
    ret = last["close"]/last["open"] - 1
    if last["close"] > prev["high"] and ret > 0.003:
        return 1.0, f"上根 4h {ret*100:+.2f}%、突破前高 ${prev['high']:.0f}"
    if last["close"] < prev["low"] and ret < -0.003:
        return -1.0, f"上根 4h {ret*100:+.2f}%、跌破前低 ${prev['low']:.0f}"
    return 0.0, f"上根 4h {ret*100:+.2f}%、區間內"

def score_funding(rates):
    if len(rates) < 3:
        return 0.0, "資料不足"
    latest = rates[-1][1]
    prev = rates[-2][1]
    pct = latest * 100
    if latest < -0.00005 and latest < prev:
        return 1.0, f"資金費 {pct:+.4f}%、續轉負(空頭付費加重)"
    if latest > 0.00005 and latest > prev:
        return -1.0, f"資金費 {pct:+.4f}%、續轉正(多頭付費加重)"
    if latest < 0:
        return 0.5, f"資金費 {pct:+.4f}%、輕微負"
    if latest > 0:
        return -0.5, f"資金費 {pct:+.4f}%、輕微正"
    return 0.0, f"資金費 {pct:+.4f}%、中性"

def score_oi_vs_price(oi_hist, klines):
    if len(oi_hist) < 2 or len(klines) < 3:
        return 0.0, "資料不足"
    oi_chg = oi_hist[-1][1] / oi_hist[-2][1] - 1
    px_chg = klines[-2]["close"] / klines[-3]["close"] - 1
    if oi_chg > 0.005 and px_chg < -0.003:
        return 1.0, f"OI {oi_chg*100:+.2f}% + 價 {px_chg*100:+.2f}% (新空進、擠空 setup)"
    if oi_chg > 0.005 and px_chg > 0.003:
        return -1.0, f"OI {oi_chg*100:+.2f}% + 價 {px_chg*100:+.2f}% (新多追、過熱)"
    if oi_chg < -0.005 and px_chg > 0.003:
        return -0.5, f"OI {oi_chg*100:+.2f}% + 價 {px_chg*100:+.2f}% (空回補、動能枯竭)"
    if oi_chg < -0.005 and px_chg < -0.003:
        return 0.5, f"OI {oi_chg*100:+.2f}% + 價 {px_chg*100:+.2f}% (多去槓桿、洗盤)"
    return 0.0, f"OI {oi_chg*100:+.2f}% / 價 {px_chg*100:+.2f}% 無顯著訊號"

def score_ls_retail(ls_hist):
    if not ls_hist:
        return 0.0, "資料不足"
    r = ls_hist[-1][1]
    # Retail is contrarian indicator: when retail is heavily long, market often falls
    if r < 0.8:
        return 1.0, f"散戶多空比 {r:.2f}(散戶偏空 → 反向偏多)"
    if r > 1.5:
        return -1.0, f"散戶多空比 {r:.2f}(散戶偏多 → 反向偏空)"
    return 0.0, f"散戶多空比 {r:.2f}(中性)"

def score_top_trader(tt_hist):
    if not tt_hist:
        return 0.0, "資料不足"
    r = tt_hist[-1][1]
    # Top traders treated as informed money: follow them
    if r > 1.5:
        return 1.0, f"大戶多空比 {r:.2f}(大戶偏多、跟單)"
    if r < 0.8:
        return -1.0, f"大戶多空比 {r:.2f}(大戶偏空、跟單)"
    return 0.0, f"大戶多空比 {r:.2f}(中性)"

def score_ema(klines):
    closes = [k["close"] for k in klines]
    e = ema(closes, 50)
    if e is None:
        return 0.0, "資料不足"
    price = closes[-1]
    diff = price / e - 1
    if diff > 0.005:
        return 1.0, f"現價 ${price:.0f} > 4h EMA50 ${e:.0f} ({diff*100:+.2f}%)"
    if diff < -0.005:
        return -1.0, f"現價 ${price:.0f} < 4h EMA50 ${e:.0f} ({diff*100:+.2f}%)"
    return 0.0, f"現價 ${price:.0f} ≈ 4h EMA50 ${e:.0f}"

def score_fng(value):
    if value < 30:
        return 1.0, f"F&G {value}(恐懼、反向偏多)"
    if value > 70:
        return -1.0, f"F&G {value}(貪婪、反向偏空)"
    return 0.0, f"F&G {value}(中性)"

# ============================================================
# Veto
# ============================================================
def check_veto(klines):
    a = atr(klines, 14)
    if a is None:
        return None
    price = klines[-1]["close"]
    atr_pct = a / price
    if atr_pct < VETO_ATR_PCT:
        return f"4h ATR 僅 {atr_pct*100:.2f}%(< {VETO_ATR_PCT*100:.1f}%)、波動異常低、等突破再決定"
    return None

# ============================================================
# Main
# ============================================================
COMPONENTS = [
    # (name, weight, fetcher_keys, scorer)
    ("4h candle",      1.0, "candle"),
    ("Funding rate",   1.5, "funding"),
    ("OI vs price",    1.2, "oi_price"),
    ("Retail L/S",     0.8, "retail"),
    ("Top trader L/S", 1.0, "top"),
    ("Price vs EMA50", 1.0, "ema"),
    ("Fear & Greed",   0.5, "fng"),
]

DECISION_LABEL = {"LONG": "做多", "SHORT": "做空", "WAIT": "觀望"}
DECISION_EMOJI = {"LONG": "🟢", "SHORT": "🔴", "WAIT": "🟡"}

def print_friendly(symbol, decision, confidence, price, chg_24h, ts, scored):
    name = SYMBOL_DISPLAY.get(symbol, symbol)
    bar = "═" * 40
    print(f"\n  {bar}")
    print(f"  {name} 4H Signal — {ts.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  {bar}\n")
    print(f"  💰 ${price:,.2f}   24h: {chg_24h:+.2f}%\n")

    label = DECISION_LABEL.get(decision, decision)
    emoji = DECISION_EMOJI.get(decision, "⚪")
    divider = "─" * 38

    print(f"  {divider}")
    print(f"  {emoji} 判讀：{decision}（{label}）")
    if decision == "WAIT" and abs(confidence) > 0.05:
        lean = "LONG" if confidence > 0 else "SHORT"
        lean_label = DECISION_LABEL[lean]
        print(f"  📈 方向偏向：{lean}（{lean_label}）{confidence*100:+.1f}%")
    elif decision != "WAIT":
        print(f"  📊 信心度：{confidence*100:+.1f}%")
    print(f"  {divider}\n")

    bullish = [(n, why) for n, s, w, why in scored if s > 0]
    bearish = [(n, why) for n, s, w, why in scored if s < 0]
    neutral = [(n, why) for n, s, w, why in scored if s == 0 and n != "VETO"]
    veto    = [(n, why) for n, s, w, why in scored if n == "VETO"]

    if veto:
        print(f"  ⛔ 否決")
        for _, why in veto:
            print(f"     • {why}")
        print()

    if bullish:
        print(f"  ✅ 偏多訊號")
        for _, why in bullish:
            print(f"     • {why}")
        print()

    if bearish:
        print(f"  ❌ 偏空訊號")
        for _, why in bearish:
            print(f"     • {why}")
        print()

    if neutral:
        print(f"  ⚪ 中性（無明確訊號）")
        for _, why in neutral:
            print(f"     • {why}")
        print()

    print(f"  {bar}")


def run_once(symbol: str = "BTCUSDT", output_json: bool = False):
    name = SYMBOL_DISPLAY.get(symbol, symbol)
    ts = datetime.now(timezone.utc)
    if not output_json:
        print(f"Fetching {name}...", flush=True)

    klines  = fetch_klines_4h(symbol, 60)
    rates   = fetch_funding_rate(symbol)
    oi_hist = fetch_open_interest_hist(symbol)
    ls_hist = fetch_long_short_ratio(symbol)
    tt_hist = fetch_top_trader_ratio(symbol)
    fng     = fetch_fear_greed()

    price = klines[-1]["close"]
    chg_24h = (klines[-1]["close"] / klines[-7]["close"] - 1) * 100  # 6 × 4h ≈ 24h

    # Veto first
    veto_msg = check_veto(klines)
    if veto_msg:
        decision = "WAIT"
        confidence = 0.0
        total = 0.0
        scored = [("VETO", 0.0, 0.0, veto_msg)]
    else:
        # Score
        scored = []
        for name, weight, key in COMPONENTS:
            if   key == "candle":   s, why = score_candle(klines)
            elif key == "funding":  s, why = score_funding(rates)
            elif key == "oi_price": s, why = score_oi_vs_price(oi_hist, klines)
            elif key == "retail":   s, why = score_ls_retail(ls_hist)
            elif key == "top":      s, why = score_top_trader(tt_hist)
            elif key == "ema":      s, why = score_ema(klines)
            elif key == "fng":      s, why = score_fng(fng)
            else:                   s, why = 0, "?"
            scored.append((name, s, weight, why))

        total = sum(s * w for _, s, w, _ in scored)
        max_w = sum(w for _, _, w, _ in scored)
        confidence = total / max_w  # [-1, +1]
        decision = "LONG" if confidence > THRESH_LONG else ("SHORT" if confidence < THRESH_SHORT else "WAIT")

    if not output_json:
        print_friendly(symbol, decision, confidence, price, chg_24h, ts, scored)

    result = {
        "symbol": symbol,
        "timestamp": ts.isoformat(),
        "price": price,
        "chg_24h_pct": chg_24h,
        "decision": decision,
        "confidence": confidence,
        "total_score": total,
        "components": [{"name": n, "score": s, "weight": w, "reason": why}
                       for n, s, w, why in scored],
    }
    if output_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return result

def send_discord(webhook: str, result: dict):
    d = result["decision"]
    emoji = DECISION_EMOJI.get(d, "⚪")
    label = DECISION_LABEL.get(d, d)
    conf = result["confidence"]
    name = SYMBOL_DISPLAY.get(result.get("symbol", ""), "")
    header = [
        f"{emoji} **{name} 4H — {d}（{label}）**",
        f"💰 ${result['price']:,.0f}　24h: {result['chg_24h_pct']:+.2f}%",
    ]
    if d == "WAIT" and abs(conf) > 0.05:
        lean = "LONG" if conf > 0 else "SHORT"
        header.append(f"📈 方向偏向：{lean}（{DECISION_LABEL[lean]}）{conf*100:+.1f}%")
    elif d != "WAIT":
        header.append(f"📊 信心度：{conf*100:+.1f}%")

    detail = []
    bullish = [c for c in result["components"] if c["score"] > 0]
    bearish = [c for c in result["components"] if c["score"] < 0]
    if bullish:
        detail.append("✅ 偏多")
        for c in bullish:
            detail.append(f"  • {c['reason']}")
    if bearish:
        detail.append("❌ 偏空")
        for c in bearish:
            detail.append(f"  • {c['reason']}")

    lines = header + ["```"] + detail + ["```"]
    try:
        requests.post(webhook, json={"content": "\n".join(lines)}, timeout=10)
    except Exception as e:
        print(f"Discord send failed: {e}", file=sys.stderr)

def append_csv(path: str, result: dict):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["symbol", "timestamp", "price", "chg_24h_pct", "decision", "confidence", "total_score"])
        w.writerow([result.get("symbol", ""), result["timestamp"], result["price"], f"{result['chg_24h_pct']:.4f}",
                    result["decision"], f"{result['confidence']:.4f}", f"{result['total_score']:.4f}"])

def sleep_to_next_4h():
    now = datetime.now(timezone.utc)
    next_hour = ((now.hour // 4) + 1) * 4
    if next_hour >= 24:
        next_dt = now.replace(hour=0, minute=0, second=5, microsecond=0) + timedelta(days=1)
    else:
        next_dt = now.replace(hour=next_hour, minute=0, second=5, microsecond=0)
    secs = (next_dt - now).total_seconds()
    print(f"\n等到下個 4h 邊界:{next_dt.strftime('%Y-%m-%d %H:%M UTC')}({secs/3600:.2f}h)")
    time.sleep(max(secs, 1))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="JSON 輸出")
    ap.add_argument("--loop", action="store_true", help="持續每 4h 自動跑")
    ap.add_argument("--discord", default=os.environ.get("DISCORD_WEBHOOK"), help="Discord webhook URL (預設讀 .env)")
    ap.add_argument("--log", help="把每次結果 append 到 CSV")
    args = ap.parse_args()

    while True:
        for i, symbol in enumerate(SYMBOLS):
            if i > 0 and not args.json:
                print("\n")
            try:
                result = run_once(symbol=symbol, output_json=args.json)
                if args.discord:
                    send_discord(args.discord, result)
                if args.log:
                    append_csv(args.log, result)
            except Exception as e:
                name = SYMBOL_DISPLAY.get(symbol, symbol)
                print(f"Error ({name}): {e}", file=sys.stderr)
        macro_hint = check_macro_event(datetime.now(timezone.utc))
        if macro_hint:
            if not args.json:
                print(f"\n  {macro_hint}\n")
            if args.discord:
                try:
                    requests.post(args.discord, json={"content": macro_hint}, timeout=10)
                except Exception:
                    pass
        if not args.loop:
            break
        sleep_to_next_4h()

if __name__ == "__main__":
    main()
