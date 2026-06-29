#!/usr/bin/env python3
"""
fetch_spcx.py — 收集 SPCX 日線 + 5m 盤前盤後資料

用法:
  python fetch_spcx.py           # 更新所有資料
"""
import os
import yfinance as yf
import pandas as pd
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
DAILY_CSV = os.path.join(DIR, "spcx_history.csv")
INTRADAY_CSV = os.path.join(DIR, "spcx_intraday.csv")

def fetch_daily():
    print("Fetching SPCX daily...", flush=True)
    df = yf.download("SPCX", period="max", interval="1d", progress=False)
    if df.empty:
        print("  No daily data")
        return
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df.to_csv(DAILY_CSV)
    print(f"  {len(df)} rows → {DAILY_CSV}")

def fetch_intraday():
    print("Fetching SPCX 5m (prepost)...", flush=True)
    df = yf.download("SPCX", period="5d", interval="5m",
                     prepost=True, progress=False)
    if df.empty:
        print("  No intraday data")
        return
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    # Append to existing, deduplicate
    if os.path.exists(INTRADAY_CSV):
        existing = pd.read_csv(INTRADAY_CSV, index_col=0, parse_dates=True)
        df = pd.concat([existing, df])
        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()

    df.to_csv(INTRADAY_CSV)
    print(f"  {len(df)} total rows → {INTRADAY_CSV}")

    # Show session breakdown for latest day
    latest_date = df.index[-1].date()
    day_data = df[df.index.date == latest_date]
    if not day_data.empty:
        pre = day_data.between_time("04:00", "09:29")   # ET premarket
        regular = day_data.between_time("09:30", "15:59")  # ET regular
        post = day_data.between_time("16:00", "20:00")   # ET afterhours
        print(f"  {latest_date}: pre={len(pre)} regular={len(regular)} post={len(post)}")

def main():
    fetch_daily()
    fetch_intraday()

if __name__ == "__main__":
    main()
