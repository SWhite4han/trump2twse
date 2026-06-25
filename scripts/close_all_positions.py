"""一次性結清所有未結清部位，清空 open_positions.json。

用途：長時間離家前手動結清，回家後重新累積績效樣本。

行為：
  - holding：以 last_close 為出場價，計算損益
  - watching：直接標記結清，無損益
  - 全部以 close_reason="manual_close" 寫入當月 CSV
  - 備份 open_positions.json → open_positions.json.bak.{today}
  - 清空 open_positions.json

執行：
    .venv/bin/python scripts/close_all_positions.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.lib.config import config
from scripts.track_recommendation_performance import (
    ACTION_SELL,
    CAPITAL_PER_TRADE,
    _save_csv,
)

CLOSE_REASON = "manual_close"


def _manual_close(pos: dict, today: str) -> None:
    pos["close_reason"] = CLOSE_REASON
    pos["close_date"]   = today

    last_close = pos.get("last_close")
    if pos.get("state") == "holding" and last_close is not None:
        pos["actual_close"] = last_close
        pos["exit_price"]   = last_close

        entry_p = pos.get("actual_entry_price")
        if entry_p:
            try:
                ep, cp  = float(entry_p), float(last_close)
                is_sell = pos.get("action") == ACTION_SELL
                raw     = (ep - cp) / ep if is_sell else (cp - ep) / ep
                pos["pnl_pct"] = round(raw * 100, 2)
                pos["pnl_twd"] = round(raw * CAPITAL_PER_TRADE, 0)
            except (TypeError, ValueError, ZeroDivisionError):
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="結清所有未結清部位")
    parser.add_argument("--dry-run", action="store_true", help="只印摘要，不寫檔")
    args = parser.parse_args()

    today    = datetime.now().strftime("%Y%m%d")
    perf_dir = config.data_dir / "performance"
    op_file  = perf_dir / "open_positions.json"

    if not op_file.exists():
        print("open_positions.json 不存在，無需結清")
        return

    positions = json.loads(op_file.read_text(encoding="utf-8"))
    if not positions:
        print("open_positions.json 為空，無需結清")
        return

    for pos in positions:
        _manual_close(pos, today)

    holding   = [p for p in positions if p.get("state") == "holding"]
    watching  = [p for p in positions if p.get("state") == "watching"]
    pnl_pcts  = [p["pnl_pct"] for p in positions if p.get("pnl_pct") is not None]
    total_twd = sum(p.get("pnl_twd", 0) or 0 for p in positions)
    win_cnt   = sum(1 for v in pnl_pcts if v > 0)

    print(f"\n📋 結清摘要（{today}）")
    print("─" * 60)
    print(f"  共 {len(positions)} 筆")
    print(f"    holding:  {len(holding)} 筆（計入損益）")
    print(f"    watching: {len(watching)} 筆（無損益）")
    if pnl_pcts:
        avg = sum(pnl_pcts) / len(pnl_pcts)
        print(f"  勝率：{win_cnt}/{len(pnl_pcts)} = {win_cnt/len(pnl_pcts)*100:.1f}%")
        print(f"  平均報酬：{avg:+.2f}%")
        print(f"  累計損益：{total_twd:+,.0f} TWD（每筆 {CAPITAL_PER_TRADE:,} 模擬資金）")

    print("\n  逐筆明細：")
    for p in sorted(positions, key=lambda x: (x.get("pnl_pct") or 0), reverse=True):
        tag = "💼" if p.get("state") == "holding" else "👀"
        pnl_pct = p.get("pnl_pct")
        pnl_str = f"{pnl_pct:+6.2f}%" if pnl_pct is not None else "   —   "
        entry   = p.get("actual_entry_price") or "-"
        close   = p.get("last_close") or "-"
        print(f"    {tag} {p['code']:>5} {p.get('name',''):<6} "
              f"{p.get('action',''):<5} entry={entry:>7} close={close:>7} {pnl_str}  "
              f"[{p.get('rule_id','')[:30]}]")

    if args.dry_run:
        print("\n[dry-run] 未實際寫入檔案")
        return

    backup = perf_dir / f"open_positions.json.bak.{today}"
    shutil.copy2(op_file, backup)
    print(f"\n💾 備份 → {backup}")

    _save_csv(positions)

    op_file.write_text("[]\n", encoding="utf-8")
    print(f"🗑  已清空 {op_file}")
    print("\n✅ 完成。回家後 pipeline 會從空白持倉開始新一輪")


if __name__ == "__main__":
    main()
