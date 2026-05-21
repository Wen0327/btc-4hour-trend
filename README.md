# BTC/ETH Signal v4

布林通道均值回歸 + KD/RSI/MACD 確認的市場狀態儀表板。
每 4 小時抓 Binance Futures 公開 API + 多個免費資料源，輸出預測信號 + 行情/宏觀分析。

## 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests
```

## 設定

將 `.env` 放在專案根目錄：

```
DISCORD_WEBHOOK=https://discord.com/api/webhooks/xxx
SOSOVALUE_API_KEY=your_key_here
FRED_API_KEY=your_key_here
GEMINI_API_KEY=your_key_here
```

- `DISCORD_WEBHOOK` — Discord 通知（必要）
- `SOSOVALUE_API_KEY` — ETF 流量 + 新聞標題（免費，[sosovalue.com/developer](https://sosovalue.com/developer)）
- `FRED_API_KEY` — Fed 利率 + FOMC 預測（免費，[fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/fred/)）
- `GEMINI_API_KEY` — 備用，目前未使用

## 用法

```bash
python btc_4h_signal.py                # 跑一次
python btc_4h_signal.py --json         # JSON 輸出
python btc_4h_signal.py --loop         # 每 4h 自動跑
python btc_4h_signal.py --log x.csv    # 記錄到 CSV
python btc_4h_signal.py --help-signal  # 顯示模型說明
```

Discord webhook 會自動從 `.env` 讀取，不需要手動傳 `--discord`。

### 排程（macOS launchd）

已設定 `~/Library/LaunchAgents/com.btc4h.signal.plist`，每 4h 自動執行。

```bash
# 查看狀態
launchctl list | grep btc4h

# 停止
launchctl unload ~/Library/LaunchAgents/com.btc4h.signal.plist

# 重新載入
launchctl load ~/Library/LaunchAgents/com.btc4h.signal.plist
```

### 排程（cron 替代方案）

```bash
crontab -e
5 0,4,8,12,16,20 * * * cd /path/to/project && .venv/bin/python3 btc_4h_signal.py --log signals.csv >> run.log 2>&1
```

## V4 預測模型

**主信號**：布林通道（20,2）均值回歸
- 價格觸及下軌 → 候選 LONG
- 價格觸及上軌 → 候選 SHORT
- 通道內 → WAIT

**確認指標**（至少 1 個才觸發）：
- RSI(14) < 30 超賣 / > 70 超買
- KD(14,3,3) K < 20 超賣 / > 80 超買
- MACD histogram 收斂

回測結果（2.5 個月，23 筆信號）：24h 勝率 60.9%，PF 3.32。

## 儀表板資訊

### 行情（衍生品）
| 指標 | 說明 |
|---|---|
| Funding rate | 正=多頭付費（偏空）、負=空頭付費（偏多） |
| OI vs 價格 | OI升+價跌=擠空 / OI升+價漲=過熱 |
| 散戶多空比 | 反向指標 |
| 大戶多空比 | 跟單指標 |

### 宏觀
| 指標 | 說明 |
|---|---|
| EMA50+200 | 日線雙重確認，強多/弱多/弱空/強空 |
| F&G 0-100 | 線性打分，恐懼偏多、貪婪偏空 |
| ETF 日流量 | BTC 現貨 ETF 淨流入/流出（有滯後性） |
| Fed 利率 | 當前利率 + FOMC 預期年底升降碼數 |

### 趨勢反轉訊號（有觸發才顯示）
- 價格穿越日線 EMA50
- EMA20/EMA50 金叉/死叉
- RSI 脫離超買/超賣區
- MACD histogram 翻轉

### FOMC/CPI 提示
公布前後 6h 自動顯示警告。2026 年日期已硬編，年底需更新。

## 重要警告

**這是研究框架，不是已驗證的交易策略。**

1. V4 模型的回測樣本偏少（23 筆），不能作為上實盤的依據
2. 均值回歸策略在趨勢行情中可能被打爆
3. 腳本沒有風控（無 stop loss、position sizing、max drawdown halt）
4. 預計 2026-06-16 用 4 週累積的 live data 回測驗證

## 資料源

| 來源 | 用途 | 費用 |
|---|---|---|
| Binance Futures API | K線、Funding、OI、多空比 | 免費 |
| Alternative.me | Fear & Greed Index | 免費 |
| SoSoValue API | ETF 流量、新聞標題 | 免費（需 key） |
| FRED API | Fed 利率、FOMC 預測 | 免費（需 key） |
