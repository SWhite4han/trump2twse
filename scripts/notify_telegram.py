"""Phase 0 啟動測試：發送一則 hello world 到 Telegram。

執行方式（從專案根目錄）：
    python scripts/notify_telegram.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# 讓本檔可獨立執行（把專案根加入 sys.path）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.lib.telegram import send  # noqa: E402


STARTUP_MESSAGE = """🚀 *Market Track v1.0 — Phase 0 啟動測試*

通道驗證成功。後續每日 09:00 會在這裡推送台股建議。

— 啟動時間：{ts}
"""


def main() -> int:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        result = send(STARTUP_MESSAGE.format(ts=ts))
        print(f"✓ 測試訊息已發送，message_id={result['result']['message_id']}")
        print("  請檢查你的 Telegram 是否收到。")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 發送失敗：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
