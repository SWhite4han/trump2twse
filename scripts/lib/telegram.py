"""Telegram Bot 推送模組。

用法（程式內）：
    from scripts.lib.telegram import send
    send("Hello from Market Track")

用法（CLI）：
    python -m scripts.lib.telegram "你的訊息"
"""
from __future__ import annotations

import sys

import requests

from scripts.lib.config import config


_API_BASE = "https://api.telegram.org"


def send(text: str, *, parse_mode: str = "Markdown", disable_preview: bool = True) -> dict:
    """發送一則訊息到設定好的 chat。

    Returns:
        Telegram API 的 JSON 回應。

    Raises:
        requests.HTTPError: HTTP 層失敗。
        RuntimeError: Telegram 回 ok=False。
    """
    url = f"{_API_BASE}/bot{config.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": config.telegram_chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API 回錯：{data}")
    return data


def main() -> int:
    if len(sys.argv) < 2:
        text = "✅ *Market Track* Telegram 通道測試成功。\n\n（這是預設測試訊息）"
    else:
        text = " ".join(sys.argv[1:])
    try:
        result = send(text)
        msg_id = result.get("result", {}).get("message_id")
        print(f"✓ 已送出，message_id={msg_id}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 送出失敗：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
