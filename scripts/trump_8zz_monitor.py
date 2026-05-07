#!/usr/bin/env python3
import json
import sys
import time
import subprocess
from datetime import datetime

def run_command(cmd):
    """Run a shell command and return result"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return {
            "exit_code": result.returncode,
            "output": result.stdout.strip(),
            "error": result.stderr.strip()
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "output": "", "error": "Timeout"}
    except Exception as e:
        return {"exit_code": -1, "output": "", "error": str(e)}

def search_news(query, max_results=5):
    """Search news using ddgs CLI"""
    # Try news search first
    result = run_command(f'ddgs news -q "{query}" -m {max_results} -t d -o json')
    if result["exit_code"] == 0 and result["output"]:
        try:
            data = json.loads(result["output"])
            if isinstance(data, list):
                return data
        except:
            pass
    
    # Fallback to text search
    result = run_command(f'ddgs text -q "{query}" -m {max_results} -t d -o json')
    if result["exit_code"] == 0 and result["output"]:
        try:
            data = json.loads(result["output"])
            if isinstance(data, list):
                return data
        except:
            pass
    
    return []

def format_impact_summary(news_items):
    """Format news into a brief impact summary"""
    if not news_items:
        return "📰 沒有找到最新相關新聞。"
    
    lines = ["🚨 Trump & 8zz 對台股影響速報", "="*40]
    for i, item in enumerate(news_items[:3], 1):
        title = item.get("title", "無標題")
        source = item.get("source", "未知來源")
        date = item.get("date", "")
        url = item.get("url", "")
        lines.append(f"{i}. {title}")
        lines.append(f"   來源: {source} | 時間: {date}")
        lines.append(f"   連結: {url}")
        lines.append("")
    
    lines.append("💡 建議觀察: 半導體、電子、出口相關股票")
    lines.append(f"⏰ 更新時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)

def send_telegram_message(message):
    """Send message via Telegram using curl or save to file"""
    # Try to send via Telegram CLI if available, otherwise save to file
    telegram_script = """
import os
import json
from datetime import datetime

# Simple telegram sender - saves to file for now
log_file = "/root/.hermes/telegram_alerts.log"
os.makedirs(os.path.dirname(log_file), exist_ok=True)

with open(log_file, "a", encoding="utf-8") as f:
    f.write(f"[{datetime.now().isoformat()}] ALERT\\n")
    f.write(message + "\\n")
    f.write("="*50 + "\\n\\n")
"""
    
    # Save the alert to a file that we can check
    alert_file = "/root/.hermes/latest_alert.txt"
    with open(alert_file, "w", encoding="utf-8") as f:
        f.write(message)
    
    print(f"Alert saved to {alert_file}")

def main():
    # Keywords to monitor
    queries = [
        "Trump Taiwan semiconductor tariff",
        "8zz 台股 投資 建議",
        "台灣股市 特朗普 影響",
        "巴逆逆 8zz 股票 直播",
        "特朗普 關稅 台灣 晶片"
    ]
    
    all_news = []
    for query in queries:
        news = search_news(query, max_results=3)
        all_news.extend(news)
        time.sleep(1)  # be gentle
    
    # Deduplicate by title
    seen = set()
    unique_news = []
    for item in all_news:
        title = item.get("title", "")
        if title and title not in seen:
            seen.add(title)
            unique_news.append(item)
    
    # Sort by date if available
    try:
        unique_news.sort(key=lambda x: x.get("date", ""), reverse=True)
    except:
        pass
    
    message = format_impact_summary(unique_news[:5])
    
    # Send alert
    send_telegram_message(message)
    
    # Also print to stdout
    print("=== Latest Alert ===")
    print(message)

if __name__ == "__main__":
    main()