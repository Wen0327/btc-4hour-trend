#!/usr/bin/env python3
"""
fetch_history.py — 從 Yahoo Finance 抓 BTC 全部歷史日線資料 + 視覺化

用法:
  python fetch_history.py                    # 抓 BTC-USD + 出圖
  python fetch_history.py --symbol ETH-USD   # 抓 ETH
  python fetch_history.py --no-chart         # 只存 CSV 不出圖
"""
import argparse
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

def make_chart(df, symbol, output_png):
    closes = df["Close"]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10), height_ratios=[3, 1],
                                    gridspec_kw={"hspace": 0.05})
    fig.patch.set_facecolor("#1a1a2e")

    # Price (log scale)
    ax1.set_facecolor("#1a1a2e")
    ax1.plot(closes.index, closes.values, color="#00d4ff", linewidth=0.8)
    ax1.fill_between(closes.index, closes.values, alpha=0.1, color="#00d4ff")
    ax1.set_yscale("log")
    ax1.set_ylabel("Price (USD, log)", color="white", fontsize=12)
    ax1.set_title(f"{symbol} All-Time Price History", color="white", fontsize=16, pad=15)
    ax1.tick_params(colors="white")
    ax1.spines[:].set_color("#333")
    ax1.grid(True, alpha=0.15, color="white")
    ax1.set_xlim(closes.index[0], closes.index[-1])

    # Milestones
    milestones = [
        ("2017-12", "2017 Bull\n~$19K", 19000),
        ("2018-12", "2018 Bear\n~$3.2K", 3200),
        ("2021-04", "2021 ATH\n~$64K", 64000),
        ("2022-11", "FTX Crash\n~$16K", 16000),
        ("2024-03", "ETF Rally\n~$73K", 73000),
    ]
    for date_str, label, price in milestones:
        try:
            idx = closes.index.get_indexer([date_str], method="nearest")[0]
            if 0 <= idx < len(closes):
                ax1.annotate(label, xy=(closes.index[idx], closes.values[idx]),
                           fontsize=7, color="#ffcc00", ha="center",
                           arrowprops=dict(arrowstyle="-", color="#ffcc00", alpha=0.5),
                           xytext=(0, 30), textcoords="offset points")
        except Exception:
            pass

    # Latest price
    latest = closes.values[-1]
    latest_date = closes.index[-1].strftime("%Y-%m-%d")
    ax1.annotate(f"${latest:,.0f}\n{latest_date}", xy=(closes.index[-1], latest),
                fontsize=9, color="#00ff88", ha="right", fontweight="bold")

    # Volume
    if "Volume" in df.columns:
        vol = df["Volume"]
        ax2.set_facecolor("#1a1a2e")
        ax2.bar(vol.index, vol.values, width=1, color="#00d4ff", alpha=0.3)
        ax2.set_ylabel("Volume", color="white", fontsize=12)
        ax2.tick_params(colors="white")
        ax2.spines[:].set_color("#333")
        ax2.grid(True, alpha=0.15, color="white")
        ax2.set_xlim(closes.index[0], closes.index[-1])
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax2.xaxis.set_major_locator(mdates.YearLocator())

    ax1.set_xticklabels([])

    plt.savefig(output_png, dpi=150, bbox_inches="tight", facecolor="#1a1a2e")
    plt.close()
    print(f"  Chart saved to {output_png}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC-USD", help="Yahoo Finance ticker (default: BTC-USD)")
    ap.add_argument("--output", default=None, help="輸出 CSV 檔名")
    ap.add_argument("--no-chart", action="store_true", help="不產生圖表")
    args = ap.parse_args()

    csv_out = args.output or f"{args.symbol.replace('-', '_').lower()}_history.csv"
    png_out = csv_out.replace(".csv", ".png")

    print(f"Fetching {args.symbol} full history...", flush=True)
    df = yf.download(args.symbol, period="max", interval="1d", progress=False)

    if df.empty:
        print("No data returned!")
        return

    # Flatten multi-level columns
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    print(f"  {len(df)} rows")
    print(f"  {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")

    df.to_csv(csv_out)
    print(f"  CSV saved to {csv_out}")

    if not args.no_chart:
        make_chart(df, args.symbol, png_out)

if __name__ == "__main__":
    main()
