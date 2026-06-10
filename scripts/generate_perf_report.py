"""從 data/performance/*.csv 生成 PERFORMANCE.md。

每次 pipeline 跑完後自動呼叫，結果 commit 到 git，GitHub 上即時可見。
"""
from __future__ import annotations

import csv
import json
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from scripts.lib.config import config
from scripts.track_recommendation_performance import MAX_WATCH_DAYS

CAPITAL_PER_TRADE = 100_000  # 每筆假設投入 10 萬
BASELINE_DATE     = "20260514"  # TA+LLM 系統正式上線日，之前資料不計入績效基準
V2_BASELINE_DATE  = "20260609"  # v2 改版起算日（market_filter NaN 防護 + trump_twitter Google News fix 之後）

PERF_STATUS_EMOJI = {
    "triggered_target": "✅ 達標",
    "triggered_stop":   "❌ 停損",
    "triggered_hold":   "⏳ 持有",
    "not_triggered":    "⬜ 未觸發",
    "no_data":          "➖ 無資料",
    "expired":          "⏰ 逾期",
    "expired_validity": "⌛ 推薦失效",
    "superseded":       "🔄 覆蓋",
}

HORIZON_LABEL = {"event": "🎯 事件", "trend": "📈 趨勢", "cycle": "🌊 循環"}


def run(date_str: str | None = None) -> None:
    rows      = _load_all_csv()
    open_pos  = _load_open_positions()

    if not rows and not open_pos:
        print("[report] 尚無績效資料，略過。")
        return

    # 累積績效驅動的規則自動調整（先更新 YAML，再產 PERFORMANCE.md 才能反映最新狀態）
    rule_updates = _check_rule_performance_updates(rows)
    if rule_updates:
        from scripts.auto_update_rules import apply_updates
        apply_updates(rule_updates)

    md   = _build_markdown(rows, open_pos)
    html = _build_html(rows, open_pos)

    out_md   = config.project_root / "PERFORMANCE.md"
    out_html = config.project_root / "PERFORMANCE.html"
    out_md.write_text(md,   encoding="utf-8")
    out_html.write_text(html, encoding="utf-8")
    print(f"[report] → {out_md}")
    print(f"[report] → {out_html}")

    _git_commit([str(out_md), str(out_html)], date_str=date_str)


# --------------------------------------------------------------------------- #

def _load_all_csv() -> list[dict]:
    perf_dir = config.data_dir / "performance"
    rows: list[dict] = []
    for f in sorted(perf_dir.glob("*.csv")):
        with open(f, encoding="utf-8", newline="") as fp:
            rows.extend(csv.DictReader(fp))
    return rows


def _rules_horizon_map() -> dict[str, str]:
    """rule_id → horizon。失敗回傳 {}。供舊資料反查 horizon 用。"""
    try:
        from scripts.auto_update_rules import _load_rules as _lr
        return {r["event"]: r.get("horizon", "trend") for r in _lr() if "event" in r}
    except Exception:
        return {}


def _resolve_horizon(rec: dict, horizon_map: dict[str, str]) -> str:
    """回傳 rec 的 horizon；缺則查規則 YAML；都查不到回 trend。"""
    h = rec.get("horizon")
    if h:
        return h
    return horizon_map.get(rec.get("rule_id", ""), "trend")


def _load_open_positions() -> list[dict]:
    f = config.data_dir / "performance" / "open_positions.json"
    if not f.exists():
        return []
    with open(f, encoding="utf-8") as fp:
        return json.load(fp)


def _compute_stats(rows: list[dict]) -> dict:
    """計算一組 rows 的績效指標（只計算已進場且已結案的交易）。"""
    closed = [r for r in rows if r.get("close_reason") in ("triggered_target", "triggered_stop")]
    target_hits = sum(1 for r in closed if r["close_reason"] == "triggered_target")
    stop_hits   = sum(1 for r in closed if r["close_reason"] == "triggered_stop")

    gains, losses, pct_list = [], [], []
    for r in closed:
        try:
            pnl_twd = float(r.get("pnl_twd") or 0)
            pnl_pct = float(r.get("pnl_pct") or 0)
        except (ValueError, TypeError):
            # fallback：從進出場價計算
            try:
                ep = float(r["actual_entry_price"])
                cp = float(r.get("exit_price") or r.get("actual_close") or 0)
                is_sell = r.get("action") == "觀察賣出"
                raw = (ep - cp) / ep if is_sell else (cp - ep) / ep
                pnl_twd = round(raw * CAPITAL_PER_TRADE, 0)
                pnl_pct = round(raw * 100, 2)
            except (ValueError, TypeError, ZeroDivisionError):
                continue
        if pnl_twd > 0:
            gains.append(pnl_twd)
        elif pnl_twd < 0:
            losses.append(pnl_twd)
        pct_list.append(pnl_pct)

    total_pnl = sum(gains) + sum(losses)
    profit_factor = (sum(gains) / abs(sum(losses))) if losses else None
    avg_pct = (sum(pct_list) / len(pct_list)) if pct_list else None

    return {
        "closed": len(closed),
        "target_hits": target_hits,
        "stop_hits": stop_hits,
        "total_pnl": total_pnl,
        "profit_factor": profit_factor,
        "avg_pct": avg_pct,
        "pnl_count": len(pct_list),
    }


def _fmt_stats_rows(s: dict) -> list[str]:
    n = s["closed"]
    hit_rate  = f"{s['target_hits']/n*100:.0f}%" if n else "—"
    stop_rate = f"{s['stop_hits']/n*100:.0f}%"  if n else "—"
    pf_str    = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "—"
    avg_str   = f"{s['avg_pct']:+.1f}%" if s["avg_pct"] is not None else "—"
    pnl_str   = f"{s['total_pnl']:+,.0f} 元（{s['pnl_count']} 筆）" if s["pnl_count"] else "—"
    return [
        f"| 完整交易筆數（有進出場）| {n} |",
        f"| 達標率 | {hit_rate} |",
        f"| 停損率 | {stop_rate} |",
        f"| Profit Factor | {pf_str} |",
        f"| 平均每筆損益 | {avg_str} |",
        f"| 模擬損益合計 | {pnl_str} |",
    ]


def _build_markdown(rows: list[dict], open_pos: list[dict]) -> str:
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    days = sorted({r["report_date"] for r in rows})

    stats_all      = _compute_stats(rows)
    baseline_rows  = [r for r in rows if r.get("report_date", "") >= BASELINE_DATE]
    stats_baseline = _compute_stats(baseline_rows)
    v2_rows        = [r for r in rows if r.get("report_date", "") >= V2_BASELINE_DATE]
    stats_v2       = _compute_stats(v2_rows)

    lines: list[str] = [
        "# Performance Report",
        "",
        f"> 自動生成　最後更新：{now}",
        "",
        "## 總覽",
        "",
        f"| 項目 | 數值 |",
        f"|---|---|",
        f"| 觀察天數 | {len(days)} 天（{days[0] if days else '—'} ～ {days[-1] if days else '—'}）|",
        f"| 總建議筆數 | {len(rows)} |",
        "",
        f"### 全期績效",
        "",
        "| 項目 | 數值 |",
        "|---|---|",
        *_fmt_stats_rows(stats_all),
        "",
        f"### 基準期績效（≥{BASELINE_DATE}，TA+LLM 定價後）",
        "",
        "| 項目 | 數值 |",
        "|---|---|",
        *_fmt_stats_rows(stats_baseline),
        "",
        f"### v2 期績效（≥{V2_BASELINE_DATE}，最新一輪改版後）",
        "",
        "| 項目 | 數值 |",
        "|---|---|",
        *_fmt_stats_rows(stats_v2),
        "",
    ]

    # 各視野績效（依 horizon 分組，缺則查 rule YAML）
    horizon_map = _rules_horizon_map()
    closed_rows = [r for r in rows if r.get("close_reason") in ("triggered_target", "triggered_stop")]
    by_horizon: dict[str, list[dict]] = defaultdict(list)
    for r in closed_rows:
        by_horizon[_resolve_horizon(r, horizon_map)].append(r)

    if by_horizon:
        lines += [
            "## 各視野績效",
            "",
            "| 視野 | 筆數 | 達標率 | 停損率 | PF | 平均損益 |",
            "|---|---|---|---|---|---|",
        ]
        for h_key in ("event", "trend", "cycle"):
            recs = by_horizon.get(h_key, [])
            if not recs:
                continue
            s = _compute_stats(recs)
            n = s["closed"]
            hit  = f"{s['target_hits']/n*100:.0f}%" if n else "—"
            stop = f"{s['stop_hits']/n*100:.0f}%"   if n else "—"
            pf   = f"{s['profit_factor']:.2f}"     if s["profit_factor"] is not None else "—"
            avg  = f"{s['avg_pct']:+.1f}%"         if s["avg_pct"]       is not None else "—"
            lines.append(f"| {HORIZON_LABEL.get(h_key, h_key)} | {n} | {hit} | {stop} | {pf} | {avg} |")
        lines.append("")

    # 各規則勝率（只統計有進出場的交易）
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for r in closed_rows:
        by_rule[r.get("rule_id", "未知")].append(r)

    if by_rule:
        from scripts.auto_update_rules import _load_rules as _lr
        try:
            _rules_map = {r["event"]: r for r in _lr()}
        except Exception:
            _rules_map = {}

        lines += [
            "## 各規則觸發績效",
            "",
            "| 規則 | 觸發 | ✅ 達標 | ❌ 停損 | 達標率 | 狀態 |",
            "|---|---|---|---|---|---|",
        ]
        for rule, recs in sorted(by_rule.items(), key=lambda x: -len(x[1])):
            t = sum(1 for r in recs if r["close_reason"] == "triggered_target")
            s = sum(1 for r in recs if r["close_reason"] == "triggered_stop")
            rate = f"{t/len(recs)*100:.0f}%" if recs else "—"
            rule_cfg = _rules_map.get(rule)
            if rule_cfg is None:
                status = "⚫ 已移除"
            elif rule_cfg.get("disabled"):
                status = "🔴 停用"
            else:
                min_conf = rule_cfg.get("min_confidence", 0.4)
                status   = f"🟢 門檻 {min_conf:.2f}"
            lines.append(f"| {rule} | {len(recs)} | {t} | {s} | {rate} | {status} |")
        lines.append("")

    # 最近 30 筆明細
    recent = sorted(rows, key=lambda r: r.get("close_date",""), reverse=True)[:30]
    if recent:
        lines += [
            "## 最近建議明細",
            "",
            "| 日期 | 股票 | 規則 | 進場價 | 目標 | 停損 | 出場價 | 當日收盤 | 損益(元) | 結果 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in recent:
            entry_price = r.get("actual_entry_price") or "—"
            exit_p   = r.get("exit_price") or "—"
            actual_c = r.get("actual_close") or "—"
            status = PERF_STATUS_EMOJI.get(r.get("close_reason",""), r.get("status_label",""))
            try:
                ep  = float(entry_price)
                cp  = float(exit_p)
                is_sell = r.get("action") == "觀察賣出"
                raw = (ep - cp) / ep if is_sell else (cp - ep) / ep
                ret      = f"({raw*100:+.1f}%)"
                pnl_cell = f"{raw*CAPITAL_PER_TRADE:+,.0f}"
            except (ValueError, TypeError):
                ret      = ""
                pnl_cell = "—"
            lines.append(
                f"| {r.get('report_date','')} "
                f"| {r.get('code','')} {r.get('name','')} "
                f"| {r.get('rule_id','')} "
                f"| {entry_price} "
                f"| {r.get('target','—')} "
                f"| {r.get('stop_loss','—')} "
                f"| {exit_p} {ret}"
                f"| {actual_c} "
                f"| {pnl_cell} "
                f"| {status} |"
            )
        lines.append("")

    # 持倉中（open positions）
    holding  = [p for p in open_pos if p.get("state") == "holding"]
    watching = [p for p in open_pos if p.get("state") == "watching"]

    if holding or watching:
        lines += ["## 目前持倉", ""]
        today_dt = datetime.now()
        if holding:
            lines += [
                f"### 已進場（{len(holding)} 筆）",
                "",
                "| 股票 | 規則 | 視野 | 進場日 | 進場價 | 目標 | 停損 | 現價 | 浮動損益 | 已持/上限 |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            for p in holding:
                lc = p.get("last_close")
                ep = p.get("actual_entry_price")
                lc_str = f"{lc:.1f}" if lc else "—"
                ep_str = f"{ep:.1f}" if ep else "—"
                try:
                    is_sell = p.get("action") == "觀察賣出"
                    raw = (ep - lc) / ep if is_sell else (lc - ep) / ep
                    pnl = f"{raw * 100:+.1f}%"
                except (TypeError, ZeroDivisionError):
                    pnl = "—"
                h = _resolve_horizon(p, horizon_map)
                max_hold = p.get("max_holding_days") or {"event": 30, "trend": 90, "cycle": 180}.get(h, 90)
                days_held = "—"
                if p.get("entry_date"):
                    try:
                        days_held = (today_dt - datetime.strptime(p["entry_date"], "%Y%m%d")).days
                    except ValueError:
                        pass
                lines.append(
                    f"| {p['code']} {p.get('name','')} "
                    f"| {p.get('rule_id','')} "
                    f"| {HORIZON_LABEL.get(h, h)} "
                    f"| {p.get('entry_date','—')} "
                    f"| {ep_str} "
                    f"| {p.get('target','—')} "
                    f"| {p.get('stop_loss','—')} "
                    f"| {lc_str} "
                    f"| {pnl} "
                    f"| {days_held}/{max_hold} |"
                )
            lines.append("")
        if watching:
            lines += [
                f"### 觀察中－等待進場（{len(watching)} 筆）",
                "",
                "| 股票 | 規則 | 視野 | 推薦日 | 進場區間 | 目標 | 停損 | 觀察天數 |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for p in watching:
                entry = f"{p.get('entry_low','—')}–{p.get('entry_high','—')}"
                h = _resolve_horizon(p, horizon_map)
                v_days = p.get("validity_days") or MAX_WATCH_DAYS
                try:
                    v_days = int(v_days)
                except (TypeError, ValueError):
                    v_days = MAX_WATCH_DAYS
                limit = min(MAX_WATCH_DAYS, v_days)
                lines.append(
                    f"| {p['code']} {p.get('name','')} "
                    f"| {p.get('rule_id','')} "
                    f"| {HORIZON_LABEL.get(h, h)} "
                    f"| {p.get('report_date','—')} "
                    f"| {entry} "
                    f"| {p.get('target','—')} "
                    f"| {p.get('stop_loss','—')} "
                    f"| {p.get('days_watched',0)}/{limit} |"
                )
            lines.append("")

    lines += [
        "---",
        "",
        "_資料來源：TWSE OpenAPI　系統：[trump2twse](https://github.com/SWhite4han/trump2twse)_",
    ]
    return "\n".join(lines) + "\n"


def _check_rule_performance_updates(rows: list[dict]) -> list[dict]:
    """依累積績效提出規則自動調整（DISABLE / CONFIDENCE_ADJUST）。

    門檻：
      DISABLE          win_rate < 25% 且 closed_trades ≥ 5
      CONFIDENCE 放寬  win_rate > 70% 且 closed_trades ≥ 5 → min_confidence = 0.30
      CONFIDENCE 收緊  win_rate < 40% 且 closed_trades ≥ 5 → min_confidence = 0.50
    """
    DISABLE_WIN_RATE  = 0.25
    BOOST_WIN_RATE    = 0.70
    TIGHTEN_WIN_RATE  = 0.40
    MIN_TRADES        = 5

    from scripts.auto_update_rules import _load_rules, _find_rule
    rules_list = _load_rules()
    rules_map  = {r["event"]: r for r in rules_list}

    closed = [r for r in rows if r.get("close_reason") in ("triggered_target", "triggered_stop")]
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for r in closed:
        by_rule[r.get("rule_id", "")].append(r)

    today   = datetime.now().strftime("%Y-%m-%d")
    updates = []

    for rule_id, recs in by_rule.items():
        if len(recs) < MIN_TRADES:
            continue
        targets  = sum(1 for r in recs if r["close_reason"] == "triggered_target")
        win_rate = targets / len(recs)
        rule     = rules_map.get(rule_id)
        if not rule:
            continue

        is_disabled   = rule.get("disabled", False)
        current_conf  = rule.get("min_confidence", 0.4)

        # 停用：達標率太低
        if win_rate < DISABLE_WIN_RATE and not is_disabled:
            updates.append({
                "operation":       "DISABLE",
                "event":           rule_id,
                "reason":          f"累積達標率 {win_rate:.0%}（{targets}/{len(recs)} 筆），低於 {DISABLE_WIN_RATE:.0%} 閾值",
                "evidence_source": "quant",
            })
            print(f"[report] 建議 DISABLE：「{rule_id}」達標率 {win_rate:.0%}")
            continue  # 已停用就不再調整門檻

        # Confidence 門檻調整（僅針對未停用規則）
        if not is_disabled:
            if win_rate > BOOST_WIN_RATE:
                new_conf = 0.30
            elif win_rate < TIGHTEN_WIN_RATE:
                new_conf = 0.50
            else:
                new_conf = 0.40

            if abs(current_conf - new_conf) > 0.01:
                updates.append({
                    "operation":       "CONFIDENCE_ADJUST",
                    "event":           rule_id,
                    "min_confidence":  new_conf,
                    "reason":          f"累積達標率 {win_rate:.0%}（{len(recs)} 筆），調整信心門檻 {current_conf:.2f} → {new_conf:.2f}",
                    "evidence_source": "quant",
                })
                print(f"[report] 建議 CONFIDENCE_ADJUST：「{rule_id}」{current_conf:.2f} → {new_conf:.2f}")

    return updates


def _build_html(rows: list[dict], open_pos: list[dict]) -> str:
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    days = sorted({r["report_date"] for r in rows})

    stats_all      = _compute_stats(rows)
    baseline_rows  = [r for r in rows if r.get("report_date", "") >= BASELINE_DATE]
    stats_baseline = _compute_stats(baseline_rows)
    v2_rows        = [r for r in rows if r.get("report_date", "") >= V2_BASELINE_DATE]
    stats_v2       = _compute_stats(v2_rows)

    from scripts.auto_update_rules import _load_rules as _lr
    try:
        rules_map = {r["event"]: r for r in _lr()}
    except Exception:
        rules_map = {}

    horizon_map = _rules_horizon_map()

    def _h(s: dict, label: str) -> str:
        n = s["closed"]
        hit  = f"{s['target_hits']/n*100:.0f}%" if n else "—"
        stop = f"{s['stop_hits']/n*100:.0f}%"   if n else "—"
        pf   = f"{s['profit_factor']:.2f}"       if s["profit_factor"] is not None else "—"
        avg  = f"{s['avg_pct']:+.1f}%"           if s["avg_pct"]        is not None else "—"
        pnl  = f"{s['total_pnl']:+,.0f} 元"      if s["pnl_count"] else "—"
        pf_color = "#28a745" if (s["profit_factor"] or 0) >= 1 else "#dc3545"
        return f"""
        <div class="stat-card">
          <h3>{label}</h3>
          <table class="kv">
            <tr><td>完整交易筆數</td><td><b>{n}</b></td></tr>
            <tr><td>達標率</td><td style="color:#28a745"><b>{hit}</b></td></tr>
            <tr><td>停損率</td><td style="color:#dc3545"><b>{stop}</b></td></tr>
            <tr><td>Profit Factor</td><td style="color:{pf_color}"><b>{pf}</b></td></tr>
            <tr><td>平均每筆損益</td><td><b>{avg}</b></td></tr>
            <tr><td>模擬損益合計</td><td><b>{pnl}</b></td></tr>
          </table>
        </div>"""

    # 各視野績效（依 horizon 分組，缺則查 rule YAML）
    closed_rows = [r for r in rows if r.get("close_reason") in ("triggered_target", "triggered_stop")]
    by_horizon: dict[str, list[dict]] = defaultdict(list)
    for r in closed_rows:
        h = _resolve_horizon(r, horizon_map)
        by_horizon[h].append(r)

    horizon_rows_html = ""
    for h_key in ("event", "trend", "cycle"):
        recs = by_horizon.get(h_key, [])
        if not recs:
            continue
        s = _compute_stats(recs)
        n = s["closed"]
        hit  = f"{s['target_hits']/n*100:.0f}%" if n else "—"
        stop = f"{s['stop_hits']/n*100:.0f}%"   if n else "—"
        pf   = f"{s['profit_factor']:.2f}"     if s["profit_factor"] is not None else "—"
        avg  = f"{s['avg_pct']:+.1f}%"         if s["avg_pct"]       is not None else "—"
        pnl  = f"{s['total_pnl']:+,.0f}"        if s["pnl_count"] else "—"
        pf_color  = "#28a745" if (s["profit_factor"] or 0) >= 1 else "#dc3545"
        hit_color = "#28a745" if (s['target_hits']/max(n,1)) >= 0.5 else "#dc3545"
        horizon_rows_html += (
            f"<tr><td>{HORIZON_LABEL.get(h_key, h_key)}</td>"
            f"<td style='text-align:center'>{n}</td>"
            f"<td style='text-align:center;color:{hit_color}'><b>{hit}</b></td>"
            f"<td style='text-align:center;color:#dc3545'>{stop}</td>"
            f"<td style='text-align:center;color:{pf_color}'><b>{pf}</b></td>"
            f"<td style='text-align:center'>{avg}</td>"
            f"<td style='text-align:right'>{pnl}</td></tr>"
        )

    # 規則績效表格
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for r in closed_rows:
        by_rule[r.get("rule_id", "未知")].append(r)

    rule_rows_html = ""
    for rule, recs in sorted(by_rule.items(), key=lambda x: -len(x[1])):
        t    = sum(1 for r in recs if r["close_reason"] == "triggered_target")
        s    = sum(1 for r in recs if r["close_reason"] == "triggered_stop")
        rate = t / len(recs) if recs else 0
        rate_str = f"{rate*100:.0f}%"
        rate_color = "#28a745" if rate >= 0.5 else "#dc3545"

        cfg = rules_map.get(rule)
        if cfg is None:
            status_html = '<span style="color:#6c757d">⚫ 已移除</span>'
        elif cfg.get("disabled"):
            status_html = '<span style="color:#dc3545">🔴 停用</span>'
        else:
            mc = cfg.get("min_confidence", 0.4)
            status_html = f'<span style="color:#28a745">🟢 門檻 {mc:.2f}</span>'

        rule_rows_html += f"""
        <tr>
          <td>{rule}</td>
          <td style="text-align:center">{len(recs)}</td>
          <td style="text-align:center;color:#28a745">{t}</td>
          <td style="text-align:center;color:#dc3545">{s}</td>
          <td style="text-align:center;color:{rate_color}"><b>{rate_str}</b></td>
          <td>{status_html}</td>
        </tr>"""

    # 最近 30 筆明細
    recent = sorted(rows, key=lambda r: r.get("close_date", ""), reverse=True)[:30]
    trade_rows_html = ""
    for r in recent:
        cr = r.get("close_reason", "")
        if cr == "triggered_target":
            row_bg = "background:#d4edda"
            badge  = '<span style="color:#28a745">✅ 達標</span>'
        elif cr == "triggered_stop":
            row_bg = "background:#f8d7da"
            badge  = '<span style="color:#dc3545">❌ 停損</span>'
        elif cr in ("still_open", "triggered_hold"):
            row_bg = "background:#fff3cd"
            badge  = "⏳ 持有中"
        else:
            row_bg = "background:#f8f9fa"
            badge  = cr or "—"

        ep_raw = r.get("actual_entry_price") or ""
        xp_raw = r.get("exit_price") or ""
        try:
            ep = float(ep_raw); xp = float(xp_raw)
            is_sell = r.get("action") == "觀察賣出"
            raw = (ep - xp) / ep if is_sell else (xp - ep) / ep
            pnl_str = f"{raw*CAPITAL_PER_TRADE:+,.0f}"
            pct_str = f"({raw*100:+.1f}%)"
            pnl_color = "#28a745" if raw > 0 else "#dc3545"
        except (ValueError, TypeError):
            pnl_str = "—"; pct_str = ""; pnl_color = "#333"

        trade_rows_html += f"""
        <tr style="{row_bg}">
          <td>{r.get('report_date','')}</td>
          <td>{r.get('code','')} {r.get('name','')}</td>
          <td style="font-size:0.85em;max-width:180px">{r.get('rule_id','')}</td>
          <td style="text-align:right">{ep_raw or '—'}</td>
          <td style="text-align:right">{r.get('target','—')}</td>
          <td style="text-align:right">{r.get('stop_loss','—')}</td>
          <td style="text-align:right">{xp_raw or '—'} <small>{pct_str}</small></td>
          <td style="text-align:right;color:{pnl_color}"><b>{pnl_str}</b></td>
          <td>{badge}</td>
        </tr>"""

    # 持倉表格
    holding  = [p for p in open_pos if p.get("state") == "holding"]
    watching = [p for p in open_pos if p.get("state") == "watching"]

    today_dt = datetime.now()

    def _pos_row(p: dict) -> str:
        lc = p.get("last_close"); ep = p.get("actual_entry_price")
        try:
            is_sell = p.get("action") == "觀察賣出"
            raw = (ep - lc) / ep if is_sell else (lc - ep) / ep
            pnl = f"{raw*100:+.1f}%"
            pnl_color = "#28a745" if raw > 0 else "#dc3545"
        except (TypeError, ZeroDivisionError):
            pnl = "—"; pnl_color = "#333"
        h = _resolve_horizon(p, horizon_map)
        h_label = HORIZON_LABEL.get(h, h)
        max_hold = p.get("max_holding_days") or {"event": 30, "trend": 90, "cycle": 180}.get(h, 90)
        days_held = "—"
        entry_date = p.get("entry_date")
        if entry_date:
            try:
                ed = datetime.strptime(entry_date, "%Y%m%d")
                days_held = (today_dt - ed).days
            except ValueError:
                pass
        return (f"<tr><td>{p['code']} {p.get('name','')}</td>"
                f"<td style='font-size:0.85em'>{p.get('rule_id','')}</td>"
                f"<td style='text-align:center'>{h_label}</td>"
                f"<td>{entry_date or '—'}</td>"
                f"<td style='text-align:right'>{ep or '—'}</td>"
                f"<td style='text-align:right'>{p.get('target','—')}</td>"
                f"<td style='text-align:right'>{p.get('stop_loss','—')}</td>"
                f"<td style='text-align:right'>{lc or '—'}</td>"
                f"<td style='text-align:right;color:{pnl_color}'><b>{pnl}</b></td>"
                f"<td style='text-align:center'>{days_held}/{max_hold}</td></tr>")

    holding_rows  = "".join(_pos_row(p) for p in holding)

    def _watch_row(p: dict) -> str:
        h = _resolve_horizon(p, horizon_map)
        h_label = HORIZON_LABEL.get(h, h)
        v_days = p.get("validity_days") or MAX_WATCH_DAYS
        try:
            v_days = int(v_days)
        except (TypeError, ValueError):
            v_days = MAX_WATCH_DAYS
        limit = min(MAX_WATCH_DAYS, v_days)
        days_watched = p.get("days_watched", 0)
        # 即將失效用警示色
        watch_color = "#dc3545" if days_watched >= limit - 1 else "#333"
        return (f"<tr><td>{p['code']} {p.get('name','')}</td>"
                f"<td style='font-size:0.85em'>{p.get('rule_id','')}</td>"
                f"<td style='text-align:center'>{h_label}</td>"
                f"<td>{p.get('report_date','—')}</td>"
                f"<td>{p.get('entry_low','—')}–{p.get('entry_high','—')}</td>"
                f"<td style='text-align:right'>{p.get('target','—')}</td>"
                f"<td style='text-align:right'>{p.get('stop_loss','—')}</td>"
                f"<td style='text-align:center;color:{watch_color}'>{days_watched}/{limit}</td></tr>")

    watching_rows = "".join(_watch_row(p) for p in watching)

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Market Track Performance</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:16px;color:#333;background:#f5f5f5}}
  h1{{margin:0 0 4px}}
  .meta{{color:#888;font-size:0.85em;margin-bottom:20px}}
  .cards{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}}
  .stat-card{{background:#fff;border-radius:8px;padding:16px 20px;box-shadow:0 1px 3px rgba(0,0,0,.1);min-width:260px}}
  .stat-card h3{{margin:0 0 10px;font-size:1em;color:#555}}
  table{{border-collapse:collapse;width:100%}}
  th{{background:#f0f0f0;padding:8px 10px;text-align:left;font-size:0.85em;white-space:nowrap}}
  td{{padding:7px 10px;border-bottom:1px solid #eee;font-size:0.875em}}
  .kv td:first-child{{color:#666;font-size:0.85em}}
  .kv td:last-child{{text-align:right}}
  .section{{background:#fff;border-radius:8px;padding:16px 20px;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-bottom:20px}}
  .section h2{{margin:0 0 12px;font-size:1.05em}}
  details{{margin-bottom:20px}}
  summary{{cursor:pointer;background:#fff;border-radius:8px;padding:12px 20px;box-shadow:0 1px 3px rgba(0,0,0,.1);font-weight:600;font-size:0.95em}}
  details[open] summary{{border-radius:8px 8px 0 0;margin-bottom:0}}
  details .inner{{background:#fff;border-radius:0 0 8px 8px;padding:0 20px 16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
</style>
</head>
<body>
<h1>📊 Market Track Performance</h1>
<p class="meta">自動生成　最後更新：{now}　觀察 {len(days)} 天（{days[0] if days else '—'} ～ {days[-1] if days else '—'}）　總建議 {len(rows)} 筆</p>

<div class="cards">
  {_h(stats_all, "全期績效")}
  {_h(stats_baseline, f"基準期（≥{BASELINE_DATE}）")}
  {_h(stats_v2, f"v2 期（≥{V2_BASELINE_DATE}）")}
</div>

{f'''<div class="section">
  <h2>各視野績效</h2>
  <table>
    <thead><tr><th>視野</th><th>筆數</th><th>達標率</th><th>停損率</th><th>PF</th><th>平均損益</th><th>合計(元)</th></tr></thead>
    <tbody>{horizon_rows_html}</tbody>
  </table>
</div>''' if horizon_rows_html else ""}

<div class="section">
  <h2>各規則觸發績效</h2>
  <table>
    <thead><tr><th>規則</th><th>觸發</th><th>✅ 達標</th><th>❌ 停損</th><th>達標率</th><th>狀態</th></tr></thead>
    <tbody>{rule_rows_html}</tbody>
  </table>
</div>

<div class="section">
  <h2>最近 30 筆明細</h2>
  <table>
    <thead><tr><th>日期</th><th>股票</th><th>規則</th><th>進場價</th><th>目標</th><th>停損</th><th>出場價</th><th>損益(元)</th><th>結果</th></tr></thead>
    <tbody>{trade_rows_html}</tbody>
  </table>
</div>

<details{"open" if holding else ""}>
  <summary>已進場持倉 ({len(holding)} 筆)</summary>
  <div class="inner">
  <table>
    <thead><tr><th>股票</th><th>規則</th><th>視野</th><th>進場日</th><th>進場價</th><th>目標</th><th>停損</th><th>現價</th><th>浮動損益</th><th>已持/上限</th></tr></thead>
    <tbody>{holding_rows or "<tr><td colspan='10' style='text-align:center;color:#aaa'>無持倉</td></tr>"}</tbody>
  </table>
  </div>
</details>

<details>
  <summary>觀察中等待進場 ({len(watching)} 筆)</summary>
  <div class="inner">
  <table>
    <thead><tr><th>股票</th><th>規則</th><th>視野</th><th>推薦日</th><th>進場區間</th><th>目標</th><th>停損</th><th>觀察天數</th></tr></thead>
    <tbody>{watching_rows or "<tr><td colspan='8' style='text-align:center;color:#aaa'>無觀察部位</td></tr>"}</tbody>
  </table>
  </div>
</details>

<p style="color:#aaa;font-size:0.8em;margin-top:8px">資料來源：TWSE OpenAPI　系統：trump2twse</p>
</body>
</html>
"""


def _git_commit(filepaths: "str | list[str]", date_str: str | None = None) -> None:
    if isinstance(filepaths, str):
        filepaths = [filepaths]
    root  = str(config.project_root)
    label = date_str or datetime.now().strftime("%Y-%m-%d")
    try:
        for fp in filepaths:
            subprocess.run(["git", "add", fp], cwd=root, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=root, capture_output=True
        )
        if result.returncode == 0:
            print("[report] 報表無變動，略過 commit。")
            return
        subprocess.run(
            ["git", "commit", "-m", f"perf: update PERFORMANCE {label}"],
            cwd=root, check=True, capture_output=True
        )
        subprocess.run(["git", "push"], cwd=root, check=True, capture_output=True)
        print("[report] 已推送到 GitHub。")
    except subprocess.CalledProcessError as e:
        print(f"[report] git 操作失敗（非致命）：{e.stderr.decode() if e.stderr else e}")


if __name__ == "__main__":
    run()
