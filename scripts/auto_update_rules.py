"""YAML 規則庫自動改寫模組（Phase 5）。

支援四種操作：
  DOWNGRADE  bullish → mixed（因績效差）
  REMOVE     從 stocks 清單移除特定標的
  ADD        新增標的到 stocks 清單
  DISCOVER   新增全新 event 區塊

安全網：
  - 每次改寫前自動 git commit（版控快照）
  - Circuit Breaker：單次異動超過總規則 10% → 暫停並發 Telegram 警報
  - 第一版不做自動 revert，異常時人工介入
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.lib.config import config
from scripts.lib.telegram import send as tg_send

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.default_flow_style = False
_yaml.indent(mapping=2, sequence=4, offset=2)

# Circuit Breaker 閾值：異動比例上限
_CIRCUIT_BREAKER_RATIO = 0.10


# --------------------------------------------------------------------------- #
# 公開介面
# --------------------------------------------------------------------------- #

def apply_updates(updates: list[dict], shadow: bool = False) -> bool:
    """套用一批規則異動。

    每筆 update dict 格式：
      {
        "operation": "DOWNGRADE" | "REMOVE" | "ADD" | "DISCOVER",
        "event": "事件名稱",          # 用於比對已有 event
        "sector": "shipping",         # REMOVE/ADD 時指定板塊
        "stock_code": "2603",         # REMOVE/ADD 時指定標的
        "reason": "說明文字",
        "evidence_source": "quant" | "qualitative",
        "confidence": 0.75,           # ADD/DISCOVER 時的信心分數
        # DISCOVER 時需額外提供完整的 event 結構
        "new_event": {...},
      }

    Args:
        updates: 一批異動指令。
        shadow: True 時為影子模式——只記錄 proposed changes，不改 YAML、不 git commit。
                用於 Phase 6 shadow run 觀察期，確認判斷品質後再正式開啟。

    Returns:
        True 表示成功套用（或 shadow 記錄完成），False 表示被 Circuit Breaker 攔截。
    """
    if not updates:
        return True

    # ── Shadow 模式：只記錄，不改，不觸發 Circuit Breaker ───────────────
    if shadow:
        _save_shadow_log(updates)
        print(f"[auto_update][SHADOW] 記錄 {len(updates)} 筆 proposed changes，未寫入 YAML。")
        print(f"[auto_update][SHADOW] 查看 data/shadow_updates/ 了解系統判斷。")
        return True

    rules = _load_rules()
    total_stocks = _count_stocks(rules)

    # Circuit Breaker 預檢（正式模式才生效）
    if _would_trigger_circuit_breaker(updates, total_stocks):
        msg = (
            f"⚠️ *Circuit Breaker 觸發*\n"
            f"本次異動 {len(updates)} 筆，超過規則庫 10% 上限（共 {total_stocks} 筆標的）。\n"
            f"請人工確認後手動執行。"
        )
        _try_tg(msg)
        print(f"[auto_update] Circuit Breaker 觸發，中止改寫。")
        return False

    # ── 正式模式：備份 → 改寫 → commit ──────────────────────────────────
    _git_snapshot("pre-update snapshot before auto_update_rules")

    changed = False
    for upd in updates:
        changed |= _apply_one(rules, upd)

    if changed:
        _save_rules(rules)
        summary = _build_commit_summary(updates)
        _git_snapshot(summary)
        print(f"[auto_update] 改寫完成，{len(updates)} 筆異動已 commit。")
    else:
        print("[auto_update] 無實際異動。")

    return True


# --------------------------------------------------------------------------- #
# 單筆操作
# --------------------------------------------------------------------------- #

def _apply_one(rules: list, upd: dict) -> bool:
    op = upd.get("operation", "").upper()
    event_name = upd.get("event", "")
    reason = upd.get("reason", "")
    evidence = upd.get("evidence_source", "")
    timestamp = datetime.now().strftime("%Y-%m-%d")
    reason_str = f"{timestamp}: {reason}"

    rule = _find_rule(rules, event_name)

    if op == "DOWNGRADE":
        if rule and rule["impact"]["direction"] == "bullish":
            rule["impact"]["direction"] = "mixed"
            rule["impact"]["last_updated_reason"] = reason_str
            rule["impact"]["evidence_source"] = evidence
            return True

    elif op == "REMOVE":
        sector = upd.get("sector", "")
        code = upd.get("stock_code", "")
        if rule and sector and code:
            stocks = rule["impact"].get("stocks", {})
            sector_list = stocks.get(sector, [])
            if code in sector_list:
                sector_list.remove(code)
                rule["impact"]["last_updated_reason"] = reason_str
                rule["impact"]["evidence_source"] = evidence
                return True

    elif op == "ADD":
        sector = upd.get("sector", "")
        code = upd.get("stock_code", "")
        confidence = upd.get("confidence", 0.5)
        if rule and sector and code:
            stocks = rule["impact"].setdefault("stocks", {})
            if sector not in stocks:
                stocks[sector] = []
            if code not in stocks[sector]:
                stocks[sector].append(code)
                rule["impact"]["last_updated_reason"] = reason_str
                rule["impact"]["evidence_source"] = evidence
                rule["impact"]["confidence"] = confidence
                return True

    elif op == "DISCOVER":
        new_event = upd.get("new_event")
        if new_event and not _find_rule(rules, event_name):
            new_event["impact"]["last_updated_reason"] = reason_str
            new_event["impact"]["evidence_source"] = evidence
            rules.append(new_event)
            return True

    return False


# --------------------------------------------------------------------------- #
# YAML IO
# --------------------------------------------------------------------------- #

def _load_rules() -> list:
    with open(config.rules_file, encoding="utf-8") as f:
        data = _yaml.load(f)
    return data or []


def _save_rules(rules: list) -> None:
    with open(config.rules_file, "w", encoding="utf-8") as f:
        _yaml.dump(rules, f)


# --------------------------------------------------------------------------- #
# Circuit Breaker
# --------------------------------------------------------------------------- #

def _count_stocks(rules: list) -> int:
    total = 0
    for rule in rules:
        for sector_list in rule.get("impact", {}).get("stocks", {}).values():
            total += len(sector_list)
    return total


def _would_trigger_circuit_breaker(updates: list[dict], total_stocks: int) -> bool:
    if total_stocks == 0:
        return False
    mutating_ops = {"REMOVE", "ADD", "DISCOVER", "DOWNGRADE"}
    mutation_count = sum(1 for u in updates if u.get("operation", "").upper() in mutating_ops)
    return mutation_count / total_stocks > _CIRCUIT_BREAKER_RATIO


# --------------------------------------------------------------------------- #
# Git 版控
# --------------------------------------------------------------------------- #

def _git_snapshot(message: str) -> None:
    rules_path = str(config.rules_file)
    try:
        subprocess.run(
            ["git", "add", rules_path],
            cwd=str(config.project_root),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=str(config.project_root),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[auto_update] git commit 失敗（非致命）：{e.stderr.decode()}")


def _build_commit_summary(updates: list[dict]) -> str:
    parts = []
    for u in updates[:3]:
        op = u.get("operation", "")
        event = u.get("event", "")
        code = u.get("stock_code", "")
        tag = f"{op} {code} ({event})" if code else f"{op} {event}"
        parts.append(tag)
    suffix = f" +{len(updates) - 3} more" if len(updates) > 3 else ""
    return f"Auto-update: {'; '.join(parts)}{suffix}"


# --------------------------------------------------------------------------- #
# 輔助
# --------------------------------------------------------------------------- #

def _find_rule(rules: list, event_name: str) -> dict | None:
    for rule in rules:
        if rule.get("event", "") == event_name:
            return rule
    return None


def _save_shadow_log(updates: list[dict]) -> None:
    """將 proposed changes 寫入 data/shadow_updates/YYYY-MM-DD.json，供人工審閱。

    去重邏輯：跨所有歷史 shadow 檔案 + _reviewed.json，相同 event 不重複提案。
    """
    import json

    today = datetime.now().strftime("%Y-%m-%d")
    shadow_dir = config.data_dir / "shadow_updates"
    shadow_dir.mkdir(parents=True, exist_ok=True)
    out_file = shadow_dir / f"{today}.json"

    # 收集所有已出現過的事件名稱（跨日 + 已審閱）
    seen_events: set[str] = set()
    for f in shadow_dir.glob("*.json"):
        if f.name.startswith("_"):
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                seen_events.update(e.get("event") for e in json.load(fp))
        except Exception:
            pass
    reviewed_file = shadow_dir / "_reviewed.json"
    if reviewed_file.exists():
        try:
            with open(reviewed_file, encoding="utf-8") as fp:
                seen_events.update(e.get("event") for e in json.load(fp))
        except Exception:
            pass

    # DISCOVER 提案額外檢查：規則已在 YAML 裡就不需要提案
    existing_rules = _load_rules()
    deduped = [
        u for u in updates
        if u.get("event") not in seen_events
        and not (u.get("operation", "").upper() == "DISCOVER" and _find_rule(existing_rules, u.get("event", "")))
    ]
    if not deduped:
        print(f"[auto_update][SHADOW] 全部 {len(updates)} 筆提案已在佇列中或規則已存在，略過。")
        return

    existing: list = []
    if out_file.exists():
        with open(out_file, encoding="utf-8") as f:
            existing = json.load(f)

    existing.extend(deduped)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"[auto_update][SHADOW] 新增 {len(deduped)} 筆提案 → {out_file}")


def _try_tg(msg: str) -> None:
    try:
        tg_send(msg)
    except Exception as e:
        print(f"[auto_update] Telegram 警報發送失敗：{e}")


if __name__ == "__main__":
    # 簡單測試：把航運 2603 從美伊衝突降級
    test_updates = [
        {
            "operation": "DOWNGRADE",
            "event": "美伊/中東衝突",
            "reason": "測試降級操作",
            "evidence_source": "quant",
        }
    ]
    apply_updates(test_updates)
