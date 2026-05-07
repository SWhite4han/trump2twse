"""LLM 抽象層。

所有 LLM 呼叫統一走這裡，透過 LLM_BACKEND 環境變數切換後端：
  claude_code → 呼叫本機 claude CLI（走 Claude Code 訂閱，不需 API key）
  claude_api  → 直接打 Anthropic API（自備 key）
  ollama      → 本地模型（離線備援）
  stub        → 不打 API，直接回傳空結果（測試用）

換後端只改 .env 一行，業務邏輯零改動。
"""
from __future__ import annotations

import json
import os
from typing import Any

from scripts.lib.config import config

# --------------------------------------------------------------------------- #
# 公開介面
# --------------------------------------------------------------------------- #

def analyze(prompt: str, context: dict[str, Any] | None = None) -> str:
    """呼叫 LLM 分析。

    Args:
        prompt: 系統 / 使用者指令（可含 context 的格式化佔位符）。
        context: 額外資料（新聞、規則清單等），會序列化後附在 prompt 末尾。

    Returns:
        模型回覆的純文字字串。
    """
    full_prompt = _build_prompt(prompt, context)
    backend = config.llm_backend.lower()

    if backend == "claude_code":
        return _call_claude_code(full_prompt)
    if backend in ("cowork", "claude_api"):
        return _call_anthropic(full_prompt)
    if backend == "ollama":
        return _call_ollama(full_prompt)
    if backend == "stub":
        return ""
    raise ValueError(f"未知的 LLM_BACKEND：{backend!r}，請改為 claude_code / claude_api / ollama / stub")


# --------------------------------------------------------------------------- #
# 內部實作
# --------------------------------------------------------------------------- #

def _build_prompt(prompt: str, context: dict | None) -> str:
    if not context:
        return prompt
    ctx_text = json.dumps(context, ensure_ascii=False, indent=2)
    return f"{prompt}\n\n---\n{ctx_text}"


def _call_claude_code(prompt: str) -> str:
    import subprocess
    result = subprocess.run(
        ["claude", "-p", "--output-format", "text", prompt],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI 失敗：{result.stderr.strip()}")
    return result.stdout.strip()


def _call_anthropic(prompt: str) -> str:
    try:
        import anthropic  # type: ignore
    except ImportError as e:
        raise ImportError(
            "缺少 anthropic 套件，請執行：pip install anthropic"
        ) from e

    api_key = config.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "使用 cowork / claude_api backend 需要在 .env 填入 ANTHROPIC_API_KEY"
        )

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def _call_ollama(prompt: str) -> str:
    import requests

    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    model = os.getenv("OLLAMA_MODEL", "llama3")
    resp = requests.post(
        f"{ollama_url}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("response", "")
