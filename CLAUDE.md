# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Self-Improvement

每次被使用者糾正後，將錯誤模式與修正規則記錄到 `tasks/lessons.md`。
每次對話開始時，若任務與已記錄的教訓相關，先讀 `tasks/lessons.md`。

## 執行環境

- Python：一律使用 `.venv/bin/python`（系統 PATH 無 `python`）
- 排程：crontab，週一至五 UTC 15:00 起每小時（台北 23:00 起），週六 UTC 09:00-22:00 fetch-only
- Shadow run 觀察期中：`daily_pipeline.py --shadow`

## 常用指令

```bash
# 主流程（正式）
.venv/bin/python scripts/daily_pipeline.py

# 常用旗標
--dry-run          # 不推送 Telegram
--skip-fetch       # 重用昨日原始資料（注意：同時略過 LLM 分類）
--shadow           # 規則更新只記錄不寫入
--date 2026-05-08  # 補跑指定日期
--fetch-only       # 只抓資料（週末排程）

# 績效報告
.venv/bin/python scripts/generate_perf_report.py

# 審閱 DISCOVER 提案
.venv/bin/python scripts/review_shadow_proposals.py

# 驗證 YAML 格式
.venv/bin/python -c "from ruamel.yaml import YAML; y=YAML(); r=y.load(open('scripts/event_impact_rules.yml')); print(len(r), '條規則')"
```

## 專案結構

| 路徑 | 說明 |
|---|---|
| `scripts/daily_pipeline.py` | 主調度器（cron 入口），含完整 10 步流程 |
| `scripts/event_impact_rules.yml` | 規則知識庫（會自我演化） |
| `scripts/auto_update_rules.py` | YAML 自動改寫 + Circuit Breaker |
| `scripts/track_recommendation_performance.py` | 績效追蹤、持倉狀態更新、觸發 DOWNGRADE |
| `scripts/generate_perf_report.py` | 產生 PERFORMANCE.md |
| `scripts/review_shadow_proposals.py` | 互動式審閱 DISCOVER 提案 |
| `scripts/lib/llm_client.py` | LLM 抽象層（換後端只改 `.env`） |
| `scripts/lib/twse_client.py` | TWSE/TPEx 股價 API + yfinance 輔助 |
| `scripts/lib/market_filter.py` | 大盤多空狀態判斷（TWII、外資流向、S&P500） |
| `scripts/lib/config.py` | 所有設定集中於此，讀 `.env` |
| `data/performance/open_positions.json` | 現有持倉/觀察中部位（live state） |
| `data/performance/YYYYMM.csv` | 已結清部位月度歸檔 |
| `data/shadow_updates/` | Shadow run 提案暫存 |
| `tasks/lessons.md` | 錯誤教訓記錄 |
| `.env` | API Keys、Bot Token（不進 git） |

## 10 步 Pipeline 架構

`daily_pipeline.py::run()` 線性執行，步驟間傳遞 Python dict list：

```
1. FETCH          → 抓 Trump 推文 + 股癌筆記
2. CLASSIFY       → LLM 解析事件 (event, confidence, direction, summary)
3. MATCH RULES    → 查 YAML 規則 → 建議清單 (code, action, entry/target/stop)
4. MARKET FILTER  → 大盤偏多/偏空 → 過濾或降低信心值
5. ENRICH PRICES  → TWSE/TPEx 現價，設預設 entry±3%, target+5%, stop-5%
6. ENRICH TA      → yfinance 三個月資料 → LLM 批次精煉進出場價
7. PORTFOLIO DECISION → LLM 決定 new/add/raise_target/hold
8. SAVE REPORT    → data/reports/daily_report_YYYYMMDD.json
9. NOTIFY         → Telegram 推送
10. TRACK PERF    → 評估昨日建議 → 結清觸發部位 → 觸發 DOWNGRADE
11. DISCOVER      → LLM 發現新規則 → shadow 記錄或寫入 YAML
12. AUTO-REVIEW   → 自動核准/拒絕 shadow 提案
13. UPDATE REPORT → 更新 PERFORMANCE.md
```

**Idempotency**：若 `daily_report_YYYYMMDD.json` 已存在且未指定 `--date`，整個 pipeline 跳過。

## 持倉生命週期

`open_positions.json` 每筆紀錄 state 流轉：

```
watching → holding → (triggered_target | triggered_stop | expired | superseded)
```

- **watching**：觀察進場，每日檢查 `lo ≤ entry_high AND hi ≥ entry_low`
- **holding**：已進場，買進用 `hi ≥ target` / `lo ≤ stop_loss`；賣出邏輯反向
- `days_watched > 10`：自動以 `expired` 結清
- `actual_entry_price`：smart fill — 買進取當日區間最低，賣出取最高

## 關鍵設計

**賣出訊號邏輯反向**：`觀察賣出` 的 target/stop 判斷與買進完全相反，務必在 track_recommendation_performance.py 中分支處理。曾因未分支造成損益方向全錯。

**進場價硬截斷**：LLM 可能建議遙遠的 MA20 進場區，`_step_enrich_ta()` 後處理強制 `entry_low ≥ close × 0.95`，`entry_high ≤ close × 1.10`。

**規則知識庫演化**：
- DOWNGRADE：stop rate > 60% → bullish 降為 mixed
- REMOVE：從 sector stock list 移除代碼
- ADD：新增代碼
- DISCOVER：新增全新事件規則（shadow 模式先暫存）
- Circuit Breaker：異動比例 > 10% 則 HALT + Telegram 告警

**LLM 呼叫點**（共 4 處）：事件分類（Step 2）、TA 精煉進出場（Step 6）、組合決策（Step 7）、規則探索（Step 11）。後端透過 `llm_client.analyze()` 統一抽象，換模型只改 `.env` 的 `LLM_BACKEND`。

**股票市場識別**：`twse_client.get_yf_ticker()` 回傳 `.TW`（上市）或 `.TWO`（上櫃）suffix，興櫃股目前無對應 suffix、不在系統偵測範圍內。

**市場濾鏡評分**：`market_filter.get_market_state()` 合計 TWII 趨勢（±1.0）、外資淨買（+0.8）、S&P500（+0.5）、台指期夜盤（+0.6）等項目，加總 ≥ +2 為 bullish，≤ -1 為 bearish。

## 規則 YAML 結構

```yaml
- event: "事件名稱"          # 唯一 ID，LLM prompt 中引用
  keywords: [...]            # 關鍵字 fallback 匹配
  max_daily_triggers: 2      # 可選，每日最多觸發幾次
  impact:
    direction: "bullish|bearish|mixed"
    description: "機制說明"
    sectors:
      sector_name: ["2603", "2330"]
    last_updated_reason: "YYYY-MM-DD: reason"
    evidence_source: "quant|qualitative"
```

## 無測試

目前無 pytest 套件，手動驗證：
- `demo_flows.py`：端對端參考流程
- `--dry-run`：驗證推送內容不實際送出
- YAML parse 指令（見上方）
