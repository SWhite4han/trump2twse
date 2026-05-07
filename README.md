# trump2twse

> 台股事件驅動情報追蹤系統  
> 從 Trump 貼文 + 股癌 Podcast 沿因果鏈推導，每日自動產出 Telegram 買賣建議。

→ [查看開發進度 ROADMAP.md](ROADMAP.md) ｜ [推薦績效 PERFORMANCE.md](PERFORMANCE.md)

```
Trump 貼文 ──┐
             ├─→ LLM 事件分類 ─→ 規則匹配 ─→ TWSE 股價 ─→ Telegram 通知
股癌 Podcast ─┘        ↓
                  DISCOVER 新規則
                  (shadow → 人工審閱 → 寫入知識庫)
```

---

## 快速開始（Linux / WSL）

```bash
# 1. 安裝套件
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 填入設定
cp .env.example .env
# 必填：TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID

# 3. 設定 crontab（每天台北 23:00 自動跑）
bash setup_linux.sh

# 4. 手動跑一次確認
.venv/bin/python scripts/daily_pipeline.py --dry-run
```

---

## 指令一覽

```bash
# 每日 pipeline（cron 自動呼叫）
.venv/bin/python scripts/daily_pipeline.py

# Dry-run：只印不推 Telegram
.venv/bin/python scripts/daily_pipeline.py --dry-run

# Shadow run：規則庫更新只記錄，不實際改 YAML
.venv/bin/python scripts/daily_pipeline.py --shadow

# 重跑昨天資料（略過擷取）
.venv/bin/python scripts/daily_pipeline.py --skip-fetch --dry-run

# 審閱 AI 提出的新規則提案
bash review.sh
```

---

## 系統架構

### 每日流程（8 步驟）

| 步驟 | 說明 |
|---|---|
| 1 擷取 | Trump 貼文（Google News RSS）+ 股癌 Podcast 筆記 |
| 2 分類 | LLM 辨識事件類別與信心分數（fallback：關鍵字比對） |
| 3 匹配 | 對照 `event_impact_rules.yml`，產生建議標的清單 |
| 4 股價 | TWSE OpenAPI 查今日收盤，補齊進出場建議 |
| 5 存檔 | `data/reports/daily_report_YYYYMMDD.json` |
| 6 推送 | Telegram Bot 發送每日摘要 |
| 7 績效 | 評估昨日建議實際表現，更新規則信心分數 |
| 8 DISCOVER | LLM 探索新事件規則 → shadow 模式下等人工審閱 |

### 知識庫自我演化

```
新聞 → DISCOVER 提案 → data/shadow_updates/
                              ↓
                        bash review.sh   ← 人工審閱 + TWSE 代碼驗證
                              ↓
                    event_impact_rules.yml  (git commit 版控)
```

---

## 檔案說明

| 路徑 | 用途 |
|---|---|
| `scripts/daily_pipeline.py` | 每日主調度器 |
| `scripts/event_impact_rules.yml` | 事件影響規則庫（會自我演化） |
| `scripts/llm_client.py` | LLM 抽象層（換後端只改 `.env` 一行） |
| `scripts/auto_update_rules.py` | YAML 自動改寫 + Circuit Breaker |
| `scripts/review_shadow_proposals.py` | 互動式審閱 DISCOVER 提案 |
| `scripts/track_recommendation_performance.py` | 績效評估 |
| `scripts/sources/trump_twitter.py` | Trump 擷取（Truth Social → Google News RSS → DDGS）|
| `scripts/sources/gua_cancer.py` | 股癌筆記爬取 |
| `scripts/lib/twse_client.py` | TWSE OpenAPI 股價查詢（含快取） |
| `scripts/lib/telegram.py` | Telegram Bot 推送 |
| `review.sh` | `bash review.sh` 一鍵審閱提案 |

---

## .env 設定說明

| 變數 | 必填 | 說明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | BotFather 取得 |
| `TELEGRAM_CHAT_ID` | ✅ | 對 [@userinfobot](https://t.me/userinfobot) 說 hi 取得 |
| `LLM_BACKEND` | 選填 | `claude_code`（預設，用 Claude Code CLI）/ `claude_api` / `ollama` / `stub` |
| `ANTHROPIC_API_KEY` | 選填 | 使用 `claude_api` backend 時才需要 |
| `TRUMP_SOURCE_URL` | 選填 | 留空自動用 Google News RSS |

---

## Shadow Run 說明

Shadow run 是觀察期模式：AI 產生的規則更新只記錄到 `data/shadow_updates/`，不實際寫入知識庫，讓你觀察判斷品質後再決定是否批准。

```bash
# 進入 shadow 模式（setup_linux.sh 預設已加 --shadow）
.venv/bin/python scripts/daily_pipeline.py --shadow

# 每天審閱 AI 提案
bash review.sh

# 確認品質 OK 後，編輯 crontab 移除 --shadow 進入正式模式
crontab -e
```

---

## 已知問題

| 問題 | 影響 | 解法 |
|---|---|---|
| Truth Social RSS 鏡像全數失效 | 已自動 fallback 到 Google News RSS | 無需處理 |
| 市場休市時 TWSE 查無資料 | 股價欄位空白 | 正常，非交易日不需擔心 |
| WSL 關機時 cron 不跑 | 錯過當天分析 | 設為 23:00 跑，一般此時電腦開著 |
