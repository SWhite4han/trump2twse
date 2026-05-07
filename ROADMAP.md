# Roadmap

## 目前狀態：Phase 6 Shadow Run 觀察期

系統已可每日自動執行完整流程並推送 Telegram 通知。
目前處於「shadow run」模式：AI 產生的規則更新只記錄、不實際寫入，觀察判斷品質約一週後進入正式模式。

---

## Phase 完成進度

| Phase | 內容 | 狀態 |
|---|---|---|
| 0 | 基礎骨架：環境設定、Telegram Bot、cron 排程 | ✅ 完成 |
| 1 | 資料源接入：Trump 貼文、股癌 Podcast 筆記 | ✅ 完成 |
| 2 | 事件分類與規則匹配：LLM + 關鍵字 fallback | ✅ 完成 |
| 3 | 通知層：每日 Telegram 摘要（含進出場建議） | ✅ 完成 |
| 4 | 績效追蹤：記錄每筆建議的實際表現 | ✅ 完成（等資料積累） |
| 5 | 自動改寫規則庫：DISCOVER / ADD / DOWNGRADE / REMOVE + Circuit Breaker | ✅ 完成 |
| 6 | Shadow run 整合測試 | 🔄 進行中 |

---

## 接下來（v1.1）

- [ ] Shadow run 觀察通過後切換正式模式，讓規則庫開始自我演化
- [ ] 績效追蹤閉環：等幾個有明確訊號的交易日積累資料後驗證
- [ ] 改善 LLM 分類品質（目前用 Claude Code CLI，考慮換 claude_api backend）

## 未來規劃（v2）

- [ ] 加入 Musk / 8ZZ 等更多訊號來源
- [ ] 反指標邏輯（特定人士喊多 → 反向操作）
- [ ] 雲端部署（目前本機 WSL，穩定後搬雲端）
- [ ] 自動下單介面（目前只「建議」，不接券商 API）

---

## 設計原則

**不做的事**
- 不自動下單（系統只提供建議，最終決策由人工判斷）
- 不付昂貴 API 費用（優先使用免費或訂閱制工具）
- 不蒐集或儲存個人資料

**核心架構選擇**
- LLM 分析走抽象層（`llm_client.py`），換後端只改 `.env` 一行
- 股價資料直接打 TWSE OpenAPI，不走第三方
- 所有規則異動有 git 版控 + Circuit Breaker 安全網
