# BTC 4H Signal

每 4 小時抓 Binance Futures 公開 API + Fear & Greed,加權打 7 項分數,輸出 LONG / SHORT / WAIT。

## 安裝
```bash
pip install requests
python btc_4h_signal.py
```

不需要任何 API key。所有資料源都是公開 endpoint。

## 用法

```bash
# 跑一次,印到 stdout
python btc_4h_signal.py

# JSON 輸出(給其他程式吃)
python btc_4h_signal.py --json

# 持續跑,每 4h 邊界(UTC 00:00 / 04:00 / 08:00 ...)自動觸發
python btc_4h_signal.py --loop

# 結果送 Discord
python btc_4h_signal.py --discord https://discord.com/api/webhooks/xxx

# 每次結果 append 到 CSV(自動建立 header)
python btc_4h_signal.py --log signals.csv

# 全部一起
python btc_4h_signal.py --loop --discord https://... --log signals.csv
```

### 排程方式(macOS / Linux)
```bash
crontab -e
# 每 4h 整點過 5 分跑一次(避開 API rate limit 高峰)
5 0,4,8,12,16,20 * * * cd /Users/wen/btc-signal && /usr/bin/python3 btc_4h_signal.py --log signals.csv --discord https://... >> run.log 2>&1
```

### Windows 排程
用工作排程器(Task Scheduler),Action 設為:
- Program: `python`
- Args: `C:\path\to\btc_4h_signal.py --log C:\path\signals.csv`
- Trigger: 每 4h 重複

## 七個維度

| 維度 | 權重 | +1 條件 | -1 條件 |
|---|---|---|---|
| 4h candle | 1.0 | 上根突破前高 + 漲 >0.3% | 跌破前低 + 跌 <-0.3% |
| Funding rate | 1.5 | 仍負且續轉負 | 仍正且續轉正 |
| OI vs price | 1.2 | OI 升 + 價跌(擠空 setup) | OI 升 + 價漲(過熱) |
| Retail L/S | 0.8 | <0.8(散戶偏空 → 反向) | >1.5(散戶偏多 → 反向) |
| Top trader L/S | 1.0 | >1.5(大戶偏多、跟單) | <0.8(大戶偏空、跟單) |
| 現價 vs EMA50(4h) | 1.0 | 高於 +0.5% | 低於 -0.5% |
| F&G Index | 0.5 | <30(恐懼、反向) | >70(貪婪、反向) |

**最高總分 ±6.5。Confidence = total / max_weight ∈ [-1, +1]**

- Confidence > +30% → LONG
- Confidence < -30% → SHORT
- 中間 → WAIT

## 否決條件(任一觸發直接 WAIT)

- 4h ATR / 價格 < 0.5%(波動異常低、等突破)

## 必須手動補的東西

腳本沒做到的(你自己加 veto):
1. **FOMC / CPI 公布前後 6 小時** — 改成 WAIT
2. **ETF 單日流出 > $300M** — 改成 WAIT 或加重 SHORT 權重
3. **Coinglass 清算地圖** — 免費 API 有限,要付費才能拿即時 heatmap
4. **美股盤中跌 >1.5%** — BTC 跟美股相關性 0.6,大跌時加重 SHORT

如果你要加,在 `check_veto()` 函式裡擴充就好。

## 重要警告

**這只是一個 starting framework,不是已驗證的 alpha**。

1. 七個權重和兩個 threshold(+0.3 / -0.3)都是猜的。**先 paper trade 至少 4 週**,記錄每筆 signal 和實際結果,再調參數。
2. 不要直接 leverage 拿真金實彈跑。signals.csv 跑一個月後,把每筆 signal 假裝下 $100 倉位,看 win rate 和 R:R。低於 win rate 45% 或 PF < 1.2 → 不能上實盤。
3. **這個腳本沒做風控**。沒有 stop loss、沒有 position sizing、沒有 max drawdown halt。實盤要自己接這些。
4. Binance Futures 公開 API rate limit 寬鬆(2400 req/min),每 4h 跑一輪只用 7 個 request,沒問題。但若改成 1m / 5m 高頻,要加 backoff。

## Output 範例

```
================================================================
BTC 4H Signal — 2026-05-15 04:00:00 UTC
================================================================
Fetching...

BTC: $80,912.00   24h: +1.27%

     4h candle          +0.00 × 1.0 = +0.00  上根 4h +0.20%、區間內
  ↑↑ Funding rate       +1.00 × 1.5 = +1.50  資金費 -0.0200%、續轉負(空頭付費加重)
  ↑↑ OI vs price        +1.00 × 1.2 = +1.20  OI +0.85% + 價 -0.42% (新空進、擠空 setup)
  ↓↓ Retail L/S         -1.00 × 0.8 = -0.80  散戶多空比 1.82(散戶偏多 → 反向偏空)
  ↓↓ Top trader L/S     -1.00 × 1.0 = -1.00  大戶多空比 0.68(大戶偏空、跟單)
  ↑↑ Price vs EMA50     +1.00 × 1.0 = +1.00  現價 $80912 > 4h EMA50 $79998 (+1.14%)
     Fear & Greed       +0.00 × 0.5 = +0.00  F&G 38(中性)

Total: +1.90   Confidence: +30.2%

→ Decision: LONG
```

## 之後想擴充的方向

1. 連接discord webhook
2. 加 SoSoValue 拿 ETF 即時流量
3. 寫個簡單回測 module:讀 `signals.csv` + Binance 歷史 4h 線,算每個 signal 後 4h、12h、24h、48h 的實際報酬
4. 用回測結果反推最佳 threshold 和 weights(grid search)
