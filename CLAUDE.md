# BTC 4H Signal — Claude Code 接手文件

> 這份檔案是給 Claude Code 讀的,不是給人。用 markdown 是因為人也可能會掃過去。
> 此專案前一輪是跟 Claude (claude.ai) 在 chat 對話設計的,腳本已寫完並 mock 測試過,
> 但**還沒在真實環境跑過**。你的任務:接手做剩下的事。

---

## TL;DR

- **是什麼**:BTC 4 小時方向判讀腳本,7 個維度加權打分 → LONG / SHORT / WAIT
- **誰寫的**:前一個 Claude 寫的 v0,在 sandbox 用 mock data 驗證 scoring 邏輯 OK
- **現狀**:`btc_4h_signal.py` 存在,語法乾淨,**沒在 user 本機跑過**
- **下一步**:幫使用者跑起來、排程、4 週紙上交易、然後一起回測調參

---

## 使用者(Wen)

- 台灣前端工程師(React/TS/Next.js 為主)
- 會 Python,但不是主力語言
- 主要溝通語言:繁體中文。技術名詞中英混用 OK
- **個性**:直接,討厭客套和廢話。明確說過「別說軟話」「如果是過度擬合就直說」
- 主動交易者:TW 股、美股、BTC(做多做空都做)
- 已經懂的概念:資金費率、OI、清算地圖、技術分析、回測方法論
- **不要對他解釋基礎概念,除非他問**

---

## 溝通風格(請照做)

- **沒辦法做的事直接說「沒辦法」**,不要繞圈。他特別在意這點
- 推他一把比哄他更有用——如果他在犯方法論錯誤,直接指出
- 短句、條列、密度高。**避免「正如你提到的」「很棒的問題」這類填空**
- 給交易看法時:給分析判讀 + 「這不是投資建議」一句帶過就好,不用反覆免責
- 不確定時:用 web search 抓即時資料。BTC 價格、ETF 流量、FOMC 日期這類**會變的東西不要憑記憶**

---

## 專案結構

```
btc-signal/
├── CLAUDE.md           # 你正在看的這份
├── README.md           # 給人讀的用法文件
├── btc_4h_signal.py    # 主腳本(7 維度評分 + 輸出)
└── (之後會有)
    ├── signals.csv     # 排程後每 4h append 結果
    └── run.log         # cron / launchd 的 stderr/stdout
```

---

## 腳本架構(讀過再改)

7 個評分維度,每個回傳 score ∈ [-1, +1] + 中文 reason 字串:

| 維度 | 權重 | 邏輯摘要 |
|---|---|---|
| 4h candle | 1.0 | 上一根 closed 4h 突破前高/低 |
| Funding rate | 1.5 | 仍負 + 續轉負 = 擠空 setup |
| OI vs price | 1.2 | OI 升 + 價跌 = 新空進場 |
| Retail L/S | 0.8 | **反向指標**:散戶偏多 → 看跌 |
| Top trader L/S | 1.0 | **跟單指標**:大戶偏多 → 看漲 |
| Price vs EMA50(4h) | 1.0 | 趨勢確認 |
| F&G Index | 0.5 | <30 反向多、>70 反向空 |

**Confidence = total / max_weight ∈ [-1, +1]**,門檻:
- > +0.30 → LONG
- < -0.30 → SHORT
- 中間 → WAIT

**Veto**:4h ATR / 價格 < 0.5% 直接 WAIT(波動太低,等突破)

**資料源**(都免費、不需 API key):
- `https://fapi.binance.com/...` — kline / funding / OI / L/S ratios
- `https://api.alternative.me/fng/` — F&G

---

## 立即要做的(使用者開 Claude Code 後)

### Step 0:確認環境
```bash
python3 --version    # 需 3.8+
python3 -c "import requests"  # 沒裝就 pip install requests
```

### Step 1:跑一次驗證 Binance API 可達
```bash
python3 btc_4h_signal.py
```
- 預期:印出 7 個維度評分 + 一個 decision
- 如果 timeout/connection error → 檢查網路或 IP 是不是被 Binance ban(部分地區會)
- 如果 JSON parse error → API schema 可能變了,讓 Claude Code 對著 Binance docs 修

### Step 2:設排程(macOS 用 launchd 或 cron)

使用者是 macOS。**推薦 launchd**(比 cron 在 macOS 上穩):

```xml
<!-- ~/Library/LaunchAgents/com.wen.btc4h.plist -->
<!-- 每 4h 跑一次,觸發時間 UTC 00:05 / 04:05 / 08:05 ... -->
```

詳細 plist 範例:你寫一個給他,記得處理 macOS 14+ 的 sandbox 權限。

或簡單版用 cron(macOS 14+ 要先給 cron Full Disk Access):
```cron
5 0,4,8,12,16,20 * * * cd ~/Downloads/Btc\ Python && /usr/bin/python3 btc_4h_signal.py --log signals.csv >> run.log 2>&1
```

注意:cron 用 UTC 0,4,8...等於台北 8,12,16,20,0,4。如果要對齊台北 4h 邊界,改成 `0,4,8,12,16,20` (Asia/Taipei) 即可,cron 看本地時區。

### Step 3:(可選)Discord webhook
讓他在 Discord 收到每次結果。建議建一個只有他自己的 server。

---

## Phase 1 — 接下來 4 週(不要跳過)

**規則**:
1. 排程跑、寫入 `signals.csv`,**不下實盤**
2. **不要動權重和門檻**——讓資料說話,不要 p-hacking
3. 每週看一次 csv,確認沒 bug,但不評估表現
4. 4 週後再回頭看

如果使用者中途想改參數、想下單,**直接提醒他**:「現在改是過度擬合的開始」「先讓資料跑完」。

---

## Phase 2 — 4 週後驗證

當 `signals.csv` 累積 ~150-160 筆(4 週 × 6 信號/天)後:

寫一個 `backtest.py`:
1. 讀 `signals.csv` 拿每筆 signal 的 timestamp + decision + confidence
2. 從 Binance 抓對應的 4h 線
3. 對每筆 signal,計算實際後續 4h / 12h / 24h / 48h 的報酬
4. 算每個 timeframe 的:
   - Win rate(LONG: 報酬 > 0;SHORT: 報酬 < 0)
   - Average return per signal
   - Profit factor
   - Sharpe(年化)
5. 跟「總是 LONG」和「總是 SHORT」對比看 alpha
6. 跟 buy-and-hold 對比

**通過標準**(全部要過):
- 至少 100 筆 signal(不含 WAIT)
- Win rate ≥ 50% 或 PF ≥ 1.3
- 跑出來比 buy-and-hold 有可比的風險調整後報酬
- 最差連續 5 筆 < 20% drawdown

**沒過就不能上實盤。不要找理由說服自己過了。**

---

## Backlog(過了 Phase 2 才動)

按優先序:

1. **加 FOMC / CPI 日曆 veto** — 公布前後 6h 強制 WAIT。FOMC 日期硬編進腳本,CPI 用第二個週二或硬編
2. **加 ETF 流量** — SoSoValue 有免費 endpoint(`/api/openapi/etf/...`),拿 BTC 現貨 ETF 日流量,單日流出 > $300M 強制 WAIT
3. **加美股 correlation veto** — 抓 SPY 4h klines(Binance 沒有,要用 yfinance 或 Alpaca),美股盤中跌 > 1.5% 加重 SHORT 權重
4. **EMA200 雙重確認** — 現有 EMA50 維度在盤整區間會產生噪音。加 EMA200 區分「真轉向」vs「只是回調」：價格 > EMA50 > EMA200 = 強多、價格 < EMA50 < EMA200 = 強空、不一致 = 打折。2026-05-18 測試結果：BTC 弱空（EMA50 > EMA200 但價格 < EMA50）、ETH 強空（價格 < EMA50 < EMA200）
5. **回測 module** — 用前 4 週累積的 signal 配合歷史 4h 線跑 grid search,優化權重和門檻
6. **風控層** — stop loss、position sizing、max DD halt。**只有要上實盤才需要**
7. **實盤接 Binance Futures API** — **絕對不要在 Phase 2 通過前做這個**

---

## 反面教材:之前回測過的一個策略(別重複)

前一輪我們回測過另一個策略給使用者看,**這是失敗案例**,作為他和你都該記住的方法論教訓:

- 策略:任何 FOMC/CPI 日 BTC 收盤 -5% 後,等 2 天確認站上低點,買進持 30 天或破低停損
- 10 年回測(2016-2026)
- 結果:**13 筆交易、勝率 30.8%、PF 1.26、年化等值 1.79%**、夏普 0.12
- 同期買入持有:總報酬 17,624%、CAGR 67.9%、夏普 1.10
- 結論:**樣本不足、過度擬合到 2017-2022 那段、2023 後完全沒觸發**

**教訓**:
- 「看起來不錯」的 PF 1.26 在 13 筆樣本下完全沒意義
- 大多數策略只在特定 regime 下有效,Out of sample 馬上死
- **任何輸給 buy-and-hold 的策略,風險調整後也要能解釋為什麼還值得跑**

如果使用者問你「我這個 idea 能不能跑?」——先問樣本量、先問 regime、先問跟 B&H 比。

---

## 當下市場 context(2026-05-15 handoff 當時)

抓的是 5/14-15 的資料,使用者跑腳本時請重新抓即時:

- BTC ~$80,900,5/14 探低 $79,640 反彈
- 區間 $76K(底) ~ $82K(頂),$82K 過去一週測 4 次都失敗
- 資金費負 ~70 天,擠空火藥充足
- **5/14 ETF 單日流出 $635M** — 結構性負面、regime change 訊號
- CPI 3.8% — Fed 2026 降息預期被清零
- 新 Fed Chair Kevin Warsh,偏鷹
- Strategy/Saylor 暗示可能賣 BTC 還股利
- 清算牆:$79.8-80.5K(多單)、$82K(空單)

**這些資訊只是給你接手時的背景,不要拿來給使用者交易建議**。腳本跑起來會自己判讀。

---

## 不要做的事

- ❌ 在 Phase 1 期間調權重、門檻、加維度
- ❌ 用「最近 1 週表現不好」的理由勸他改策略
- ❌ 沒跑過驗證就建議他上實盤
- ❌ 推薦他用 > 2x 槓桿——BTC 4h ATR 高,2x 就夠刺激
- ❌ 把這個腳本包裝成「賺錢工具」——它是研究框架
- ❌ 接 Binance Futures 下單 API 前沒做 dry-run

---

## 如果使用者偏離計畫怎麼辦

他是成年人,自己決定。但你的工作是把方法論的代價講清楚:

- 想跳過 Phase 1 直接上實盤 → 講清楚樣本不足會發生什麼
- 想加更多維度 → 講清楚為什麼 v0 要先驗證才能擴充
- 想改門檻 → 講清楚這就是 p-hacking 的起點
- 想直接下 100x leverage → 拒絕協助寫,告訴他理由

說完讓他自己決定。**不要重複講第二次**。

給 Claude Code 的一個小提醒
如果它在 Phase 1 期間勸你「最近一週訊號表現不好、要不要調權重」——這是它違反 CLAUDE.md 的訊號。叫它重讀「Phase 1」和「不要做的事」那兩段就好。

---

最後:這份 CLAUDE.md 寫得有點長,但目的是讓你不用問使用者太多問題就能接手。
真正的工作開始於 `python3 btc_4h_signal.py` 跑起來那一刻。祝順利。