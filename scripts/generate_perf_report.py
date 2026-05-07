"""從 data/performance/*.csv 生成 PERFORMANCE.md。

每次 pipeline 跑完後自動呼叫，結果 commit 到 git，GitHub 上即時可見。
"""
from __future__ import annotations

import csv
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from scripts.lib.config import config

PERF_STATUS_EMOJI = {
    "triggered_target": "✅ 達標",
    "triggered_stop":   "❌ 停損",
    "triggered_hold":   "⏳ 持有",
    "not_triggered":    "⬜ 未觸發",
    "no_data":          "➖ 無資料",
}


def run() -> None:
    rows = _load_all_csv()
    if not rows:
        print("[report] 尚無績效資料，略過。")
        return

    md = _build_markdown(rows)
    out = config.project_root / "PERFORMANCE.md"
    out.write_text(md, encoding="utf-8")
    print(f"[report] → {out}")

    _git_commit(str(out))


# --------------------------------------------------------------------------- #

def _load_all_csv() -> list[dict]:
    perf_dir = config.data_dir / "performance"
    rows: list[dict] = []
    for f in sorted(perf_dir.glob("*.csv")):
        with open(f, encoding="utf-8", newline="") as fp:
            rows.extend(csv.DictReader(fp))
    return rows


def _build_markdown(rows: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    triggered = [r for r in rows if r["status"] not in ("not_triggered", "no_data")]
    target_hits = sum(1 for r in triggered if r["status"] == "triggered_target")
    stop_hits   = sum(1 for r in triggered if r["status"] == "triggered_stop")

    days = sorted({r["report_date"] for r in rows})
    hit_rate  = f"{target_hits/len(triggered)*100:.0f}%" if triggered else "—"
    stop_rate = f"{stop_hits/len(triggered)*100:.0f}%"   if triggered else "—"

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
            t = sum(1 for r in recs if r["status"] == "triggered_target")
            s = sum(1 for r in recs if r["status"] == "triggered_stop")
            h = sum(1 for r in recs if r["status"] == "triggered_hold")
            rate = f"{t/len(recs)*100:.0f}%" if recs else "—"
            lines.append(f"| {rule} | {len(recs)} | {t} | {s} | {h} | {rate} |")
        lines.append("")

    # 最近 30 筆明細
    recent = sorted(rows, key=lambda r: r.get("eval_date",""), reverse=True)[:30]
    if recent:
        lines += [
            "## 最近建議明細",
            "",
            "| 日期 | 股票 | 規則 | 進場區間 | 目標 | 停損 | 收盤 | 結果 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in recent:
            entry = f"{r.get('entry_low','—')}–{r.get('entry_high','—')}"
            close = r.get("actual_close") or "—"
            status = PERF_STATUS_EMOJI.get(r.get("status",""), r.get("status_label",""))
            lines.append(
                f"| {r.get('report_date','')} "
                f"| {r.get('code','')} {r.get('name','')} "
                f"| {r.get('rule_id','')} "
                f"| {entry} "
                f"| {r.get('target','—')} "
                f"| {r.get('stop_loss','—')} "
                f"| {close} "
                f"| {status} |"
            )
        lines.append("")

    lines += [
        "---",
        "",
        "_資料來源：TWSE OpenAPI　系統：[trump2twse](https://github.com/SWhite4han/trump2twse)_",
    ]
    return "\n".join(lines) + "\n"


def _git_commit(filepath: str) -> None:
    root = str(config.project_root)
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
            ["git", "commit", "-m", f"perf: update PERFORMANCE.md {datetime.now().strftime('%Y-%m-%d')}"],
            cwd=root, check=True, capture_output=True
        )
        subprocess.run(["git", "push"], cwd=root, check=True, capture_output=True)
        print("[report] PERFORMANCE.md 已推送到 GitHub。")
    except subprocess.CalledProcessError as e:
        print(f"[report] git 操作失敗（非致命）：{e.stderr.decode() if e.stderr else e}")


if __name__ == "__main__":
    run()
