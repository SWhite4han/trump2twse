"""每日主調度器（cron 呼叫這個）。

流程：
  1. 擷取資料（Trump 貼文 + 股癌筆記）
  2. LLM 事件分類
  3. 規則匹配 → 產生建議清單
  4. 查今日股價，補齊進出場建議
  5. 儲存日報 JSON
  6. Telegram 推送
  7. 績效追蹤（評估昨日建議 + 自動改寫規則庫）
  8. DISCOVER — 探索新事件規則

執行方式：
    python scripts/daily_pipeline.py
    python scripts/daily_pipeline.py --skip-fetch    # 略過擷取（用昨天的資料測試）
    python scripts/daily_pipeline.py --dry-run       # 只印不推送 Telegram
    python scripts/daily_pipeline.py --shadow        # Phase 6 影子模式：規則更新只記錄不寫入
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

# 讓本檔可從專案根執行
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.lib.config import config
from scripts.lib.telegram import send as tg_send
from scripts.lib.twse_client import get_prices, format_price_line


# --------------------------------------------------------------------------- #
# Pipeline 主流程
# --------------------------------------------------------------------------- #

def run(skip_fetch: bool = False, dry_run: bool = False, shadow: bool = False) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    today_compact = datetime.now().strftime("%Y%m%d")
    mode_tag = " [SHADOW]" if shadow else ""
    print(f"\n{'='*60}")
    print(f"  Market Track Daily Pipeline — {today}{mode_tag}")
    print(f"{'='*60}\n")

    # ── Step 1：擷取資料 ──────────────────────────────────────────────
    raw_texts: list[str] = []
    if not skip_fetch:
        raw_texts = _step_fetch()
    else:
        print("[1/8] 略過擷取（--skip-fetch）")

    # ── Step 2：LLM 事件分類 ─────────────────────────────────────────
    matched_events = _step_classify(raw_texts)

    # ── Step 3：規則匹配 → 建議清單 ──────────────────────────────────
    recommendations = _step_match_rules(matched_events)

    # ── Step 4：補齊股價 ─────────────────────────────────────────────
    recommendations = _step_enrich_prices(recommendations)

    # ── Step 5：儲存日報 ─────────────────────────────────────────────
    report = _step_save_report(recommendations, matched_events, today_compact)

    # ── Step 6：Telegram 推送 ─────────────────────────────────────────
    if not dry_run:
        _step_notify(report, today)
    else:
        print("[6/8] Dry-run，略過 Telegram 推送")
        print(_build_telegram_msg(report, today))

    # ── Step 7：績效追蹤 + 自動改寫規則庫 ────────────────────────────
    _step_track_performance(shadow=shadow)

    # ── Step 8：DISCOVER — 探索新事件規則 ────────────────────────────
    _step_discover_rules(raw_texts, shadow=shadow)

    print(f"\n✅ Pipeline 完成 — {today}{mode_tag}")
    return 0


# --------------------------------------------------------------------------- #
# Step 1：擷取
# --------------------------------------------------------------------------- #

def _step_fetch() -> list[str]:
    print("[1/8] 擷取 Trump 貼文 & 股癌筆記…")
    texts: list[str] = []

    try:
        from scripts.sources.trump_twitter import fetch as fetch_trump
        from email.utils import parsedate_to_datetime
        posts = fetch_trump(save=True)
        for p in posts:
            date_prefix = ""
            if p.get("published_at"):
                try:
                    dt = parsedate_to_datetime(p["published_at"])
                    date_prefix = f"[{dt.strftime('%m/%d')}] "
                except Exception:
                    pass
            text = p.get("text", "").strip()
            if text:
                texts.append(f"{date_prefix}{text}")
        print(f"       Trump：{len(posts)} 則")
    except Exception as e:
        print(f"       Trump 擷取失敗（繼續）：{e}")

    try:
        from scripts.sources.gua_cancer import fetch as fetch_gua
        notes = fetch_gua(save=True)
        for n in notes:
            ep = n.get("episode", "")
            ep_prefix = f"[股癌EP{ep}] " if ep else ""
            content = n.get("content", "").strip()
            if content:
                texts.append(f"{ep_prefix}{content}")
        print(f"       股癌：{len(notes)} 集")
    except Exception as e:
        print(f"       股癌擷取失敗（繼續）：{e}")

    return [t for t in texts if t.strip()]


# --------------------------------------------------------------------------- #
# Step 2：LLM 事件分類
# --------------------------------------------------------------------------- #

def _step_classify(texts: list[str]) -> list[dict]:
    print("[2/8] LLM 事件分類…")
    if not texts:
        print("       無文字輸入，略過 LLM。")
        return []

    try:
        from scripts.llm_client import analyze
        rules = _load_rules_summary()
        combined = "\n\n---\n".join(texts[:10])  # 最多 10 段，節省 token
        prompt = (
            "你是一位台股分析師助理。請從以下文字中，辨識出任何可能對台股造成影響的重要市場事件。\n"
            "對每個偵測到的事件，回傳 JSON 陣列，格式如下：\n"
            '[{"event": "事件名稱（若與規則庫事件相符請使用完全相同名稱；若為新事件請自行命名，15字以內）", '
            '"confidence": 0.0~1.0, "direction": "bullish|bearish|mixed", '
            '"summary": "一句話摘要", '
            '"source_date": "消息來源日期或集數，從文字前綴 [MM/DD] 或 [股癌EPxxx] 取得，例如：05/06、EP659"}]\n\n'
            f"規則庫已知事件（供參考，不限於此）：{json.dumps(rules, ensure_ascii=False)}\n\n"
            f"待分析文字：\n{combined}"
        )
        raw = analyze(prompt)
        import re
        m = re.search(r"\[.*?\](?=\s*$|\s*[^,\]\[])", raw, re.DOTALL)
        if not m:
            m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                events = json.loads(m.group())
            except json.JSONDecodeError:
                # LLM 在 JSON 後面附了說明文字，嘗試只截取到第一個完整陣列
                bracket_depth = 0
                end = 0
                for i, ch in enumerate(raw):
                    if ch == "[":
                        bracket_depth += 1
                    elif ch == "]":
                        bracket_depth -= 1
                        if bracket_depth == 0:
                            end = i + 1
                            break
                events = json.loads(raw[raw.index("["):end]) if end else []
            print(f"       偵測到 {len(events)} 個事件")
            return events
    except Exception as e:
        print(f"       LLM 分類失敗（fallback 關鍵字比對）：{e}")

    # Fallback：關鍵字比對
    return _keyword_classify(texts)


def _keyword_classify(texts: list[str]) -> list[dict]:
    from ruamel.yaml import YAML as _YAML
    _y = _YAML()
    with open(config.rules_file, encoding="utf-8") as f:
        rules = _y.load(f) or []

    combined = " ".join(texts).lower()
    found = []
    for rule in rules:
        kws = [k.lower() for k in rule.get("keywords", [])]
        hits = sum(1 for kw in kws if kw in combined)
        if hits > 0:
            found.append({
                "event": rule["event"],
                "confidence": min(0.5 + hits * 0.05, 0.9),
                "direction": rule["impact"].get("direction", "mixed"),
                "summary": f"關鍵字比對命中 {hits} 個",
            })
    return found


def _load_rules_summary() -> list[str]:
    from ruamel.yaml import YAML as _YAML
    _y = _YAML()
    with open(config.rules_file, encoding="utf-8") as f:
        rules = _y.load(f) or []
    return [r["event"] for r in rules]


def _load_shadow_proposed_events() -> list[str]:
    """讀取今日 shadow_updates 已提案的事件名稱，用於 DISCOVER 去重。"""
    today = datetime.now().strftime("%Y-%m-%d")
    shadow_file = config.data_dir / "shadow_updates" / f"{today}.json"
    if not shadow_file.exists():
        return []
    try:
        with open(shadow_file, encoding="utf-8") as f:
            entries = json.load(f)
        return list({e.get("event", "") for e in entries if e.get("event")})
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Step 3：規則匹配
# --------------------------------------------------------------------------- #

def _step_match_rules(matched_events: list[dict]) -> list[dict]:
    print(f"[3/8] 規則匹配（{len(matched_events)} 個事件）…")
    if not matched_events:
        return []

    from ruamel.yaml import YAML as _YAML
    _y = _YAML()
    with open(config.rules_file, encoding="utf-8") as f:
        rules = _y.load(f) or []

    rule_map = {r["event"]: r for r in rules}
    recommendations: list[dict] = []
    seen_codes: set[str] = set()

    for ev in matched_events:
        if ev.get("confidence", 0) < 0.4:
            continue
        rule = rule_map.get(ev["event"])
        if not rule:
            continue
        stocks = rule.get("impact", {}).get("stocks", {})
        direction = ev.get("direction") or rule["impact"].get("direction", "mixed")

        for sector, codes in stocks.items():
            for code in codes:
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                action = _direction_to_action(direction, sector)
                recommendations.append({
                    "code": code,
                    "name": "",  # 由 Step 4 填入
                    "action": action,
                    "sector": sector,
                    "rule_id": ev["event"],
                    "confidence": ev.get("confidence", 0.5),
                    "event_summary": ev.get("summary", ""),
                    "entry_low": None,
                    "entry_high": None,
                    "target": None,
                    "stop_loss": None,
                })

    print(f"       產生 {len(recommendations)} 筆建議")
    return recommendations


def _direction_to_action(direction: str, sector: str) -> str:
    if direction == "bullish":
        return "觀察買進"
    if direction == "bearish":
        return "停看等"
    # mixed：依板塊判斷
    positive_sectors = {"defense", "gold", "foundry", "impacted_positive"}
    return "觀察買進" if sector in positive_sectors else "停看等"


# --------------------------------------------------------------------------- #
# Step 4：補齊股價
# --------------------------------------------------------------------------- #

def _step_enrich_prices(recs: list[dict]) -> list[dict]:
    print("[4/8] 查詢股價…")
    if not recs:
        return []

    codes = [r["code"] for r in recs]
    prices = get_prices(codes)

    for rec in recs:
        info = prices.get(rec["code"])
        if info:
            rec["name"] = info.get("name", rec["code"])
            close = info.get("close")
            if close:
                # 簡易進出場估算（±3% / ±5%）
                rec["entry_low"] = round(close * 0.97, 1)
                rec["entry_high"] = round(close * 1.00, 1)
                rec["target"] = round(close * 1.05, 1)
                rec["stop_loss"] = round(close * 0.95, 1)

    return recs


# --------------------------------------------------------------------------- #
# Step 5：存日報
# --------------------------------------------------------------------------- #

def _step_save_report(recs: list[dict], events: list[dict], date_compact: str) -> dict:
    print("[5/8] 儲存日報…")
    report = {
        "date": date_compact,
        "generated_at": datetime.now().isoformat(),
        "events": events,
        "recommendations": recs,
    }
    out_dir = config.data_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"daily_report_{date_compact}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"       → {out_file}")
    return report


# --------------------------------------------------------------------------- #
# Step 6：Telegram 推送
# --------------------------------------------------------------------------- #

def _step_notify(report: dict, today: str) -> None:
    print("[6/8] 推送 Telegram…")
    msg = _build_telegram_msg(report, today)
    try:
        result = tg_send(msg)
        print(f"       ✓ message_id={result['result']['message_id']}")
    except Exception as e:
        print(f"       ✗ 推送失敗：{e}")


def _build_telegram_msg(report: dict, today: str) -> str:
    lines = [f"📊 *{today} 每日台股建議*\n"]

    events = report.get("events", [])
    if events:
        high_conf = [e for e in events if e.get("confidence", 0) >= 0.7]
        if high_conf:
            lines.append("🔴 *重大事件*")
            for e in high_conf[:3]:
                conf = e.get("confidence", 0)
                date_tag = f" `{e['source_date']}`" if e.get("source_date") else ""
                lines.append(f"• {e['event']}（信心 {conf:.2f}）{date_tag}")
                if e.get("summary"):
                    lines.append(f"  ↳ {e['summary']}")
            lines.append("")

    recs = report.get("recommendations", [])
    buy = [r for r in recs if r.get("action") == "觀察買進"]
    watch = [r for r in recs if r.get("action") != "觀察買進"]

    if buy:
        lines.append("📈 *建議觀察買進*")
        for r in buy[:8]:
            entry = ""
            if r.get("entry_low") and r.get("entry_high"):
                entry = f"  進場 {r['entry_low']}-{r['entry_high']}"
                if r.get("target"):
                    entry += f"  目標 {r['target']}"
                if r.get("stop_loss"):
                    entry += f"  停損 {r['stop_loss']}"
            conf_str = f"（信心 {r['confidence']:.2f}）" if r.get("confidence") else ""
            lines.append(f"• {r['code']} {r.get('name', '')}{conf_str}")
            if entry:
                lines.append(entry)
            if r.get("rule_id"):
                lines.append(f"  依據：{r['rule_id']}")
        lines.append("")

    if watch:
        lines.append("📉 *建議停看等*")
        for r in watch[:5]:
            lines.append(f"• {r['code']} {r.get('name', '')}  依據：{r.get('rule_id', '')}")
        lines.append("")

    if not buy and not watch:
        lines.append("今日無明確建議標的，維持觀望。")
        # 列出偵測到但尚無規則匹配的事件，讓用戶知道市場在發生什麼
        matched_rule_ids = {r.get("rule_id") for r in recs}
        unmatched = [e for e in events if e.get("event") not in matched_rule_ids and e.get("confidence", 0) >= 0.5]
        if unmatched:
            lines.append("\n⚠️ *偵測到以下事件（規則庫尚無對應標的）*")
            for e in unmatched[:5]:
                date_tag = f" `{e['source_date']}`" if e.get("source_date") else ""
                lines.append(f"• {e['event']}（信心 {e.get('confidence', 0):.2f}）{date_tag}")
                if e.get("summary"):
                    lines.append(f"  ↳ {e['summary']}")

    lines.append(f"_更新時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Step 7：績效追蹤
# --------------------------------------------------------------------------- #

def _step_track_performance(shadow: bool = False) -> None:
    print("[7/8] 績效追蹤（昨日建議）…")
    try:
        from scripts.track_recommendation_performance import run as perf_run
        perf_run(shadow=shadow)
        shadow_note = "（shadow 模式，規則更新已記錄到 data/shadow_updates/）" if shadow else ""
        print(f"       規則庫自動改寫完成 {shadow_note}")
    except Exception as e:
        print(f"       績效追蹤失敗（繼續）：{e}")


# --------------------------------------------------------------------------- #
# Step 8：DISCOVER — 探索新事件規則
# --------------------------------------------------------------------------- #

def _step_discover_rules(raw_texts: list[str], shadow: bool = False) -> None:
    print("[8/8] DISCOVER — 探索新事件規則…")
    if not raw_texts:
        print("       無文字輸入，略過。")
        return
    if config.llm_backend == "stub":
        print("       stub 模式，略過。")
        return

    try:
        import re
        from scripts.llm_client import analyze
        from scripts.auto_update_rules import apply_updates

        existing_events = _load_rules_summary()
        # 加入 shadow_updates 已提案的事件，避免每天重複提議相同的規則
        existing_events = existing_events + _load_shadow_proposed_events()
        combined = "\n\n---\n".join(raw_texts[:10])
        prompt = (
            "你是一位台股事件規則分析師。請從以下新聞文字中，找出「現有規則庫尚未涵蓋」的全新事件模式。\n\n"
            "現有規則庫與已提案事件（不要重複提議）：\n"
            f"{json.dumps(existing_events, ensure_ascii=False)}\n\n"
            "要求：\n"
            "- 只提議有強力新聞佐證、信心度 >= 0.8 的全新事件\n"
            "- 若無值得新增的事件，直接回傳 []\n"
            "- 股票代碼只填你有把握的台股四位數代碼；若不確定，stocks 留 {}\n"
            "- 事件名稱簡短精確（建議 15 字以內）\n"
            "- 只輸出 JSON 陣列，不要有其他文字\n\n"
            "輸出格式（JSON 陣列）：\n"
            '[\n'
            '  {\n'
            '    "event": "事件名稱",\n'
            '    "reason": "為何值得追蹤",\n'
            '    "confidence": 0.85,\n'
            '    "impact": {\n'
            '      "direction": "bullish",\n'
            '      "description": "影響機制一句話",\n'
            '      "sectors": ["sector_name"],\n'
            '      "stocks": {\n'
            '        "sector_name": ["2330"]\n'
            '      }\n'
            '    },\n'
            '    "keywords": ["keyword1", "keyword2"]\n'
            '  }\n'
            ']\n\n'
            f"待分析文字：\n{combined}"
        )

        raw = analyze(prompt)

        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            print("       未解析到 JSON，略過。")
            return

        try:
            items = json.loads(m.group())
        except json.JSONDecodeError:
            bracket_depth = 0
            end = 0
            for i, ch in enumerate(raw):
                if ch == "[":
                    bracket_depth += 1
                elif ch == "]":
                    bracket_depth -= 1
                    if bracket_depth == 0:
                        end = i + 1
                        break
            items = json.loads(raw[raw.index("["):end]) if end else []
        if not items:
            print("       Claude 判斷無新事件值得新增。")
            return

        updates: list[dict] = []
        for item in items:
            event_name = item.get("event", "")
            if not event_name:
                continue
            updates.append({
                "operation": "DISCOVER",
                "event": event_name,
                "reason": item.get("reason", ""),
                "evidence_source": "qualitative",
                "confidence": item.get("confidence", 0.8),
                "new_event": {
                    "event": event_name,
                    "keywords": item.get("keywords", []),
                    "impact": item.get("impact", {
                        "direction": "mixed",
                        "description": "",
                        "sectors": [],
                        "stocks": {},
                    }),
                },
            })

        if not updates:
            print("       無有效 DISCOVER 項目。")
            return

        apply_updates(updates, shadow=shadow)
        shadow_note = "（shadow 模式）" if shadow else ""
        print(f"       提議 {len(updates)} 個新事件規則 {shadow_note}")

    except Exception as e:
        print(f"       DISCOVER 步驟失敗（繼續）：{e}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Market Track Daily Pipeline")
    parser.add_argument("--skip-fetch", action="store_true", help="略過資料擷取")
    parser.add_argument("--dry-run", action="store_true", help="不推送 Telegram，只印訊息")
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="Phase 6 影子模式：規則庫更新只記錄到 data/shadow_updates/，不實際寫入 YAML",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        code = run(skip_fetch=args.skip_fetch, dry_run=args.dry_run, shadow=args.shadow)
    except Exception:
        traceback.print_exc()
        try:
            tg_send("⚠️ *Market Track Pipeline 異常中止*\n請檢查日誌。")
        except Exception:
            pass
        code = 1
    sys.exit(code)
