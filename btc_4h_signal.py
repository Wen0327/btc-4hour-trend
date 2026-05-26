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
SOSOVALUE_API = "https://openapi.sosovalue.com/openapi/v1"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
SYMBOL_DISPLAY = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
HEADERS = {"User-Agent": "Mozilla/5.0 (btc-4h-signal/1.0)"}

# Scoring thresholds (tune after paper trading)
THRESH_LONG = 0.30      # confidence > +30% → LONG
THRESH_SHORT = -0.30    # confidence < -30% → SHORT
VETO_ATR_PCT = 0.005    # 4h ATR < 0.5% of price → too quiet, veto

# Feature toggles
ENABLE_NEWS = False      # 新聞標題輸出
SENTIMENT_HOUR = 0       # 每日情緒摘要發送時間（本地時區，0 = UTC 00:05 = 台北 08:05）

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

def _fred_latest(series_id: str, api_key: str) -> Optional[str]:
    try:
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": series_id, "api_key": api_key,
                                 "file_type": "json", "sort_order": "desc", "limit": 1},
                         timeout=10)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        if obs and obs[0]["value"] != ".":
            return obs[0]["value"]
    except Exception:
        pass
    return None

def fetch_fed_rate() -> Optional[str]:
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return None
    try:
        effr = _fred_latest("EFFR", api_key)          # 有效利率
        upper = _fred_latest("DFEDTARU", api_key)      # 目標上限
        lower = _fred_latest("DFEDTARL", api_key)      # 目標下限
        # FOMC dot plot: 抓當年的預測
        proj = None
        try:
            current_year = str(datetime.now(timezone.utc).year)
            r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                             params={"series_id": "FEDTARMD", "api_key": api_key,
                                     "file_type": "json", "sort_order": "desc", "limit": 10},
                             timeout=10)
            r.raise_for_status()
            for obs in r.json().get("observations", []):
                if obs["date"].startswith(current_year) and obs["value"] != ".":
                    proj = obs["value"]
                    break
        except Exception:
            pass

        if not effr:
            return None

        parts = [f"Fed 利率 {effr}%"]
        if upper and lower:
            parts[0] = f"Fed 利率 {effr}%（目標 {lower}-{upper}%）"

        if proj and upper:
            current_mid = (float(upper) + float(lower)) / 2 if lower else float(upper)
            proj_f = float(proj)
            diff = current_mid - proj_f
            cuts = round(diff / 0.25)
            if cuts > 0:
                new_upper = float(upper) - cuts * 0.25
                new_lower = float(lower) - cuts * 0.25 if lower else new_upper - 0.25
                parts.append(f"FOMC 預期年底降 {cuts} 碼至 {new_lower:.2f}-{new_upper:.2f}%（偏多）")
            elif cuts < 0:
                ups = abs(cuts)
                new_upper = float(upper) + ups * 0.25
                new_lower = float(lower) + ups * 0.25 if lower else new_upper - 0.25
                parts.append(f"FOMC 預期年底升 {ups} 碼至 {new_lower:.2f}-{new_upper:.2f}%（偏空）")
            else:
                parts.append(f"FOMC 預期年底維持不變（中性）")

        return "｜".join(parts)
    except Exception:
        pass
    return None

def check_macro_event(ts: datetime) -> Optional[str]:
    for name, event_time in MACRO_EVENTS_2026:
        diff_h = abs((ts - event_time).total_seconds()) / 3600
        if diff_h <= MACRO_WINDOW_H:
            direction = "後" if ts > event_time else "前"
            rate = fetch_fed_rate()
            rate_str = f"｜當前利率 {rate}%" if rate else ""
            return f"⚠️ {name} 公布{direction} {diff_h:.1f}h{rate_str} — 波動劇烈，技術面指標可能失效"
    return None

NEWS_SENT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".news_sent.json")

def _load_sent_ids() -> set:
    try:
        with open(NEWS_SENT_PATH, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_sent_ids(ids: set):
    # Only keep last 200 to prevent file growing forever
    recent = list(ids)[-200:]
    with open(NEWS_SENT_PATH, "w") as f:
        json.dump(recent, f)

def fetch_news_titles(limit=15) -> List[str]:
    """Fetch BTC news titles from SoSoValue, skip already sent."""
    api_key = os.environ.get("SOSOVALUE_API_KEY")
    if not api_key:
        return []
    try:
        r = requests.get(f"{SOSOVALUE_API}/news/featured/currency",
                         headers={"x-soso-api-key": api_key},
                         params={"currency": "BTC", "pageNum": 1, "pageSize": limit},
                         timeout=10)
        r.raise_for_status()
        items = r.json().get("data", {}).get("list", [])
        sent_ids = _load_sent_ids()
        titles = []
        new_ids = set()
        for item in items:
            nid = item.get("id", "")
            if nid in sent_ids:
                continue
            en = [m for m in item.get("multilanguageContent", []) if m["language"] == "en"]
            title = en[0].get("title") if en else None
            if title and len(title.strip()) > 10:
                titles.append(title.strip())
                new_ids.add(nid)
        # Save all (old + new)
        _save_sent_ids(sent_ids | new_ids)
        return titles
    except Exception:
        return []

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

def fetch_daily_klines(symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
    data = get(f"{BINANCE_FAPI}/fapi/v1/klines",
               {"symbol": symbol, "interval": "1d", "limit": limit})
    return [{
        "time":   datetime.fromtimestamp(k[0]/1000, tz=timezone.utc),
        "open":   float(k[1]),
        "high":   float(k[2]),
        "low":    float(k[3]),
        "close":  float(k[4]),
        "volume": float(k[5]),
    } for k in data]

def fetch_etf_flow() -> Optional[Dict]:
    api_key = os.environ.get("SOSOVALUE_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.get(f"{SOSOVALUE_API}/etfs/summary-history",
                         headers={"x-soso-api-key": api_key},
                         params={"symbol": "BTC", "country_code": "US", "limit": 10},
                         timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return None
        from collections import defaultdict
        by_date = defaultdict(list)
        for d in data:
            by_date[d["date"]].append(d)
        latest_date = max(by_date.keys())
        daily = min(by_date[latest_date], key=lambda x: abs(x["total_value_traded"]))
        return {"date": latest_date, "flow": daily["total_net_inflow"], "assets": daily["total_net_assets"]}
    except Exception:
        return None

# ============================================================
# Technical helpers
# ============================================================
def bollinger(closes: List[float], period: int = 20, std_mult: float = 2):
    if len(closes) < period:
        return None, None, None
    sma = sum(closes[-period:]) / period
    variance = sum((c - sma)**2 for c in closes[-period:]) / period
    std = variance ** 0.5
    return sma, sma + std_mult * std, sma - std_mult * std

def calc_kd(klines: List[dict], period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    if len(klines) < period + smooth_k + smooth_d:
        return None, None
    raw_k = []
    for i in range(period - 1, len(klines)):
        window = klines[i - period + 1:i + 1]
        lowest = min(k["low"] for k in window)
        highest = max(k["high"] for k in window)
        raw_k.append((klines[i]["close"] - lowest) / (highest - lowest) * 100 if highest != lowest else 50)
    k_vals = []
    for i in range(smooth_k - 1, len(raw_k)):
        k_vals.append(sum(raw_k[i - smooth_k + 1:i + 1]) / smooth_k)
    d_vals = []
    for i in range(smooth_d - 1, len(k_vals)):
        d_vals.append(sum(k_vals[i - smooth_d + 1:i + 1]) / smooth_d)
    return k_vals[-1] if k_vals else None, d_vals[-1] if d_vals else None

def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def calc_rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    if len(closes) < period + 1:
        return [None] * len(closes)
    rsis: List[Optional[float]] = [None] * period
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    rsis.append(100 - 100 / (1 + ag / al) if al else 100)
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        rsis.append(100 - 100 / (1 + ag / al) if al else 100)
    return rsis

def calc_macd(closes: List[float], fast=12, slow=26, sig=9):
    if len(closes) < slow + sig:
        return None, None, None
    k_f = 2 / (fast + 1)
    ema_f = [sum(closes[:fast]) / fast]
    for c in closes[fast:]:
        ema_f.append(c * k_f + ema_f[-1] * (1 - k_f))
    k_s = 2 / (slow + 1)
    ema_s = [sum(closes[:slow]) / slow]
    for c in closes[slow:]:
        ema_s.append(c * k_s + ema_s[-1] * (1 - k_s))
    offset = slow - fast
    macd_line = [ema_f[i + offset] - ema_s[i] for i in range(len(ema_s))]
    k_sig = 2 / (sig + 1)
    sig_line = [sum(macd_line[:sig]) / sig]
    for m in macd_line[sig:]:
        sig_line.append(m * k_sig + sig_line[-1] * (1 - k_sig))
    off2 = sig - 1
    hist = [macd_line[i + off2] - sig_line[i] for i in range(len(sig_line))]
    return macd_line, sig_line, hist

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

def score_ema(klines, daily_klines=None):
    """EMA50 + EMA200 雙重確認（用日線）。有日線用日線，沒有 fallback 到 4h。"""
    if daily_klines and len(daily_klines) >= 201:
        closes = [k["close"] for k in daily_klines]
        price = closes[-1]
        e50 = ema(closes, 50)
        e200 = ema(closes, 200)
        if e50 is None or e200 is None:
            return 0.0, "日線資料不足"
        d50 = (price / e50 - 1) * 100
        d200 = (price / e200 - 1) * 100
        if price > e50 and e50 > e200:
            return 1.0, f"強多：價格 > EMA50 > EMA200（EMA50 {d50:+.1f}%｜EMA200 {d200:+.1f}%）"
        if price < e50 and e50 < e200:
            return -1.0, f"強空：價格 < EMA50 < EMA200（EMA50 {d50:+.1f}%｜EMA200 {d200:+.1f}%）"
        if price > e50 and e50 < e200:
            return 0.3, f"弱多：價格 > EMA50 但 EMA50 < EMA200（可能只是反彈）"
        if price < e50 and e50 > e200:
            return -0.3, f"弱空：價格 < EMA50 但 EMA50 > EMA200（可能只是回調）"
        return 0.0, f"中性：EMA50 ${e50:,.0f}（{d50:+.1f}%）EMA200 ${e200:,.0f}（{d200:+.1f}%）"
    # Fallback: 4h EMA50
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
    # 線性映射：0(極度恐懼)→+1.0, 50(中性)→0.0, 100(極度貪婪)→-1.0
    score = -(value - 50) / 50  # [-1, +1]
    if value < 25:
        label = "極度恐懼"
    elif value < 40:
        label = "恐懼"
    elif value <= 60:
        label = "中性"
    elif value <= 75:
        label = "貪婪"
    else:
        label = "極度貪婪"
    return round(score, 2), f"F&G {value}/100（{label}）{score:+.2f}"

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
# Main — Dashboard mode
# ============================================================
# 行情 components (derivatives)
MARKET_COMPONENTS = [
    ("Funding rate",   1.5, "funding"),
    ("OI vs price",    1.2, "oi_price"),
    ("Retail L/S",     0.8, "retail"),
    ("Top trader L/S", 1.0, "top"),
]

# 宏觀 components
MACRO_COMPONENTS = [
    ("Price vs EMA50", 1.0, "ema"),
    ("Fear & Greed",   0.5, "fng"),
]

def calc_category_score(scored):
    total = sum(s * w for _, s, w, _ in scored)
    max_w = sum(w for _, _, w, _ in scored)
    return (total / max_w * 100) if max_w else 0

def detect_reversals(daily_klines) -> List[str]:
    """Detect trend reversal signals from daily klines."""
    signals = []
    closes = [k["close"] for k in daily_klines]
    if len(closes) < 52:
        return signals

    # 1. Price vs daily EMA50 crossover
    ema50_now = ema(closes, 50)
    ema50_prev = ema(closes[:-1], 50)
    if ema50_now and ema50_prev:
        if closes[-2] < ema50_prev and closes[-1] > ema50_now:
            signals.append(f"🟢 價格上穿日線 EMA50（偏多）")
        elif closes[-2] > ema50_prev and closes[-1] < ema50_now:
            signals.append(f"🔴 價格下穿日線 EMA50（偏空）")

    # 2. EMA20/EMA50 golden/death cross
    if len(closes) >= 52:
        ema20_now = ema(closes, 20)
        ema20_prev = ema(closes[:-1], 20)
        if ema20_now and ema20_prev and ema50_now and ema50_prev:
            if ema20_prev < ema50_prev and ema20_now > ema50_now:
                signals.append(f"🟢 日線 EMA20 上穿 EMA50 金叉（偏多）")
            elif ema20_prev > ema50_prev and ema20_now < ema50_now:
                signals.append(f"🔴 日線 EMA20 下穿 EMA50 死叉（偏空）")

    # 3. RSI extreme zone exit
    rsi_vals = calc_rsi(closes, 14)
    if len(rsi_vals) >= 2 and rsi_vals[-1] is not None and rsi_vals[-2] is not None:
        if rsi_vals[-2] < 30 and rsi_vals[-1] >= 30:
            signals.append(f"🟢 RSI 脫離超賣區 {rsi_vals[-1]:.0f}（偏多）")
        elif rsi_vals[-2] > 70 and rsi_vals[-1] <= 70:
            signals.append(f"🔴 RSI 脫離超買區 {rsi_vals[-1]:.0f}（偏空）")

    # 4. MACD histogram flip
    _, _, hist = calc_macd(closes)
    if hist and len(hist) >= 2:
        if hist[-2] < 0 and hist[-1] > 0:
            signals.append(f"🟢 MACD histogram 由負轉正（偏多）")
        elif hist[-2] > 0 and hist[-1] < 0:
            signals.append(f"🔴 MACD histogram 由正轉負（偏空）")

    return signals

def run_once(symbol: str = "BTCUSDT", output_json: bool = False):
    sym_name = SYMBOL_DISPLAY.get(symbol, symbol)
    ts = datetime.now(timezone.utc)
    if not output_json:
        print(f"Fetching {sym_name}...", flush=True)

    klines  = fetch_klines_4h(symbol, 80)
    rates   = fetch_funding_rate(symbol)
    oi_hist = fetch_open_interest_hist(symbol)
    ls_hist = fetch_long_short_ratio(symbol)
    tt_hist = fetch_top_trader_ratio(symbol)
    fng     = fetch_fear_greed()
    daily   = fetch_daily_klines(symbol, 250)
    etf     = fetch_etf_flow() if symbol == "BTCUSDT" else None
    fed_rate = fetch_fed_rate() if symbol == "BTCUSDT" else None

    price = klines[-1]["close"]
    chg_24h = (klines[-1]["close"] / klines[-7]["close"] - 1) * 100

    # Score 行情 (derivatives)
    market_scored = []
    for name, weight, key in MARKET_COMPONENTS:
        if   key == "funding":  s, why = score_funding(rates)
        elif key == "oi_price": s, why = score_oi_vs_price(oi_hist, klines)
        elif key == "retail":   s, why = score_ls_retail(ls_hist)
        elif key == "top":      s, why = score_top_trader(tt_hist)
        else:                   s, why = 0, "?"
        market_scored.append((name, s, weight, why))
    market_pct = calc_category_score(market_scored)

    # Score 宏觀
    macro_scored = []
    for name, weight, key in MACRO_COMPONENTS:
        if   key == "ema":  s, why = score_ema(klines, daily)
        elif key == "fng":  s, why = score_fng(fng)
        else:               s, why = 0, "?"
        macro_scored.append((name, s, weight, why))
    macro_pct = calc_category_score(macro_scored)

    # V4 Prediction: Bollinger + confirmations
    closes_4h = [k["close"] for k in klines]
    mid, upper, lower = bollinger(closes_4h, 20, 2)
    v4_decision = "WAIT"
    v4_reasons = []
    if mid and len(klines) >= 2:
        prev_price = klines[-2]["close"]
        touch_lower = prev_price >= lower and price < lower
        touch_upper = prev_price <= upper and price > upper

        if touch_lower or touch_upper:
            # Check confirmations
            rsi_vals = calc_rsi(closes_4h, 14)
            rsi_now = rsi_vals[-1] if rsi_vals and rsi_vals[-1] is not None else None
            kd_k, kd_d = calc_kd(klines, 14, 3, 3)
            _, _, m_hist = calc_macd(closes_4h)
            hist_now = m_hist[-1] if m_hist and len(m_hist) >= 1 else None
            hist_prev = m_hist[-2] if m_hist and len(m_hist) >= 2 else None

            confirms = []
            if touch_lower:
                v4_reasons.append(f"價格觸及布林下軌 ${lower:,.0f}")
                if rsi_now and rsi_now < 30:
                    confirms.append(f"RSI {rsi_now:.0f} 超賣")
                if kd_k and kd_k < 20:
                    confirms.append(f"K {kd_k:.0f} 超賣")
                if hist_now and hist_prev and hist_now < 0 and hist_now > hist_prev:
                    confirms.append("MACD 空頭收斂")
                if len(confirms) >= 1:
                    v4_decision = "LONG"
            elif touch_upper:
                v4_reasons.append(f"價格觸及布林上軌 ${upper:,.0f}")
                if rsi_now and rsi_now > 70:
                    confirms.append(f"RSI {rsi_now:.0f} 超買")
                if kd_k and kd_k > 80:
                    confirms.append(f"K {kd_k:.0f} 超買")
                if hist_now and hist_prev and hist_now > 0 and hist_now < hist_prev:
                    confirms.append("MACD 多頭收斂")
                if len(confirms) >= 1:
                    v4_decision = "SHORT"

            if confirms:
                v4_reasons.extend(confirms)

    # Band info for display
    band_info = None
    if mid:
        band_width = (upper - lower) / mid * 100
        band_info = {
            "mid": mid, "upper": upper, "lower": lower,
            "width": band_width,
            "pos": "上軌上方" if price > upper else ("下軌下方" if price < lower else "通道內"),
        }

    # Trend reversals
    reversals = detect_reversals(daily)

    # ETF + Fed rate info
    etf_str = None
    if etf:
        flow = etf["flow"]
        assets = etf["assets"]
        etf_str = f"ETF {etf['date']} 淨流{'入' if flow > 0 else '出'} ${abs(flow)/1e6:,.0f}M｜總資產 ${assets/1e9:,.1f}B"
    fed_str = fed_rate  # fetch_fed_rate() already returns formatted string

    result = {
        "symbol": symbol,
        "timestamp": ts.isoformat(),
        "price": price,
        "chg_24h_pct": chg_24h,
        "v4_decision": v4_decision,
        "v4_reasons": v4_reasons,
        "band_info": band_info,
        "market_score": market_pct,
        "macro_score": macro_pct,
        "market_details": [{"name": n, "score": s, "weight": w, "reason": why} for n, s, w, why in market_scored],
        "macro_details": [{"name": n, "score": s, "weight": w, "reason": why} for n, s, w, why in macro_scored],
        "reversals": reversals,
        "etf": etf_str,
        "fed_rate": fed_str,
    }

    if not output_json:
        print_dashboard(sym_name, ts, price, chg_24h, v4_decision, v4_reasons, band_info,
                        market_pct, market_scored, macro_pct, macro_scored, etf_str, reversals, fed_str)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))

    return result

DECISION_LABEL = {"LONG": "做多", "SHORT": "做空", "WAIT": "觀望"}
DECISION_EMOJI = {"LONG": "🟢", "SHORT": "🔴", "WAIT": "🟡"}

def print_dashboard(name, ts, price, chg_24h, v4_decision, v4_reasons, band_info,
                    market_pct, market_scored, macro_pct, macro_scored, etf_str, reversals, fed_str=None):
    bar = "═" * 40
    div = "─" * 38

    print(f"\n  {bar}")
    print(f"  {name} 市場狀態 — {ts.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  {bar}\n")
    print(f"  💰 ${price:,.2f}   24h: {chg_24h:+.2f}%\n")

    # V4 Prediction
    d_emoji = DECISION_EMOJI.get(v4_decision, "⚪")
    d_label = DECISION_LABEL.get(v4_decision, v4_decision)
    print(f"  {div}")
    print(f"  {d_emoji} 預測：{v4_decision}（{d_label}）")
    if v4_reasons:
        for r in v4_reasons:
            print(f"     • {r}")
    elif band_info:
        print(f"     • 布林通道{band_info['pos']}，無觸發信號")
    print(f"  {div}\n")

    # Bollinger info
    if band_info:
        print(f"  📊 布林通道（20,2）寬度 {band_info['width']:.1f}%")
        print(f"     上軌 ${band_info['upper']:,.0f} ｜ 中軌 ${band_info['mid']:,.0f} ｜ 下軌 ${band_info['lower']:,.0f}")
        print()

    # 行情
    m_emoji = "🟢" if market_pct > 10 else ("🔴" if market_pct < -10 else "⚪")
    m_label = "偏多" if market_pct > 10 else ("偏空" if market_pct < -10 else "中性")
    print(f"  {div}")
    print(f"  {m_emoji} 行情（衍生品）{market_pct:+.1f}%（{m_label}）")
    print(f"  {div}")
    for _, s, w, why in market_scored:
        print(f"     • {why}")
    print()

    # 宏觀（每個維度獨立顯示分數）
    print(f"  {div}")
    print(f"  🌍 宏觀")
    print(f"  {div}")
    for _, s, w, why in macro_scored:
        print(f"     • {why}")
    if etf_str:
        print(f"     • {etf_str}")
    if fed_str:
        print(f"     • {fed_str}")
    print()

    # 趨勢反轉
    if reversals:
        print(f"  {div}")
        print(f"  🔄 趨勢反轉訊號")
        print(f"  {div}")
        for r in reversals:
            print(f"     {r}")
        print()

    # 底部總覽
    print(f"  {bar}")
    macro_parts = [f"{n} {s:+.2f}" for n, s, w, _ in macro_scored]
    print(f"  行情 {market_pct:+.1f}% ｜ {' ｜ '.join(macro_parts)}")
    print(f"  {bar}")

def send_discord(webhook: str, result: dict):
    name = SYMBOL_DISPLAY.get(result.get("symbol", ""), "")
    mp = result["market_score"]
    v4 = result.get("v4_decision", "WAIT")
    v4_emoji = DECISION_EMOJI.get(v4, "⚪")
    v4_label = DECISION_LABEL.get(v4, v4)
    m_emoji = "🟢" if mp > 10 else ("🔴" if mp < -10 else "⚪")
    band = result.get("band_info")

    header = [
        f"{v4_emoji} **{name} — {v4}（{v4_label}）**",
        f"💰 ${result['price']:,.0f}　24h: {result['chg_24h_pct']:+.2f}%",
    ]
    if result.get("v4_reasons"):
        for r in result["v4_reasons"]:
            header.append(f"　• {r}")

    detail = []
    if band:
        detail.append(f"📊 布林（20,2）寬 {band['width']:.1f}%")
        detail.append(f"  上 ${band['upper']:,.0f} ｜ 中 ${band['mid']:,.0f} ｜ 下 ${band['lower']:,.0f}")
        detail.append("")
    detail.append(f"{m_emoji} 行情（衍生品）{mp:+.1f}%")
    for c in result["market_details"]:
        detail.append(f"  • {c['reason']}")
    detail.append("")
    detail.append("🌍 宏觀")
    for c in result["macro_details"]:
        detail.append(f"  • {c['reason']}")
    if result.get("etf"):
        detail.append(f"  • {result['etf']}")
    if result.get("fed_rate"):
        detail.append(f"  • {result['fed_rate']}")

    lines = header + ["```"] + detail + ["```"]

    # Reversals
    if result.get("reversals"):
        lines.append("🔄 **趨勢反轉訊號**")
        for r in result["reversals"]:
            lines.append(r)

    macro_parts = [f"{c['name']} {c['score']:+.2f}" for c in result["macro_details"]]
    lines.append(f"行情 {mp:+.1f}% ｜ {' ｜ '.join(macro_parts)}")

    try:
        requests.post(webhook, json={"content": "\n".join(lines)}, timeout=10)
    except Exception as e:
        print(f"Discord send failed: {e}", file=sys.stderr)

def build_daily_sentiment(results: List[dict]) -> str:
    """根據所有 symbol 的結果產生每日情緒摘要。"""
    ts = datetime.now(timezone.utc)
    lines = [
        f"📋 **每日市場情緒摘要** — {ts.strftime('%Y-%m-%d')}",
        "",
    ]

    for r in results:
        name = SYMBOL_DISPLAY.get(r.get("symbol", ""), "")
        price = r["price"]
        chg = r["chg_24h_pct"]
        mp = r["market_score"]
        v4 = r.get("v4_decision", "WAIT")
        band = r.get("band_info")

        # Overall sentiment
        signals_bull = []
        signals_bear = []
        signals_neutral = []

        # V4
        if v4 == "LONG":
            signals_bull.append(f"V4 觸發 LONG")
        elif v4 == "SHORT":
            signals_bear.append(f"V4 觸發 SHORT")

        # Market score
        if mp > 10:
            signals_bull.append(f"行情偏多 {mp:+.1f}%")
        elif mp < -10:
            signals_bear.append(f"行情偏空 {mp:+.1f}%")
        else:
            signals_neutral.append(f"行情中性 {mp:+.1f}%")

        # Macro details
        for c in r.get("macro_details", []):
            if c["score"] > 0.3:
                signals_bull.append(c["reason"][:40])
            elif c["score"] < -0.3:
                signals_bear.append(c["reason"][:40])

        # Reversals
        for rev in r.get("reversals", []):
            if "🟢" in rev:
                signals_bull.append(rev.replace("🟢 ", ""))
            elif "🔴" in rev:
                signals_bear.append(rev.replace("🔴 ", ""))

        # ETF
        if r.get("etf") and "流出" in r["etf"]:
            signals_bear.append(r["etf"][:40])
        elif r.get("etf") and "流入" in r["etf"]:
            signals_bull.append(r["etf"][:40])

        # Determine overall
        bull_count = len(signals_bull)
        bear_count = len(signals_bear)
        if bull_count > bear_count + 1:
            sentiment = "🟢 偏多"
        elif bear_count > bull_count + 1:
            sentiment = "🔴 偏空"
        else:
            sentiment = "🟡 中性偏觀望"

        lines.append(f"**{name}** ${price:,.0f}（24h {chg:+.1f}%）→ {sentiment}")
        if signals_bull:
            lines.append(f"  多：{' / '.join(signals_bull[:3])}")
        if signals_bear:
            lines.append(f"  空：{' / '.join(signals_bear[:3])}")
        if band:
            lines.append(f"  布林：{band['pos']}（寬 {band['width']:.1f}%）")
        lines.append("")

    # Fed rate (from first BTC result)
    btc_result = next((r for r in results if r.get("symbol") == "BTCUSDT"), None)
    if btc_result and btc_result.get("fed_rate"):
        lines.append(f"💵 {btc_result['fed_rate']}")

    return "\n".join(lines)

def append_csv(path: str, result: dict):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["symbol", "timestamp", "price", "chg_24h_pct", "v4_decision", "market_score", "macro_score"])
        w.writerow([result.get("symbol", ""), result["timestamp"], result["price"], f"{result['chg_24h_pct']:.4f}",
                    result.get("v4_decision", ""), f"{result['market_score']:.1f}", f"{result['macro_score']:.1f}"])

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

HELP_SIGNAL = """
══════════════════════════════════════════
  BTC/ETH Signal v4 — 使用說明
══════════════════════════════════════════

📌 預測模型（V4）
──────────────────────────────────────
  主信號：布林通道（20,2）均值回歸
  • 價格觸及下軌 → 候選 LONG
  • 價格觸及上軌 → 候選 SHORT
  • 通道內 → WAIT

  確認指標（至少 1 個才觸發）：
  • RSI(14) < 30 超賣 / > 70 超買
  • KD(14,3,3) K < 20 超賣 / > 80 超買
  • MACD histogram 收斂

  🟢 LONG = 觸下軌 + 至少 1 個確認
  🔴 SHORT = 觸上軌 + 至少 1 個確認
  🟡 WAIT = 無觸發或無確認

📊 布林通道
──────────────────────────────────────
  上軌/中軌/下軌 + 寬度百分比
  寬度越窄 = 即將突破，寬度越寬 = 波動大

📈 行情（衍生品）
──────────────────────────────────────
  Funding rate — 正=多頭付費（偏空）負=空頭付費（偏多）
  OI vs 價格  — OI升+價跌=擠空 / OI升+價漲=過熱
  散戶多空比  — 反向指標（散戶做多→偏空）
  大戶多空比  — 跟單指標（大戶做多→偏多）

🌍 宏觀
──────────────────────────────────────
  EMA50+200 — 強多/弱多/弱空/強空 四級判讀
  F&G 0-100 — 線性打分（0=極度恐懼偏多，100=極度貪婪偏空）
  ETF 日流量 — BTC 現貨 ETF 淨流入/流出（僅供參考，有滯後性）
  Fed 利率   — 當前利率 + FOMC 預期年底升降碼數

🔄 趨勢反轉（有觸發才顯示）
──────────────────────────────────────
  • 價格穿越日線 EMA50
  • EMA20/EMA50 金叉/死叉
  • RSI 脫離超買/超賣區
  • MACD histogram 翻轉

⚠️ FOMC/CPI 提示（前後 6h 顯示）
──────────────────────────────────────
  公布前後波動劇烈，技術面可能失效

⚙️ 用法
──────────────────────────────────────
  python btc_4h_signal.py                # 跑一次
  python btc_4h_signal.py --json         # JSON 輸出
  python btc_4h_signal.py --loop         # 每 4h 自動跑
  python btc_4h_signal.py --log x.csv    # 記錄到 CSV
  python btc_4h_signal.py --help-signal  # 顯示本說明

⚠️ 這是研究框架，不是已驗證的交易策略。
══════════════════════════════════════════
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="JSON 輸出")
    ap.add_argument("--loop", action="store_true", help="持續每 4h 自動跑")
    ap.add_argument("--discord", default=os.environ.get("DISCORD_WEBHOOK"), help="Discord webhook URL (預設讀 .env)")
    ap.add_argument("--log", help="把每次結果 append 到 CSV")
    ap.add_argument("--help-signal", action="store_true", help="顯示 V4 模型說明")
    args = ap.parse_args()

    if args.help_signal:
        print(HELP_SIGNAL)
        return

    while True:
        all_results = []
        for i, symbol in enumerate(SYMBOLS):
            if i > 0 and not args.json:
                print("\n")
            try:
                result = run_once(symbol=symbol, output_json=args.json)
                all_results.append(result)
                if args.discord:
                    send_discord(args.discord, result)
                if args.log:
                    append_csv(args.log, result)
            except Exception as e:
                name = SYMBOL_DISPLAY.get(symbol, symbol)
                print(f"Error ({name}): {e}", file=sys.stderr)
        # Daily sentiment summary (at SENTIMENT_HOUR local time)
        now_local = datetime.now()
        if now_local.hour == SENTIMENT_HOUR and all_results and args.discord:
            sentiment_msg = build_daily_sentiment(all_results)
            if not args.json:
                print(f"\n{sentiment_msg}")
            try:
                requests.post(args.discord, json={"content": sentiment_msg}, timeout=10)
            except Exception:
                pass
        # Collect all hints
        hints = []
        macro_hint = check_macro_event(datetime.now(timezone.utc))
        if macro_hint:
            hints.append(macro_hint)
        # Backtest reminder (V4 data collection started 2026-05-19, backtest 2026-06-16)
        backtest_date = datetime(2026, 6, 16, tzinfo=timezone.utc)
        days_left = (backtest_date - datetime.now(timezone.utc)).days
        if days_left == 0:
            hints.append("📋 V4 回測日期已到！請執行回測。")
        if hints:
            hint_block = "\n".join(hints)
            if not args.json:
                print(f"\n  {'─' * 38}")
                for h in hints:
                    print(f"  {h}")
                print()
            if args.discord:
                try:
                    requests.post(args.discord, json={"content": hint_block}, timeout=10)
                except Exception:
                    pass
        # News headlines
        if ENABLE_NEWS:
            news = fetch_news_titles(15)
            if news:
                if not args.json:
                    print(f"\n  {'─' * 38}")
                    print(f"  📰 最新 BTC 相關新聞")
                    print(f"  {'─' * 38}")
                    for t in news[:10]:
                        print(f"  • {t[:80]}")
                    print()
                if args.discord:
                    lines = ["📰 **BTC 相關新聞**", "```"]
                    for t in news[:10]:
                        lines.append(f"• {t[:80]}")
                    lines.append("```")
                    try:
                        requests.post(args.discord, json={"content": "\n".join(lines)}, timeout=10)
                    except Exception:
                        pass
        if not args.loop:
            break
        sleep_to_next_4h()

if __name__ == "__main__":
    main()
