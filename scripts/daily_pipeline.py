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
    python scripts/daily_pipeline.py --date 2026-05-08  # 補跑指定日期
    python scripts/daily_pipeline.py --fetch-only       # 只抓資料（週末排程用）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# 讓本檔可從專案根執行
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.lib.config import config
from scripts.lib.telegram import send as tg_send
from scripts.lib.twse_client import get_prices, format_price_line, get_yf_ticker

# 價格帶預設值（Step 4 初始設定 / Step 4.5 後處理截斷）
_ENTRY_LOW_FACTOR  = 0.97   # entry_low  = close × 0.97
_ENTRY_HIGH_FACTOR = 1.00   # entry_high = close × 1.00
_TARGET_FACTOR_BUY  = 1.15  # 觀察買進 target = close × 1.15（+15%）
_TARGET_FACTOR_SELL = 0.85  # 觀察賣出 target = close × 0.85（-15%，做空目標在下方）
_STOP_FACTOR_BUY    = 0.92  # 觀察買進 stop  = close × 0.92（-8%）
_STOP_FACTOR_SELL   = 1.08  # 觀察賣出 stop  = close × 1.08（+8%，做空停損在上方）
_TA_ENTRY_FLOOR    = 0.95   # TA 後處理：進場價下緣截斷
_TA_ENTRY_CEIL     = 1.10   # TA 後處理：進場價上緣截斷

# 推薦有效期間預設值（依時間視野）：天數 = 觀察窗口失效前的日曆天
_HORIZON_DEFAULT_VALIDITY = {"event": 5, "trend": 7, "cycle": 10}

# 過熱濾鏡：近 3 日漲幅超過此值的買進、或跌幅超過此值的賣出，視為追高/追空而剔除
_OVERHEAT_GAIN_THRESHOLD = 0.15
# 連續推薦去重：近 N 日 daily_report 內已出現過的 (rule_id, code) 視為重複而剔除
_DEDUP_LOOKBACK_DAYS = 5

# C-guard 跨日反向訊號攔截：新訊號 conf 同時滿足 (≥ 舊倉 conf + margin) 且 (≥ abs floor) → 反轉警示模式，否則純擋
_CGUARD_CONF_MARGIN = 0.10
_CGUARD_CONF_ABS_FLOOR = 0.65

# 方案 D 重複訊號強化（同 (code, rule_id) 已 holding）參數
_REINFORCE_THROTTLE_DAYS = 3              # 同部位再強化最短間隔（日曆天，近似 3 交易日）
_REINFORCE_TARGET_CAP_RATIO = 1.5         # 累積 target 不得超過原始 target × 1.5（賣方向相反）
_REINFORCE_STOP_LOOSEN_RATIO = 0.97       # 買方 stop × 0.97（往下放鬆）；賣方對稱用 1.03
_REINFORCE_STOP_FLOOR_RATIO = 0.85        # 買方 stop 下限 = actual_entry × 0.85；賣方上限 = entry × 1.15

# Action 常數（與 track_recommendation_performance 一致）
_ACTION_BUY = "觀察買進"
_ACTION_SELL = "觀察賣出"
_ACTION_WATCH = "停看等"


# --------------------------------------------------------------------------- #
# 共用工具
# --------------------------------------------------------------------------- #

def _compute_valid_until(report_date: str, validity_days: int) -> str:
    """report_date (YYYYMMDD) + validity_days 日曆天 → YYYYMMDD 字串。"""
    try:
        dt = datetime.strptime(report_date, "%Y%m%d")
    except (TypeError, ValueError):
        try:
            dt = datetime.strptime(report_date, "%Y-%m-%d")
        except (TypeError, ValueError):
            return ""
    return (dt + timedelta(days=int(validity_days))).strftime("%Y%m%d")


def _parse_llm_json_array(raw: str) -> list:
    """從 LLM 回傳字串中提取第一個 JSON 陣列。

    先用 greedy regex，失敗時改用 bracket-depth 計數精確截取。失敗一律回傳 []。
    """
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group())
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
        if not end:
            return []
        try:
            return json.loads(raw[raw.index("["):end])
        except (json.JSONDecodeError, ValueError):
            return []


# --------------------------------------------------------------------------- #
# Pipeline 主流程
# --------------------------------------------------------------------------- #

def run(skip_fetch: bool = False, dry_run: bool = False, shadow: bool = False,
        date_str: str | None = None, fetch_only: bool = False) -> int:
    if date_str:
        _dt = datetime.strptime(date_str, "%Y-%m-%d")
        today = date_str
        today_compact = _dt.strftime("%Y%m%d")
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        today_compact = datetime.now().strftime("%Y%m%d")
    mode_tag = " [SHADOW]" if shadow else ""

    # 幂等保護：同一天若報告已存在，直接跳出（補跑 --date 不擋）
    report_path = config.data_dir / "reports" / f"daily_report_{today_compact}.json"
    if report_path.exists() and not date_str:
        print(f"[skip] 今日報告已存在（{report_path.name}），略過。")
        return 0
    print(f"\n{'='*60}")
    print(f"  Market Track Daily Pipeline — {today}{mode_tag}")
    print(f"{'='*60}\n")

    # ── Step 1/10：擷取資料 ──────────────────────────────────────────
    raw_texts: list[str] = []
    if not skip_fetch:
        raw_texts = _step_fetch(date_str=today)
    else:
        print("[1/10] 略過擷取（--skip-fetch）")

    if fetch_only:
        print(f"\n✅ --fetch-only 完成 — {today}")
        return 0

    rules = _load_rules()

    # ── Step 2/10：LLM 事件分類 ─────────────────────────────────────
    matched_events = _step_classify(raw_texts, rules=rules)

    # ── Step 3/10：規則匹配 → 建議清單（含 C-guard 反向訊號攔截）────
    recommendations, reverse_alerts = _step_match_rules(matched_events, rules=rules)

    # ── Step 4/10：大盤方向濾網（台指/TWII）─────────────────────────
    recommendations = _step_market_filter(recommendations)

    # ── Step 5/10：補齊股價（TWSE 今日收盤 + ±3/5% 預設值）──────────
    recommendations = _step_enrich_prices(recommendations)

    # ── Step 6/10：技術面分析（yfinance MA/RSI + batch LLM）─────────
    recommendations = _step_enrich_ta(recommendations, report_date=today_compact)

    # ── Step 6.5/10：過熱濾鏡（近 3 日漲跌幅）───────────────────────
    recommendations = _step_overheat_filter(recommendations)

    # ── Step 6.6/10：近 N 日重複推薦去重 ────────────────────────────
    recommendations = _step_dedup_recent(recommendations, today_compact)

    # ── Step 6.7/10：方案 D 重複訊號強化（同 code+rule 已 holding）─
    open_positions = _load_open_positions()
    recommendations, reinforce_updates = _step_reinforce_positions(
        recommendations, open_positions, today_compact, shadow=shadow
    )

    # ── Step 7/10（前）：投資組合決策（持倉感知）───────────────────
    recommendations, port_updates = _step_portfolio_decision(recommendations, open_positions, dry_run=dry_run)
    position_updates = reinforce_updates + port_updates

    # ── Step 7/10：儲存日報 ─────────────────────────────────────────
    report = _step_save_report(
        recommendations, matched_events, today_compact,
        position_updates=position_updates, reverse_alerts=reverse_alerts,
    )

    # ── Step 8/10：Telegram 推送 ─────────────────────────────────────
    if not dry_run:
        _step_notify(report, today)
    else:
        print("[8/10] Dry-run，略過 Telegram 推送")
        print(_build_telegram_msg(report, today))

    # ── Step 9/10：績效追蹤 + 自動改寫規則庫 ────────────────────────
    _step_track_performance(shadow=shadow, today=today)

    # ── Step 10/10：DISCOVER — 探索新事件規則 ────────────────────────
    _step_discover_rules(raw_texts, shadow=shadow, today=today)

    # ── Step 9/10（後）：更新 PERFORMANCE.md ─────────────────────────
    _step_update_perf_report(today=today)

    # ── Step 10：自動審閱 shadow 提案 ────────────────────────────────
    if not dry_run:
        _step_auto_review_proposals()

    print(f"\n✅ Pipeline 完成 — {today}{mode_tag}")
    return 0


def _step_update_perf_report(today: str | None = None) -> None:
    print("[9/10] 更新 PERFORMANCE.md…")
    try:
        from scripts.generate_perf_report import run as perf_report_run
        perf_report_run(date_str=today)
    except Exception as e:
        print(f"       PERFORMANCE.md 更新失敗（繼續）：{e}")


# --------------------------------------------------------------------------- #
# Step 1：擷取
# --------------------------------------------------------------------------- #

def _step_fetch(date_str: str | None = None) -> list[str]:
    print("[1/10] 擷取 Trump 貼文 & 股癌筆記…")
    trump_texts: list[str] = []
    gua_texts: list[str] = []

    try:
        from scripts.sources.trump_twitter import fetch as fetch_trump
        from email.utils import parsedate_to_datetime
        posts = fetch_trump(save=True, date=date_str)
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
                trump_texts.append(f"{date_prefix}{text}")
        print(f"       Trump：{len(posts)} 則")
    except Exception as e:
        print(f"       Trump 擷取失敗（繼續）：{e}")

    try:
        from scripts.sources.gua_cancer import fetch as fetch_gua
        notes = fetch_gua(save=True, date=date_str)
        for n in notes:
            ep = n.get("episode", "")
            ep_prefix = f"[股癌EP{ep}] " if ep else ""
            content = n.get("content", "").strip()
            if content:
                gua_texts.append(f"{ep_prefix}{content}")
        print(f"       股癌：{len(notes)} 集")
    except Exception as e:
        print(f"       股癌擷取失敗（繼續）：{e}")

    # 股癌優先（含完整選股分析），Trump 取前 30 則（macro 事件）
    return [t for t in gua_texts + trump_texts[:30] if t.strip()]


# --------------------------------------------------------------------------- #
# Step 2：LLM 事件分類
# --------------------------------------------------------------------------- #

def _step_classify(texts: list[str], rules: list[dict] | None = None) -> list[dict]:
    print("[2/10] LLM 事件分類…")
    if not texts:
        print("       無文字輸入，略過 LLM。")
        return []

    if rules is None:
        rules = _load_rules()
    try:
        from scripts.llm_client import analyze
        from scripts.lib.market_filter import get_market_state
        rules_summary = [r["event"] for r in rules]
        # 股癌排前面（完整），Trump 取前 8 則（macro 事件）
        gua_texts = [t for t in texts if t.startswith("[股癌")]
        trump_texts = [t for t in texts if not t.startswith("[股癌")]
        combined = "\n\n---\n".join(gua_texts + trump_texts[:8])

        # 取得大盤背景（有每日 cache，不重複呼叫 API）
        try:
            mkt = get_market_state()
            sf = mkt.get("sector_flows") or {}
            inflow = sf.get("inflow_5d") or []
            outflow = sf.get("outflow_5d") or []
            sector_line = ""
            if inflow or outflow:
                in_str = "、".join(f"{s['name']}(+{s['net_5d']:.0f}億)" for s in inflow) or "—"
                out_str = "、".join(f"{s['name']}({s['net_5d']:+.0f}億)" for s in outflow) or "—"
                sector_line = (
                    f"近5日法人資金（板塊維度）：流入 {in_str}；流出 {out_str}\n"
                    f"→ 若事件影響板塊與資金流入方向一致，可給高 confidence；"
                    f"若事件 bullish 但對應板塊正被法人賣超，請降 confidence 或標記逆勢。\n"
                )
            market_ctx = (
                f"\n\n## 今日市場背景（請參考此背景調整 confidence 與 direction）\n"
                f"{mkt.get('bias_summary', mkt.get('reason', ''))}\n"
                f"{sector_line}"
                f"→ 若大盤整體偏多，bearish 事件請降低 confidence；"
                f"若大盤整體偏空，bullish 事件需有更強基本面支撐才給高 confidence。\n"
            )
        except Exception:
            market_ctx = ""

        prompt = (
            "你是一位台股分析師助理。請從以下文字中，辨識出任何可能對台股造成影響的重要市場事件。\n\n"
            "文字來源說明：\n"
            "- [股癌EPxxx] 開頭：股癌 Podcast 筆記，包含具體個股選股邏輯、產業趨勢分析，請從中提取具體事件（例如：被動元件缺料、特定族群啟動）\n"
            "- [MM/DD] 開頭：Trump 相關新聞，關注宏觀政策事件（關稅、貿易戰等）\n\n"
            "對每個偵測到的事件，回傳 JSON 陣列，格式如下：\n"
            '[{"event": "事件名稱（若與規則庫事件相符請使用完全相同名稱；若為新事件請自行命名，15字以內）", '
            '"confidence": 0.0~1.0, "direction": "bullish|bearish|mixed", '
            '"summary": "一句話摘要", '
            '"source_date": "消息來源日期或集數，從文字前綴 [MM/DD] 或 [股癌EPxxx] 取得，例如：05/06、EP659"}]\n\n'
            f"規則庫已知事件（供參考，不限於此）：{json.dumps(rules_summary, ensure_ascii=False)}\n\n"
            f"{market_ctx}"
            f"待分析文字：\n{combined}"
        )
        raw = analyze(prompt)
        events = _parse_llm_json_array(raw)
        print(f"       偵測到 {len(events)} 個事件")
        return events
    except Exception as e:
        print(f"       LLM 分類失敗（fallback 關鍵字比對）：{e}")

    # Fallback：關鍵字比對
    return _keyword_classify(texts, rules=rules)


def _load_rules() -> list[dict]:
    """載入完整規則清單，失敗回傳 []。"""
    from ruamel.yaml import YAML as _YAML
    _y = _YAML()
    with open(config.rules_file, encoding="utf-8") as f:
        return _y.load(f) or []


def _keyword_classify(texts: list[str], rules: list[dict] | None = None) -> list[dict]:
    if rules is None:
        rules = _load_rules()
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
    return [r["event"] for r in _load_rules()]


def _load_shadow_proposed_events(today: str | None = None) -> list[str]:
    """讀取今日 shadow_updates 已提案的事件名稱，用於 DISCOVER 去重。"""
    if today is None:
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
# 持倉讀取
# --------------------------------------------------------------------------- #

def _load_open_positions() -> list[dict]:
    f = config.data_dir / "performance" / "open_positions.json"
    if not f.exists():
        return []
    with open(f, encoding="utf-8") as fp:
        return json.load(fp)


# --------------------------------------------------------------------------- #
# Step 3：規則匹配
# --------------------------------------------------------------------------- #

def _step_match_rules(matched_events: list[dict],
                      rules: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """產生今日建議清單，同時回傳跨日反向訊號的警示（C-guard）。

    Returns:
        recommendations: 通過所有過濾的建議
        reverse_alerts:  與舊倉反向且 confidence 強於舊倉的訊號（擋掉新建倉，僅警示）
    """
    print(f"[3/10] 規則匹配（{len(matched_events)} 個事件）…")
    if not matched_events:
        return [], []

    if rules is None:
        rules = _load_rules()
    rule_map = {r["event"]: r for r in rules}
    recommendations: list[dict] = []
    reverse_alerts: list[dict] = []
    seen_codes: set[str] = set()

    existing = _load_open_positions()
    # 只擋同 (code, rule_id) 且 state=watching 的；holding 讓建議流過去由 _step_reinforce_positions 處理
    active_pairs_watching: set[tuple[str, str]] = {
        (p["code"], p["rule_id"])
        for p in existing
        if p.get("state") == "watching"
    }
    # 同 code 跨規則反向訊號偵測用：code → [(action, confidence, rule_id), ...]
    code_to_active: dict[str, list[tuple[str, float, str]]] = {}
    for p in existing:
        if p.get("state") not in ("watching", "holding"):
            continue
        act = p.get("action")
        if act not in (_ACTION_BUY, _ACTION_SELL):
            continue
        code_to_active.setdefault(p["code"], []).append((
            act,
            float(p.get("confidence") or 0.5),
            p.get("rule_id", ""),
        ))
    rule_counts: dict[str, int] = {}

    for ev in matched_events:
        rule = rule_map.get(ev["event"])
        if not rule:
            continue
        if rule.get("disabled"):
            continue
        min_conf = rule.get("min_confidence", 0.4)
        if ev.get("confidence", 0) < min_conf:
            continue

        rule_id = ev["event"]
        max_triggers = rule.get("max_daily_triggers")

        if max_triggers and rule_counts.get(rule_id, 0) >= max_triggers:
            continue

        stocks = rule.get("impact", {}).get("stocks", {})
        direction = ev.get("direction") or rule["impact"].get("direction", "mixed")

        for sector, codes in stocks.items():
            for code in codes:
                if max_triggers and rule_counts.get(rule_id, 0) >= max_triggers:
                    break
                if code in seen_codes:
                    continue
                if (code, rule_id) in active_pairs_watching:
                    continue
                action = _direction_to_action(direction, sector)
                new_conf = float(ev.get("confidence") or 0.5)

                # C-guard：跨日反向訊號攔截（買↔賣才算反向，停看等不算）
                if action in (_ACTION_BUY, _ACTION_SELL):
                    opposites = [
                        (a, c, r) for a, c, r in code_to_active.get(code, [])
                        if {a, action} == {_ACTION_BUY, _ACTION_SELL}
                    ]
                    if opposites:
                        max_old_conf = max(c for _, c, _ in opposites)
                        old_act = opposites[0][0]
                        old_rule = opposites[0][2]
                        if (new_conf >= max_old_conf + _CGUARD_CONF_MARGIN
                                and new_conf >= _CGUARD_CONF_ABS_FLOOR):
                            reverse_alerts.append({
                                "code": code,
                                "new_action": action,
                                "new_rule_id": rule_id,
                                "new_confidence": round(new_conf, 2),
                                "existing_action": old_act,
                                "existing_rule_id": old_rule,
                                "existing_confidence": round(max_old_conf, 2),
                                "summary": ev.get("summary", ""),
                            })
                            print(f"       ⚠️  [C-guard] {code} 反向訊號強於舊倉：新 {action}/{new_conf:.2f} > 舊 {old_act}/{max_old_conf:.2f} → 警示（仍擋新倉）")
                        else:
                            print(f"       🛑 [C-guard] {code} 反向訊號未達門檻：新 {action}/{new_conf:.2f} vs 舊 {old_act}/{max_old_conf:.2f} → 擋掉")
                        continue

                seen_codes.add(code)
                rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1
                _horizon = rule.get("horizon", "trend")
                recommendations.append({
                    "code": code,
                    "name": "",  # 由 Step 4 填入
                    "action": action,
                    "sector": sector,
                    "rule_id": rule_id,
                    "confidence": ev.get("confidence", 0.5),
                    "event_summary": ev.get("summary", ""),
                    "entry_low": None,
                    "entry_high": None,
                    "target": None,
                    "stop_loss": None,
                    "horizon": _horizon,
                    "max_holding_days": {"event": 30, "trend": 90, "cycle": 180}.get(_horizon, 90),
                })

    print(f"       產生 {len(recommendations)} 筆建議（watching 鎖 {len(active_pairs_watching)} 對，反向警示 {len(reverse_alerts)} 筆）")
    return recommendations, reverse_alerts


def _direction_to_action(direction: str, sector: str) -> str:
    if direction == "bullish":
        return "觀察買進"
    if direction == "bearish":
        return "觀察賣出"
    # mixed：依板塊判斷
    positive_sectors = {"defense", "gold", "foundry", "impacted_positive"}
    return "觀察買進" if sector in positive_sectors else "停看等"


# --------------------------------------------------------------------------- #
# Step 3.5：大盤方向濾網
# --------------------------------------------------------------------------- #

def _step_market_filter(recs: list[dict]) -> list[dict]:
    """依大盤綜合偏向過濾或調整訊號。

    bullish（台股多頭 + 外資買超 + 美股收漲）：移除「觀察賣出」
    bearish（台股空頭 + 外資賣超 + 美股收跌）：買進建議信心度 × 0.75
    neutral / unknown：不過濾
    """
    print("[4/10] 大盤方向濾網…")
    from scripts.lib.market_filter import get_market_state
    mkt = get_market_state()
    _state_to_bias = {"bull": "bullish", "bear": "bearish", "neutral": "neutral", "unknown": "neutral"}
    bias = mkt.get("bias") or _state_to_bias.get(mkt.get("state", "neutral"), "neutral")
    print(f"       TWII 狀態：{mkt['state'].upper()}（{mkt['reason']}）")
    if "bias_summary" in mkt:
        print(f"       綜合偏向：{bias.upper()} — {mkt.get('bias_summary', '')}")

    if bias == "bullish":
        sell = [r for r in recs if r.get("action") == "觀察賣出"]
        if sell:
            codes = ", ".join(r["code"] for r in sell)
            print(f"       移除 {len(sell)} 筆逆勢賣出訊號（{codes}）")
        return [r for r in recs if r.get("action") != "觀察賣出"]

    if bias == "bearish":
        adjusted = sum(1 for r in recs if r.get("action") == "觀察買進")
        for r in recs:
            if r.get("action") == "觀察買進":
                r["confidence"] = round(r.get("confidence", 0.8) * 0.75, 2)
        if adjusted:
            print(f"       大盤偏空：{adjusted} 筆買進建議信心度降至 75%")

    return recs


# --------------------------------------------------------------------------- #
# Step 4：補齊股價
# --------------------------------------------------------------------------- #

def _step_enrich_prices(recs: list[dict]) -> list[dict]:
    print("[5/10] 查詢股價…")
    if not recs:
        return []

    try:
        codes = [r["code"] for r in recs]
        prices = get_prices(codes)
        for rec in recs:
            info = prices.get(rec["code"])
            if info:
                rec["name"] = info.get("name", rec["code"])
                close = info.get("close")
                if close:
                    is_sell = rec.get("action") == "觀察賣出"
                    rec["last_close"] = close
                    rec["entry_low"]  = round(close * _ENTRY_LOW_FACTOR,  1)
                    rec["entry_high"] = round(close * _ENTRY_HIGH_FACTOR, 1)
                    rec["target"]     = round(close * (_TARGET_FACTOR_SELL if is_sell else _TARGET_FACTOR_BUY), 1)
                    rec["stop_loss"]  = round(close * (_STOP_FACTOR_SELL   if is_sell else _STOP_FACTOR_BUY),   1)
    except Exception as e:
        print(f"       TWSE 股價查詢失敗（繼續）：{e}")

    return recs


# --------------------------------------------------------------------------- #
# Step 4.5：技術面分析（yfinance + batch LLM）
# --------------------------------------------------------------------------- #

def _compute_ta(hist, current_close: float | None = None) -> dict:
    """計算技術指標，current_close 優先用 TWSE 今日收盤（比 yfinance 早一天）。"""
    import math
    close = hist["Close"]
    high  = hist["High"]
    low   = hist["Low"]

    def r(x):
        try:
            v = float(x)
            return None if math.isnan(v) else round(v, 1)
        except Exception:
            return None

    ma5  = r(close.rolling(5).mean().iloc[-1])
    ma20 = r(close.rolling(20).mean().iloc[-1])
    ma60 = r(close.rolling(60).mean().iloc[-1]) if len(close) >= 60 else None
    high20 = r(high.rolling(20).max().iloc[-1])
    low20  = r(low.rolling(20).min().iloc[-1])

    # RSI(14)
    delta = close.diff()
    avg_gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    avg_loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    if avg_loss and avg_loss != 0:
        rs = avg_gain / avg_loss
        rsi = r(100 - 100 / (1 + rs))
    else:
        rsi = 100.0

    # 近 3 個交易日漲跌幅（供過熱濾鏡判斷追高/追空）
    gain_3d = None
    if len(close) >= 4:
        try:
            prev = float(close.iloc[-4])
            cur  = float(close.iloc[-1])
            if prev > 0:
                gain_3d = round((cur - prev) / prev, 4)
        except Exception:
            pass

    return {
        "close": current_close or r(close.iloc[-1]),
        "ma5": ma5, "ma20": ma20, "ma60": ma60,
        "high20": high20, "low20": low20, "rsi": rsi,
        "gain_3d": gain_3d,
    }


def _fetch_ta_data(recs: list[dict], yf) -> dict[str, dict]:
    """每支股票下載 yfinance 歷史資料並計算 TA 指標。

    副作用：若 rec["name"] 為空，從 yfinance info 填入名稱（上櫃股 TWSE 查不到）。
    回傳 ta_map: {code: {close, ma5, ma20, ma60, high20, low20, rsi}}。
    """
    ta_map: dict[str, dict] = {}
    for rec in recs:
        code = rec["code"]
        yf_sym = get_yf_ticker(code)
        try:
            ticker = yf.Ticker(yf_sym)
            h = ticker.history(period="3mo", auto_adjust=True)
            if not h.empty and len(h) >= 20:
                ta = _compute_ta(h, rec.get("last_close"))
                ta_map[code] = ta
                rec["gain_3d"] = ta.get("gain_3d")
                if not rec.get("name"):
                    info = ticker.info
                    yf_name = info.get("shortName") or info.get("longName") or ""
                    if yf_name:
                        rec["name"] = yf_name.replace("*", "").strip()
        except Exception:
            pass
    return ta_map


def _build_ta_prompt(recs: list[dict], ta_map: dict[str, dict]) -> tuple[str, list[str]]:
    """組裝批次 LLM prompt，回傳 (prompt_str, valid_codes)。"""
    header = "| 代碼 | 名稱 | 現價 | MA5 | MA20 | MA60 | 近20日高 | 近20日低 | RSI | 方向 | 視野 |"
    sep    = "|------|------|------|-----|------|------|---------|---------|-----|------|------|"
    rows   = [header, sep]
    valid_codes: list[str] = []
    for rec in recs:
        code = rec["code"]
        if code not in ta_map:
            continue
        ta = ta_map[code]
        horizon = rec.get("horizon", "trend")
        rows.append(
            f"| {code} | {rec.get('name', code)} | {ta['close']} "
            f"| {ta['ma5']} | {ta['ma20']} | {ta['ma60'] or 'N/A'} "
            f"| {ta['high20']} | {ta['low20']} | {ta['rsi']} | {rec.get('action', '停看等')} | {horizon} |"
        )
        valid_codes.append(code)

    prompt = (
        "以下是今日推薦股票的技術面資料，請根據操作方向給出進場區間、目標、停損與一句中文理由。\n\n"
        + "\n".join(rows)
        + "\n\n【方向硬性規則 — 違反會被系統駁回】：\n"
        "- 觀察買進：target > entry_high（目標在進場價上方），stop_loss < entry_low（停損在進場價下方）\n"
        "- 觀察賣出：target < entry_low（目標在進場價下方），stop_loss > entry_high（停損在進場價上方）\n\n"
        "原則（自行判斷，不需死守）：\n"
        "- 觀察買進：進場接近 MA20 支撐，目標近20日高點，停損 MA60 下方\n"
        "- 觀察賣出：進場接近近期壓力減碼，目標近20日低點，停損近期壓力上方\n"
        "- 停看等：提供關鍵支撐/壓力區間作為參考\n\n"
        "視野說明（請根據各股的視野欄位，設定符合該時間框架的 entry/target/stop_loss）：\n"
        "- event（事件驅動，30天）：停損 -8~12%，目標 +15~20%，以近期催化劑消化為退場依據\n"
        "- trend（趨勢行情，90天）：停損 -12~18%，目標 +20~35%，以趨勢結束或反轉訊號為退場依據\n"
        "- cycle（景氣循環，180天）：停損 -15~20%，目標 +30~50%，持股需耐心等待循環高點\n\n"
        "【重要限制】台股每日漲跌幅 ±10%，進場區間（entry_low / entry_high）必須在現價的 95%～110% 以內。"
        "若 MA20 遠低於現價（股票過熱），entry 仍需設在現價 -5% 以內的可執行區間，"
        "並在 reason 中說明需等回測，不可直接寫出遙不可及的 MA20 價位。\n\n"
        "validity_days：進場區間的有效天數（日曆天），超過此區間視訊號失效。建議：\n"
        "- event 視野：3~5 天（催化劑消化快）\n"
        "- trend 視野：5~8 天\n"
        "- cycle 視野：7~10 天（給足建倉時間）\n"
        "若進場價貼近現價可給較短天數，若需等回測支撐可給較長天數，上限 10 天。\n\n"
        "只輸出 JSON 陣列，不要其他說明文字：\n"
        '[{"code":"XXXX","entry_low":X,"entry_high":Y,"target":Z,"stop_loss":W,"validity_days":N,"reason":"一句話"}]'
    )
    return prompt, valid_codes


def _apply_ta_results(recs: list[dict], items: list[dict]) -> dict:
    """將 LLM 回傳的 TA 結果套用到 recs（in-place），並硬截斷進場價邊界。回傳 {code: item}。

    方向驗證：若 LLM 回傳的 target/stop 與 action 方向不一致（例如觀察賣出卻給買進方向的
    target/stop），則回退到 Step 5 的方向感知預設值，避免錯誤建議流出。
    """
    ta_result = {item["code"]: item for item in items if "code" in item}
    for rec in recs:
        item = ta_result.get(rec["code"])
        if item:
            for key in ("entry_low", "entry_high", "target", "stop_loss", "reason", "validity_days"):
                if item.get(key) is not None:
                    rec[key] = item[key]
        close = rec.get("last_close")
        if close:
            floor = round(close * _TA_ENTRY_FLOOR, 1)
            ceil  = round(close * _TA_ENTRY_CEIL,  1)
            if rec.get("entry_low") is not None:
                rec["entry_low"]  = max(floor, min(ceil, rec["entry_low"]))
            if rec.get("entry_high") is not None:
                rec["entry_high"] = max(floor, min(ceil, rec["entry_high"]))
        _enforce_direction(rec)
    return ta_result


def _enforce_direction(rec: dict) -> None:
    """驗證 target/stop 方向與 action 一致；不一致則回退到 close 為基準的方向感知預設值。"""
    action = rec.get("action")
    close  = rec.get("last_close")
    entry_low  = rec.get("entry_low")
    entry_high = rec.get("entry_high")
    target = rec.get("target")
    stop   = rec.get("stop_loss")
    if not close or entry_low is None or entry_high is None or target is None or stop is None:
        return

    is_sell = action == "觀察賣出"
    is_buy  = action == "觀察買進"
    if not (is_sell or is_buy):
        return  # 停看等不需要方向驗證

    if is_buy:
        ok = target > entry_high and stop < entry_low
    else:
        ok = target < entry_low and stop > entry_high

    if ok:
        return

    new_target = round(close * (_TARGET_FACTOR_SELL if is_sell else _TARGET_FACTOR_BUY), 1)
    new_stop   = round(close * (_STOP_FACTOR_SELL   if is_sell else _STOP_FACTOR_BUY),   1)
    print(f"       [警告] {rec.get('code')} {action} target/stop 方向不一致 "
          f"(target={target} stop={stop} entry={entry_low}~{entry_high})，回退預設值 "
          f"target={new_target} stop={new_stop}")
    rec["target"]    = new_target
    rec["stop_loss"] = new_stop


def _step_enrich_ta(recs: list[dict], report_date: str | None = None) -> list[dict]:
    """yfinance 拉歷史 OHLCV → 計算 TA → 一次 LLM call → 覆寫 entry/target/stop/reason。"""
    if not recs:
        return recs
    print("[6/10] 技術面分析（yfinance + LLM）…")

    try:
        import yfinance as yf
    except ImportError:
        print("       yfinance 未安裝，略過。")
        _finalize_validity(recs, report_date)
        return recs

    ta_map = _fetch_ta_data(recs, yf)
    if not ta_map:
        print("       TA 資料全部失敗，使用 ±3/5% 預設值")
        _finalize_validity(recs, report_date)
        return recs

    prompt, valid_codes = _build_ta_prompt(recs, ta_map)
    if not valid_codes:
        _finalize_validity(recs, report_date)
        return recs

    try:
        from scripts.llm_client import analyze
        raw = analyze(prompt)
        items = _parse_llm_json_array(raw)
        if not items:
            print("       TA LLM 未回傳 JSON，使用預設值")
        else:
            ta_result = _apply_ta_results(recs, items)
            print(f"       TA 完成，{len(ta_result)} 支股票已更新進出場價")
    except Exception as e:
        print(f"       TA LLM 失敗（使用 ±3/5% 預設值）：{e}")

    _finalize_validity(recs, report_date)

    # 若 TWSE 與 yfinance 都查不到名稱，視為已下市/無效代碼，丟棄避免報告出現裸代碼
    nameless = [r for r in recs if not r.get("name")]
    if nameless:
        for r in nameless:
            print(f"       ⚠️  丟棄 {r.get('code')}：TWSE/yfinance 皆查無名稱（可能已下市或代碼錯誤）")
        recs = [r for r in recs if r.get("name")]
    return recs


def _finalize_validity(recs: list[dict], report_date: str | None = None) -> None:
    """補齊 validity_days（缺則依 horizon 預設）並計算 valid_until。in-place 改 recs。"""
    today_compact = report_date or datetime.now().strftime("%Y%m%d")
    for rec in recs:
        horizon = rec.get("horizon", "trend")
        default = _HORIZON_DEFAULT_VALIDITY.get(horizon, 7)
        try:
            v = int(rec.get("validity_days") or default)
        except (TypeError, ValueError):
            v = default
        # 上限以 MAX_WATCH_DAYS 為硬界線（避免 LLM 給出脫離追蹤能力的天數）
        v = max(1, min(v, 14))
        rec["validity_days"] = v
        rec["valid_until"]   = _compute_valid_until(today_compact, v)


# --------------------------------------------------------------------------- #
# Step 4.6：過熱濾鏡（近 3 日漲跌幅檢查）
# --------------------------------------------------------------------------- #

def _step_overheat_filter(recs: list[dict]) -> list[dict]:
    """剔除近 3 日漲幅過大的觀察買進、跌幅過大的觀察賣出（追高/追空保護）。"""
    if not recs:
        return recs
    print(f"[6.5/10] 過熱濾鏡（近 3 日 |漲跌| > {_OVERHEAT_GAIN_THRESHOLD*100:.0f}%）…")
    kept: list[dict] = []
    dropped: list[tuple[dict, float, str]] = []
    for r in recs:
        gain = r.get("gain_3d")
        action = r.get("action", "")
        if gain is None:
            kept.append(r)
            continue
        if "買進" in action and gain > _OVERHEAT_GAIN_THRESHOLD:
            dropped.append((r, gain, "買進追高"))
        elif "賣出" in action and gain < -_OVERHEAT_GAIN_THRESHOLD:
            dropped.append((r, gain, "賣出追空"))
        else:
            kept.append(r)
    for r, g, why in dropped:
        print(f"       ✂️  {r.get('code')} {r.get('name','')} 近3日 {g*100:+.1f}% — {why}")
    print(f"       保留 {len(kept)} / {len(recs)} 筆")
    return kept


# --------------------------------------------------------------------------- #
# Step 4.6.5：近 N 日重複推薦去重
# --------------------------------------------------------------------------- #

def _step_dedup_recent(recs: list[dict], today_compact: str) -> list[dict]:
    """剔除近 N 日 daily_report 已出現過的 (rule_id, code) 組合。"""
    if not recs:
        return recs
    print(f"[6.6/10] 近 {_DEDUP_LOOKBACK_DAYS} 日重複推薦去重…")
    reports_dir = config.data_dir / "reports"
    try:
        today_dt = datetime.strptime(today_compact, "%Y%m%d")
    except ValueError:
        print("       today_compact 解析失敗，略過去重")
        return recs
    seen: set[tuple[str, str]] = set()
    for offset in range(1, _DEDUP_LOOKBACK_DAYS + 1):
        d = (today_dt - timedelta(days=offset)).strftime("%Y%m%d")
        path = reports_dir / f"daily_report_{d}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for old in data.get("recommendations", []):
            rid = old.get("rule_id")
            code = old.get("code")
            if rid and code:
                seen.add((rid, code))
    kept: list[dict] = []
    dropped: list[dict] = []
    for r in recs:
        key = (r.get("rule_id"), r.get("code"))
        if key[0] and key[1] and key in seen:
            dropped.append(r)
        else:
            kept.append(r)
    for r in dropped:
        print(f"       ✂️  {r.get('code')} {r.get('name','')} — {r.get('rule_id','')[:24]} 近 {_DEDUP_LOOKBACK_DAYS} 日已推過")
    print(f"       保留 {len(kept)} / {len(recs)} 筆")
    return kept


# --------------------------------------------------------------------------- #
# Step 4.65：重複訊號強化（方案 D）
# --------------------------------------------------------------------------- #

def _step_reinforce_positions(
    recs: list[dict],
    open_positions: list[dict],
    today_compact: str,
    shadow: bool = False,
) -> tuple[list[dict], list[dict]]:
    """方案 D：同 (code, rule_id) 已 holding 的新訊號 → 抽出轉為 raise_target 強化。

    規則：
      - Target = max(舊 target, 今日 target)，cap = 原始 target × 1.5
      - Stop（買進）= max(舊 stop × 0.97, entry × 0.85)；賣方對稱
      - 節流：同部位 ≥ N 日才能再 reinforce
      - 啟動門檻：浮動獲利 ≥ 0%（浮虧中不強化）
      - shadow=True：log 並寫入 report 但標記 shadow，track 階段不套用
    """
    if not recs:
        return recs, []
    holdings_by_key: dict[tuple[str, str], dict] = {}
    for p in open_positions:
        if p.get("state") != "holding":
            continue
        key = (p.get("code"), p.get("rule_id"))
        if all(key):
            holdings_by_key[key] = p

    if not holdings_by_key:
        return recs, []

    print(f"[6.7/10] 重複訊號強化檢查（{len(recs)} 筆 vs {len(holdings_by_key)} holding）…")

    try:
        today_dt = datetime.strptime(today_compact, "%Y%m%d")
    except ValueError:
        print("       today_compact 解析失敗，略過強化")
        return recs, []

    kept_recs: list[dict] = []
    reinforce_updates: list[dict] = []

    for r in recs:
        key = (r.get("code"), r.get("rule_id"))
        pos = holdings_by_key.get(key)
        if not pos:
            kept_recs.append(r)
            continue

        # 節流
        last_rein = pos.get("last_reinforced_date")
        if last_rein:
            try:
                last_dt = datetime.strptime(last_rein, "%Y%m%d")
                if (today_dt - last_dt).days < _REINFORCE_THROTTLE_DAYS:
                    print(f"       ⏳ [D] {r.get('code')} {r.get('name','')} 跳過：{last_rein} 已 reinforce（< {_REINFORCE_THROTTLE_DAYS} 日節流）")
                    continue
            except ValueError:
                pass

        entry = pos.get("actual_entry_price")
        last_close = pos.get("last_close")
        old_target = pos.get("target")
        old_stop = pos.get("stop_loss")
        if not (entry and last_close and old_target and old_stop):
            print(f"       ⊝ [D] {r.get('code')} 跳過：舊倉欄位不完整")
            continue

        is_sell = pos.get("action") == _ACTION_SELL
        pnl_pct = ((entry - last_close) if is_sell else (last_close - entry)) / entry
        if pnl_pct < 0:
            print(f"       💤 [D] {r.get('code')} 跳過：浮虧 {pnl_pct*100:+.1f}%（待趨勢確認）")
            continue

        # 原始 target（第一次 reinforce 時鎖定）
        original_target = pos.get("target_original") or old_target

        # 計算 new_target
        today_target = r.get("target")
        if is_sell:
            # 賣方目標在下方，「強化」= target 更低
            candidate = min(old_target, today_target) if today_target else old_target
            cap = original_target / _REINFORCE_TARGET_CAP_RATIO  # 下界
            new_target = max(candidate, cap)
        else:
            candidate = max(old_target, today_target) if today_target else old_target
            cap = original_target * _REINFORCE_TARGET_CAP_RATIO
            new_target = min(candidate, cap)

        # 計算 new_stop（往「不利方向」放鬆 = 給趨勢更多空間）
        if is_sell:
            stop_ceiling = entry * (2 - _REINFORCE_STOP_FLOOR_RATIO)  # = entry × 1.15
            new_stop = min(old_stop * (2 - _REINFORCE_STOP_LOOSEN_RATIO), stop_ceiling)
        else:
            stop_floor = entry * _REINFORCE_STOP_FLOOR_RATIO
            new_stop = max(old_stop * _REINFORCE_STOP_LOOSEN_RATIO, stop_floor)

        new_target = round(new_target, 2)
        new_stop = round(new_stop, 2)
        target_changed = abs(new_target - old_target) > 0.01
        stop_changed = abs(new_stop - old_stop) > 0.01

        if not target_changed and not stop_changed:
            print(f"       ⊝ [D] {r.get('code')} 無實質變化（已達上下限）")
            continue

        tag = "[SHADOW]" if shadow else ""
        print(f"       🔧 [D]{tag} {r.get('code')} {r.get('name','')} target {old_target}→{new_target}  stop {old_stop}→{new_stop}  浮動 {pnl_pct*100:+.1f}%")

        reinforce_updates.append({
            "code": r.get("code"),
            "name": r.get("name", pos.get("name", "")),
            "update_type": "raise_target",
            "new_target": new_target if target_changed else None,
            "new_stop": new_stop if stop_changed else None,
            "reason": f"規則「{(r.get('rule_id') or '')[:18]}」再次觸發（浮動 {pnl_pct*100:+.1f}%）",
            "rule_id": r.get("rule_id", ""),
            "reinforced_on": today_compact,
            "source": "reinforce",
            "shadow": shadow,
        })

    print(f"       保留 {len(kept_recs)} / {len(recs)} 筆，產出 {len(reinforce_updates)} 筆強化指令")
    return kept_recs, reinforce_updates


# --------------------------------------------------------------------------- #
# Step 4.7：投資組合決策
# --------------------------------------------------------------------------- #

def _step_portfolio_decision(
    recs: list[dict],
    open_positions: list[dict],
    dry_run: bool = False,
) -> tuple[list[dict], list[dict]]:
    """依持倉狀態讓 LLM 決定每支推薦的操作類型。

    Returns:
        filtered_recs:    update_type 為 new / add 的建議（進入正常開倉流程）
        position_updates: update_type 為 raise_target 的調整指令
    """
    if not recs:
        return [], []

    open_codes = {p["code"] for p in open_positions}

    if not open_codes:
        for r in recs:
            r["update_type"] = "new"
        return recs, []

    holding_lines = []
    for p in open_positions:
        entry = p.get("actual_entry_price") or "觀察中"
        lc = p.get("last_close", "?")
        state = "持有中" if p.get("state") == "holding" else "觀察中"
        pnl_str = ""
        if p.get("actual_entry_price") and p.get("last_close"):
            pnl = (p["last_close"] - p["actual_entry_price"]) / p["actual_entry_price"] * 100
            pnl_str = f" 浮動{pnl:+.1f}%"
        holding_lines.append(
            f"  {p['code']} {p.get('name','')}｜{state}｜進場{entry}｜"
            f"目標{p.get('target','?')}｜現價{lc}{pnl_str}｜"
            f"持有{p.get('days_watched',0)}天｜規則:{p.get('rule_id','')[:14]}"
        )

    rec_lines = []
    for r in recs:
        rec_lines.append(
            f"  {r['code']} {r.get('name','')}｜{r.get('action','')}｜"
            f"進場{r.get('entry_low','?')}-{r.get('entry_high','?')}｜"
            f"目標{r.get('target','?')}｜規則:{r.get('rule_id','')[:16]}"
        )

    prompt = (
        "你是一位台股投資組合管理師。以下是今日市場訊號觸發的推薦標的，以及目前已追蹤的倉位。\n\n"
        "請對每支推薦股票決定操作類型：\n"
        "- \"new\"：目前無此股票倉位，建議開新倉\n"
        "- \"add\"：已有倉位，但今日有更好的進場點或訊號更強，建議加倉（獨立計算損益）\n"
        "- \"raise_target\"：已進場持有（holding）的倉位，今日訊號支持上調目標；"
        "new_stop 必須低於 entry_low（買進）或高於 entry_high（賣出），"
        "觀察中（watching）的部位不調整停損\n"
        "- \"hold\"：已有倉位，今日訊號只是再次確認，繼續持有，無需動作\n\n"
        "判斷原則：\n"
        "- 已持倉且浮動獲利 > 5% → 傾向 raise_target 而非 add\n"
        "- 已持倉且新進場區間明顯更低（加碼攤低）→ 可考慮 add\n"
        "- 同一支股票 add 不超過一次\n\n"
        f"目前持倉/觀察（{len(open_positions)} 筆）：\n" + "\n".join(holding_lines) + "\n\n"
        f"今日新訊號推薦（{len(recs)} 筆）：\n" + "\n".join(rec_lines) + "\n\n"
        "只輸出 JSON 陣列，不要其他文字：\n"
        '[{"code":"XXXX","update_type":"new|add|raise_target|hold",'
        '"new_target":null,"new_stop":null,"reason":"一句話"}]'
    )

    try:
        from scripts.llm_client import analyze
        raw = analyze(prompt, model=None if dry_run else "claude-opus-4-7")
        parsed = _parse_llm_json_array(raw)
        if not parsed:
            raise ValueError("LLM 未回傳 JSON")
        decisions = {d["code"]: d for d in parsed if "code" in d}
    except Exception as e:
        print(f"       投資組合決策 LLM 失敗（fallback）：{e}")
        decisions = {}

    filtered_recs: list[dict] = []
    position_updates: list[dict] = []

    for r in recs:
        dec = decisions.get(r["code"])
        if dec:
            update_type = dec.get("update_type", "new")
        else:
            update_type = "new" if r["code"] not in open_codes else "hold"
        r["update_type"] = update_type

        if update_type in ("new", "add"):
            if dec and dec.get("reason"):
                r.setdefault("reason", dec["reason"])
            filtered_recs.append(r)
        elif update_type == "raise_target":
            position_updates.append({
                "code":       r["code"],
                "name":       r.get("name", ""),
                "update_type": "raise_target",
                "new_target": dec.get("new_target") if dec else r.get("target"),
                "new_stop":   dec.get("new_stop")   if dec else r.get("stop_loss"),
                "reason":     dec.get("reason", "")  if dec else "",
                "rule_id":    r.get("rule_id", ""),
            })
        # "hold" → 不開倉，不調整，略過

    new_count = sum(1 for r in filtered_recs if r["update_type"] == "new")
    add_count  = sum(1 for r in filtered_recs if r["update_type"] == "add")
    upd_count  = len(position_updates)
    print(f"       新倉 {new_count} 筆｜加倉 {add_count} 筆｜調目標 {upd_count} 筆")
    return filtered_recs, position_updates


# --------------------------------------------------------------------------- #
# Step 5：存日報
# --------------------------------------------------------------------------- #

def _step_save_report(recs: list[dict], events: list[dict], date_compact: str,
                      position_updates: list[dict] | None = None,
                      reverse_alerts: list[dict] | None = None) -> dict:
    print("[7/10] 儲存日報…")
    from scripts.lib.market_filter import get_market_state
    report = {
        "date": date_compact,
        "generated_at": datetime.now().isoformat(),
        "market_state": get_market_state(),
        "events": events,
        "recommendations": recs,
        "position_updates": position_updates or [],
        "reverse_alerts": reverse_alerts or [],
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
    print("[8/10] 推送 Telegram…")
    msg = _build_telegram_msg(report, today)
    try:
        result = tg_send(msg)
        print(f"       ✓ message_id={result['result']['message_id']}")
    except Exception as e:
        print(f"       ✗ 推送失敗：{e}")


def _partition_report(report: dict) -> dict:
    """將 report 的建議拆分為顯示分組。"""
    recs = report.get("recommendations", [])
    new_recs = [r for r in recs if r.get("update_type") != "add"]
    add_recs = [r for r in recs if r.get("update_type") == "add"]
    return {
        "all_recs":    recs,
        "buy":         [r for r in new_recs if r.get("action") == "觀察買進"],
        "sell":        [r for r in new_recs if r.get("action") == "觀察賣出"],
        "watch":       [r for r in new_recs if r.get("action") == "停看等"],
        "add_recs":    add_recs,
        "pos_updates": report.get("position_updates", []),
    }


def _fmt_rec(r: dict, label_override: str | None = None) -> list[str]:
    """將一筆建議格式化為 Telegram 訊息行列。"""
    action = r.get("action", "")
    conf_str = f"（信心 {r['confidence']:.2f}）" if r.get("confidence") else ""
    name = r.get("name", "").replace("*", "").strip()
    tag = f" `{label_override}`" if label_override else ""
    out = [f"• {r['code']} {name}{conf_str}{tag}"]
    el, eh = r.get("entry_low"), r.get("entry_high")
    tgt, stp = r.get("target"), r.get("stop_loss")
    if el and eh:
        label = "減碼" if action == "觀察賣出" else "進場"
        price_line = f"  {label} {el}-{eh}"
        if tgt:
            price_line += f"  目標 {tgt}"
        if stp:
            price_line += f"  停損 {stp}"
        out.append(price_line)
    elif r.get("last_close"):
        price_line = f"  現價 {r['last_close']}"
        if stp:
            price_line += f"  停損 {stp}"
        out.append(price_line)
    if r.get("reason"):
        out.append(f"  ↳ {r['reason']}")
    elif r.get("rule_id"):
        out.append(f"  依據：{r['rule_id']}")
    return out


def _build_telegram_msg(report: dict, today: str) -> str:
    lines = [f"📊 *{today} 每日台股建議*\n"]

    sf = (report.get("market_state") or {}).get("sector_flows") or {}
    inflow = sf.get("inflow_5d") or []
    outflow = sf.get("outflow_5d") or []
    if inflow or outflow:
        lines.append("💰 *板塊資金（近5日法人）*")
        if inflow:
            lines.append("🟢 流入 " + "、".join(s["name"] for s in inflow))
        if outflow:
            lines.append("🔴 流出 " + "、".join(s["name"] for s in outflow))
        lines.append("")

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

    groups = _partition_report(report)
    buy, sell, watch = groups["buy"], groups["sell"], groups["watch"]
    add_recs, pos_updates = groups["add_recs"], groups["pos_updates"]

    if buy:
        lines.append("📈 *建議觀察買進*")
        for r in buy[:8]:
            lines.extend(_fmt_rec(r))
        lines.append("")

    if sell:
        lines.append("📉 *建議觀察賣出*")
        for r in sell[:5]:
            lines.extend(_fmt_rec(r))
        lines.append("")

    if watch:
        lines.append("⏸ *停看等*")
        for r in watch[:5]:
            lines.extend(_fmt_rec(r))
        lines.append("")

    if add_recs:
        lines.append("➕ *建議加倉*")
        for r in add_recs[:5]:
            lines.extend(_fmt_rec(r, label_override="加倉"))
        lines.append("")

    if pos_updates:
        reinforce_ups = [u for u in pos_updates if u.get("source") == "reinforce"]
        other_ups = [u for u in pos_updates if u.get("source") != "reinforce"]

        if reinforce_ups:
            lines.append("🔧 *持倉強化（重複訊號）*")
            for u in reinforce_ups[:5]:
                shadow_tag = " `[SHADOW]`" if u.get("shadow") else ""
                name_line = f"• {u['code']} {u.get('name','')}".rstrip() + shadow_tag
                if u.get("new_target"):
                    name_line += f"  目標→{u['new_target']}"
                if u.get("new_stop"):
                    name_line += f"  停損→{u['new_stop']}"
                lines.append(name_line)
                if u.get("reason"):
                    lines.append(f"  ↳ {u['reason']}")
            lines.append("")

        if other_ups:
            lines.append("🎯 *持倉目標上調*")
            for u in other_ups[:5]:
                name_line = f"• {u['code']}"
                if u.get("new_target"):
                    name_line += f"  新目標 {u['new_target']}"
                if u.get("new_stop"):
                    name_line += f"  新停損 {u['new_stop']}"
                lines.append(name_line)
                if u.get("reason"):
                    lines.append(f"  ↳ {u['reason']}")
            lines.append("")

    rev_alerts = report.get("reverse_alerts") or []
    if rev_alerts:
        lines.append("⚠️ *反向訊號警示（請手動審視）*")
        for a in rev_alerts[:5]:
            new_a = a.get("new_action", "")
            old_a = a.get("existing_action", "")
            new_c = a.get("new_confidence", 0)
            old_c = a.get("existing_confidence", 0)
            lines.append(f"• {a['code']}：新 {new_a}/{new_c:.2f} vs 舊 {old_a}/{old_c:.2f}")
            if a.get("summary"):
                lines.append(f"  ↳ {a['summary']}")
            lines.append(f"  新規則：{a.get('new_rule_id','')[:24]}")
            lines.append(f"  舊規則：{a.get('existing_rule_id','')[:24]}")
        lines.append("")

    if not buy and not watch and not add_recs and not pos_updates and not rev_alerts:
        lines.append("今日無明確建議標的，維持觀望。")
        matched_rule_ids = {r.get("rule_id") for r in groups["all_recs"]}
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

def _step_track_performance(shadow: bool = False, today: str | None = None) -> None:
    print("[9/10] 績效追蹤（昨日建議）…")
    try:
        from scripts.track_recommendation_performance import run as perf_run
        perf_run(shadow=shadow, today_str=today)
        shadow_note = "（shadow 模式，規則更新已記錄到 data/shadow_updates/）" if shadow else ""
        print(f"       規則庫自動改寫完成 {shadow_note}")
    except Exception as e:
        print(f"       績效追蹤失敗（繼續）：{e}")


# --------------------------------------------------------------------------- #
# Step 8：DISCOVER — 探索新事件規則
# --------------------------------------------------------------------------- #

def _step_discover_rules(raw_texts: list[str], shadow: bool = False, today: str | None = None) -> None:
    print("[10/10] DISCOVER — 探索新事件規則…")
    if not raw_texts:
        print("       無文字輸入，略過。")
        return
    if config.llm_backend == "stub":
        print("       stub 模式，略過。")
        return

    try:
        from scripts.llm_client import analyze
        from scripts.auto_update_rules import apply_updates

        existing_events = list(dict.fromkeys(
            _load_rules_summary() + _load_shadow_proposed_events(today=today)
        ))  # 去重，保持順序
        gua_texts = [t[:1500] for t in raw_texts if t.startswith("[股癌")]  # 截斷避免 LLM 超時
        trump_texts = [t for t in raw_texts if not t.startswith("[股癌")]
        combined = "\n\n---\n".join(gua_texts + trump_texts[:8])
        prompt = (
            "你是一位台股事件規則分析師。請從以下新聞文字中，找出「現有規則庫尚未涵蓋」的全新事件模式。\n\n"
            "文字來源說明（用於填寫 source_names 欄位）：\n"
            "- 前綴 [股癌EP...] 的文字來自 gua_cancer\n"
            "- 前綴 [MM/DD] 的文字來自 trump_news\n\n"
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
            '    "source_names": ["gua_cancer"],\n'
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
        items = _parse_llm_json_array(raw)
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
                "source_names": item.get("source_names", []),
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
# Step 10：自動審閱 shadow 提案
# --------------------------------------------------------------------------- #

def _load_source_trust() -> dict:
    """讀取 source_trust.yml，回傳 {source_name: trust_level}。"""
    trust_file = Path(__file__).parent / "source_trust.yml"
    if not trust_file.exists():
        return {}
    from ruamel.yaml import YAML as _YAML
    _y = _YAML()
    with open(trust_file, encoding="utf-8") as f:
        data = _y.load(f) or {}
    return {k: v.get("trust", "low") for k, v in data.get("sources", {}).items()}


def _step_auto_review_proposals() -> None:
    """依 source_trust.yml 自動審閱 shadow 提案，approve 寫入 YAML，reject 記錄到 _reviewed.json。"""
    print("[10/10] 自動審閱 shadow 提案…")
    try:
        from scripts.review_shadow_proposals import _load_all_proposals, _load_reviewed, _save_reviewed
        from scripts.auto_update_rules import apply_updates

        trust_map = _load_source_trust()
        all_proposals = _load_all_proposals()
        reviewed = _load_reviewed()

        # 去重：同名事件只取最新一筆
        seen: dict[str, dict] = {}
        for p in all_proposals:
            seen[p["event"]] = p
        pending = [p for p in seen.values() if p["event"] not in reviewed]

        if not pending:
            print("       無待審提案。")
            return

        print(f"       待審 {len(pending)} 筆，開始判斷…")

        fast_approve, fast_reject, need_llm = [], [], []

        for p in pending:
            conf = p.get("confidence", 0)
            sources = p.get("source_names", [])
            has_stocks = bool(p.get("new_event", {}).get("impact", {}).get("stocks"))

            # 快速拒絕：信心過低
            if conf < 0.75:
                fast_reject.append(p)
                continue

            # 快速接受：全部來源都是 high trust + 信心夠 + 有具體股票
            source_trusts = [trust_map.get(s, "low") for s in sources] if sources else ["low"]
            if all(t == "high" for t in source_trusts) and conf >= 0.85 and has_stocks:
                fast_approve.append(p)
                continue

            need_llm.append(p)

        # 處理快速結果
        for p in fast_approve:
            print(f"       ✅ 自動接受（high trust + conf {p['confidence']:.2f}）：{p['event']}")
            apply_updates([p], shadow=False)
            _save_reviewed(p["event"], status="auto_approved")

        for p in fast_reject:
            print(f"       ❌ 自動拒絕（conf {p['confidence']:.2f} < 0.75）：{p['event']}")
            _save_reviewed(p["event"], status="auto_rejected")

        # LLM 批次審閱剩餘提案
        if need_llm:
            trust_desc = "\n".join(
                f"- {k}: {'HIGH → 信心≥0.80 即接受' if v == 'high' else 'MEDIUM → 需強力佐證' if v == 'medium' else 'LOW → 不自動接受'}"
                for k, v in trust_map.items()
            )
            existing_rules = _load_rules_summary()
            proposal_lines = []
            for i, p in enumerate(need_llm, 1):
                stocks = p.get("new_event", {}).get("impact", {}).get("stocks", {})
                src = p.get("source_names", ["未知"])
                proposal_lines.append(
                    f"[{i}] event: \"{p['event']}\", sources: {src}, "
                    f"confidence: {p.get('confidence', 0):.2f}, stocks: {stocks}\n"
                    f"    reason: \"{p.get('reason', '')[:100]}\""
                )
            prompt = (
                "你是台股事件規則審閱員。以下是 DISCOVER 機制提議的新規則，請逐一決定是否加入規則庫。\n\n"
                f"來源信任度：\n{trust_desc}\n\n"
                f"現有規則庫（不要重複接受）：{json.dumps(existing_rules, ensure_ascii=False)}\n\n"
                "待審提案：\n" + "\n".join(proposal_lines) + "\n\n"
                "只輸出 JSON 陣列，不要說明文字：\n"
                '[{"event": "...", "decision": "approve", "reason": "一句話"}]'
            )
            try:
                from scripts.llm_client import analyze
                raw = analyze(prompt)
                decisions = _parse_llm_json_array(raw)
                if decisions:
                    dec_map = {d["event"]: d for d in decisions if "event" in d}
                    for p in need_llm:
                        dec = dec_map.get(p["event"])
                        if dec and dec.get("decision") == "approve":
                            print(f"       ✅ LLM 接受：{p['event']}（{dec.get('reason', '')}）")
                            apply_updates([p], shadow=False)
                            _save_reviewed(p["event"], status="llm_approved")
                        else:
                            reason = dec.get("reason", "") if dec else "LLM 未回傳決定"
                            print(f"       ❌ LLM 拒絕：{p['event']}（{reason}）")
                            _save_reviewed(p["event"], status="llm_rejected")
                else:
                    print("       LLM 未回傳 JSON，跳過 LLM 審閱。")
            except Exception as e:
                print(f"       LLM 審閱失敗（繼續）：{e}")

        total_approved = len(fast_approve) + sum(
            1 for p in need_llm
            if (config.data_dir / "shadow_updates" / "_reviewed.json").exists()
        )
        print(f"       完成：接受 {len(fast_approve)} 筆（快速）+ LLM 審閱 {len(need_llm)} 筆")

    except Exception as e:
        print(f"       自動審閱失敗（繼續）：{e}")


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
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="補跑指定日期（預設今天）",
    )
    parser.add_argument("--fetch-only", action="store_true", help="只擷取原始資料，不分析不推送（週末用）")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        code = run(skip_fetch=args.skip_fetch, dry_run=args.dry_run, shadow=args.shadow,
                   date_str=args.date, fetch_only=args.fetch_only)
    except Exception:
        traceback.print_exc()
        try:
            tg_send("⚠️ *Market Track Pipeline 異常中止*\n請檢查日誌。")
        except Exception:
            pass
        code = 1
    sys.exit(code)
