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
from scripts.lib.twse_client import get_prices, format_price_line, get_yf_ticker


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

    # ── Step 1：擷取資料 ──────────────────────────────────────────────
    raw_texts: list[str] = []
    if not skip_fetch:
        raw_texts = _step_fetch(date_str=today)
    else:
        print("[1/8] 略過擷取（--skip-fetch）")

    if fetch_only:
        print(f"\n✅ --fetch-only 完成 — {today}")
        return 0

    # ── Step 2：LLM 事件分類 ─────────────────────────────────────────
    matched_events = _step_classify(raw_texts)

    # ── Step 3：規則匹配 → 建議清單 ──────────────────────────────────
    recommendations = _step_match_rules(matched_events)

    # ── Step 4：補齊股價（TWSE 今日收盤 + ±3/5% 預設值）────────────────
    recommendations = _step_enrich_prices(recommendations)

    # ── Step 4.5：技術面分析（yfinance MA/RSI + batch LLM）─────────────
    recommendations = _step_enrich_ta(recommendations)

    # ── Step 4.7：投資組合決策（持倉感知）────────────────────────────
    open_positions = _load_open_positions()
    recommendations, position_updates = _step_portfolio_decision(recommendations, open_positions)

    # ── Step 5：儲存日報 ─────────────────────────────────────────────
    report = _step_save_report(recommendations, matched_events, today_compact, position_updates)

    # ── Step 6：Telegram 推送 ─────────────────────────────────────────
    if not dry_run:
        _step_notify(report, today)
    else:
        print("[6/8] Dry-run，略過 Telegram 推送")
        print(_build_telegram_msg(report, today))

    # ── Step 7：績效追蹤 + 自動改寫規則庫 ────────────────────────────
    _step_track_performance(shadow=shadow, today=today)

    # ── Step 8：DISCOVER — 探索新事件規則 ────────────────────────────
    _step_discover_rules(raw_texts, shadow=shadow, today=today)

    # ── Step 9：更新 PERFORMANCE.md 並推送 GitHub ─────────────────────
    _step_update_perf_report(today=today)

    # ── Step 10：自動審閱 shadow 提案 ────────────────────────────────
    if not dry_run:
        _step_auto_review_proposals()

    print(f"\n✅ Pipeline 完成 — {today}{mode_tag}")
    return 0


def _step_update_perf_report(today: str | None = None) -> None:
    print("[9/9] 更新 PERFORMANCE.md…")
    try:
        from scripts.generate_perf_report import run as perf_report_run
        perf_report_run(date_str=today)
    except Exception as e:
        print(f"       PERFORMANCE.md 更新失敗（繼續）：{e}")


# --------------------------------------------------------------------------- #
# Step 1：擷取
# --------------------------------------------------------------------------- #

def _step_fetch(date_str: str | None = None) -> list[str]:
    print("[1/8] 擷取 Trump 貼文 & 股癌筆記…")
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

def _step_classify(texts: list[str]) -> list[dict]:
    print("[2/8] LLM 事件分類…")
    if not texts:
        print("       無文字輸入，略過 LLM。")
        return []

    try:
        from scripts.llm_client import analyze
        rules = _load_rules_summary()
        # 股癌排前面（完整），Trump 取前 8 則（macro 事件）
        gua_texts = [t for t in texts if t.startswith("[股癌")]
        trump_texts = [t for t in texts if not t.startswith("[股癌")]
        combined = "\n\n---\n".join(gua_texts + trump_texts[:8])
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
        return "觀察賣出"
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

    try:
        codes = [r["code"] for r in recs]
        prices = get_prices(codes)
        for rec in recs:
            info = prices.get(rec["code"])
            if info:
                rec["name"] = info.get("name", rec["code"])
                close = info.get("close")
                if close:
                    rec["last_close"] = close
                    rec["entry_low"] = round(close * 0.97, 1)
                    rec["entry_high"] = round(close * 1.00, 1)
                    rec["target"] = round(close * 1.05, 1)
                    rec["stop_loss"] = round(close * 0.95, 1)
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

    return {
        "close": current_close or r(close.iloc[-1]),
        "ma5": ma5, "ma20": ma20, "ma60": ma60,
        "high20": high20, "low20": low20, "rsi": rsi,
    }


def _step_enrich_ta(recs: list[dict]) -> list[dict]:
    """yfinance 拉歷史 OHLCV → 計算 TA → 一次 LLM call → 覆寫 entry/target/stop/reason。"""
    if not recs:
        return recs
    print("[4.5] 技術面分析（yfinance + LLM）…")

    try:
        import yfinance as yf
    except ImportError:
        print("       yfinance 未安裝，略過。")
        return recs

    # 每支股票抓歷史資料；順便補上櫃股名稱（TWSE 查不到的）
    ta_map: dict[str, dict] = {}
    for rec in recs:
        code = rec["code"]
        current_close = rec.get("last_close")
        yf_sym = get_yf_ticker(code)
        try:
            ticker = yf.Ticker(yf_sym)
            h = ticker.history(period="3mo", auto_adjust=True)
            if not h.empty and len(h) >= 20:
                ta_map[code] = _compute_ta(h, current_close)
                if not rec.get("name"):
                    info = ticker.info
                    yf_name = info.get("shortName") or info.get("longName") or ""
                    if yf_name:
                        rec["name"] = yf_name.replace("*", "").strip()
        except Exception:
            pass

    if not ta_map:
        print("       TA 資料全部失敗，使用 ±3/5% 預設值")
        return recs

    # 組一次性批次 LLM prompt（table 格式）
    header = "| 代碼 | 名稱 | 現價 | MA5 | MA20 | MA60 | 近20日高 | 近20日低 | RSI | 方向 |"
    sep    = "|------|------|------|-----|------|------|---------|---------|-----|------|"
    rows   = [header, sep]
    valid_codes = []
    for rec in recs:
        code = rec["code"]
        if code not in ta_map:
            continue
        ta = ta_map[code]
        rows.append(
            f"| {code} | {rec.get('name', code)} | {ta['close']} "
            f"| {ta['ma5']} | {ta['ma20']} | {ta['ma60'] or 'N/A'} "
            f"| {ta['high20']} | {ta['low20']} | {ta['rsi']} | {rec.get('action', '停看等')} |"
        )
        valid_codes.append(code)

    if not valid_codes:
        return recs

    prompt = (
        "以下是今日推薦股票的技術面資料，請根據操作方向給出進場區間、目標、停損與一句中文理由。\n\n"
        + "\n".join(rows)
        + "\n\n原則（自行判斷，不需死守）：\n"
        "- 觀察買進：進場接近 MA20 支撐，目標近20日高點，停損 MA60 下方\n"
        "- 觀察賣出：提供合理減碼價位，停損設在近期壓力上方\n"
        "- 停看等：提供關鍵支撐/壓力區間作為參考\n\n"
        "只輸出 JSON 陣列，不要其他說明文字：\n"
        '[{"code":"XXXX","entry_low":X,"entry_high":Y,"target":Z,"stop_loss":W,"reason":"一句話"}]'
    )

    try:
        from scripts.llm_client import analyze
        import re
        raw = analyze(prompt)
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            print("       TA LLM 未回傳 JSON，使用預設值")
            return recs
        items = json.loads(m.group())
        ta_result = {item["code"]: item for item in items if "code" in item}
        for rec in recs:
            item = ta_result.get(rec["code"])
            if item:
                for key in ("entry_low", "entry_high", "target", "stop_loss", "reason"):
                    if item.get(key) is not None:
                        rec[key] = item[key]
        print(f"       TA 完成，{len(ta_result)} 支股票已更新進出場價")
    except Exception as e:
        print(f"       TA LLM 失敗（使用 ±3/5% 預設值）：{e}")

    return recs


# --------------------------------------------------------------------------- #
# Step 4.7：投資組合決策
# --------------------------------------------------------------------------- #

def _step_portfolio_decision(
    recs: list[dict],
    open_positions: list[dict],
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
        "- \"raise_target\"：已有倉位，不需加倉，但今日訊號支持上調目標價與停損\n"
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
        import re
        raw = analyze(prompt)
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            raise ValueError("LLM 未回傳 JSON")
        decisions = {d["code"]: d for d in json.loads(m.group()) if "code" in d}
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
                      position_updates: list[dict] | None = None) -> dict:
    print("[5/8] 儲存日報…")
    report = {
        "date": date_compact,
        "generated_at": datetime.now().isoformat(),
        "events": events,
        "recommendations": recs,
        "position_updates": position_updates or [],
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
    new_recs  = [r for r in recs if r.get("update_type") != "add"]
    add_recs  = [r for r in recs if r.get("update_type") == "add"]
    pos_updates = report.get("position_updates", [])

    def _fmt_rec(r: dict, label_override: str | None = None) -> list[str]:
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

    buy   = [r for r in new_recs if r.get("action") == "觀察買進"]
    sell  = [r for r in new_recs if r.get("action") == "觀察賣出"]
    watch = [r for r in new_recs if r.get("action") == "停看等"]

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
        lines.append("🎯 *持倉目標上調*")
        for u in pos_updates[:5]:
            name_line = f"• {u['code']}"
            if u.get("new_target"):
                name_line += f"  新目標 {u['new_target']}"
            if u.get("new_stop"):
                name_line += f"  新停損 {u['new_stop']}"
            lines.append(name_line)
            if u.get("reason"):
                lines.append(f"  ↳ {u['reason']}")
        lines.append("")

    if not buy and not watch and not add_recs and not pos_updates:
        lines.append("今日無明確建議標的，維持觀望。")
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

def _step_track_performance(shadow: bool = False, today: str | None = None) -> None:
    print("[7/8] 績效追蹤（昨日建議）…")
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
    print("[10] 自動審閱 shadow 提案…")
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
                import re
                m = re.search(r"\[.*\]", raw, re.DOTALL)
                if m:
                    decisions = json.loads(m.group())
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
