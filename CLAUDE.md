# Market Track — Claude Code 指引

## Self-Improvement

每次被使用者糾正後，將錯誤模式與修正規則記錄到 `tasks/lessons.md`。
每次對話開始時，若任務與已記錄的教訓相關，先讀 `tasks/lessons.md`。

## 執行環境

- Python：一律使用 `.venv/bin/python`（系統 PATH 無 `python`）
- 排程：crontab，週一至五 UTC 15:00 起每小時（台北 23:00 起），週六 UTC 09:00-22:00 fetch-only
- Shadow run 觀察期中：`daily_pipeline.py --shadow`

## 專案結構

| 路徑 | 說明 |
|---|---|
| `scripts/daily_pipeline.py` | 主調度器（cron 入口） |
| `scripts/event_impact_rules.yml` | 規則知識庫（會自我演化） |
| `scripts/auto_update_rules.py` | YAML 自動改寫 + Circuit Breaker |
| `scripts/review_shadow_proposals.py` | 互動式審閱 DISCOVER 提案 |
| `data/shadow_updates/` | Shadow run 提案暫存 |
| `tasks/lessons.md` | 錯誤教訓記錄 |
| `.env` | API Keys、Bot Token（不進 git） |
