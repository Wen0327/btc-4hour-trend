# BTC/ETH Signal v4 — Claude Code 接手文件

> 給 Claude Code 讀的。目的是讓你不用問使用者太多問題就能接手。

---

## TL;DR

- **是什麼**：BTC/ETH 市場狀態儀表板 + V4 預測模型（布林通道 + KD/RSI/MACD 確認）
- **現狀**：已在 macOS launchd 排程運作，每 4h 推 Discord + 寫 CSV
- **階段**：Phase 1 資料收集中（2026-05-19 起），預計 **2026-06-16** 回測

---

## 使用者(Wen)

- 軟體工程師
- 會 Python，但不是主力語言
- 主要溝通語言：繁體中文。技術名詞中英混用 OK
- **個性**：直接，討厭客套和廢話。明確說過「別說軟話」「如果是過度擬合就直說」
- 主動交易者：TW 股、美股、BTC（做多做空都做）
- 已經懂的概念：資金費率、OI、清算地圖、技術分析、回測方法論
- **不要對他解釋基礎概念，除非他問**

---

## 溝通風格(請照做)

- **誠實第一**：不奉承、不給違背真實狀況的解答。數據說沒用就說沒用
- **沒辦法做的事直接說「沒辦法」**，不要繞圈
- 推他一把比哄他更有用——如果他在犯方法論錯誤，直接指出
- 短句、條列、密度高。**避免「正如你提到的」「很棒的問題」這類填空**
- 給交易看法時：給分析判讀 + 「這不是投資建議」一句帶過就好
- 不確定時：用 web search 抓即時資料。**會變的東西不要憑記憶**

---

## 專案結構

```
Btc Python/
├── CLAUDE.md              # 你正在看的這份
├── README.md              # 給人讀的用法文件
├── btc_4h_signal.py       # 主腳本（V4 模型 + 儀表板）
├── backtest.py            # V1 回測
├── backtest_v2.py         # V2（變化量）回測
├── backtest_v3.py         # V3（混合）回測
├── backtest_daily.py      # 日線 regime 回測
├── .env                   # API keys（不進 git）
├── .gitignore
├── .news_sent.json        # 新聞去重記錄（不進 git）
├── signals.csv            # 排程產出的資料（不進 git）
└── run.log                # 排程 log（不進 git）
```

---

## V4 模型架構

### 預測信號
- **主信號**：布林通道（20,2）均值回歸
  - 觸下軌 → 候選 LONG
  - 觸上軌 → 候選 SHORT
  - 通道內 → WAIT
- **確認指標**（至少 1 個才觸發）：
  - RSI(14) < 30 / > 70
  - KD(14,3,3) K < 20 / > 80
  - MACD histogram 收斂
- 回測結果（2.5 個月，23 筆）：24h 勝率 60.9%，PF 3.32

### 儀表板資訊（不參與預測，僅供參考）

**行情（衍生品）** — 加權打分
- Funding rate（權重 1.5）
- OI vs 價格（權重 1.2）
- 散戶多空比（權重 0.8，反向指標）
- 大戶多空比（權重 1.0，跟單指標）

**宏觀** — 各維度獨立顯示
- 日線 EMA50 + EMA200 雙重確認（強多/弱多/弱空/強空）
- F&G 0-100 線性打分
- BTC 現貨 ETF 日流量（SoSoValue，有滯後性）
- Fed 利率 + FOMC dot plot 年底預測（FRED API）

**趨勢反轉訊號**（有觸發才顯示）
- 價格穿越日線 EMA50
- EMA20/EMA50 金叉/死叉
- RSI 脫離超買/超賣區
- MACD histogram 翻轉

**FOMC/CPI 提示** — 前後 6h 顯示警告，2026 日期硬編，年底需更新

**新聞** — SoSoValue 抓 BTC 相關標題，目前 `ENABLE_NEWS = False`

### 資料源

| 來源 | 用途 | 費用 |
|---|---|---|
| Binance Futures API | K線、Funding、OI、多空比 | 免費 |
| Alternative.me | Fear & Greed Index | 免費 |
| SoSoValue API | ETF 流量、新聞標題 | 免費（需 key） |
| FRED API | Fed 利率、FOMC 預測 | 免費（需 key） |

---

## 排程

macOS launchd，每 4h 執行：
- plist: `~/Library/LaunchAgents/com.btc4h.signal.plist`
- Discord webhook 從 `.env` 讀取
- 結果寫入 `signals.csv` + `run.log`

---

## 模型演進歷史

| 版本 | 方法 | 結果 |
|---|---|---|
| V1 | 7 維度加權打分 | 24h PF 1.06，無 edge，LONG 偏見嚴重（89%） |
| V2 | 變化量 scoring | 2.5 個月只觸發 2 筆，無法驗證 |
| V3 | 條件+觸發混合 | 24h PF 1.64，樣本 14 筆太少 |
| 日線 regime | 4 維度日線 | 含熊市後 PF 0.61，虧錢 |
| RSI+MACD+EMA | 純技術面 | 24h PF 0.62，虧錢 |
| **V4 布林+確認** | 布林觸軌+KD/RSI/MACD | **24h 勝率 60.9%，PF 3.32**（23 筆） |

### 已驗證無效的方向
- BTC 與外部資產（SPY、MSTR、GLD、DXY）相關性不穩定，無預測力
- ETF 流量有滯後性，對 4h/24h 預測無效
- 加權打分框架預測方向沒有 edge（V1~V3 都試過）
- 均值回歸在趨勢行情可能失效（V4 的已知風險）

---

## Backlog

1. ~~FOMC/CPI + Fed 利率~~ ✅
2. ~~ETF 流量~~ ❌ 放棄
3. ~~美股 correlation~~ ❌ 放棄
4. ~~EMA200 雙重確認~~ ✅
5. ~~**回測 V4**~~ ❌ 4 週 0 筆信號，無法回測。已升級為 V5（Ichimoku+Fib+OBV）
6. **回測 V5** — V5 從 2026-06-15 開始收資料，預計 **2026-07-13** 回測
6. **風控層** — 實盤才需要
7. **實盤接 Binance Futures API** — Phase 2 通過前不做

---

## 不要做的事

- ❌ 在 Phase 1 期間調 V4 的布林參數或確認條件
- ❌ 沒跑過驗證就建議上實盤
- ❌ 推薦 > 2x 槓桿
- ❌ 把這個腳本包裝成「賺錢工具」——它是研究框架
- ❌ 接 Binance Futures 下單 API 前沒做 dry-run

---

## 如果使用者偏離計畫

他是成年人，自己決定。但把方法論的代價講清楚：
- 想跳過回測直接上實盤 → 講清楚樣本不足
- 想改布林參數 → 講清楚 p-hacking
- 想直接下 100x leverage → 拒絕協助

說完讓他決定。**不要重複講第二次**。

---

## 反面教材

之前回測過的失敗案例：
- 策略：FOMC/CPI 日 BTC 收盤 -5% 後買進
- 結果：13 筆、勝率 30.8%、PF 1.26、年化 1.79%
- 同期 buy-and-hold：CAGR 67.9%
- **教訓**：小樣本的 PF 沒意義，大多數策略只在特定 regime 有效
