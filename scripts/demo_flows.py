"""兩個核心流程的端對端示範。

用法：
    .venv/bin/python scripts/demo_flows.py

Flow 1 — 新增事件（DISCOVER）：
    模擬一則新聞 → LLM 找出新規則 → 存入 shadow_updates → 告知如何批准

Flow 2 — 推薦標的：
    模擬 LLM 偵測到一個已知事件 → 規則匹配 → 查今日股價 → 印出 Telegram 訊息草稿
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.lib.config import config
from scripts.auto_update_rules import apply_updates

DIVIDER = "=" * 60


# --------------------------------------------------------------------------- #
# Flow 1：新增事件（DISCOVER）
# --------------------------------------------------------------------------- #

def demo_discover() -> None:
    print(f"\n{DIVIDER}")
    print("  FLOW 1：新增事件（DISCOVER）")
    print(DIVIDER)

    # 模擬一則新聞（這個事件目前不在規則庫裡）
    sample_news = """
    【Reuters】Taiwan's TSMC and MediaTek are expected to benefit significantly
    from AI server demand surge. Nvidia's Blackwell GPU shipments require
    CoWoS advanced packaging — only TSMC has capacity. Morgan Stanley raises
    TSMC target price to NT$1,300. AI inference chips demand for data centers
    set to triple by 2026. 台積電 CoWoS 先進封裝產能已完全被 AI 伺服器客戶預訂到 2026 年底。
    """

    print("\n📰 [模擬新聞]")
    print(sample_news.strip())
    print()

    # 檢查這個事件是否已在規則庫
    from ruamel.yaml import YAML
    _yaml = YAML()
    with open(config.rules_file, encoding="utf-8") as f:
        rules = _yaml.load(f) or []
    existing = [r["event"] for r in rules]

    new_event_name = "AI 伺服器需求爆發帶動台積電先進封裝"
    already_exists = any(new_event_name in e for e in existing)

    print("📋 [現有規則庫事件]")
    for e in existing:
        print(f"   • {e}")
    print(f"\n❓ '{new_event_name}' 在規則庫裡嗎？{'是（跳過）' if already_exists else '否 → 值得 DISCOVER'}")

    if already_exists:
        print("   （規則已存在，DISCOVER 不會重複提案）")
        return

    # 構建 DISCOVER 提案（實務上由 LLM 產生，這裡手動示範結構）
    discover_update = {
        "operation": "DISCOVER",
        "event": new_event_name,
        "reason": "AI 伺服器需求帶動 CoWoS 先進封裝，台積電獨家產能造成轉單效益，Morgan Stanley 上調目標價",
        "evidence_source": "qualitative",
        "confidence": 0.88,
        "new_event": {
            "event": new_event_name,
            "keywords": ["CoWoS", "AI server", "advanced packaging", "Blackwell", "先進封裝", "AI伺服器", "台積電"],
            "impact": {
                "direction": "bullish",
                "description": "AI GPU 需求帶動 CoWoS 先進封裝，台積電獨家供應，聯發科 AI 晶片設計受益",
                "sectors": ["foundry", "ic_design", "packaging"],
                "stocks": {
                    "foundry": ["2330"],       # 台積電
                    "ic_design": ["2454"],     # 聯發科
                    "packaging": ["3711"],     # 日月光（封裝）
                },
            },
        },
    }

    print(f"\n🔍 [DISCOVER 提案]")
    print(f"   事件：{discover_update['event']}")
    print(f"   信心：{discover_update['confidence']}")
    print(f"   方向：{discover_update['new_event']['impact']['direction']}")
    print(f"   依據：{discover_update['reason']}")
    print(f"   股票：{discover_update['new_event']['impact']['stocks']}")

    # 存入 shadow_updates（不寫 YAML）
    print(f"\n💾 [寫入 shadow_updates（shadow 模式）]")
    apply_updates([discover_update], shadow=True)

    # 確認檔案
    today = datetime.now().strftime("%Y-%m-%d")
    shadow_file = config.data_dir / "shadow_updates" / f"{today}.json"
    if shadow_file.exists():
        with open(shadow_file, encoding="utf-8") as f:
            entries = json.load(f)
        ai_entries = [e for e in entries if new_event_name in e.get("event", "")]
        if ai_entries:
            print(f"\n✅ 已存入 {shadow_file.name}，等待人工審閱")
            print(f"\n📌 [下一步] 執行以下指令審閱並決定是否批准：")
            print(f"   bash review.sh")
        else:
            print(f"\n⚠️ 提案被去重邏輯攔截（今天已提過相同事件）")


# --------------------------------------------------------------------------- #
# Flow 2：推薦標的
# --------------------------------------------------------------------------- #

def demo_recommend() -> None:
    print(f"\n{DIVIDER}")
    print("  FLOW 2：推薦標的")
    print(DIVIDER)

    # 模擬 Step 2 LLM 分類的輸出（假設今天偵測到中東衝突）
    mock_events = [
        {
            "event": "美伊/中東衝突",
            "confidence": 0.82,
            "direction": "bullish",
            "summary": "以色列對伊朗發動空襲，油價急漲 4%，紅海航線再度受阻",
        }
    ]

    print(f"\n🧠 [Step 2 LLM 分類結果（模擬）]")
    for ev in mock_events:
        print(f"   事件：{ev['event']}")
        print(f"   信心：{ev['confidence']}  方向：{ev['direction']}")
        print(f"   摘要：{ev['summary']}")

    # Step 3：規則匹配
    print(f"\n📐 [Step 3 規則匹配]")
    from ruamel.yaml import YAML
    _yaml = YAML()
    with open(config.rules_file, encoding="utf-8") as f:
        rules = _yaml.load(f) or []

    rule_map = {r["event"]: r for r in rules}
    recommendations = []
    seen_codes: set[str] = set()

    for ev in mock_events:
        rule = rule_map.get(ev["event"])
        if not rule:
            print(f"   ✗ '{ev['event']}' 在規則庫找不到對應規則")
            continue
        print(f"   ✓ 命中規則：{ev['event']}")
        stocks = rule.get("impact", {}).get("stocks", {})
        direction = ev.get("direction") or rule["impact"].get("direction", "mixed")
        for sector, codes in stocks.items():
            for code in codes:
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                action = "觀察買進" if direction == "bullish" else "停看等"
                recommendations.append({
                    "code": code,
                    "name": "",
                    "action": action,
                    "sector": sector,
                    "rule_id": ev["event"],
                    "confidence": ev["confidence"],
                    "event_summary": ev["summary"],
                    "entry_low": None, "entry_high": None,
                    "target": None, "stop_loss": None,
                })
        print(f"   → 產生 {len(recommendations)} 筆建議標的")

    if not recommendations:
        print("   無建議標的")
        return

    # Step 4：查今日股價
    print(f"\n💹 [Step 4 查詢今日股價（TWSE API）]")
    try:
        from scripts.lib.twse_client import get_prices
        codes = [r["code"] for r in recommendations]
        prices = get_prices(codes)

        for rec in recommendations:
            info = prices.get(rec["code"])
            if info:
                rec["name"] = info.get("name", rec["code"])
                close = info.get("close")
                if close:
                    rec["entry_low"] = round(close * 0.97, 1)
                    rec["entry_high"] = round(close * 1.00, 1)
                    rec["target"] = round(close * 1.05, 1)
                    rec["stop_loss"] = round(close * 0.95, 1)
                    print(f"   {rec['code']} {rec['name']}  收盤 {close}  進場 {rec['entry_low']}-{rec['entry_high']}  目標 {rec['target']}  停損 {rec['stop_loss']}")
            else:
                print(f"   {rec['code']}  今日無交易資料（休市或 ETF）")
    except Exception as e:
        print(f"   TWSE API 查詢失敗：{e}（繼續，股價欄位空白）")

    # Step 6：組 Telegram 訊息
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"\n📱 [Step 6 Telegram 訊息預覽]")
    print("─" * 40)

    lines = [f"📊 *{today_str} 每日台股建議*\n"]
    lines.append("🔴 *重大事件*")
    for ev in mock_events:
        if ev["confidence"] >= 0.7:
            lines.append(f"• {ev['event']}（信心 {ev['confidence']:.2f}）")
            lines.append(f"  ↳ {ev['summary']}")
    lines.append("")

    buy = [r for r in recommendations if r["action"] == "觀察買進"]
    if buy:
        lines.append("📈 *建議觀察買進*")
        for r in buy:
            name = r.get("name") or r["code"]
            conf_str = f"（信心 {r['confidence']:.2f}）"
            lines.append(f"• {r['code']} {name}{conf_str}")
            if r.get("entry_low"):
                lines.append(f"  進場 {r['entry_low']}-{r['entry_high']}  目標 {r['target']}  停損 {r['stop_loss']}")
            lines.append(f"  依據：{r['rule_id']}")
        lines.append("")

    lines.append(f"_更新時間：{datetime.now().strftime('%H:%M')}_")
    print("\n".join(lines))
    print("─" * 40)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    print(f"\n{'#'*60}")
    print("  Market Track — 流程示範")
    print(f"{'#'*60}")

    demo_discover()
    demo_recommend()

    print(f"\n{'#'*60}")
    print("  示範結束")
    print(f"{'#'*60}\n")
