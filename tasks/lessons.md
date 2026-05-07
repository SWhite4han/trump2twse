# Lessons Learned

每次被糾正後記錄在此，防止同樣錯誤重現。

---

## Python 環境

- **規則**：執行指令一律用 `.venv/bin/python`，不用 `python` 或 `python3`。
- **Why**：系統 PATH 沒有 `python`，相依套件在 `.venv` 裡，系統 python3 缺套件。
- **範例**：`.venv/bin/python scripts/daily_pipeline.py --dry-run`
