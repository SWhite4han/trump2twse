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

PERF_STATUS_EMOJI = {
    "triggered_target": "✅ 達標",
    "triggered_stop":   "❌ 停損",
    "triggered_hold":   "⏳ 持有",
    "not_triggered":    "⬜ 未觸發",
    "no_data":          "➖ 無資料",
}


def run(date_str: str | None = None) -> None:
    rows      = _load_all_csv()
    open_pos  = _load_open_positions()

    if not rows and not open_pos:
        print("[report] 尚無績效資料，略過。")
        return

    md = _build_markdown(rows, open_pos)
    out = config.project_root / "PERFORMANCE.md"
    out.write_text(md, encoding="utf-8")
    print(f"[report] → {out}")

    _git_commit(str(out), date_str=date_str)


# --------------------------------------------------------------------------- #

def _load_all_csv() -> list[dict]:
    perf_dir = config.data_dir / "performance"
    rows: list[dict] = []
    for f in sorted(perf_dir.glob("*.csv")):
        with open(f, encoding="utf-8", newline="") as fp:
            rows.extend(csv.DictReader(fp))
    return rows


def _load_open_positions() -> list[dict]:
    f = config.data_dir / "performance" / "open_positions.json"
    if not f.exists():
        return []
    with open(f, encoding="utf-8") as fp:
        return json.load(fp)


def _build_markdown(rows: list[dict], open_pos: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    triggered = [r for r in rows if r["close_reason"] not in ("not_triggered", "no_data")]
    target_hits = sum(1 for r in triggered if r["close_reason"] == "triggered_target")
    stop_hits   = sum(1 for r in triggered if r["close_reason"] == "triggered_stop")

    days = sorted({r["report_date"] for r in rows})
    hit_rate  = f"{target_hits/len(triggered)*100:.0f}%" if triggered else "—"
    stop_rate = f"{stop_hits/len(triggered)*100:.0f}%"   if triggered else "—"

    # TWD P&L（只算有進場且已結案的）
    total_pnl = 0.0
    pnl_count = 0
    for r in triggered:
        try:
            ep = float(r["actual_entry_price"])
            cp = float(r.get("exit_price") or r["actual_close"])
            is_sell = r.get("action") == "觀察賣出"
            raw = (ep - cp) / ep if is_sell else (cp - ep) / ep
            total_pnl += raw * CAPITAL_PER_TRADE
            pnl_count += 1
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    pnl_str = f"{total_pnl:+,.0f} 元（{pnl_count} 筆，每筆 {CAPITAL_PER_TRADE//10000} 萬）" if pnl_count else "—"

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
        f"| 觸發進場 | {len(triggered)} |",
        f"| 達標率 | {hit_rate} |",
        f"| 停損率 | {stop_rate} |",
        f"| 模擬損益 | {pnl_str} |",
        "",
    ]

    # 各規則勝率
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for r in triggered:
        by_rule[r.get("rule_id", "未知")].append(r)

    if by_rule:
        lines += [
            "## 各規則觸發績效",
            "",
            "| 規則 | 觸發 | ✅ 達標 | ❌ 停損 | ⏳ 持有 | 達標率 |",
            "|---|---|---|---|---|---|",
        ]
        for rule, recs in sorted(by_rule.items(), key=lambda x: -len(x[1])):
            t = sum(1 for r in recs if r["close_reason"] == "triggered_target")
            s = sum(1 for r in recs if r["close_reason"] == "triggered_stop")
            h = sum(1 for r in recs if r["close_reason"] == "triggered_hold")
            rate = f"{t/len(recs)*100:.0f}%" if recs else "—"
            lines.append(f"| {rule} | {len(recs)} | {t} | {s} | {h} | {rate} |")
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
        if holding:
            lines += [
                f"### 已進場（{len(holding)} 筆）",
                "",
                "| 股票 | 規則 | 進場日 | 進場價 | 目標 | 停損 | 現價 | 浮動損益 |",
                "|---|---|---|---|---|---|---|---|",
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
                lines.append(
                    f"| {p['code']} {p.get('name','')} "
                    f"| {p.get('rule_id','')} "
                    f"| {p.get('entry_date','—')} "
                    f"| {ep_str} "
                    f"| {p.get('target','—')} "
                    f"| {p.get('stop_loss','—')} "
                    f"| {lc_str} "
                    f"| {pnl} |"
                )
            lines.append("")
        if watching:
            lines += [
                f"### 觀察中－等待進場（{len(watching)} 筆）",
                "",
                "| 股票 | 規則 | 推薦日 | 進場區間 | 目標 | 停損 | 觀察天數 |",
                "|---|---|---|---|---|---|---|",
            ]
            for p in watching:
                entry = f"{p.get('entry_low','—')}–{p.get('entry_high','—')}"
                lines.append(
                    f"| {p['code']} {p.get('name','')} "
                    f"| {p.get('rule_id','')} "
                    f"| {p.get('report_date','—')} "
                    f"| {entry} "
                    f"| {p.get('target','—')} "
                    f"| {p.get('stop_loss','—')} "
                    f"| {p.get('days_watched',0)}/{MAX_WATCH_DAYS} |"
                )
            lines.append("")

    lines += [
        "---",
        "",
        "_資料來源：TWSE OpenAPI　系統：[trump2twse](https://github.com/SWhite4han/trump2twse)_",
    ]
    return "\n".join(lines) + "\n"


def _git_commit(filepath: str, date_str: str | None = None) -> None:
    root = str(config.project_root)
    label = date_str or datetime.now().strftime("%Y-%m-%d")
    try:
        subprocess.run(["git", "add", filepath], cwd=root, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=root, capture_output=True
        )
        if result.returncode == 0:
            print("[report] PERFORMANCE.md 無變動，略過 commit。")
            return
        subprocess.run(
            ["git", "commit", "-m", f"perf: update PERFORMANCE.md {label}"],
            cwd=root, check=True, capture_output=True
        )
        subprocess.run(["git", "push"], cwd=root, check=True, capture_output=True)
        print("[report] PERFORMANCE.md 已推送到 GitHub。")
    except subprocess.CalledProcessError as e:
        print(f"[report] git 操作失敗（非致命）：{e.stderr.decode() if e.stderr else e}")


if __name__ == "__main__":
    run()
