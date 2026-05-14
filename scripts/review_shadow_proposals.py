"""互動式審閱 shadow DISCOVER 提案。

用法：
    python scripts/review_shadow_proposals.py

流程：
    1. 掃描 data/shadow_updates/*.json，收集所有 DISCOVER 提案
    2. 過濾已審閱過的（記錄在 _reviewed.json）
    3. 用 TWSE API 驗證股票代碼是否存在
    4. 逐一讓使用者批准（y）或拒絕（n）
    5. 批准 → 寫入 event_impact_rules.yml + git commit
    6. 所有決定記錄到 _reviewed.json，防止重複出現
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# 讓 scripts/ 下的模組可以找到
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.auto_update_rules import apply_updates
from scripts.lib.config import config


def _load_all_proposals() -> list[dict]:
    shadow_dir = config.data_dir / "shadow_updates"
    proposals: list[dict] = []
    for f in sorted(shadow_dir.glob("*.json")):
        if f.name.startswith("_"):
            continue
        with open(f, encoding="utf-8") as fp:
            for entry in json.load(fp):
                if entry.get("operation") == "DISCOVER":
                    entry["_source_file"] = f.name
                    proposals.append(entry)
    return proposals


def _load_reviewed() -> set[str]:
    reviewed_file = config.data_dir / "shadow_updates" / "_reviewed.json"
    if not reviewed_file.exists():
        return set()
    with open(reviewed_file, encoding="utf-8") as f:
        return {e.get("event") for e in json.load(f)}


def _save_reviewed(event: str, status: str) -> None:
    reviewed_file = config.data_dir / "shadow_updates" / "_reviewed.json"
    existing: list = []
    if reviewed_file.exists():
        with open(reviewed_file, encoding="utf-8") as f:
            existing = json.load(f)
    existing.append({
        "event": event,
        "status": status,
        "reviewed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    with open(reviewed_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def _validate_stocks(new_event: dict) -> dict[str, str]:
    """驗證 new_event 內所有股票代碼，回傳 {code: "ok" | "not_found" | "api_unavailable"}。"""
    all_codes: list[str] = []
    for sector_list in new_event.get("impact", {}).get("stocks", {}).values():
        all_codes.extend(sector_list)

    if not all_codes:
        return {}

    try:
        from scripts.lib.twse_client import get_prices
        results = get_prices(all_codes)
        validation = {
            code: ("ok" if info is not None else "not_found")
            for code, info in results.items()
        }
        # TWSE 查不到的代碼，嘗試上櫃（TPEx）.TWO
        not_found = [c for c, s in validation.items() if s == "not_found"]
        if not_found:
            try:
                import yfinance as yf
                for code in not_found:
                    h = yf.Ticker(f"{code}.TWO").history(period="5d", auto_adjust=True)
                    if not h.empty:
                        validation[code] = "ok"
            except Exception:
                pass
        return validation
    except Exception:
        return {code: "api_unavailable" for code in all_codes}


def _print_proposal(idx: int, total: int, p: dict, validation: dict[str, str]) -> None:
    ne = p.get("new_event", {})
    impact = ne.get("impact", {})
    print(f"\n{'='*60}")
    print(f"提案 [{idx}/{total}]  來源：{p.get('_source_file', '?')}")
    print(f"{'='*60}")
    print(f"  事件：{p.get('event')}")
    print(f"  方向：{impact.get('direction', '?')}  信心：{p.get('confidence', '?')}")
    print(f"  說明：{impact.get('description', '')}")
    print(f"  依據：{p.get('reason', '')}")

    stocks = impact.get("stocks", {})
    if stocks:
        print("  股票：")
        for sector, codes in stocks.items():
            for code in codes:
                status = validation.get(code, "?")
                tag = {"ok": "✓", "not_found": "✗ 查無此代碼", "api_unavailable": "? API 無回應"}.get(status, "?")
                print(f"    [{sector}] {code}  {tag}")
    print(f"{'='*60}")


def main() -> None:
    proposals = _load_all_proposals()
    reviewed = _load_reviewed()
    pending = [p for p in proposals if p.get("event") not in reviewed]

    # 同一 event 只保留最新一筆（最後出現的）
    seen: dict[str, dict] = {}
    for p in pending:
        seen[p.get("event", "")] = p
    pending = list(seen.values())

    if not pending:
        print("沒有待審閱的 DISCOVER 提案。")
        return

    print(f"\n共 {len(pending)} 筆待審 DISCOVER 提案")

    approved_count = rejected_count = 0
    for idx, p in enumerate(pending, 1):
        ne = p.get("new_event", {})
        validation = _validate_stocks(ne)

        _print_proposal(idx, len(pending), p, validation)

        invalid_codes = [c for c, s in validation.items() if s == "not_found"]
        if invalid_codes:
            print(f"  ⚠️  以下代碼在 TWSE 查無資料：{', '.join(invalid_codes)}")
            print("     批准前請確認代碼是否正確。")

        while True:
            choice = input("  批准 (y) / 拒絕 (n) / 跳過 (s) / 離開 (q)？ ").strip().lower()
            if choice in ("y", "n", "s", "q"):
                break
            print("  請輸入 y / n / s / q")

        event_name = p.get("event", "")

        if choice == "q":
            print("已離開（未完成的提案下次繼續）。")
            break
        elif choice == "s":
            print("  已跳過（不記錄）。")
            continue
        elif choice == "n":
            _save_reviewed(event_name, "rejected")
            rejected_count += 1
            print("  已拒絕。")
        elif choice == "y":
            # 移除 _source_file（非標準欄位）
            clean = {k: v for k, v in p.items() if not k.startswith("_")}
            success = apply_updates([clean], shadow=False)
            if success:
                _save_reviewed(event_name, "approved")
                approved_count += 1
                print(f"  已批准並寫入 YAML。")
            else:
                print("  ⚠️  Circuit Breaker 攔截，未寫入。請人工確認。")

    print(f"\n完成：批准 {approved_count} 筆，拒絕 {rejected_count} 筆。")


if __name__ == "__main__":
    main()
